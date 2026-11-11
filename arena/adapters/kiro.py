# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio
import json
import os

from arena.bridge import ROOT, ask, atomic_json
from .common import BRIDGE_SERVER, RPC, HarnessTurnError, emit, session_missing, version


def metadata_usage(params, turn_key):
    raw = {key: params[key] for key in ("contextUsagePercentage", "meteringUsage", "turnDurationMs")
           if key in params}
    if not raw:
        return None
    return {"source": "kiro.ACP._kiro.dev/metadata", "mode": "unavailable",
            "scope": params.get("sessionId"), "turn_key": turn_key, "raw": raw,
            "coverage": "unavailable",
            "exclusions": ["Kiro reported credits and context occupancy, not task input/output token counts."]}


class KiroAdapter:
    def __init__(self, turn):
        self.turn = turn
        self.session_id = turn.get("session_id")
        # Notifications may arrive before session/new returns its session ID.
        # Keep the latest snapshot per session, including later revocations.
        self.bridge_sessions = {}
        self.bridge_changed = asyncio.Event()

    async def wait_for_bridge(self):
        timeout = self.turn.get("setup_seconds", 90)
        required = {"ask_user", "ready"}
        previous = None
        state = "registering"
        try:
            async with asyncio.timeout(timeout):
                while True:
                    self.bridge_changed.clear()
                    if self.rpc.reader.done():
                        cause = None if self.rpc.reader.cancelled() else self.rpc.reader.exception()
                        raise HarnessTurnError(
                            "multi-coding-harness-compare clarification bridge unavailable: native protocol stream closed. "
                            "The task prompt was not submitted.", terminal=False) from cause
                    bridge = self.bridge_sessions.get(self.session_id, {})
                    state = bridge.get("status", "registering")
                    if state != previous:
                        emit("bridge_status", {"server": "arena", "status": state})
                        previous = state
                    if state in ("running", "connected"):
                        if not required.issubset(bridge.get("tools", set())):
                            raise HarnessTurnError(
                                "multi-coding-harness-compare clarification bridge unavailable: missing its required "
                                "ask_user/ready tools. The task prompt was not submitted.", terminal=False)
                        return
                    if state in ("failed", "error", "disabled", "needs-auth", "disconnected"):
                        # Only a disabled server or a missing login needs a human.
                        raise HarnessTurnError(
                            f"multi-coding-harness-compare clarification bridge unavailable: {state}. "
                            "The task prompt was not submitted.",
                            terminal=state in ("disabled", "needs-auth"))
                    changed = asyncio.create_task(self.bridge_changed.wait())
                    try:
                        # Observe EOF without cancelling the shared RPC reader
                        # when this wait times out or the run is cancelled.
                        await asyncio.wait((changed, self.rpc.reader),
                                           return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        changed.cancel()
                        await asyncio.gather(changed, return_exceptions=True)
        except TimeoutError as exc:
            raise HarnessTurnError(
                f"multi-coding-harness-compare clarification bridge unavailable after {timeout}s "
                f"(status: {state}). The task prompt was not submitted.", terminal=False) from exc

    async def request(self, method, p):
        if method == "session/request_permission":
            options = p.get("options", [])
            # Native shell/tools stay autonomous within the OS sandbox.
            if self.turn["config"]["auto_approve"]:
                selected = next((o for o in options if o["kind"] == "allow_once"), None)
            else:
                text = json.dumps(p.get("toolCall", {}), indent=2)
                answer = await ask(text, [o["name"] for o in options], "approval")
                selected = next((o for o in options if o["name"] == answer), None)
            return {"outcome": {"outcome": "selected", "optionId": selected["optionId"]} if selected
                    else {"outcome": "cancelled"}}
        raise RuntimeError(f"Unsupported ACP request: {method}; client capability not advertised")

    async def notification(self, method, p):
        emit("native", {"method": method, "params": p})
        if method == "_kiro.dev/commands/available" and p.get("sessionId"):
            server = next((s for s in p.get("mcpServers", []) if s.get("name") == "arena"), {})
            tools = {
                tool["name"].removeprefix("mcp__arena__")
                for tool in p.get("tools", [])
                if tool.get("source") == "mcp:arena" and isinstance(tool.get("name"), str)
            }
            self.bridge_sessions[p["sessionId"]] = {
                "status": server.get("status", "registering"), "tools": tools}
            self.bridge_changed.set()
        if method == "_kiro.dev/metadata":
            usage = metadata_usage(p, self.turn["turn_key"])
            if usage:
                emit("usage", usage)
        update = p.get("update", p)
        kind = update.get("sessionUpdate", update.get("type", ""))
        if kind in ("agent_message_chunk", "AgentMessageChunk"):
            content = update.get("content", {})
            if isinstance(content, dict) and content.get("text"):
                emit("text", {"text": content["text"]})
        # ACP context-window usage and credits are NOT billed token totals.
        if "usage" in kind.lower():
            emit("usage", {"source": "kiro.ACP." + kind, "mode": "unavailable",
                           "scope": self.session_id, "raw": update, "coverage": "unavailable",
                           "exclusions": ["ACP context occupancy/credits are not task token consumption."]})

    async def run(self):
        config = self.turn["config"]
        # Same budget the bridge wait uses; a cold microVM pays for paging in a
        # one-gigabyte CLI before any of these answer.
        setup_seconds = self.turn.get("setup_seconds", 90)
        # A host cli_path means nothing inside an AgentCore microVM, where kiro-cli is linked
        # onto PATH; honour it only where that file exists.
        configured = config.get("cli_path")
        executable = (os.environ.get("CODECOUNCIL_KIRO_CLI")
                      or (configured if configured and os.path.isfile(configured) else None) or "kiro-cli")
        ver = await version(executable, "--version", timeout=setup_seconds)
        emit("capabilities", {"version": ver, "interface": "ACP"})
        # whoami is a native read-only credential check.
        auth = await version(executable, "whoami", timeout=setup_seconds)
        if "not logged" in auth.lower() or "not authenticated" in auth.lower():
            raise RuntimeError("Kiro requires kiro-cli login in its dedicated HOME")
        emit("capabilities", {"version": ver, "authenticated": True, "interface": "ACP",
                              "model_requested": config.get("model"), "effort_requested": config.get("effort")})
        cmd = [executable, "acp"]
        for flag in ("model", "agent", "effort"):
            if config.get(flag):
                cmd.extend(["--" + flag, config[flag]])
        self.rpc = await RPC(cmd, self.request, self.notification).start()
        try:
            result = await self.rpc.call("initialize", {"protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "codecouncil", "version": "0.1.0"}}, setup_seconds)
            emit("capabilities", result)
            servers = [{"name": "arena", **BRIDGE_SERVER,
                        "env": [{"name": key, "value": value} for key, value in BRIDGE_SERVER["env"].items()]}]
            for name, server in config.get("mcp_servers", {}).items():
                if name != "arena":
                    servers.append({"name": name, **server})
            params = {"cwd": self.turn.get("workspace", "/workspace"), "mcpServers": servers}
            resuming = bool(self.session_id)
            if resuming:
                if not result.get("agentCapabilities", {}).get("loadSession"):
                    raise RuntimeError("Installed Kiro ACP does not advertise session/load")
                params["sessionId"] = self.session_id
                try:
                    response = await self.rpc.call("session/load", params, setup_seconds)
                except RuntimeError as exc:
                    if not session_missing(exc):
                        raise
                    emit("diagnostic", {"text": f"Kiro has no record of session {self.session_id}; "
                                                "starting a new one"})
                    resuming = False
                    params.pop("sessionId", None)
            if not resuming:
                response = await self.rpc.call("session/new", params, setup_seconds)
                self.session_id = response["sessionId"]
            emit("session", {"session_id": self.session_id, "configuration": response,
                              "model": response.get("models", {}).get("currentModelId") or config.get("model")})
            emit("capabilities", {"session_configuration": response})
            # Kiro returns session/new and session/load while MCP startup is
            # still in progress. Do not give the model a turn without the tools
            # needed to register questions and finish clarification.
            await self.wait_for_bridge()
            emit("capabilities", {"clarification_bridge_startup_version": 1})
            # Current Kiro documents content, while standard ACP uses prompt.
            # Installed Kiro accepts the standard protocol field; compatibility
            # fallback only occurs on invalid params, before any turn has started.
            content = [{"type": "text", "text": self.turn["prompt"]}]
            try:
                final = await self.rpc.call("session/prompt", {"sessionId": self.session_id, "prompt": content})
            except RuntimeError as exc:
                if "-32602" not in str(exc):
                    raise
                final = await self.rpc.call("session/prompt", {"sessionId": self.session_id, "content": content})
            if final.get("stopReason") in ("cancelled", "max_tokens", "max_turn_requests"):
                raise RuntimeError("Kiro stopped: " + json.dumps(final))
            emit("turn_complete", final)
        finally:
            await self.rpc.close()

    async def cancel(self):
        if self.session_id:
            await self.rpc.notify("session/cancel", {"sessionId": self.session_id})
