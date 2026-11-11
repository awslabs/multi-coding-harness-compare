# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for strip_claude_model_policy and the hook points in prepare_home / inherit_setup."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena.config import HarnessConfig
from arena.host_setup import (
    CLAUDE_MODEL_POLICY_ENV,
    CLAUDE_MODEL_POLICY_KEYS,
    strip_claude_model_policy,
    prepare_home,
    inherit_setup,
)


class StripClaudeModelPolicyTests(unittest.TestCase):
    """Unit tests for strip_claude_model_policy()."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def settings_path(self, data: dict) -> Path:
        p = self.root / "settings.json"
        p.write_text(json.dumps(data))
        return p

    # ------------------------------------------------------------------
    # Removal of policy keys
    # ------------------------------------------------------------------

    def test_all_top_level_policy_keys_are_removed(self):
        data = {
            "model": "us.anthropic.claude-opus-5",
            "availableModels": ["opus"],
            "enforceAvailableModels": True,
            "modelOverrides": {},
            "fallbackModel": "claude-haiku",
            "modelSettings": {"effortLevel": "low"},
            "effortLevel": "low",
            "permissions": {"allow": ["Bash"]},
            "hooks": {},
        }
        p = self.settings_path(data)
        removed = strip_claude_model_policy(p)
        result = json.loads(p.read_text())
        for key in CLAUDE_MODEL_POLICY_KEYS:
            self.assertNotIn(key, result, f"{key} should have been removed")
        # Surviving keys
        self.assertIn("permissions", result)
        self.assertIn("hooks", result)
        self.assertCountEqual(
            removed,
            ["model", "availableModels", "enforceAvailableModels",
             "modelOverrides", "fallbackModel", "modelSettings", "effortLevel"],
        )

    def test_env_anthropic_model_is_removed_from_env_block(self):
        data = {
            "env": {
                "ANTHROPIC_MODEL": "us.anthropic.claude-opus-5",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "some-haiku",
                "CLAUDE_CODE_USE_BEDROCK": "1",
            }
        }
        p = self.settings_path(data)
        removed = strip_claude_model_policy(p)
        result = json.loads(p.read_text())
        env = result["env"]
        self.assertNotIn("ANTHROPIC_MODEL", env)
        self.assertIn("ANTHROPIC_DEFAULT_HAIKU_MODEL", env)
        self.assertIn("CLAUDE_CODE_USE_BEDROCK", env)
        self.assertIn("env.ANTHROPIC_MODEL", removed)

    def test_full_user_key_set_is_removed_and_other_env_survives(self):
        data = {
            "model": "opus",
            "availableModels": ["opus"],
            "enforceAvailableModels": True,
            "modelOverrides": {"fast": "haiku"},
            "fallbackModel": "haiku",
            "modelSettings": {"effortLevel": "low"},
            "effortLevel": "low",
            "env": {
                "ANTHROPIC_MODEL": "opus",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "haiku-id",
                "CLAUDE_CODE_USE_BEDROCK": "1",
            },
            "permissions": {"allow": ["Read"]},
        }
        p = self.settings_path(data)
        removed = strip_claude_model_policy(p)
        result = json.loads(p.read_text())
        for key in CLAUDE_MODEL_POLICY_KEYS:
            self.assertNotIn(key, result)
        env = result["env"]
        self.assertNotIn("ANTHROPIC_MODEL", env)
        self.assertIn("ANTHROPIC_DEFAULT_HAIKU_MODEL", env)
        self.assertIn("CLAUDE_CODE_USE_BEDROCK", env)
        self.assertIn("permissions", result)
        expected_removed = set(CLAUDE_MODEL_POLICY_KEYS) | {"env.ANTHROPIC_MODEL"}
        self.assertCountEqual(removed, expected_removed)

    def test_no_policy_keys_present_returns_empty_list(self):
        data = {"permissions": {"allow": ["Bash"]}, "hooks": {}}
        p = self.settings_path(data)
        removed = strip_claude_model_policy(p)
        self.assertEqual(removed, [])
        # File unchanged
        self.assertEqual(json.loads(p.read_text()), data)

    def test_missing_file_returns_empty_list(self):
        p = self.root / "nonexistent.json"
        removed = strip_claude_model_policy(p)
        self.assertEqual(removed, [])

    def test_invalid_json_is_left_untouched_and_returns_empty_list(self):
        p = self.root / "settings.json"
        bad = b"not json {"
        p.write_bytes(bad)
        removed = strip_claude_model_policy(p)
        self.assertEqual(removed, [])
        self.assertEqual(p.read_bytes(), bad)

    def test_file_mode_is_preserved_after_rewrite(self):
        data = {"model": "opus"}
        p = self.settings_path(data)
        os.chmod(p, 0o600)
        strip_claude_model_policy(p)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_non_dict_env_block_does_not_crash(self):
        data = {"model": "opus", "env": ["not", "a", "dict"]}
        p = self.settings_path(data)
        removed = strip_claude_model_policy(p)
        self.assertNotIn("env.ANTHROPIC_MODEL", removed)
        result = json.loads(p.read_text())
        self.assertNotIn("model", result)

    # ------------------------------------------------------------------
    # Values survive byte-for-byte as JSON values
    # ------------------------------------------------------------------

    def test_surviving_values_are_identical_after_strip(self):
        data = {
            "model": "opus",
            "permissions": {"allow": ["Bash", "Read"]},
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]},
            "env": {
                "ANTHROPIC_MODEL": "opus",
                "CLAUDE_CODE_USE_BEDROCK": "1",
            },
        }
        p = self.settings_path(data)
        strip_claude_model_policy(p)
        result = json.loads(p.read_text())
        self.assertEqual(result["permissions"], data["permissions"])
        self.assertEqual(result["hooks"], data["hooks"])
        self.assertEqual(result["env"]["CLAUDE_CODE_USE_BEDROCK"], "1")


class PrepareHomeModelPolicyTests(unittest.TestCase):
    """prepare_home strips model policy for claude, not for other harnesses."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _make_claude_source(self, settings: dict) -> Path:
        src = self.root / "claude-config"
        src.mkdir(exist_ok=True)
        (src / "settings.json").write_text(json.dumps(settings))
        return src

    # ------------------------------------------------------------------
    # Default mode (no home_template)
    # ------------------------------------------------------------------

    def test_default_mode_strips_model_policy(self):
        src = self._make_claude_source({
            "model": "opus",
            "availableModels": ["opus"],
            "permissions": {"allow": ["Read"]},
        })
        home = self.root / "run-home"
        configured = HarnessConfig()
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            detail = prepare_home("claude", configured, home)
        result = json.loads((home / ".claude" / "settings.json").read_text())
        self.assertNotIn("model", result)
        self.assertNotIn("availableModels", result)
        self.assertIn("permissions", result)
        self.assertIn("model_policy_removed", detail)
        self.assertCountEqual(detail["model_policy_removed"], ["model", "availableModels"])

    def test_default_mode_no_policy_keys_no_detail_key(self):
        src = self._make_claude_source({"permissions": {"allow": ["Read"]}})
        home = self.root / "run-home"
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            detail = prepare_home("claude", HarnessConfig(), home)
        self.assertNotIn("model_policy_removed", detail)

    def test_continuation_refresh_re_strips_settings(self):
        """On a second call (home already exists), refresh_files re-copies then strips."""
        src = self._make_claude_source({
            "model": "opus",
            "permissions": {"allow": ["Read"]},
        })
        home = self.root / "run-home"
        configured = HarnessConfig(refresh_files=[".claude/settings.json"])
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            prepare_home("claude", configured, home)
            # Second call simulates continuation — home already exists
            detail = prepare_home("claude", configured, home)
        result = json.loads((home / ".claude" / "settings.json").read_text())
        self.assertNotIn("model", result)
        self.assertIn("model_policy_removed", detail)

    # ------------------------------------------------------------------
    # Template mode
    # ------------------------------------------------------------------

    def test_template_mode_strips_model_policy(self):
        template = self.root / "template"
        (template / ".claude").mkdir(parents=True)
        (template / ".claude" / "settings.json").write_text(json.dumps({
            "model": "opus",
            "enforceAvailableModels": True,
            "hooks": {},
        }))
        home = self.root / "run-home"
        configured = HarnessConfig(home_template=str(template))
        detail = prepare_home("claude", configured, home)
        result = json.loads((home / ".claude" / "settings.json").read_text())
        self.assertNotIn("model", result)
        self.assertNotIn("enforceAvailableModels", result)
        self.assertIn("hooks", result)
        self.assertIn("model_policy_removed", detail)
        self.assertCountEqual(detail["model_policy_removed"], ["model", "enforceAvailableModels"])

    def test_template_mode_no_policy_keys_no_detail_key(self):
        template = self.root / "template"
        (template / ".claude").mkdir(parents=True)
        (template / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))
        home = self.root / "run-home"
        configured = HarnessConfig(home_template=str(template))
        detail = prepare_home("claude", configured, home)
        self.assertNotIn("model_policy_removed", detail)

    # ------------------------------------------------------------------
    # Other harnesses are not touched
    # ------------------------------------------------------------------

    def test_codex_home_settings_are_not_touched(self):
        src = self.root / "codex-config"
        src.mkdir()
        (src / "config.toml").write_text('model_provider = "amazon-bedrock"\n')
        home = self.root / "run-home"
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            detail = prepare_home("codex", HarnessConfig(), home)
        self.assertNotIn("model_policy_removed", detail)

    def test_kiro_home_is_not_touched(self):
        home = self.root / "run-home"
        detail = prepare_home("kiro", HarnessConfig(), home)
        self.assertNotIn("model_policy_removed", detail)


