# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Kiro ACP startup ordering, using protocol notifications without model calls."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from arena.adapters.kiro import KiroAdapter


def bridge_snapshot(session="current", status="running", tools=("ask_user", "ready", "shared_specification"),
                    source="mcp:arena"):
    return {
        "sessionId": session,
        "mcpServers": [{"name": "arena", "status": status, "toolCount": len(tools)}],
        "tools": [{"name": name, "source": source} for name in tools],
    }


class ProtocolPeer:
    def __init__(self):
        self.reader = asyncio.get_running_loop().create_future()
        self.session_created = asyncio.Event()
        self.calls = []
        self.closed = False
        self.before_response = []
        self.prompt_error = None

    async def start(self):
        return self

    async def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        if method == "initialize":
            return {"agentCapabilities": {"loadSession": True}}
        if method in ("session/new", "session/load"):
            for snapshot in self.before_response:
                await self.announce(snapshot)
            self.session_created.set()
            return {"sessionId": "current", "models": {"currentModelId": "requested-model"}}
        if method == "session/prompt":
            if self.prompt_error:
                error, self.prompt_error = self.prompt_error, None
                raise RuntimeError(error)
            return {"stopReason": "end_turn"}
        raise AssertionError("Unexpected RPC request: " + method)

    async def announce(self, snapshot):
        await self.on_notification("_kiro.dev/commands/available", snapshot)

    async def close(self):
        self.closed = True
        self.reader.cancel()

    @property
    def prompts(self):
        return [params for method, params in self.calls if method == "session/prompt"]


class KiroStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = ProtocolPeer()
        self.tasks = []
        self.emissions = []

        def rpc(command, request, notification):
            self.peer.command = command
            self.peer.on_notification = notification
            return self.peer

        for target, replacement in (
            ("arena.adapters.kiro.RPC", rpc),
            ("arena.adapters.kiro.version", AsyncMock(return_value="authenticated test CLI")),
            ("arena.adapters.kiro.emit", lambda kind, data: self.emissions.append((kind, data))),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.peer.reader.cancel()

    def start(self, **overrides):
        turn = {
            "turn_key": "turn", "phase": "clarify", "prompt": "The unchanged user task",
            "setup_seconds": 1, "workspace": "/assigned/workspace",
            "config": {"model": "requested-model", "agent": "chosen-agent", "auto_approve": True},
            **overrides,
        }
        adapter = KiroAdapter(turn)
        task = asyncio.create_task(adapter.run())
        self.tasks.append(task)
        return adapter, task

    async def waiting(self):
        await asyncio.wait_for(self.peer.session_created.wait(), 1)
        await asyncio.sleep(0)
        self.assertFalse(self.peer.prompts)

    async def completed(self, task):
        await asyncio.wait_for(task, 1)
        self.assertTrue(self.peer.closed)

    async def test_task_waits_for_required_tools_then_submits_original_prompt_once(self):
        self.peer.before_response = [bridge_snapshot(status="loading", tools=())]
        _, task = self.start()
        await self.waiting()
        await self.peer.on_notification("_kiro.dev/mcp/server_initialized",
                                        {"sessionId": "current", "serverName": "arena"})
        await asyncio.sleep(0)
        self.assertFalse(self.peer.prompts)
        await self.peer.announce(bridge_snapshot())
        await self.completed(task)
        self.assertEqual(self.peer.prompts, [{
            "sessionId": "current", "prompt": [{"type": "text", "text": "The unchanged user task"}]}])
        self.assertEqual(self.peer.command,
                         ["kiro-cli", "acp", "--model", "requested-model", "--agent", "chosen-agent"])
        self.assertEqual([data["status"] for kind, data in self.emissions if kind == "bridge_status"],
                         ["loading", "running"])

    async def test_readiness_notification_before_new_session_response_is_retained(self):
        self.peer.before_response = [bridge_snapshot()]
        _, task = self.start()
        await self.completed(task)
        self.assertEqual(len(self.peer.prompts), 1)

    async def test_resumed_session_also_waits_and_ignores_other_session_readiness(self):
        _, task = self.start(session_id="current", phase="develop")
        await self.waiting()
        await self.peer.announce(bridge_snapshot(session="different-session"))
        await asyncio.sleep(0)
        self.assertFalse(self.peer.prompts)
        await self.peer.announce(bridge_snapshot())
        await self.completed(task)
        self.assertEqual(len(self.peer.prompts), 1)
        self.assertIn("session/load", [method for method, _ in self.peer.calls])
        self.assertNotIn("session/new", [method for method, _ in self.peer.calls])

    async def test_latest_loading_snapshot_revokes_earlier_readiness(self):
        self.peer.before_response = [bridge_snapshot(), bridge_snapshot(status="loading", tools=())]
        _, task = self.start()
        await self.waiting()
        await self.peer.announce(bridge_snapshot())
        await self.completed(task)
        self.assertEqual(len(self.peer.prompts), 1)

    async def test_unrelated_loading_server_does_not_delay_ready_arena(self):
        snapshot = bridge_snapshot()
        snapshot["mcpServers"].append({"name": "optional-server", "status": "loading", "toolCount": 0})
        self.peer.before_response = [snapshot]
        _, task = self.start()
        await self.completed(task)
        self.assertEqual(len(self.peer.prompts), 1)

    async def test_missing_required_tool_fails_without_submitting_task(self):
        self.peer.before_response = [bridge_snapshot(tools=("ready", "shared_specification"))]
        _, task = self.start()
        with self.assertRaisesRegex(RuntimeError, "missing its required ask_user/ready tools"):
            await self.completed(task)
        self.assertFalse(self.peer.prompts)
        self.assertTrue(self.peer.closed)

    async def test_same_named_tools_from_another_server_cannot_satisfy_readiness(self):
        self.peer.before_response = [bridge_snapshot(source="mcp:another-server")]
        _, task = self.start()
        with self.assertRaisesRegex(RuntimeError, "missing its required"):
            await self.completed(task)
        self.assertFalse(self.peer.prompts)
        self.assertTrue(self.peer.closed)

    async def test_prefixed_arena_tool_names_are_recognized(self):
        self.peer.before_response = [bridge_snapshot(tools=("mcp__arena__ask_user", "mcp__arena__ready"))]
        _, task = self.start()
        await self.completed(task)
        self.assertEqual(len(self.peer.prompts), 1)

    async def test_failed_bridge_stops_before_submitting_task(self):
        _, task = self.start()
        await self.waiting()
        await self.peer.announce(bridge_snapshot(status="failed", tools=()))
        with self.assertRaisesRegex(RuntimeError, "bridge unavailable: failed"):
            await self.completed(task)
        self.assertFalse(self.peer.prompts)
        self.assertTrue(self.peer.closed)

    async def test_startup_timeout_is_bounded_and_reports_last_bridge_state(self):
        self.peer.before_response = [bridge_snapshot(status="loading", tools=())]
        _, task = self.start(setup_seconds=.02)
        with self.assertRaisesRegex(RuntimeError, r"unavailable after 0.02s \(status: loading\)"):
            await self.completed(task)
        self.assertFalse(self.peer.prompts)
        self.assertTrue(self.peer.closed)

    async def test_protocol_eof_wakes_startup_wait_without_consuming_setup_budget(self):
        _, task = self.start(setup_seconds=100)
        await self.waiting()
        self.peer.reader.set_result(None)
        with self.assertRaisesRegex(RuntimeError, "native protocol stream closed"):
            await self.completed(task)
        self.assertFalse(self.peer.prompts)
        self.assertTrue(self.peer.closed)

    async def test_cancellation_while_loading_closes_rpc_without_submitting_task(self):
        _, task = self.start()
        await self.waiting()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.peer.prompts)
        self.assertTrue(self.peer.closed)

    async def test_existing_invalid_params_fallback_occurs_only_after_bridge_is_ready(self):
        self.peer.prompt_error = '{"code": -32602, "message": "use content"}'
        _, task = self.start()
        await self.waiting()
        await self.peer.announce(bridge_snapshot())
        await self.completed(task)
        self.assertEqual(len(self.peer.prompts), 2)
        self.assertIn("prompt", self.peer.prompts[0])
        self.assertIn("content", self.peer.prompts[1])
        self.assertEqual(self.peer.prompts[0]["prompt"], self.peer.prompts[1]["content"])

    async def test_prompt_runtime_error_does_not_trigger_another_model_turn(self):
        self.peer.before_response = [bridge_snapshot()]
        self.peer.prompt_error = "provider connection failed"
        _, task = self.start()
        with self.assertRaisesRegex(RuntimeError, "provider connection failed"):
            await self.completed(task)
        self.assertEqual(len(self.peer.prompts), 1)
        self.assertTrue(self.peer.closed)


if __name__ == "__main__":
    unittest.main()
