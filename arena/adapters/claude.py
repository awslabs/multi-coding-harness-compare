# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio
import dataclasses
import os
import re
import warnings

from arena.bridge import ROOT, ask, atomic_json
from .common import BRIDGE_SERVER, HarnessTurnError, emit

# The app's clarification tools: the run's channel to the user.
ARENA_TOOLS = ("mcp__arena__ask_user", "mcp__arena__ready", "mcp__arena__shared_specification")


class ClaudeAdapter:
    def __init__(self, turn):
        self.turn = turn
        self.client = None
        # The permission mode Claude Code reports being in; None until it reports one.
        self.permission_mode = None

    def track_permission_mode(self, data):
        """Follow the mode Claude Code reports at startup and on every change."""
        mode = data.get("permissionMode")
        if mode is None or mode == self.permission_mode:
            return
        self.permission_mode = mode
        if mode != "auto":
            emit("diagnostic", {"text": f"Claude Code is in {mode} permission mode rather than "
                                        "auto mode, so any permission prompts it sends follow auto_approve."})

    async def wait_for_bridge(self, client):
        timeout = self.turn.get("setup_seconds", 90)
        previous = None
        reconnected = False
        try:
            async with asyncio.timeout(timeout):
                while True:
                    status = await client.get_mcp_status()
                    server = next((s for s in status.get("mcpServers", []) if s.get("name") == "arena"), {})
                    state = server.get("status", "registering")
                    if state != previous:
                        emit("bridge_status", {"server": "arena", "status": state})
                        previous = state
                    if state == "connected":
                        tools = server.get("tools")
                        if tools is not None and not {"ask_user", "ready"}.issubset(
                                t["name"].removeprefix("mcp__arena__") for t in tools):
                            raise HarnessTurnError(
                                "multi-coding-harness-compare clarification bridge is missing its required tools",
                                terminal=False)
                        return
                    # Recent CLI versions defer pending MCP connections until
                    # a query. Explicitly connect the bridge before submitting
                    # the task so the required clarification tools are present.
                    if state in ("pending", "failed") and not reconnected:
                        reconnected = True
                        await client.reconnect_mcp_server("arena")
                    elif state in ("failed", "needs-auth", "disabled"):
                        # Only a disabled server or a missing login needs a human.
                        raise HarnessTurnError(
                            f"multi-coding-harness-compare clarification bridge unavailable: {state}. "
                            "The task prompt was not submitted.",
                            terminal=state in ("disabled", "needs-auth"))
                    await asyncio.sleep(.5)
        except TimeoutError as exc:
            raise HarnessTurnError(
                f"multi-coding-harness-compare clarification bridge unavailable after {timeout}s "
                f"(status: {previous}). The task prompt was not submitted.", terminal=False) from exc

    async def permission(self, tool_name, tool_input, context):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        if tool_name == "AskUserQuestion":
            answers = {}
            for q in tool_input.get("questions", []):
                answers[q["question"]] = str(await ask(q["question"],
                    [o["label"] for o in q.get("options", [])]))
            return PermissionResultAllow(updated_input={**tool_input, "answers": answers})
        # In auto mode, Claude Code's classifier has already allowed what it can, so a
        # prompt that still arrives is one auto mode wants a person to answer.
        # auto_approve answers only when auto mode is not in effect.
        if self.permission_mode != "auto" and self.turn["config"]["auto_approve"]:
            return PermissionResultAllow(updated_input=tool_input)
        answer = await ask(f"{tool_name}\n{tool_input}", ["Allow", "Deny"], "approval")
        return (PermissionResultAllow(updated_input=tool_input) if str(answer).lower() == "allow"
                else PermissionResultDeny(message="User denied this operation"))

    async def run(self):
        import importlib.metadata
        from claude_agent_sdk import (CanUseToolShadowedWarning, ClaudeSDKClient, ClaudeAgentOptions,
                                      ResultMessage, SystemMessage)
        config = self.turn["config"]
        options = ClaudeAgentOptions(
            cli_path=os.environ.get("MCHC_CLAUDE_CLI") or config.get("cli_path"),
            cwd=self.turn.get("workspace", "/workspace"), model=config.get("model"), resume=self.turn.get("session_id"),
            system_prompt={"type": "preset", "preset": "claude_code"},
            setting_sources=["user", "project", "local"],
            mcp_servers={**config.get("mcp_servers", {}), "arena": BRIDGE_SERVER},
            # Auto mode: Claude Code's classifier allows routine actions and blocks risky
            # ones itself, and only what it escalates reaches permission(). With a model
            # that has no auto mode, Claude Code starts in default (Manual) mode instead.
            can_use_tool=self.permission, permission_mode="auto",
            # Allow rules are checked before auto mode's classifier, so the app's own
            # tools run without a classifier call or an approval card.
            allowed_tools=list(ARENA_TOOLS),
            max_turns=self.turn["max_turns"], include_partial_messages=True,
            effort=config.get("effort"),
        )
        # Allowing the arena tools outright is intended; the SDK's notice that
        # can_use_tool will not see them would add a diagnostic line to every run.
        warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning, message=re.escape(
            "can_use_tool will not be invoked for: " + ", ".join(ARENA_TOOLS) + "."))
        emit("capabilities", {"sdk_version": importlib.metadata.version("claude-agent-sdk"),
                              "cli_path": options.cli_path,
                              "interface": "ClaudeSDKClient", "model_requested": config.get("model"),
                              "effort_requested": config.get("effort"),
                              "permission_mode_requested": options.permission_mode,
                              "settings": options.setting_sources})
        async with ClaudeSDKClient(options=options) as client:
            self.client = client
            await self.wait_for_bridge(client)
            await client.query(self.turn["prompt"])
            complete = False
            async for message in client.receive_response():
                data = dataclasses.asdict(message)
                emit("native", {"message_type": type(message).__name__, **data})
                if isinstance(message, SystemMessage) and message.subtype in ("init", "status"):
                    # init carries the mode the session started in; status reports each change.
                    self.track_permission_mode(message.data)
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    session_id = message.data["session_id"]
                    emit("session", {"session_id": session_id, "model": message.data.get("model")})
                    emit("capabilities", message.data)
                for block in getattr(message, "content", []):
                    if getattr(block, "text", None):
                        emit("text", {"text": block.text})
                if isinstance(message, ResultMessage):
                    # model_usage is the documented whole-turn agent-tree aggregate.
                    # Select it instead of adding top-level/streamed usage again.
                    model_usage = getattr(message, "model_usage", None)
                    raw = model_usage or message.usage
                    counters = list(model_usage.values()) if model_usage else [message.usage or {}]
                    token_keys = {"inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens",
                                  "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
                    measured = any(isinstance(v, (int, float)) and v > 0
                                   for bucket in counters for k, v in bucket.items() if k in token_keys)
                    emit("usage", {"source": "claude.ResultMessage." + ("model_usage" if model_usage else "usage"),
                        "mode": "delta" if measured else "unavailable",
                        "scope": message.session_id, "event_id": getattr(message, "uuid", None)
                        or self.turn["turn_key"], "raw": raw,
                        "coverage": "partial" if measured else "unavailable",
                        "included_activity": "Whole-turn per-model totals, including delegated calls." if model_usage
                                             else "Top-level native turn totals.",
                        "exclusions": (["Interrupted turns may lack terminal counters."]
                            if model_usage else ["Top-level turn usage only; delegated-call counters unavailable."])
                            + ([] if measured else ["All-zero SDK counters without measurable model activity are not presented as a task total."])})
                    if message.is_error:
                        raise RuntimeError(f"{message.subtype}: {getattr(message, 'errors', None) or message.result}")
                    complete = True
                    emit("turn_complete", {"session_id": message.session_id})
            if not complete:
                raise RuntimeError("Claude exited without ResultMessage")

    async def cancel(self):
        if self.client:
            await self.client.interrupt()
