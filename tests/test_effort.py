# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The effort chosen in the UI reaches every native harness, for every attempt of a run."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from arena.config import Config, HarnessConfig
from arena.service import Service, harness_catalogue_fingerprint


class EffortSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                          harnesses={"claude": HarnessConfig(), "codex": HarnessConfig(),
                                     "kiro": HarnessConfig(effort="medium")})
        self.service = Service(self.cfg)

    def test_selected_efforts_are_saved_and_applied_to_each_run(self):
        created = self.service.create("Build a CLI", efforts={"claude": "max", "codex": "xhigh"})
        self.assertEqual(created["efforts"], {"claude": "max", "codex": "xhigh", "kiro": "medium"})
        for run in created["runs"]:
            self.assertEqual(self.cfg.harness_for_run(run).effort, created["efforts"][run["harness"]])
            self.assertEqual(run["requested_effort"], created["efforts"][run["harness"]])

    def test_unknown_effort_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "effort"):
            self.service.create("Build a CLI", efforts={"codex": "extreme"})

    def test_effort_must_be_one_the_picked_model_advertises(self):
        fp = harness_catalogue_fingerprint(self.cfg)
        self.service.store.put("system", {"id": "model-catalogue", "fingerprint": fp, "checked_at": 1.0,
            "harnesses": {"claude": {"models": [{"id": "opus-4-6", "efforts": ["low", "medium", "high", "max"]},
                                               {"id": "no-effort", "efforts": []}]}}})
        with self.assertRaisesRegex(ValueError, "opus-4-6 does not support effort xhigh"):
            self.service.create("Build a CLI", models={"claude": "opus-4-6"}, efforts={"claude": "xhigh"})
        with self.assertRaisesRegex(ValueError, "choose one of: none"):
            self.service.create("Build a CLI", models={"claude": "no-effort"}, efforts={"claude": "low"})
        created = self.service.create("Build a CLI", models={"claude": "opus-4-6"}, efforts={"claude": "max"})
        self.assertEqual(created["efforts"]["claude"], "max")

    def test_configured_effort_the_model_lacks_falls_back_to_the_models_default(self):
        fp = harness_catalogue_fingerprint(self.cfg)
        self.service.store.put("system", {"id": "model-catalogue", "fingerprint": fp, "checked_at": 1.0,
            "harnesses": {"kiro": {"models": [{"id": "small", "efforts": ["low", "high"]}]}}})
        created = self.service.create("Build a CLI", models={"kiro": "small"})
        self.assertIsNone(created["efforts"]["kiro"])  # configured "medium" is not offered by "small"
        with self.assertRaisesRegex(ValueError, "small does not support effort medium"):
            self.service.create("Build a CLI", models={"kiro": "small"}, efforts={"kiro": "medium"})

    def test_runs_created_before_effort_selection_keep_the_configured_effort(self):
        self.assertEqual(self.cfg.harness_for_run({"harness": "kiro", "requested_model": None}).effort, "medium")


class CatalogueEffortTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_catalogue_keeps_each_models_effort_levels(self):
        from arena import model_probe
        info = {"models": [
            {"value": "opus", "displayName": "Opus", "supportsEffort": True,
             "supportedEffortLevels": ["low", "medium", "high", "max"]},
            {"value": "haiku", "displayName": "Haiku", "supportsEffort": False},
            {"value": "legacy", "displayName": "Legacy"}]}

        class Client:
            def __init__(self, options):
                pass

            async def __aenter__(self):
                return Mock(get_server_info=AsyncMock(return_value=info))

            async def __aexit__(self, *exc):
                return False

        with patch("claude_agent_sdk.ClaudeSDKClient", Client):
            models = (await model_probe.discover("claude"))["models"]
        self.assertEqual([m.get("efforts") for m in models], [["low", "medium", "high", "max"], [], None])

    async def test_codex_catalogue_keeps_supported_and_default_efforts(self):
        from arena import model_probe

        async def call(method, params, timeout=None):
            if method == "model/list":
                return {"data": [{"id": "gpt", "model": "us.openai.gpt-5.6-sol", "displayName": "Sol",
                                  "defaultReasoningEffort": "low",
                                  "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": ""},
                                                                {"reasoningEffort": "max", "description": ""}]}]}
            return {}

        rpc = Mock(call=call, notify=AsyncMock(), close=AsyncMock())
        with patch.dict("os.environ", {"MCHC_CODEX_DEFAULT_REGION": ""}), \
             patch("arena.model_probe.RPC") as constructor:
            constructor.return_value.start = AsyncMock(return_value=rpc)
            model = (await model_probe.discover("codex"))["models"][0]
        self.assertEqual((model["efforts"], model["default_effort"]), (["low", "max"], "low"))


class AdapterEffortTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_passes_effort_to_the_sdk(self):
        from arena.adapters.claude import ClaudeAdapter
        seen = {}

        class Client:
            def __init__(self, options):
                seen["effort"] = options.effort

            async def __aenter__(self):
                raise RuntimeError("stop after options")

            async def __aexit__(self, *exc):
                return False

        adapter = ClaudeAdapter({"config": {"auto_approve": True, "effort": "max"}, "max_turns": 1, "prompt": "p"})
        with patch("arena.adapters.claude.emit"), patch("claude_agent_sdk.ClaudeSDKClient", Client):
            with self.assertRaisesRegex(RuntimeError, "stop after options"):
                await adapter.run()
        self.assertEqual(seen["effort"], "max")

    async def test_codex_passes_effort_on_turn_start(self):
        from arena.adapters.codex import CodexAdapter
        adapter = CodexAdapter({"setup_seconds": 90, "config": {"auto_approve": True, "effort": "xhigh"},
                                "prompt": "p"})
        turns = []

        async def record(method, params, timeout=None):
            if method in ("initialize", "account/read"):
                return {"requiresOpenaiAuth": False, "account": {"type": "amazonBedrock"}}
            if method == "thread/start":
                return {"thread": {"id": "thread-1"}}
            if method == "turn/start":
                turns.append(params)
            raise RuntimeError("stop after turn start")

        with patch("arena.adapters.codex.emit"), \
             patch("arena.adapters.codex.version", AsyncMock(return_value="codex-cli 0.154.0")), \
             patch("arena.adapters.codex.RPC") as constructor:
            constructor.return_value.start = AsyncMock(
                return_value=Mock(call=record, notify=AsyncMock(), close=AsyncMock()))
            with self.assertRaises(RuntimeError):
                await adapter.run()
        self.assertEqual(turns[0]["effort"], "xhigh")

    async def test_kiro_uses_a_configured_cli_path_only_where_it_exists(self):
        import tempfile
        from arena.adapters.kiro import KiroAdapter
        with tempfile.NamedTemporaryFile() as cli:
            for configured, expected in ((cli.name, cli.name), ("/absent/on/this/host/kiro-cli", "kiro-cli")):
                adapter = KiroAdapter({"setup_seconds": 5, "config": {"cli_path": configured}, "prompt": "p"})
                versions = AsyncMock(return_value="kiro 1.0")
                rpc = Mock(call=AsyncMock(side_effect=RuntimeError("stop")), close=AsyncMock())
                with self.subTest(configured=configured), patch("arena.adapters.kiro.emit"), \
                     patch.dict("os.environ", {}, clear=False), \
                     patch("arena.adapters.kiro.version", versions), \
                     patch("arena.adapters.kiro.RPC") as constructor:
                    import os
                    os.environ.pop("MCHC_KIRO_CLI", None)
                    constructor.return_value.start = AsyncMock(return_value=rpc)
                    with self.assertRaises(RuntimeError):
                        await adapter.run()
                    self.assertEqual(versions.call_args_list[0].args[0], expected)
                    self.assertEqual(constructor.call_args.args[0][0], expected)

    async def test_kiro_passes_effort_on_the_acp_command(self):
        from arena.adapters.kiro import KiroAdapter
        adapter = KiroAdapter({"setup_seconds": 5, "config": {"model": "kimi-k3", "effort": "high"},
                               "prompt": "p"})
        rpc = Mock(call=AsyncMock(side_effect=RuntimeError("stop after launch")), close=AsyncMock())
        with patch("arena.adapters.kiro.emit"), \
             patch("arena.adapters.kiro.version", AsyncMock(return_value="kiro 1.0")), \
             patch("arena.adapters.kiro.RPC") as constructor:
            constructor.return_value.start = AsyncMock(return_value=rpc)
            with self.assertRaises(RuntimeError):
                await adapter.run()
        self.assertEqual(constructor.call_args.args[0],
                         ["kiro-cli", "acp", "--model", "kimi-k3", "--effort", "high"])


if __name__ == "__main__":
    unittest.main()
