# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Supplemental evidence of native CLI executions, never final harness narratives."""
import json
import re
from pathlib import Path


def native_execution_evidence(store, run: dict) -> dict:
    from .isolation import is_virtual_environment
    workspace = store.path.parent / "comparisons" / run["comparison_id"] / "runs" / run["id"] / "workspace"
    environments = [str(p.parent.relative_to(workspace)) for p in workspace.rglob("pyvenv.cfg")
                    if is_virtual_environment(p.parent)]
    attempts = [a for a in run.get("attempts", []) if a.get("phase") == "develop"
                and a.get("spec_version") == run["spec_version"]]
    started = attempts[-1]["started_at"] if attempts else float("inf")
    with store.connect() as db:
        rows = db.execute("SELECT seq,at,data FROM events WHERE run_id=? AND type='native' "
                          "AND at>=? ORDER BY seq", (run["id"], started)).fetchall()
    pending, executions = {}, []
    for row in rows:
        data = json.loads(row["data"])
        command, output, exit_code, failed = "", "", None, None
        if data.get("message_type") == "AssistantMessage":
            for block in data.get("content", []):
                if block.get("name") == "Bash":
                    pending[block["id"]] = block.get("input", {}).get("command", "")
        elif data.get("message_type") == "UserMessage":
            for block in data.get("content", []):
                if block.get("tool_use_id") in pending:
                    command = pending.pop(block["tool_use_id"])
                    native = data.get("tool_use_result") or {}
                    output = (native.get("stdout", "") + "\n" + native.get("stderr", "")
                              if isinstance(native, dict) and "stdout" in native
                              else str(block.get("content", "")))
                    failed = block.get("is_error")
        elif data.get("method") == "item/completed":
            item = data.get("params", {}).get("item", {})
            if item.get("type") == "commandExecution":
                command, output = item.get("command", ""), item.get("aggregatedOutput", "")
                exit_code = item.get("exitCode")
                failed = exit_code != 0 if exit_code is not None else None
        elif data.get("method") == "session/update":
            update = data.get("params", {}).get("update", {})
            if (update.get("sessionUpdate") == "tool_call_update"
                    and update.get("kind") == "execute" and update.get("status") in ("completed", "failed")):
                command = update.get("rawInput", {}).get("command", "")
                for item in (update.get("rawOutput") or {}).get("items", []):
                    value = item.get("Json", {})
                    if isinstance(value, dict):
                        output += value.get("stdout", "") + "\n" + value.get("stderr", "")
                        match = re.search(r"exit status: (\d+)", str(value.get("exit_status", "")))
                        if match:
                            exit_code = int(match[1])
                failed = update.get("status") == "failed"
        if not re.search(r"\bpython(?:3(?:\.\d+)?)?\s+[^\s;]*\.py(?:\s|\\)", command):
            continue
        # The journal is host-owned. Its output is still untrusted data and
        # supplementary historical evidence, not a new independent live test.
        executions.append({"event_seq": row["seq"], "at": row["at"],
            "command": command.replace(str(Path.home()), "~"),
            "output": output.replace(str(Path.home()), "~"),
            "exit_code": exit_code, "tool_reported_error": failed})
    return {"snapshot_sha256": run.get("artifacts", {}).get("sha256"), "executions": executions,
            "original_virtual_environments": environments,
            "scope": "Native CLI tool executions during development before this snapshot. "
                     "No final narrative is included. Independently executed checks must establish code behavior."}
