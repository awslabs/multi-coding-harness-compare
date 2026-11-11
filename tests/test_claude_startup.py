# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import unittest
from unittest.mock import AsyncMock, patch

from arena.adapters.claude import ClaudeAdapter


class ClaudeStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_bridge_is_connected_before_task_can_start(self):
        client = AsyncMock()
        client.get_mcp_status.side_effect = [
            {"mcpServers": [{"name": "arena", "status": "pending"}]},
            {"mcpServers": [{"name": "arena", "status": "connected",
                            "tools": [{"name": "ask_user"}, {"name": "ready"}]}]},
        ]
        with patch("arena.adapters.claude.emit"):
            await ClaudeAdapter({"setup_seconds": 2}).wait_for_bridge(client)
        client.reconnect_mcp_server.assert_awaited_once_with("arena")
        client.query.assert_not_awaited()

    async def test_connection_failure_is_bounded_and_does_not_submit_task(self):
        client = AsyncMock()
        client.get_mcp_status.return_value = {
            "mcpServers": [{"name": "arena", "status": "failed"}]}
        with patch("arena.adapters.claude.emit"):
            with self.assertRaisesRegex(RuntimeError, "bridge unavailable: failed"):
                await ClaudeAdapter({"setup_seconds": 2}).wait_for_bridge(client)
        client.reconnect_mcp_server.assert_awaited_once_with("arena")
        client.query.assert_not_awaited()

    async def test_connection_requires_clarification_tools(self):
        client = AsyncMock()
        client.get_mcp_status.return_value = {
            "mcpServers": [{"name": "arena", "status": "connected",
                            "tools": [{"name": "shared_specification"}]}]}
        with patch("arena.adapters.claude.emit"):
            with self.assertRaisesRegex(RuntimeError, "missing its required tools"):
                await ClaudeAdapter({"setup_seconds": 2}).wait_for_bridge(client)
        client.query.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
