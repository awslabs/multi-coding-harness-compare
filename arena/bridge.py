# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Per-run file mailbox. No network access or access to competing runs is needed."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

ROOT = Path(os.environ.get("ARENA_BRIDGE", "/bridge"))


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


async def ask(question: str, choices: list[str] | None = None,
              kind: str = "requirement", native_id: str | None = None, schema: dict | None = None):
    id = uuid4().hex
    turn = json.loads((ROOT / "turn.json").read_text())
    session_file = ROOT / "session.json"
    session = json.loads(session_file.read_text()) if session_file.exists() else {}
    atomic_json(ROOT / "requests" / f"{id}.json", {
        "id": id, "question": question, "choices": choices or [], "kind": kind,
        "native_id": native_id, "schema": schema, "turn_key": turn["turn_key"],
        "session_id": session.get("session_id"),
    })
    answer_file = ROOT / "answers" / f"{id}.json"
    while not answer_file.exists():
        await asyncio.sleep(.25)
    return json.loads(answer_file.read_text())["answer"]


def serve():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("arena")

    @mcp.tool()
    async def ask_user(question: str, choices: list[str] | None = None,
                       kind: str = "requirement") -> str:
        """Ask essential clarification or approval. Pauses until the user answers.

        kind is 'requirement' (shared with all implementations) or 'approval'
        (applies only to this run). Free text is always available.
        """
        return str(await ask(question, choices, kind if kind == "approval" else "requirement"))

    @mcp.tool()
    def ready(assumptions: str = "") -> str:
        """Required in the initial clarification phase once all questions are resolved."""
        turn = json.loads((ROOT / "turn.json").read_text())
        atomic_json(ROOT / "requests" / ("ready-" + turn["turn_key"] + ".json"),
                    {"type": "ready", "turn_key": turn["turn_key"], "assumptions": assumptions})
        return "Readiness recorded. Finish this clarification turn; development will follow."

    @mcp.tool()
    def shared_specification() -> dict:
        """Read the current shared task, answers and specification version."""
        return json.loads((ROOT / "specification.json").read_text())

    mcp.run()


if __name__ == "__main__":
    serve()
