# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read native model metadata without submitting a development prompt."""
import asyncio
import json
import os
import sys

from .adapters.common import RPC, codex_command, emit


async def mantle_models(region):
    """Read the actual inference endpoint's catalog, not Bedrock Runtime's list."""
    import boto3
    import httpx
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    session = boto3.Session(profile_name=os.environ.get("CODECOUNCIL_CODEX_AWS_PROFILE"),
                            region_name=region)
    request = AWSRequest(method="GET", url=f"https://bedrock-mantle.{region}.api.aws/v1/models")
    SigV4Auth(session.get_credentials().get_frozen_credentials(), "bedrock-mantle", region).add_auth(request)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(request.url, headers=dict(request.headers))
        response.raise_for_status()
        return {m["id"] for m in response.json()["data"]}


async def reject_request(method, params):
    raise RuntimeError("Model discovery does not authorize tool calls")


async def ignore_notification(method, params):
    pass


async def discover(harness):
    if harness == "claude":
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
        options = ClaudeAgentOptions(
            cli_path=os.environ.get("CODECOUNCIL_CLAUDE_CLI"),
            cwd=os.getcwd(), setting_sources=["user"], mcp_servers={},
            allowed_tools=[], max_turns=1,
        )
        async with ClaudeSDKClient(options) as client:
            info = await client.get_server_info() or {}
            models = [{"id": m["value"], "name": m.get("displayName", m["value"]),
                       "description": m.get("description", ""),
                       **({"efforts": m.get("supportedEffortLevels") or [] if m["supportsEffort"] else []}
                          if "supportsEffort" in m else {})}
                      for m in info.get("models", [])]
        return {"models": models, "source": "Claude Code initialization"}

    command = codex_command("app-server") if harness == "codex" else ["kiro-cli", "acp"]
    rpc = await RPC(command, reject_request, ignore_notification).start()
    try:
        client = {"name": "codecouncil", "version": "0.1.0"}
        if harness == "codex":
            await rpc.call("initialize", {"clientInfo": client,
                "capabilities": {"experimentalApi": True}}, 60)
            await rpc.notify("initialized")
            models, cursor = [], None
            for _ in range(20):
                page = await rpc.call("model/list",
                    {"limit": 100, "cursor": cursor, "includeHidden": False}, 20)
                models.extend({"id": m.get("model") or m["id"],
                               "name": m.get("displayName") or m["id"],
                               "description": m.get("description", ""),
                               "is_default": m.get("isDefault", False),
                               **({"efforts": [e["reasoningEffort"] for e in m["supportedReasoningEfforts"]],
                                   "default_effort": m.get("defaultReasoningEffort")}
                                  if m.get("supportedReasoningEfforts") is not None else {})}
                              for m in page.get("data", []) if not m.get("hidden"))
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            region = os.environ.get("CODECOUNCIL_CODEX_DEFAULT_REGION")
            if region:
                routes = json.loads(os.environ.get("CODECOUNCIL_CODEX_MODEL_REGIONS", "{}"))
                regions = sorted({region, *(routes.get(m["id"], region) for m in models)})
                available = dict(zip(regions, await asyncio.gather(*(mantle_models(r) for r in regions)), strict=True))
                models = [{**m, "region": routes.get(m["id"], region),
                           "description": m["description"] + f" · Bedrock {routes.get(m['id'], region)}"}
                          for m in models if m["id"] in available[routes.get(m["id"], region)]]
                return {"models": models, "source": "Codex + regional Bedrock Mantle catalog",
                        "availability_verified": True}
            return {"models": models, "source": "Codex model/list"}
        await rpc.call("initialize", {"protocolVersion": 1,
            "clientCapabilities": {}, "clientInfo": client}, 20)
        session = await rpc.call("session/new", {"cwd": os.getcwd(), "mcpServers": []}, 30)
        native = session.get("models", {})
        return {"models": [
            {"id": m["modelId"], "name": m.get("name", m["modelId"]),
             "description": m.get("description", ""),
             "is_default": m["modelId"] == native.get("currentModelId")}
            for m in native.get("availableModels", [])], "source": "Kiro ACP session models"}
    finally:
        await rpc.close()


async def main():
    try:
        result = await discover(sys.argv[1])
        if not result["models"]:
            raise RuntimeError("The native harness returned no models")
        emit("model_catalogue", result)
    except Exception as exc:
        # No native diagnostic text or credential-bearing configuration is
        # returned through the browser's model-catalogue endpoint.
        emit("model_catalogue_error", {"kind": type(exc).__name__})
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
