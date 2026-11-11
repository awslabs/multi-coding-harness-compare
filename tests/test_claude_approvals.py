# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Claude approvals: Claude Code runs in auto mode, whose classifier decides what needs a
person; what it escalates comes to the user, and auto_approve answers only when auto mode
is not in effect."""
import dataclasses
import unittest
import warnings
from unittest.mock import AsyncMock, Mock, patch

from claude_agent_sdk import (CanUseToolShadowedWarning, PermissionResultAllow, PermissionResultDeny,
                              ResultMessage, SystemMessage, ToolPermissionContext)

from arena import bridge
from arena.adapters.claude import ARENA_TOOLS, ClaudeAdapter

BASH = {"command": "pip install requests", "description": "Install requests"}
CARD = (f"Bash\n{BASH}", ["Allow", "Deny"], "approval")


def adapter_in(mode, auto_approve):
    adapter = ClaudeAdapter({"config": {"auto_approve": auto_approve}})
    adapter.permission_mode = mode
    return adapter


class ClaudePermissionRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def decide(self, adapter, answer="Allow", tool="Bash", tool_input=BASH):
        ask = AsyncMock(return_value=answer)
        with patch("arena.adapters.claude.ask", ask):
            result = await adapter.permission(tool, tool_input, ToolPermissionContext())
        return result, ask

    async def test_auto_mode_escalations_come_to_the_user_even_with_auto_approve(self):
        for answer, expected in (("Allow", PermissionResultAllow(updated_input=BASH)),
                                 ("Deny", PermissionResultDeny(message="User denied this operation"))):
            with self.subTest(answer=answer):
                result, ask = await self.decide(adapter_in("auto", True), answer)
                self.assertEqual(ask.await_args.args, CARD)
                self.assertEqual(result, expected)

    async def test_without_auto_mode_auto_approve_answers_as_before(self):
        for mode in ("default", "plan", None):
            with self.subTest(mode=mode):
                result, ask = await self.decide(adapter_in(mode, True))
                ask.assert_not_awaited()
                self.assertEqual(result, PermissionResultAllow(updated_input=BASH))

    async def test_without_auto_approve_every_prompt_comes_to_the_user(self):
        for mode in ("auto", "default", None):
            with self.subTest(mode=mode):
                result, ask = await self.decide(adapter_in(mode, False), "deny")
                self.assertEqual(ask.await_args.args, CARD)
                self.assertIsInstance(result, PermissionResultDeny)

    async def test_ask_user_question_is_forwarded_in_every_mode(self):
        question = {"questions": [{"question": "Which database?",
                                   "options": [{"label": "SQLite"}, {"label": "Postgres"}]}]}
        for mode in ("auto", "default"):
            with self.subTest(mode=mode):
                result, ask = await self.decide(adapter_in(mode, True), "Postgres",
                                                "AskUserQuestion", question)
                self.assertEqual(ask.await_args.args, ("Which database?", ["SQLite", "Postgres"]))
                self.assertEqual(result.updated_input["answers"], {"Which database?": "Postgres"})


class ClaudeSessionOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def start(self):
        # Private SDK helpers, imported here so an SDK change fails only these tests.
        from claude_agent_sdk.types import _configure_can_use_tool, _warn_if_can_use_tool_shadowed
        seen = {}

        class Client:
            def __init__(self, options):
                seen["options"] = options

            async def __aenter__(self):
                # As ClaudeSDKClient.connect() does, plus one shadowing notice that names
                # another tool, which must still be shown.
                _configure_can_use_tool(seen["options"])
                _warn_if_can_use_tool_shadowed(dataclasses.replace(
                    seen["options"], allowed_tools=[*ARENA_TOOLS, "Bash"]))
                raise RuntimeError("stop after options")

            async def __aexit__(self, *exc):
                return False

        adapter = ClaudeAdapter({"config": {"auto_approve": True}, "max_turns": 1, "prompt": "p"})
        emit = Mock()
        with warnings.catch_warnings(record=True) as caught, \
             patch("arena.adapters.claude.emit", emit), patch("claude_agent_sdk.ClaudeSDKClient", Client):
            warnings.simplefilter("always")
            with self.assertRaisesRegex(RuntimeError, "stop after options"):
                await adapter.run()
        shown = [str(w.message) for w in caught if issubclass(w.category, CanUseToolShadowedWarning)]
        return adapter, seen["options"], shown, emit

    async def test_session_requests_auto_mode_and_allows_the_arena_tools(self):
        adapter, options, _, emit = await self.start()
        self.assertEqual(options.permission_mode, "auto")
        self.assertEqual(options.can_use_tool, adapter.permission)
        self.assertEqual(options.allowed_tools, ["mcp__arena__ask_user", "mcp__arena__ready",
                                                 "mcp__arena__shared_specification"])
        self.assertEqual(emit.call_args_list[0].args[1]["permission_mode_requested"], "auto")

    async def test_allowed_tools_are_exactly_the_bridge_tools(self):
        from mcp.server.fastmcp import FastMCP
        servers = []
        # Build the bridge's MCP server without serving it, then list what it registered.
        with warnings.catch_warnings(), patch.object(FastMCP, "run", lambda server: servers.append(server)):
            warnings.simplefilter("ignore")
            bridge.serve()
        names = [tool.name for tool in await servers[0].list_tools()]
        self.assertEqual(sorted("mcp__arena__" + name for name in names), sorted(ARENA_TOOLS))

    async def test_only_the_notice_about_the_arena_tools_is_silenced(self):
        from claude_agent_sdk.types import _get_can_use_tool_shadowed_warning
        _, options, shown, _ = await self.start()
        # The SDK does raise this notice for the session's options...
        notice = _get_can_use_tool_shadowed_warning(options.permission_mode, options.allowed_tools)
        self.assertTrue(notice.startswith(
            "can_use_tool will not be invoked for: " + ", ".join(ARENA_TOOLS) + ". "))
        # ...but only the one that also names another tool reached the warnings.
        self.assertEqual(len(shown), 1)
        self.assertIn(", ".join(ARENA_TOOLS) + ", Bash.", shown[0])


class ClaudeModeTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def turn(self, mode_reports, auto_approve=True):
        """One turn: Claude Code reports each mode in turn, then asks to run Bash."""
        adapter = ClaudeAdapter({"config": {"auto_approve": auto_approve}, "max_turns": 1,
                                 "prompt": "p", "turn_key": "t1", "setup_seconds": 2})
        decisions = []

        class Client:
            def __init__(self, options):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_mcp_status(self):
                return {"mcpServers": [{"name": "arena", "status": "connected",
                                        "tools": [{"name": "mcp__arena__ask_user"},
                                                  {"name": "mcp__arena__ready"}]}]}

            async def query(self, prompt):
                pass

            async def receive_response(self):
                for subtype, data in mode_reports:
                    yield SystemMessage(subtype=subtype, data={"session_id": "s1", **data})
                decisions.append(await adapter.permission("Bash", BASH, ToolPermissionContext()))
                yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                    is_error=False, num_turns=1, session_id="s1")

        emit, ask = Mock(), AsyncMock(return_value="Allow")
        # catch_warnings keeps run()'s process-wide warnings filter out of other tests.
        with warnings.catch_warnings(), patch("arena.adapters.claude.emit", emit), \
             patch("arena.adapters.claude.ask", ask), patch("claude_agent_sdk.ClaudeSDKClient", Client):
            await adapter.run()
        diagnostics = [c.args[1]["text"] for c in emit.call_args_list if c.args[0] == "diagnostic"]
        return adapter, decisions[0], ask, diagnostics

    async def test_session_in_auto_mode_forwards_its_escalations(self):
        adapter, decision, ask, diagnostics = await self.turn([("init", {"permissionMode": "auto"})])
        self.assertEqual(adapter.permission_mode, "auto")
        self.assertEqual(ask.await_args.args, CARD)
        self.assertEqual(decision, PermissionResultAllow(updated_input=BASH))
        self.assertEqual(diagnostics, [])

    async def test_session_that_starts_in_manual_mode_keeps_auto_approve(self):
        adapter, decision, ask, diagnostics = await self.turn([("init", {"permissionMode": "default"})])
        self.assertEqual(adapter.permission_mode, "default")
        ask.assert_not_awaited()
        self.assertEqual(decision, PermissionResultAllow(updated_input=BASH))
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("default permission mode rather than auto mode", diagnostics[0])

    async def test_a_reported_mode_change_is_followed(self):
        adapter, _, ask, diagnostics = await self.turn([
            ("init", {"permissionMode": "auto"}),
            ("status", {"status": "requesting"}),
            ("status", {"status": None, "permissionMode": "default"}),
            ("status", {"status": None, "permissionMode": "default"})])
        self.assertEqual(adapter.permission_mode, "default")
        ask.assert_not_awaited()
        self.assertEqual(len(diagnostics), 1)

    async def test_status_without_a_mode_leaves_auto_mode_in_effect(self):
        adapter, _, ask, _ = await self.turn([("init", {"permissionMode": "auto"}),
                                              ("status", {"status": "compacting"})])
        self.assertEqual(adapter.permission_mode, "auto")
        self.assertEqual(ask.await_args.args, CARD)


if __name__ == "__main__":
    unittest.main()
