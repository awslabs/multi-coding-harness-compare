# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Codex approvals: Codex's own approval flow decides what needs approval, auto-review
answers it when auto_approve is set, and what reaches the user is a choice card."""
import unittest
from unittest.mock import AsyncMock, Mock, patch

from arena.adapters.codex import CodexAdapter
from arena.adapters.common import BRIDGE_SERVER

IDS = {"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1", "startedAtMs": 1}
TOOL_APPROVAL = {
    **IDS, "serverName": "arena", "mode": "form",
    "message": 'Allow the arena MCP server to run tool "shared_specification"?',
    "requestedSchema": {"type": "object", "properties": {}},
    "_meta": {"codex_approval_kind": "mcp_tool_call", "persist": ["session", "always"],
              "tool_params": {"question": "Which file name?"}},
}


class CodexApprovalCardTests(unittest.IsolatedAsyncioTestCase):
    async def reply(self, method, params, answer):
        ask = AsyncMock(return_value=answer)
        with patch("arena.adapters.codex.ask", ask):
            result = await CodexAdapter({}).request(method, params)
        return result, ask

    async def test_mcp_tool_approval_is_a_choice_card_not_a_json_form(self):
        result, ask = await self.reply("mcpServer/elicitation/request", TOOL_APPROVAL, "Allow")
        text, choices, kind = ask.await_args.args
        self.assertEqual(choices, ["Allow", "Allow for this session",
                                   "Allow and don't ask me again", "Deny"])
        self.assertEqual(kind, "approval")
        self.assertIsNone(ask.await_args.kwargs.get("schema"))
        self.assertIn('run tool "shared_specification"', text)
        self.assertIn("Which file name?", text)
        self.assertEqual(result, {"action": "accept", "content": {}, "_meta": None})

    async def test_mcp_tool_approval_remember_choices_map_to_codex_persist(self):
        for answer, persist in (("Allow for this session", "session"),
                                ("allow and don't ask me again", "always")):
            with self.subTest(answer=answer):
                result, _ = await self.reply("mcpServer/elicitation/request", TOOL_APPROVAL, answer)
                self.assertEqual(result, {"action": "accept", "content": {},
                                          "_meta": {"persist": persist}})

    async def test_mcp_tool_approval_deny_and_free_text_decline(self):
        for answer in ("Deny", "not sure, explain first"):
            with self.subTest(answer=answer):
                result, _ = await self.reply("mcpServer/elicitation/request", TOOL_APPROVAL, answer)
                self.assertEqual(result, {"action": "decline", "content": None, "_meta": None})

    async def test_mcp_tool_approval_offers_only_the_remember_options_codex_sends(self):
        for persist, expected in ((None, ["Allow", "Deny"]),
                                  ("session", ["Allow", "Allow for this session", "Deny"]),
                                  (["always"], ["Allow", "Allow and don't ask me again", "Deny"])):
            with self.subTest(persist=persist):
                meta = {"codex_approval_kind": "mcp_tool_call"}
                if persist is not None:
                    meta["persist"] = persist
                _, ask = await self.reply("mcpServer/elicitation/request",
                                          {**TOOL_APPROVAL, "_meta": meta}, "Deny")
                self.assertEqual(ask.await_args.args[1], expected)

    async def test_command_approval_card_reads_like_a_tool_call(self):
        params = {**IDS, "kind": "command", "command": "rm -rf build", "cwd": "/workspace",
                  "reason": "clean the build output", "commandActions": [{"type": "unknown"}]}
        result, ask = await self.reply("item/commandExecution/requestApproval", params, "allow")
        text, choices, kind = ask.await_args.args
        self.assertEqual((choices, kind), (["Allow", "Deny"], "approval"))
        self.assertEqual(text.splitlines()[0], "Run command")
        for detail in ("Command: rm -rf build", "Directory: /workspace",
                       "Reason: clean the build output"):
            self.assertIn(detail, text)
        for noise in ("thread-1", "turn-1", "item-1", "commandActions"):
            self.assertNotIn(noise, text)
        self.assertEqual(result, {"decision": "accept"})
        result, _ = await self.reply("item/commandExecution/requestApproval", params, "Deny")
        self.assertEqual(result, {"decision": "decline"})

    async def test_file_change_approval(self):
        params = {**IDS, "reason": "write outside the workspace", "grantRoot": "/tmp/out"}
        result, ask = await self.reply("item/fileChange/requestApproval", params, "Allow")
        text = ask.await_args.args[0]
        self.assertEqual(text.splitlines()[0], "Apply file changes")
        self.assertIn("Reason: write outside the workspace", text)
        self.assertIn("Write access to: /tmp/out", text)
        self.assertEqual(result, {"decision": "accept"})

    async def test_permissions_approval(self):
        params = {**IDS, "environmentId": None, "cwd": "/workspace", "reason": "needs network",
                  "permissions": {"network": {"enabled": True}}}
        result, ask = await self.reply("item/permissions/requestApproval", params, "Allow")
        self.assertEqual(ask.await_args.args[0].splitlines()[0], "Grant additional permissions")
        self.assertEqual(result, {"permissions": {"network": {"enabled": True}}, "scope": "turn"})
        result, _ = await self.reply("item/permissions/requestApproval", params, "Deny")
        self.assertEqual(result, {"permissions": {}, "scope": "turn"})

    async def test_confirmation_form_offers_accept_and_decline(self):
        params = {**IDS, "serverName": "docs", "mode": "form", "message": "Continue?", "_meta": None,
                  "requestedSchema": {"type": "object", "properties": {}}}
        result, ask = await self.reply("mcpServer/elicitation/request", params, "Accept")
        self.assertEqual(ask.await_args.args[1], ["Accept", "Decline"])
        self.assertEqual(result, {"action": "accept", "content": {}})
        result, _ = await self.reply("mcpServer/elicitation/request", params, "Decline")
        self.assertEqual(result, {"action": "decline", "content": None})

    async def test_single_choice_form_fields_become_choices(self):
        cases = (
            ({"type": "string", "enum": ["eu-west-1", "us-east-1"]},
             "us-east-1", ["eu-west-1", "us-east-1", "Decline"], "us-east-1"),
            ({"type": "string", "oneOf": [{"const": "f", "title": "Fahrenheit"},
                                          {"const": "c", "title": "Celsius"}]},
             "Celsius", ["Fahrenheit", "Celsius", "Decline"], "c"),
            ({"type": "boolean"}, "No", ["Yes", "No", "Decline"], False),
        )
        for spec, answer, choices, value in cases:
            with self.subTest(spec=spec):
                params = {**IDS, "serverName": "weather", "mode": "form", "message": "Pick one",
                          "requestedSchema": {"type": "object", "properties": {"unit": spec}}}
                result, ask = await self.reply("mcpServer/elicitation/request", params, answer)
                self.assertEqual(ask.await_args.args[1], choices)
                self.assertEqual(result, {"action": "accept", "content": {"unit": value}})

    async def test_richer_forms_still_take_a_json_answer(self):
        schema = {"type": "object", "properties": {"name": {"type": "string"},
                                                   "count": {"type": "integer"}}}
        params = {**IDS, "serverName": "tickets", "mode": "form", "message": "Details",
                  "requestedSchema": schema}
        result, ask = await self.reply("mcpServer/elicitation/request", params,
                                       '{"name": "a", "count": 2}')
        self.assertEqual(ask.await_args.kwargs["schema"], schema)
        self.assertEqual(result, {"action": "accept", "content": {"name": "a", "count": 2}})

    async def test_url_elicitation_is_unchanged(self):
        params = {**IDS, "serverName": "auth", "mode": "url", "message": "Sign in",
                  "url": "https://example.com/login", "elicitationId": "e1"}
        result, ask = await self.reply("mcpServer/elicitation/request", params, "Completed")
        self.assertEqual(ask.await_args.args[1], ["Completed", "Cancel"])
        self.assertEqual(result, {"action": "accept"})
        result, _ = await self.reply("mcpServer/elicitation/request", params, "Cancel")
        self.assertEqual(result, {"action": "cancel"})

    async def test_tool_approval_asked_as_user_input_is_a_run_approval(self):
        questions = [{"id": "mcp_tool_call_approval_call-1", "question": "Allow arena to run ready?",
                      "options": [{"label": "Allow"}, {"label": "Cancel"}]},
                     {"id": "q2", "question": "Which unit?", "options": [{"label": "C"}]}]
        ask = AsyncMock(side_effect=["Allow", "C"])
        with patch("arena.adapters.codex.ask", ask):
            result = await CodexAdapter({}).request("item/tool/requestUserInput",
                                                    {**IDS, "questions": questions})
        kinds = [call.args[2] for call in ask.await_args_list]
        self.assertEqual(kinds, ["approval", "requirement"])
        self.assertEqual(result, {"answers": {"mcp_tool_call_approval_call-1": {"answers": ["Allow"]},
                                              "q2": {"answers": ["C"]}}})

    async def test_unknown_requests_still_fail_visibly(self):
        with self.assertRaisesRegex(RuntimeError, "Unsupported native request"):
            await CodexAdapter({}).request("item/tool/call", {**IDS})


class CodexThreadApprovalPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def thread_params(self, config, session_id=None):
        turn = {"setup_seconds": 90, "config": config, "prompt": "p"}
        if session_id:
            turn["session_id"] = session_id
        sent = {}

        async def record(method, params, timeout=None):
            if method in ("initialize", "account/read"):
                return {"requiresOpenaiAuth": False, "account": {"type": "amazonBedrock"}}
            if method in ("thread/start", "thread/resume"):
                sent[method] = params
            raise RuntimeError("stop after the thread request")

        with patch("arena.adapters.codex.emit"), \
             patch("arena.adapters.codex.version", AsyncMock(return_value="codex-cli 0.154.0")), \
             patch("arena.adapters.codex.RPC") as constructor:
            constructor.return_value.start = AsyncMock(
                return_value=Mock(call=record, notify=AsyncMock(), close=AsyncMock()))
            with self.assertRaises(RuntimeError):
                await CodexAdapter(turn).run()
        return sent

    async def test_auto_approve_hands_codex_approvals_to_auto_review_not_never(self):
        params = (await self.thread_params({"auto_approve": True}))["thread/start"]
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["approvalsReviewer"], "auto_review")
        self.assertEqual(params["sandbox"], "danger-full-access")

    async def test_without_auto_approve_codex_approvals_come_to_the_user(self):
        params = (await self.thread_params({"auto_approve": False}))["thread/start"]
        self.assertEqual((params["approvalPolicy"], params["approvalsReviewer"]),
                         ("on-request", "user"))

    async def test_arena_tools_are_trusted_and_other_servers_are_unchanged(self):
        docs = {"command": "docs-server", "args": []}
        params = (await self.thread_params({"auto_approve": True,
                                            "mcp_servers": {"docs": docs}}))["thread/start"]
        arena = params["config"]["mcp_servers.arena"]
        self.assertEqual(arena["default_tools_approval_mode"], "approve")
        self.assertEqual({key: arena[key] for key in BRIDGE_SERVER}, BRIDGE_SERVER)
        self.assertNotIn("default_tools_approval_mode", BRIDGE_SERVER)
        self.assertEqual(params["config"]["mcp_servers.docs"], docs)

    async def test_a_resumed_thread_keeps_the_same_approval_settings(self):
        params = (await self.thread_params({"auto_approve": True},
                                           session_id="thread-9"))["thread/resume"]
        self.assertEqual(params["threadId"], "thread-9")
        self.assertEqual((params["approvalPolicy"], params["approvalsReviewer"]),
                         ("on-request", "auto_review"))


if __name__ == "__main__":
    unittest.main()