class InheritSetupModelPolicyTests(unittest.TestCase):
    """inherit_setup strips model policy for claude after copying settings.json."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _make_fake_executable(self, name="claude") -> Path:
        exe = self.root / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        return exe

    def test_inherit_setup_strips_policy_and_reports_removed(self):
        src = self.root / "claude-src"
        src.mkdir()
        (src / "settings.json").write_text(json.dumps({
            "model": "opus",
            "availableModels": ["opus"],
            "enforceAvailableModels": True,
            "modelSettings": {"effortLevel": "low"},
            "fallbackModel": "haiku",
            "modelOverrides": {},
            "effortLevel": "low",
            "env": {
                "ANTHROPIC_MODEL": "opus",
                "CLAUDE_CODE_USE_BEDROCK": "1",
            },
            "permissions": {"allow": ["Bash"]},
        }))
        home = self.root / "run-home"
        home.mkdir()
        exe = self._make_fake_executable("claude")
        configured = HarnessConfig(cli_path=str(exe))
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            _env, _reads, detail = inherit_setup("claude", configured, home)
        target = home / ".claude" / "settings.json"
        result = json.loads(target.read_text())
        for key in CLAUDE_MODEL_POLICY_KEYS:
            self.assertNotIn(key, result, f"{key} should be stripped")
        self.assertNotIn("ANTHROPIC_MODEL", result.get("env", {}))
        self.assertIn("CLAUDE_CODE_USE_BEDROCK", result.get("env", {}))
        self.assertIn("permissions", result)
        self.assertIn("model_policy_removed", detail)
        expected = set(CLAUDE_MODEL_POLICY_KEYS) | {"env.ANTHROPIC_MODEL"}
        self.assertCountEqual(detail["model_policy_removed"], expected)

    def test_inherit_setup_no_policy_keys_omits_detail_key(self):
        src = self.root / "claude-src2"
        src.mkdir()
        (src / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Read"]}}))
        home = self.root / "run-home2"
        home.mkdir()
        exe = self._make_fake_executable("claude2")
        configured = HarnessConfig(cli_path=str(exe))
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            _env, _reads, detail = inherit_setup("claude", configured, home)
        self.assertNotIn("model_policy_removed", detail)

    def test_inherit_setup_codex_settings_are_not_stripped(self):
        src = self.root / "codex-src"
        src.mkdir()
        (src / "config.toml").write_text('model_provider = "openai"\n')
        home = self.root / "run-home-cx"
        home.mkdir()
        exe = self._make_fake_executable("codex")
        configured = HarnessConfig(cli_path=str(exe))
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            _env, _reads, detail = inherit_setup("codex", configured, home)
        self.assertNotIn("model_policy_removed", detail)


if __name__ == "__main__":
    unittest.main()
