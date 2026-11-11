# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from arena.config import Config
from arena.models import ModelCatalogue
from arena.store import Store


class ModelCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.td.name))
        self.catalogue = ModelCatalogue(Config(runtime="native", data_dir=self.td.name), self.store)

    def tearDown(self):
        self.td.cleanup()

    def test_reads_serve_an_old_cache_without_probing(self):
        self.store.put("system", {**self.catalogue.cached(), "checked_at": 1.0})
        with patch.object(self.catalogue, "discover_all", AsyncMock()) as discover:
            asyncio.run(self.catalogue.get())
        discover.assert_not_called()

    def test_refresh_lists_bedrock_profiles_only_on_agentcore(self):
        with patch.object(self.catalogue.runtime, "cleanup_owned", AsyncMock()), \
                patch.object(self.catalogue, "bedrock_profiles") as listed, \
                patch.object(self.catalogue, "probe", AsyncMock(return_value={"models": [{"id": "m"}]})):
            data = asyncio.run(self.catalogue.get(refresh=True))
        listed.assert_not_called()
        self.assertEqual([m["id"] for m in data["harnesses"]["claude"]["models"]], ["m"])

    def test_refresh_appends_bedrock_profiles_for_claude_and_codex(self):
        self.catalogue.config = self.catalogue.config.model_copy(update={"runtime": "agentcore"})
        profiles = {"us.anthropic.claude-sonnet-5-5": "US Sonnet 5.5",
                    "us.openai.gpt-6-sol": "US GPT-6 Sol", "us.amazon.nova-pro-v1:0": "US Nova Pro"}
        probe = AsyncMock(return_value={"models": [{"id": "us.openai.gpt-6-sol"}], "source": "native"})
        with patch.object(self.catalogue.runtime, "cleanup_owned", AsyncMock()), \
                patch.object(self.catalogue, "bedrock_profiles", return_value=profiles), \
                patch.object(self.catalogue, "probe", probe):
            data = asyncio.run(self.catalogue.get(refresh=True))
        ids = {h: [m["id"] for m in e["models"]] for h, e in data["harnesses"].items()}
        self.assertEqual(ids["claude"], ["us.openai.gpt-6-sol", "us.anthropic.claude-sonnet-5-5"])
        self.assertEqual(ids["codex"], ["us.openai.gpt-6-sol"])
        self.assertEqual(ids["kiro"], ["us.openai.gpt-6-sol"])


if __name__ == "__main__":
    unittest.main()
