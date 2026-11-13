# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio
import json
import os
import time

from arena.bridge import ROOT, ask, atomic_json
from .common import (BRIDGE_SERVER, RPC, HarnessTurnError, codex_command, emit,
                     session_missing, version)

ALLOW, DENY = "Allow", "Deny"
# Codex's own labels for remembering an MCP tool-call approval, keyed by the
# _meta.persist value it offers (codex-rs core/src/mcp_tool_call.rs).
REMEMBER = {"session": "Allow for this session", "always": "Allow and don't ask me again"}
# Fields that identify a request, or offer options the cards do not, rather than
# describe the action being approved.
NOT_SHOWN = {"threadId", "turnId", "itemId", "startedAtMs", "approvalId", "environmentId", "kind",
             "commandActions", "proposedExecpolicyAmendment", "proposedNetworkPolicyAmendments"}


def picked(answer, choices):
    """The choice the user picked; typed text that names no choice picks nothing."""
    text = str(answer).strip().lower()
    return next((choice for choice in choices if choice.lower() == text), None)


def describe(action, p, *details):
    """Card text shaped like Claude's: the action first, then what it acts on."""
    rest = {k: v for k, v in p.items() if k not in NOT_SHOWN and v not in (None, "", [], {})}
    lines = [action]
    for key, label in details:
        if key in rest:
            value = rest.pop(key)
            lines.append(f"{label}: {value if isinstance(value, str) else json.dumps(value)}")
    if rest:
        lines.append(json.dumps(rest))
    return "\n".join(lines)


def single_choice(spec):
    """Label -> value for a form field answered by picking one option, else None."""
    if not isinstance(spec, dict):
        return None
    if spec.get("type") == "boolean":
        return {"Yes": True, "No": False}
    if isinstance(spec.get("enum"), list):
        names = spec.get("enumNames") or spec["enum"]
        return {str(name): value for name, value in zip(names, spec["enum"])}
    if isinstance(spec.get("oneOf"), list):
        return {str(o.get("title", o["const"])): o["const"]
                for o in spec["oneOf"] if isinstance(o, dict) and "const" in o}
    return None


class CodexAdapter:
    def __init__(self, turn):
        self.turn = turn
        self.finished = asyncio.Event()
        self.turn_id = None
        self.session_id = turn.get("session_id")
        self.failed = None

    async def approve(self, text):
        return picked(await ask(text, [ALLOW, DENY], "approval"), [ALLOW, DENY]) == ALLOW

    async def request(self, method, p):
        if method == "item/tool/requestUserInput":
            answers = {}
            for q in p["questions"]:
                # With its MCP elicitation feature off, Codex asks for MCP tool approval
                # this way; the answer belongs to this run, not the shared requirements.
                kind = ("approval" if str(q.get("id", "")).startswith("mcp_tool_call_approval")
                        else "requirement")
                answer = await ask(q["question"], [o["label"] for o in (q.get("options") or [])],
                                   kind, native_id=q["id"])
                answers[q["id"]] = {"answers": [str(answer)]}
            return {"answers": answers}
        if method == "item/commandExecution/requestApproval":
            allowed = await self.approve(describe(
                "Run command", p, ("command", "Command"), ("cwd", "Directory"),
                ("networkApprovalContext", "Network"), ("reason", "Reason")))
            return {"decision": "accept" if allowed else "decline"}
        if method == "item/fileChange/requestApproval":
            allowed = await self.approve(describe(
                "Apply file changes", p, ("reason", "Reason"), ("grantRoot", "Write access to")))
            return {"decision": "accept" if allowed else "decline"}
        if method == "item/permissions/requestApproval":
            allowed = await self.approve(describe(
                "Grant additional permissions", p, ("permissions", "Permissions"),
                ("cwd", "Directory"), ("reason", "Reason")))
            return {"permissions": p["permissions"] if allowed else {}, "scope": "turn"}
        if method == "mcpServer/elicitation/request":
            return await self.elicitation(p)
        raise RuntimeError(f"Unsupported native request: {method}; approval not granted")

    async def elicitation(self, p):
        meta = p.get("_meta") if isinstance(p.get("_meta"), dict) else {}
        message = f"MCP server {p.get('serverName', '')}: " + p.get("message", "Input requested")
        if p.get("mode") == "url":
            answer = await ask(message + "\n" + p["url"], ["Completed", "Cancel"], "approval")
            return {"action": "accept" if answer == "Completed" else "cancel"}
        if meta.get("codex_approval_kind") == "mcp_tool_call":
            # Codex's approval for an MCP tool call is an empty form: the answer is the
            # action itself, plus whichever ways of remembering it Codex offers.
            persist = meta.get("persist") or []
            persist = [persist] if isinstance(persist, str) else persist
            remember = {REMEMBER[o]: o for o in persist if o in REMEMBER}
            choices = [ALLOW, *remember, DENY]
            text = p.get("message") or f"Allow the {p.get('serverName', '')} MCP server to run a tool?"
            if meta.get("tool_params"):
                text += "\n" + json.dumps(meta["tool_params"])
            choice = picked(await ask(text, choices, "approval"), choices)
            if choice == ALLOW or choice in remember:
                return {"action": "accept", "content": {},
                        "_meta": {"persist": remember[choice]} if choice in remember else None}
            return {"action": "decline", "content": None, "_meta": None}
        schema = p.get("requestedSchema") if isinstance(p.get("requestedSchema"), dict) else {}
        fields = schema.get("properties") or {}
        options = single_choice(next(iter(fields.values()))) if len(fields) == 1 else None
        if not fields or options:
            # A plain confirmation, or one field answered by picking: offer choices, as
            # Claude's cards do, rather than asking for a JSON object.
            options = options or {"Accept": None}
            choices = [*options, "Decline"]
            choice = picked(await ask(message, choices, "approval"), choices)
            if choice in options:
                return {"action": "accept",
                        "content": {next(iter(fields)): options[choice]} if fields else {}}
            return {"action": "decline", "content": None}
        # Richer forms are answered as JSON, validated against the schema when submitted.
        answer = await ask(message, kind="approval", schema=p.get("requestedSchema"))
        if isinstance(answer, str):
            answer = json.loads(answer)
        return {"action": "accept", "content": answer}

    async def notification(self, method, p):
        emit("native", {"method": method, "params": p})
        if method == "thread/tokenUsage/updated":
            primary = p["threadId"] == self.session_id
            emit("usage", {"source": "codex.thread/tokenUsage/updated",
                           "mode": "cumulative" if primary else "unavailable", "scope": p["threadId"],
                           "raw": p["tokenUsage"]["total"],
                           "coverage": "partial",
                           "exclusions": ["Delegated-session counters are retained separately; parent inclusion is "
                                          "not established, so child counters are not added to parent totals."]})
        if method == "item/agentMessage/delta":
            emit("text", {"text": p.get("delta", "")})
        if method == "turn/completed" and p.get("threadId") == self.session_id:
            t = p.get("turn", {})
            if t.get("status") in ("failed", "interrupted"):
                self.failed = t
            self.finished.set()

    async def run(self):
        # Startup shares the one configured budget, as the other adapters do. Measured
        # cold in an AgentCore microVM, app-server takes 26-51s to answer initialize, so
        # the 30s this used to hardcode lost the race as soon as the microVM was cold.
        setup_seconds = self.turn.get("setup_seconds", 90)
        executable = os.environ.get("MCHC_CODEX_CLI") or self.turn["config"].get("cli_path") or "codex"
        started = time.monotonic()
        cli_version = await version(executable, "--version", timeout=setup_seconds)
        emit("capabilities", {"version": cli_version, "cli_path": executable,
                              "version_seconds": round(time.monotonic() - started, 1),
                              "bedrock_region": os.environ.get("MCHC_CODEX_REGION"),
                              "interface": "app-server stdio", "model_requested": self.turn["config"].get("model"),
                              "effort_requested": self.turn["config"].get("effort")})
        self.rpc = await RPC(codex_command("app-server", executable=executable), self.request, self.notification).start()
        try:
            started = time.monotonic()
            init = await self.rpc.call("initialize", {
                "clientInfo": {"name": "mchc", "version": "1.2.1"},
                "capabilities": {"experimentalApi": True}}, setup_seconds)
            # Remote stdout reaches the host in one buffered flush, so per-event
            # timestamps cannot separate the version check from app-server startup.
            # Recording the measurement keeps the remaining margin observable.
            initialize_seconds = round(time.monotonic() - started, 1)
            await self.rpc.notify("initialized")
            account = await self.rpc.call("account/read", {"refreshToken": False}, setup_seconds)
            emit("capabilities", {"initialize": init, "initialize_seconds": initialize_seconds,
                                  "authenticated": account.get("account") is not None
                                  or not account.get("requiresOpenaiAuth", True)})
            if account.get("requiresOpenaiAuth") and account.get("account") is None:
                raise HarnessTurnError(
                    "Codex authentication unavailable: run codex login in its dedicated HOME",
                    terminal=True)
            config = self.turn["config"]
            # The arena tools are the run's channel to the user, so Codex runs them
            # without an approval round of its own.
            overrides = {"mcp_servers.arena": {**BRIDGE_SERVER, "default_tools_approval_mode": "approve"}}
            for name, server in config.get("mcp_servers", {}).items():
                if name != "arena":
                    overrides[f"mcp_servers.{name}"] = server
            # Codex's own approval flow decides what needs approval. With auto_approve,
            # its auto-review agent answers those requests ("Approve for me"); without
            # it, they come to the user. "never" would instead refuse them silently.
            params = {"cwd": self.turn.get("workspace", "/workspace"), "model": config.get("model"),
                      "approvalPolicy": "on-request",
                      "approvalsReviewer": "auto_review" if config["auto_approve"] else "user",
                      "sandbox": "danger-full-access", "config": overrides}
            # The selected runtime enforces the external OS sandbox, including
            # a read-only workspace during clarification.
            if self.session_id:
                params["threadId"] = self.session_id
            try:
                response = await self.rpc.call("thread/resume" if self.session_id else "thread/start",
                                               params, setup_seconds)
            except RuntimeError as exc:
                # The thread lives in a HOME that a turn killed mid-flight never returned.
                if not (self.session_id and session_missing(exc)):
                    raise
                emit("diagnostic", {"text": f"Codex has no record of thread {self.session_id}; "
                                            "starting a new one"})
                params.pop("threadId", None)
                self.session_id = None
                response = await self.rpc.call("thread/start", params, setup_seconds)
            self.session_id = response["thread"]["id"]
            emit("session", {"session_id": self.session_id, "model": response.get("model")})
            try:
                caps = await self.rpc.call("mcpServerStatus/list", {}, 5)
                emit("capabilities", {"mcp": caps})
            except (RuntimeError, TimeoutError) as e:
                emit("diagnostic", {"text": "MCP capability listing unavailable: "
                      + (str(e) or "metadata request timed out; native startup continues")})
            t = await self.rpc.call("turn/start", {
                "threadId": self.session_id,
                "input": [{"type": "text", "text": self.turn["prompt"]}],
                "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "enabled"},
                # None keeps the model's advertised default effort.
                "effort": config.get("effort")}, setup_seconds)
            self.turn_id = t["turn"]["id"]
            completed = asyncio.create_task(self.finished.wait())
            done, _ = await asyncio.wait([completed, self.rpc.reader], return_when=asyncio.FIRST_COMPLETED)
            if self.rpc.reader in done and not self.finished.is_set():
                completed.cancel()
                # A CLI that died mid-turn is worth another attempt.
                raise HarnessTurnError("Codex exited without turn/completed", terminal=False)
            if self.failed:
                # Turn payloads carry arbitrary model text; never let its
                # wording decide whether this run can be retried.
                raise HarnessTurnError(json.dumps(self.failed), terminal=False)
            emit("turn_complete", {"session_id": self.session_id})
        finally:
            await self.rpc.close()

    async def cancel(self):
        if self.turn_id:
            await self.rpc.call("turn/interrupt", {"threadId": self.session_id, "turnId": self.turn_id}, 10)
