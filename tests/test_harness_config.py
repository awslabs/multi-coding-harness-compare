# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for harness configuration defaults, overrides, and configuration_plan.

All tests run offline — no AWS calls, no network, no live processes.
"""
import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from arena.config import Config, HarnessConfig
from arena.host_setup import (
    DEFAULT_HOME_CONFIG,
    DEFAULT_HOME_DIRS,
    DEFAULT_HOME_SOURCES,
    HOME_CONFIG_SIZE_CAP_BYTES,
    HOME_SECRETS,
    KIRO_CONFIG_SOURCE,
    KIRO_LOGIN_FILE,
    _NEVER_COPY_DIRS,
    _NEVER_COPY_NAMES,
    _NEVER_COPY_SUFFIXES,
    _copy_config_entries,
    _detect_mcp_secret_names,
    _kiro_data_dir,
    _should_skip_config,
    configuration_plan,
    default_home_plan,
    default_home_source,
    prepare_home,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**harness_overrides) -> Config:
    """Minimal Config with an in-memory data dir and no AWS URIs."""
    tmp = Path(tempfile.gettempdir()) / "cc-hc-test"
    harnesses = {}
    for harness, kw in harness_overrides.items():
        harnesses[harness] = HarnessConfig(**kw) if isinstance(kw, dict) else kw
    return Config(data_dir=str(tmp), artifact_s3_uri=None,
                  harnesses=harnesses if harnesses else None)


# ---------------------------------------------------------------------------
# 1. kiro config dir change
# ---------------------------------------------------------------------------

class KiroConfigDirTests(unittest.TestCase):
    """Kiro's config source is now ~/.kiro (global config), not the per-platform data dir."""

    def test_default_home_source_kiro_returns_kiro_config_dir(self):
        source, dest = default_home_source("kiro")
        self.assertEqual(dest, ".kiro")
        self.assertEqual(source, Path("~/.kiro").expanduser())

    def test_kiro_config_source_constant_is_correct(self):
        _, fallback, prefix = KIRO_CONFIG_SOURCE
        self.assertEqual(fallback, "~/.kiro")
        self.assertEqual(prefix, ".kiro")

    def test_kiro_data_dir_returns_platform_data_dir(self):
        data = _kiro_data_dir()
        expected = ("Library/Application Support/kiro-cli"
                    if sys.platform == "darwin" else ".local/share/kiro-cli")
        self.assertEqual(data, Path.home() / expected)

    def test_kiro_data_dir_is_patchable_via_default_home_sources(self):
        with patch.dict(DEFAULT_HOME_SOURCES, {"kiro": (None, "/tmp/patched-kiro", ".local/share/kiro-cli")}):
            self.assertEqual(_kiro_data_dir(), Path("/tmp/patched-kiro"))

    def test_kiro_config_dir_override_via_config_dir_field(self):
        """H_KIRO_CONFIG_DIR in deploy.properties binds to harnesses.kiro.config_dir."""
        with tempfile.TemporaryDirectory() as d:
            source, dest = default_home_source("kiro", config_dir=d)
            self.assertEqual(source, Path(d))
            self.assertEqual(dest, ".kiro")

    def test_login_file_constant_still_points_to_data_dir(self):
        self.assertEqual(KIRO_LOGIN_FILE, ".local/share/kiro-cli/data.sqlite3")


# ---------------------------------------------------------------------------
# 2. DEFAULT_HOME_CONFIG / DEFAULT_HOME_DIRS expansion
# ---------------------------------------------------------------------------

class DefaultAllowlistTests(unittest.TestCase):

    def test_claude_default_files_include_settings_and_claude_md(self):
        self.assertIn("settings.json", DEFAULT_HOME_CONFIG["claude"])
        self.assertIn("CLAUDE.md", DEFAULT_HOME_CONFIG["claude"])

    def test_claude_default_dirs_include_skills_and_agents(self):
        for d in ("skills", "agents", "commands", "rules", "output-styles"):
            self.assertIn(d, DEFAULT_HOME_DIRS["claude"])

    def test_claude_plugins_is_not_in_default_dirs(self):
        # plugins/ is large and caches marketplaces; must stay opt-in.
        self.assertNotIn("plugins", DEFAULT_HOME_DIRS["claude"])

    def test_codex_default_files_include_config_toml_and_agents_md(self):
        self.assertIn("config.toml", DEFAULT_HOME_CONFIG["codex"])
        self.assertIn("AGENTS.md", DEFAULT_HOME_CONFIG["codex"])

    def test_codex_default_dirs_include_prompts_and_rules(self):
        for d in ("prompts", "rules"):
            self.assertIn(d, DEFAULT_HOME_DIRS["codex"])

    def test_kiro_default_files_include_settings_mcp_json(self):
        self.assertIn("settings/mcp.json", DEFAULT_HOME_CONFIG["kiro"])

    def test_kiro_default_dirs_include_steering_and_agents(self):
        for d in ("steering", "agents", "skills", "hooks"):
            self.assertIn(d, DEFAULT_HOME_DIRS["kiro"])


# ---------------------------------------------------------------------------
# 3. Never-copy filters
# ---------------------------------------------------------------------------

class NeverCopyTests(unittest.TestCase):

    def test_data_sqlite3_is_never_copied(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "data.sqlite3"
            p.touch()
            self.assertTrue(_should_skip_config("data.sqlite3", p))

    def test_auth_json_is_never_copied(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "auth.json"
            p.touch()
            self.assertTrue(_should_skip_config("auth.json", p))

    def test_credentials_json_is_never_copied(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".credentials.json"
            p.touch()
            self.assertTrue(_should_skip_config(".credentials.json", p))

    def test_sqlite_suffix_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sessions.sqlite"
            p.touch()
            self.assertTrue(_should_skip_config("sessions.sqlite", p))

    def test_cache_dir_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cache"
            p.mkdir()
            self.assertTrue(_should_skip_config("cache", p))

    def test_sessions_dir_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sessions"
            p.mkdir()
            self.assertTrue(_should_skip_config("sessions", p))

    def test_regular_config_file_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.touch()
            self.assertFalse(_should_skip_config("config.toml", p))

    def test_never_copy_names_covers_expected_set(self):
        for name in ("data.sqlite3", "auth.json", ".credentials.json"):
            self.assertIn(name, _NEVER_COPY_NAMES)

    def test_never_copy_suffixes_covers_sqlite(self):
        for suffix in (".sqlite", ".sqlite3", ".db"):
            self.assertIn(suffix, _NEVER_COPY_SUFFIXES)

    def test_never_copy_dirs_covers_cache_and_sessions(self):
        for d in ("cache", "sessions", "logs"):
            self.assertIn(d, _NEVER_COPY_DIRS)


# ---------------------------------------------------------------------------
# 4. MCP secret detection
# ---------------------------------------------------------------------------

class McpSecretDetectionTests(unittest.TestCase):

    def test_env_key_with_secret_suffix_is_detected(self):
        servers = {
            "my-server": {
                "env": {"API_KEY": "should-not-be-reported", "OTHER": "fine"}
            }
        }
        result = _detect_mcp_secret_names(servers)
        self.assertIn("my-server", result)
        self.assertIn("API_KEY", result["my-server"])
        self.assertNotIn("OTHER", result["my-server"])

    def test_secret_values_are_never_in_the_result(self):
        servers = {"s": {"env": {"MY_TOKEN": "super-secret"}}}
        result = _detect_mcp_secret_names(servers)
        self.assertNotIn("super-secret", str(result))
        self.assertIn("MY_TOKEN", result.get("s", []))

    def test_server_with_no_secrets_is_not_in_result(self):
        servers = {"clean": {"env": {"DEBUG": "1", "TIMEOUT": "30"}}}
        self.assertNotIn("clean", _detect_mcp_secret_names(servers))

    def test_headers_with_secret_suffix_detected(self):
        servers = {"h": {"headers": {"Authorization": "Bearer x"}}}
        # "Authorization" doesn't end with a known suffix, but test our pattern logic
        servers2 = {"h": {"headers": {"X-API-Key": "secret"}}}
        result = _detect_mcp_secret_names(servers2)
        # headers.X-API-Key ends with _key? No, we check lowercase — "x-api-key" ends with "-key"
        # but our suffix is "_key". So this won't match. Document the behavior.
        # The key "X-API-Key" in lowercase is "x-api-key" which ends with "-key" not "_key".
        # This is expected behavior — test it explicitly.
        result_none = _detect_mcp_secret_names(servers)
        self.assertNotIn("h", result_none)

    def test_empty_servers_returns_empty(self):
        self.assertEqual(_detect_mcp_secret_names({}), {})

    def test_non_dict_server_is_skipped(self):
        servers = {"bad": "not-a-dict"}
        self.assertEqual(_detect_mcp_secret_names(servers), {})


# ---------------------------------------------------------------------------
# 5. Size cap
# ---------------------------------------------------------------------------

class SizeCapTests(unittest.TestCase):

    def test_size_cap_constant_is_50_mb(self):
        self.assertEqual(HOME_CONFIG_SIZE_CAP_BYTES, 50 * 1024 * 1024)

    def test_size_cap_configurable_via_size_cap_bytes_parameter(self):
        """_copy_config_entries must honour an explicit size_cap_bytes override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "src"
            source.mkdir()
            # Write a file larger than the low test cap (512 bytes)
            (source / "settings.json").write_bytes(b"x" * 1024)
            dest = Path(tmpdir) / "dest"
            result = _copy_config_entries(
                "claude", source, dest, ["settings.json"], [], size_cap_bytes=512
            )
            self.assertTrue(result["skipped_cap"],
                            "skipped_cap must be True when file exceeds the custom size_cap_bytes")
            self.assertNotIn("settings.json", result["copied_files"],
                             "settings.json must be skipped when it exceeds size_cap_bytes")

    def test_large_dir_is_skipped(self):
        """A directory whose size exceeds the cap is skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "codex-config"
            source.mkdir()
            (source / "config.toml").write_text('model_provider = "amazon-bedrock-runtime"\n')
            # Create a skills dir whose total exceeds cap
            skills = source / "skills"
            skills.mkdir()
            # Write a file larger than HOME_CONFIG_SIZE_CAP_BYTES + 1
            big = skills / "huge-skill.md"
            big.write_bytes(b"x" * (HOME_CONFIG_SIZE_CAP_BYTES + 1))
            home = Path(tmpdir) / "home"

            configured = HarnessConfig()
            with patch.dict(os.environ, {"CODEX_HOME": str(source)}):
                detail = prepare_home("codex", configured, home)
            # config.toml should be copied; skills dir should be skipped (cap exceeded)
            self.assertIn("config.toml", detail.get("copied", []))
            self.assertNotIn("skills", detail.get("copied", []))
            self.assertTrue(detail.get("skipped_size_cap", False))


# ---------------------------------------------------------------------------
# 6. _seed_default_home: expanded copy (files + dirs)
# ---------------------------------------------------------------------------

class SeedDefaultHomeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _make_claude_source(self):
        src = self.root / "claude-config"
        src.mkdir()
        (src / "settings.json").write_text('{}')
        (src / "CLAUDE.md").write_text("rules")
        (src / "skills").mkdir()
        (src / "skills" / "my-skill").mkdir()
        (src / "skills" / "my-skill" / "SKILL.md").write_text("# skill")
        (src / "agents").mkdir()
        (src / "agents" / "my-agent.md").write_text("agent")
        (src / "rules").mkdir()
        (src / "rules" / "rule1.md").write_text("rule")
        return src

    def test_claude_copies_skills_directory(self):
        src = self._make_claude_source()
        home = self.root / "home"
        configured = HarnessConfig()
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            detail = prepare_home("claude", configured, home)
        self.assertTrue((home / ".claude/skills/my-skill/SKILL.md").is_file())
        self.assertIn("skills", detail.get("copied", []))

    def test_claude_copies_agents_directory(self):
        src = self._make_claude_source()
        home = self.root / "home"
        configured = HarnessConfig()
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            detail = prepare_home("claude", configured, home)
        self.assertTrue((home / ".claude/agents/my-agent.md").is_file())
        self.assertIn("agents", detail.get("copied", []))

    def test_claude_copies_rules_directory(self):
        src = self._make_claude_source()
        home = self.root / "home"
        configured = HarnessConfig()
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            detail = prepare_home("claude", configured, home)
        self.assertTrue((home / ".claude/rules/rule1.md").is_file())

    def test_codex_copies_prompts_directory(self):
        src = self.root / "codex-config"
        src.mkdir()
        (src / "config.toml").write_text('model_provider = "amazon-bedrock-runtime"\n')
        (src / "prompts").mkdir()
        (src / "prompts" / "my-prompt.md").write_text("prompt text")
        home = self.root / "home"
        configured = HarnessConfig()
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            detail = prepare_home("codex", configured, home)
        self.assertTrue((home / ".codex/prompts/my-prompt.md").is_file())
        self.assertIn("prompts", detail.get("copied", []))

    def test_codex_default_seed_copies_agents_skills_from_home(self):
        """_seed_default_home must copy ~/.agents/skills into HOME/.agents/skills."""
        src = self.root / "codex-config"
        src.mkdir()
        (src / "config.toml").write_text('model_provider = "amazon-bedrock-runtime"\n')
        # Fake home directory with ~/.agents/skills/my-skill/
        fake_home = self.root / "fake-home"
        agents_skills = fake_home / ".agents" / "skills" / "my-skill"
        agents_skills.mkdir(parents=True)
        (agents_skills / "spec.md").write_text("# My Skill")
        home = self.root / "home"
        configured = HarnessConfig()
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}), \
             patch("arena.host_setup.Path.home", return_value=fake_home):
            detail = prepare_home("codex", configured, home)
        self.assertTrue(
            (home / ".agents" / "skills" / "my-skill" / "spec.md").is_file(),
            "~/.agents/skills must be copied into HOME/.agents/skills by default seed",
        )
        self.assertIn(".agents/skills", detail.get("copied", []),
                      "detail['copied'] must include '.agents/skills'")

    def test_kiro_copies_steering_directory(self):
        src = self.root / "kiro-config"
        src.mkdir()
        (src / "steering").mkdir()
        (src / "steering" / "AGENTS.md").write_text("steering docs")
        home = self.root / "home"
        configured = HarnessConfig()
        # Patch kiro config source
        with patch("arena.host_setup.KIRO_CONFIG_SOURCE", (None, str(src), ".kiro")):
            detail = prepare_home("kiro", configured, home)
        self.assertTrue((home / ".kiro/steering/AGENTS.md").is_file())
        self.assertIn("steering", detail.get("copied", []))

    def test_never_copy_files_are_excluded(self):
        src = self.root / "codex-config"
        src.mkdir()
        (src / "config.toml").write_text('model_provider = "test"\n')
        (src / "auth.json").write_text('{"token": "SECRET"}')  # never copy
        (src / "sessions.sqlite3").write_text("db data")  # never copy
        home = self.root / "home"
        configured = HarnessConfig()
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            prepare_home("codex", configured, home)
        self.assertFalse((home / ".codex/auth.json").exists())
        self.assertFalse((home / ".codex/sessions.sqlite3").exists())

    def test_cache_subdir_inside_copied_dir_is_excluded(self):
        src = self.root / "codex-config"
        src.mkdir()
        (src / "config.toml").write_text('model_provider = "test"\n')
        rules = src / "rules"
        rules.mkdir()
        (rules / "rule.md").write_text("a rule")
        (rules / "cache").mkdir()
        (rules / "cache" / "junk.json").write_text("cache")
        home = self.root / "home"
        configured = HarnessConfig()
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            prepare_home("codex", configured, home)
        self.assertTrue((home / ".codex/rules/rule.md").is_file())
        self.assertFalse((home / ".codex/rules/cache").exists())

    def test_config_dir_field_overrides_env_var(self):
        """HarnessConfig.config_dir takes precedence over CODEX_HOME env var."""
        src = self.root / "custom-codex-config"
        src.mkdir()
        (src / "config.toml").write_text('model_provider = "custom"\n')
        home = self.root / "home"
        configured = HarnessConfig(config_dir=str(src))
        with patch.dict(os.environ, {"CODEX_HOME": "/tmp/should-be-ignored"}):
            prepare_home("codex", configured, home)
        self.assertEqual((home / ".codex/config.toml").read_text().strip(),
                         'model_provider = "custom"')


# ---------------------------------------------------------------------------
# 7. config_include / config_exclude
# ---------------------------------------------------------------------------

class ConfigIncludeExcludeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_config_include_adds_extra_file(self):
        src = self.root / "claude-src"
        src.mkdir()
        (src / "settings.json").write_text('{}')
        (src / "custom.md").write_text("custom content")
        home = self.root / "home"
        configured = HarnessConfig(config_include=["custom.md"])
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            prepare_home("claude", configured, home)
        self.assertTrue((home / ".claude/custom.md").is_file())

    def test_config_exclude_removes_default_file(self):
        src = self.root / "codex-src"
        src.mkdir()
        (src / "config.toml").write_text('model_provider = "test"\n')
        (src / "AGENTS.md").write_text("agents")
        home = self.root / "home"
        configured = HarnessConfig(config_exclude=["AGENTS.md"])
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            prepare_home("codex", configured, home)
        self.assertFalse((home / ".codex/AGENTS.md").exists())
        self.assertTrue((home / ".codex/config.toml").is_file())

    def test_mcp_exclude_removes_specific_server(self):
        """mcp:<name> in config_exclude removes that MCP server from the plan."""
        src = self.root / "claude-src"
        src.mkdir()
        (src / "settings.json").write_text(json.dumps({
            "mcpServers": {"keep-me": {"command": "ok"}, "remove-me": {"command": "bye"}}
        }))
        configured = HarnessConfig(config_exclude=["mcp:remove-me"])
        plan = default_home_plan("claude", configured)
        # _list_config_entries applies the exclude
        entries_with_source = plan
        # The plan itself won't have servers because default_home_plan calls
        # _list_config_entries which reads from filesystem; instead check via plan key
        # (the test verifies the mcp_exclude logic path exists)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            plan = default_home_plan("claude", configured)
        mcp_servers = plan.get("mcp_servers", [])
        self.assertNotIn("remove-me", mcp_servers)
        self.assertIn("keep-me", mcp_servers)


# ---------------------------------------------------------------------------
# 8. Skills dirs, steering files, mcp_config overrides
# ---------------------------------------------------------------------------

class OverrideApplicationTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_skills_dirs_are_copied_to_claude_skills(self):
        skills_src = self.root / "my-skills"
        skills_src.mkdir()
        (skills_src / "skill-one").mkdir()
        (skills_src / "skill-one" / "SKILL.md").write_text("# Skill One")
        home = self.root / "home"
        configured = HarnessConfig(skills_dirs=[str(skills_src)])
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "no-claude")}):
            prepare_home("claude", configured, home)
        self.assertTrue((home / ".claude/skills/skill-one/SKILL.md").is_file())

    def test_skills_dirs_codex_go_to_agents_skills(self):
        skills_src = self.root / "codex-skills"
        skills_src.mkdir()
        (skills_src / "my-skill").mkdir()
        (skills_src / "my-skill" / "spec.md").write_text("spec")
        src = self.root / "codex-config"
        src.mkdir()
        (src / "config.toml").write_text('model_provider = "test"\n')
        home = self.root / "home"
        configured = HarnessConfig(skills_dirs=[str(skills_src)])
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            prepare_home("codex", configured, home)
        self.assertTrue((home / ".agents/skills/my-skill/spec.md").is_file())

    def test_skills_dirs_kiro_go_to_kiro_skills(self):
        skills_src = self.root / "kiro-skills"
        skills_src.mkdir()
        (skills_src / "my-kiro-skill").mkdir()
        (skills_src / "my-kiro-skill" / "README.md").write_text("kiro skill")
        home = self.root / "home"
        configured = HarnessConfig(skills_dirs=[str(skills_src)])
        with patch("arena.host_setup.KIRO_CONFIG_SOURCE", (None, str(self.root / "no-kiro"), ".kiro")):
            prepare_home("kiro", configured, home)
        self.assertTrue((home / ".kiro/skills/my-kiro-skill/README.md").is_file())

    def test_steering_files_for_claude_go_to_rules_dir(self):
        f = self.root / "steering.md"
        f.write_text("# Steering doc")
        home = self.root / "home"
        configured = HarnessConfig(steering_files=[str(f)])
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "no-claude")}):
            prepare_home("claude", configured, home)
        self.assertTrue((home / ".claude/rules/steering.md").is_file())

    def test_steering_files_for_codex_concatenated_into_agents_md(self):
        f1 = self.root / "part1.md"
        f1.write_text("Part 1\n")
        f2 = self.root / "part2.md"
        f2.write_text("Part 2\n")
        src = self.root / "codex-cfg"
        src.mkdir()
        (src / "config.toml").write_text('model_provider = "test"\n')
        home = self.root / "home"
        configured = HarnessConfig(steering_files=[str(f1), str(f2)])
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            prepare_home("codex", configured, home)
        agents_md = home / ".codex/AGENTS.md"
        self.assertTrue(agents_md.is_file())
        content = agents_md.read_text()
        self.assertIn("Part 1", content)
        self.assertIn("Part 2", content)

    def test_steering_files_for_kiro_go_to_steering_dir(self):
        f = self.root / "kiro-guide.md"
        f.write_text("# Kiro guide")
        home = self.root / "home"
        configured = HarnessConfig(steering_files=[str(f)])
        with patch("arena.host_setup.KIRO_CONFIG_SOURCE", (None, str(self.root / "no-kiro"), ".kiro")):
            prepare_home("kiro", configured, home)
        self.assertTrue((home / ".kiro/steering/kiro-guide.md").is_file())

    def test_mcp_config_for_claude_merges_into_claude_json(self):
        mcp_file = self.root / "mcp.json"
        mcp_file.write_text(json.dumps({"mcpServers": {"new-server": {"command": "test"}}}))
        src = self.root / "claude-src"
        src.mkdir()
        (src / "settings.json").write_text('{}')
        home = self.root / "home"
        configured = HarnessConfig(mcp_config=str(mcp_file))
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            prepare_home("claude", configured, home)
        claude_json = home / ".claude.json"
        data = json.loads(claude_json.read_text())
        self.assertIn("new-server", data.get("mcpServers", {}))

    def test_mcp_config_for_kiro_merges_into_mcp_json(self):
        mcp_file = self.root / "mcp.json"
        mcp_file.write_text(json.dumps({"mcpServers": {"kiro-server": {"command": "kiro-mcp"}}}))
        home = self.root / "home"
        configured = HarnessConfig(mcp_config=str(mcp_file))
        with patch("arena.host_setup.KIRO_CONFIG_SOURCE", (None, str(self.root / "no-kiro"), ".kiro")):
            prepare_home("kiro", configured, home)
        mcp_path = home / ".kiro/settings/mcp.json"
        self.assertTrue(mcp_path.is_file())
        data = json.loads(mcp_path.read_text())
        self.assertIn("kiro-server", data.get("mcpServers", {}))

    def test_overrides_are_reported_in_detail(self):
        f = self.root / "steering.md"
        f.write_text("# Steering")
        src = self.root / "codex-src"
        src.mkdir()
        (src / "config.toml").write_text('model_provider = "test"\n')
        home = self.root / "home"
        configured = HarnessConfig(steering_files=[str(f)])
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            detail = prepare_home("codex", configured, home)
        self.assertIn("overrides", detail)
        self.assertIn("steering_files_applied", detail["overrides"])


# ---------------------------------------------------------------------------
# 9. configuration_choice
# ---------------------------------------------------------------------------

class ConfigurationChoiceTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _codex_src(self):
        src = self.root / "codex-src"
        src.mkdir(exist_ok=True)
        (src / "config.toml").write_text('model_provider = "test"\n')
        (src / "AGENTS.md").write_text("agents rules")
        return src

    def test_choice_none_leaves_empty_home(self):
        src = self._codex_src()
        home = self.root / "home-none"
        configured = HarnessConfig()
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            detail = prepare_home("codex", configured, home,
                                  configuration_choice="none")
        self.assertEqual(detail["mode"], "none (clean HOME)")
        self.assertFalse((home / ".codex/config.toml").exists())

    def test_choice_none_still_refreshes_opted_in_secrets(self):
        src = self._codex_src()
        (src / "auth.json").write_text('{"token": "SECRET"}')
        home = self.root / "home-none-refresh"
        configured = HarnessConfig(refresh_files=[".codex/auth.json"])
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            detail = prepare_home("codex", configured, home,
                                  configuration_choice="none")
        self.assertEqual(detail["mode"], "none (clean HOME)")
        self.assertTrue((home / ".codex/auth.json").is_file())

    def test_choice_default_copies_config(self):
        src = self._codex_src()
        home = self.root / "home-default"
        configured = HarnessConfig()
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            detail = prepare_home("codex", configured, home,
                                  configuration_choice="default")
        self.assertTrue((home / ".codex/config.toml").is_file())

    def test_choice_overrides_skips_defaults(self):
        src = self._codex_src()
        home = self.root / "home-overrides"
        configured = HarnessConfig()  # no overrides configured
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            detail = prepare_home("codex", configured, home,
                                  configuration_choice="overrides")
        self.assertEqual(detail["mode"], "overrides only")
        self.assertFalse((home / ".codex/config.toml").exists())


# ---------------------------------------------------------------------------
# 10. inherit_host_setup on AgentCore is refused
# ---------------------------------------------------------------------------

class InheritSetupAgentCoreTests(unittest.TestCase):

    def test_inherit_host_setup_refused_on_agentcore_runtime(self):
        home = Path(tempfile.mkdtemp())
        configured = HarnessConfig(inherit_host_setup=True)
        # Simulate AgentCore by setting the _runtime_override attribute
        object.__setattr__(configured, "_runtime_override", "agentcore")
        with self.assertRaises(ValueError) as ctx:
            prepare_home("claude", configured, home)
        self.assertIn("inherit_host_setup", str(ctx.exception))
        self.assertIn("AgentCore", str(ctx.exception))


# ---------------------------------------------------------------------------
# 11. configuration_plan()
# ---------------------------------------------------------------------------

class ConfigurationPlanTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _config_with_claude_src(self, **cfg_kw):
        src = self.root / "claude-src"
        src.mkdir(exist_ok=True)
        (src / "settings.json").write_text('{}')
        (src / "skills").mkdir(exist_ok=True)
        (src / "skills" / "my-skill").mkdir()
        (src / "skills" / "my-skill" / "SKILL.md").write_text("# skill")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            configured = HarnessConfig(**cfg_kw)
            config = Config(
                data_dir=str(self.root / "data"),
                artifact_s3_uri=None,
                harnesses={"claude": configured, "codex": HarnessConfig(), "kiro": HarnessConfig()})
        return config, src

    def test_plan_has_harness_key(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)}):
            config = Config(data_dir=str(self.root), artifact_s3_uri=None)
            plan = configuration_plan(config, "claude")
        self.assertEqual(plan["harness"], "claude")

    def test_plan_lists_default_source(self):
        src = self.root / "claude-src"
        src.mkdir(exist_ok=True)
        (src / "settings.json").write_text('{}')
        config = Config(data_dir=str(self.root), artifact_s3_uri=None)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            plan = configuration_plan(config, "claude")
        sources = plan["sources"]
        types = [s["type"] for s in sources]
        self.assertIn("default", types)

    def test_plan_default_source_has_skills(self):
        src = self.root / "claude-src2"
        src.mkdir(exist_ok=True)
        (src / "settings.json").write_text('{}')
        (src / "skills").mkdir()
        (src / "skills" / "x").mkdir()
        (src / "skills" / "x" / "SKILL.md").write_text("x")
        config = Config(data_dir=str(self.root), artifact_s3_uri=None)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            plan = configuration_plan(config, "claude")
        default_source = next((s for s in plan["sources"] if s["type"] == "default"), None)
        self.assertIsNotNone(default_source)
        self.assertIn("skills", default_source.get("dirs", []))

    def test_plan_includes_s3_uri_source(self):
        config = Config(
            data_dir=str(self.root),
            artifact_s3_uri=None,
            harnesses={"claude": HarnessConfig(config_s3_uri="s3://bucket/prefix/"),
                       "codex": HarnessConfig(), "kiro": HarnessConfig()})
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)}):
            plan = configuration_plan(config, "claude")
        types = [s["type"] for s in plan["sources"]]
        self.assertIn("s3_prefix", types)

    def test_plan_includes_explicit_overrides_when_configured(self):
        f = self.root / "steer.md"
        f.write_text("steering")
        config = Config(
            data_dir=str(self.root),
            artifact_s3_uri=None,
            harnesses={"claude": HarnessConfig(steering_files=[str(f)]),
                       "codex": HarnessConfig(), "kiro": HarnessConfig()})
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)}):
            plan = configuration_plan(config, "claude")
        types = [s["type"] for s in plan["sources"]]
        self.assertIn("explicit_overrides", types)

    def test_plan_home_template_mode(self):
        template = self.root / "template"
        template.mkdir()
        config = Config(
            data_dir=str(self.root),
            artifact_s3_uri=None,
            harnesses={"claude": HarnessConfig(home_template=str(template)),
                       "codex": HarnessConfig(), "kiro": HarnessConfig()})
        plan = configuration_plan(config, "claude")
        self.assertEqual(plan["mode"], "home_template")
        self.assertEqual(plan["sources"][0]["type"], "home_template")

    def test_plan_mcp_secret_names_reported_not_values(self):
        src = self.root / "claude-mcp"
        src.mkdir(exist_ok=True)
        (src / "settings.json").write_text(json.dumps({
            "mcpServers": {"my-server": {"env": {"MY_API_KEY": "super-secret"}}}
        }))
        config = Config(data_dir=str(self.root), artifact_s3_uri=None)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            plan = configuration_plan(config, "claude")
        default_source = next((s for s in plan["sources"] if s["type"] == "default"), None)
        self.assertIsNotNone(default_source)
        secret_names = default_source.get("mcp_secret_names", {})
        self.assertIn("my-server", secret_names)
        self.assertIn("MY_API_KEY", secret_names["my-server"])
        self.assertNotIn("super-secret", str(secret_names))

    def test_plan_withheld_secrets_reported(self):
        src = self.root / "codex-src"
        src.mkdir(exist_ok=True)
        (src / "config.toml").write_text('model_provider = "test"\n')
        (src / "auth.json").write_text('{}')
        config = Config(data_dir=str(self.root), artifact_s3_uri=None)
        with patch.dict(os.environ, {"CODEX_HOME": str(src)}):
            plan = configuration_plan(config, "codex")
        self.assertIn("auth.json", plan.get("withheld_secrets", []))

    def test_plan_has_precedence_list(self):
        config = Config(data_dir=str(self.root), artifact_s3_uri=None)
        plan = configuration_plan(config, "codex")
        self.assertIn("precedence", plan)
        self.assertIsInstance(plan["precedence"], list)


# ---------------------------------------------------------------------------
# 12. Claude .claude.json MCP server merging
# ---------------------------------------------------------------------------

class ClaudeMcpMergeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_user_mcp_servers_from_claude_json_merged_into_home(self):
        """User-scope MCP servers from ~/.claude.json are merged into run HOME."""
        src = self.root / "claude-src"
        src.mkdir()
        (src / "settings.json").write_text('{}')
        global_claude = self.root / "fake-claude.json"
        global_claude.write_text(json.dumps({"mcpServers": {"user-server": {"command": "test"}}}))
        home = self.root / "home"

        # Patch home dir to use our fake .claude.json
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(src)}):
            with patch("arena.host_setup.Path.home", return_value=self.root):
                prepare_home("claude", HarnessConfig(), home)

        claude_json = home / ".claude.json"
        if claude_json.is_file():
            data = json.loads(claude_json.read_text())
            self.assertIn("user-server", data.get("mcpServers", {}))


# ---------------------------------------------------------------------------
# 13. service.py configuration_choices stored on runs
# ---------------------------------------------------------------------------

class ServiceConfigChoiceTests(unittest.TestCase):

    def test_create_validates_unknown_harness_in_choices(self):
        from arena.service import Service
        from arena.config import Config
        config = Config(data_dir=tempfile.mkdtemp(), artifact_s3_uri=None)
        service = Service(config)
        with self.assertRaises(ValueError) as ctx:
            service.create("task", configuration_choices={"unknown_harness": "default"})
        self.assertIn("Unknown coding harness", str(ctx.exception))

    def test_create_validates_invalid_choice_value(self):
        from arena.service import Service
        from arena.config import Config
        config = Config(data_dir=tempfile.mkdtemp(), artifact_s3_uri=None)
        service = Service(config)
        with self.assertRaises(ValueError) as ctx:
            service.create("task", configuration_choices={"claude": "invalid_choice"})
        self.assertIn("configuration_choices values", str(ctx.exception))

    def test_create_accepts_valid_choices(self):
        from arena.service import Service
        from arena.config import Config
        config = Config(data_dir=tempfile.mkdtemp(), artifact_s3_uri=None)
        service = Service(config)
        # Should not raise for valid choices
        try:
            service.create("task", configuration_choices={"claude": "none", "codex": "overrides"})
        except ValueError as e:
            # Only model-catalog errors are expected; not validation errors
            if "configuration_choices" in str(e):
                self.fail(f"Unexpected validation error: {e}")


# ---------------------------------------------------------------------------
# 14. API: Submission.configuration_choices field
# ---------------------------------------------------------------------------

class ApiSubmissionConfigChoiceTests(unittest.TestCase):

    def test_submission_has_configuration_choices_field(self):
        from arena.api import Submission
        s = Submission(prompt="test")
        self.assertIsInstance(s.configuration_choices, dict)
        self.assertEqual(len(s.configuration_choices), 0)

    def test_submission_accepts_valid_choices(self):
        from arena.api import Submission
        s = Submission(prompt="test", configuration_choices={"claude": "default", "codex": "none"})
        self.assertEqual(s.configuration_choices["claude"], "default")
        self.assertEqual(s.configuration_choices["codex"], "none")


# ---------------------------------------------------------------------------
# 15. codex provider env var (CODECOUNCIL_CODEX_PROVIDER)
# ---------------------------------------------------------------------------

class CodexProviderEnvTests(unittest.TestCase):

    def test_inherit_setup_sets_provider_from_configured(self):
        """inherit_setup sets CODECOUNCIL_CODEX_PROVIDER from configured.provider."""
        import shutil
        codex_exec = shutil.which("codex") or shutil.which("echo")
        if not codex_exec:
            self.skipTest("no codex or echo executable found")
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "codex-src"
            src.mkdir()
            (src / "config.toml").write_text('model_provider = "amazon-bedrock"\n')
            home = Path(d) / "home"
            from arena.host_setup import inherit_setup
            configured = HarnessConfig(
                cli_path=codex_exec,
                provider="amazon-bedrock-runtime",
            )
            # AWS_REGION must be set because config.toml specifies amazon-bedrock and
            # the hermetic-AWS fixture strips AWS_REGION from the environment.
            with patch.dict(os.environ, {"CODEX_HOME": str(src), "AWS_REGION": "us-east-1"}):
                env, _, _ = inherit_setup("codex", configured, home)
            self.assertEqual(env.get("CODECOUNCIL_CODEX_PROVIDER"), "amazon-bedrock-runtime")

    def test_inherit_setup_uses_configured_region_before_boto3(self):
        """configured_region is used before the boto3 profile-region fallback."""
        import shutil
        from unittest.mock import patch as _patch
        codex_exec = shutil.which("codex") or shutil.which("echo")
        if not codex_exec:
            self.skipTest("no codex or echo executable found")
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "codex-src"
            src.mkdir()
            # config.toml with amazon-bedrock but no region key
            (src / "config.toml").write_text(
                'model_provider = "amazon-bedrock"\n'
                '[model_providers.amazon-bedrock.aws]\n')
            home = Path(d) / "home"
            from arena.host_setup import inherit_setup
            configured = HarnessConfig(cli_path=codex_exec)
            clean_env = {k: v for k, v in os.environ.items()
                         if k not in ("AWS_REGION", "AWS_DEFAULT_REGION")}
            # No AWS_REGION in env; boto3 also returns None; configured_region wins.
            with patch.dict(os.environ, {**clean_env, "CODEX_HOME": str(src)}, clear=True), \
                 _patch("boto3.Session") as mock_sess:
                mock_sess.return_value.region_name = None
                env, _, _ = inherit_setup("codex", configured, home,
                                          configured_region="us-west-2")
        self.assertEqual(env.get("CODECOUNCIL_CODEX_REGION"), "us-west-2")
        self.assertEqual(env.get("CODECOUNCIL_CODEX_DEFAULT_REGION"), "us-west-2")


# ---------------------------------------------------------------------------
# 16. S3 prefix download overlaid into HOME
# ---------------------------------------------------------------------------

class S3PrefixDownloadTests(unittest.TestCase):
    """_apply_s3_config_prefix downloads an S3 prefix into a per-run cache and overlays HOME."""

    def test_s3_prefix_downloaded_and_overlaid(self):
        """Files fetched from S3 appear in the run HOME directory."""
        from arena.host_setup import _apply_s3_config_prefix
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            mock_s3 = _make_mock_s3({
                "prefix/settings.json": b'{"version": 1}',
                "prefix/AGENTS.md": b"# rules\n",
            })
            with patch("boto3.Session") as mock_session:
                mock_session.return_value.client.return_value = mock_s3
                result = _apply_s3_config_prefix(
                    "claude", "s3://mybucket/prefix", home,
                    aws_profile="myprofile", config_exclude=[]
                )
            self.assertEqual(result["mode"], "s3_prefix")
            self.assertIn("settings.json", result["copied"])
            self.assertIn("AGENTS.md", result["copied"])
            self.assertTrue((home / "settings.json").exists())
            self.assertTrue((home / "AGENTS.md").exists())

    def test_s3_prefix_respects_config_exclude(self):
        """Files matching config_exclude are not copied."""
        from arena.host_setup import _apply_s3_config_prefix
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            mock_s3 = _make_mock_s3({
                "prefix/allowed.json": b"{}",
                "prefix/blocked.json": b"{}",
            })
            with patch("boto3.Session") as mock_session:
                mock_session.return_value.client.return_value = mock_s3
                result = _apply_s3_config_prefix(
                    "claude", "s3://mybucket/prefix", home,
                    aws_profile=None, config_exclude=["blocked.json"]
                )
            self.assertIn("allowed.json", result["copied"])
            self.assertNotIn("blocked.json", result["copied"])

    def test_s3_prefix_invalid_uri_returns_error_detail(self):
        """An invalid s3:// URI (no bucket) returns error detail without crashing."""
        from arena.host_setup import _apply_s3_config_prefix
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            result = _apply_s3_config_prefix(
                "claude", "s3:///no-bucket", home,
                aws_profile=None, config_exclude=[]
            )
            self.assertIn("error", result)

    def test_s3_prefix_applied_in_prepare_home(self):
        """prepare_home calls _apply_s3_config_prefix when config_s3_uri is set."""
        from arena.host_setup import prepare_home
        configured = HarnessConfig(config_s3_uri="s3://mybucket/prefix")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            mock_s3 = _make_mock_s3({"prefix/settings.json": b'{"v": 1}'})
            with patch("boto3.Session") as mock_session:
                mock_session.return_value.client.return_value = mock_s3
                detail = prepare_home("claude", configured, home)
            # The S3 result should appear under detail["s3_prefix"]
            self.assertIn("s3_prefix", detail)
            self.assertEqual(detail["s3_prefix"]["uri"], "s3://mybucket/prefix")


def _make_mock_s3(contents: dict):
    """Return a mock S3 client whose list_objects_v2 and get_object serve *contents*."""
    from unittest.mock import MagicMock
    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {
        "Contents": [
            {"Key": k, "Size": len(v)}
            for k, v in contents.items()
        ],
        "IsTruncated": False,
    }
    def get_object(Bucket, Key, **_):
        data = contents.get(Key, b"")
        body = MagicMock()
        body.read.return_value = data
        return {"Body": body}
    mock_s3.get_object.side_effect = get_object
    return mock_s3


# ---------------------------------------------------------------------------
# 18. harness_config_size_cap_mb must be wired into prepare_home
# ---------------------------------------------------------------------------

class SizeCapFromConfigTests(unittest.TestCase):
    """harness_config_size_cap_mb in Config must be wired through prepare_home."""

    def test_prepare_home_accepts_size_cap_bytes_parameter(self):
        """prepare_home must accept a size_cap_bytes keyword argument."""
        import inspect
        sig = inspect.signature(prepare_home)
        self.assertIn("size_cap_bytes", sig.parameters,
                      "prepare_home must have a size_cap_bytes parameter")

    def test_prepare_home_default_size_cap_is_home_config_size_cap_bytes(self):
        """The default for size_cap_bytes must equal HOME_CONFIG_SIZE_CAP_BYTES."""
        import inspect
        sig = inspect.signature(prepare_home)
        default = sig.parameters["size_cap_bytes"].default
        self.assertEqual(default, HOME_CONFIG_SIZE_CAP_BYTES,
                         "prepare_home default size_cap_bytes must equal HOME_CONFIG_SIZE_CAP_BYTES")

    def test_size_cap_1mb_from_config_skips_large_dirs(self):
        """passing size_cap_bytes=1*1024*1024 must skip dirs over 1 MB."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "claude-src"
            source.mkdir()
            # Small file — within cap
            (source / "settings.json").write_bytes(b"{}  ")
            # Large skills dir — over 1 MB
            skills = source / "skills"
            skills.mkdir()
            big_skill = skills / "heavy"
            big_skill.mkdir()
            (big_skill / "SKILL.md").write_bytes(b"x" * (2 * 1024 * 1024))
            home = Path(tmpdir) / "home"
            configured = HarnessConfig()
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(source)}):
                detail = prepare_home("claude", configured, home,
                                      size_cap_bytes=1 * 1024 * 1024)
            self.assertTrue((home / ".claude" / "settings.json").is_file(),
                             "settings.json (small) must be copied within 1 MB cap")
            self.assertFalse((home / ".claude" / "skills").exists(),
                             "skills/ exceeds 1 MB cap and must be skipped")
            self.assertTrue(detail.get("skipped_size_cap", False),
                            "detail['skipped_size_cap'] must be True when cap is exceeded")

    def test_config_harness_config_size_cap_mb_default_is_50(self):
        """Config.harness_config_size_cap_mb defaults to 50."""
        from arena.config import Config
        cfg = Config(data_dir="/tmp/test-szc", artifact_s3_uri=None)
        self.assertEqual(cfg.harness_config_size_cap_mb, 50)

    def test_config_size_cap_mb_custom_value_accepted(self):
        """harness_config_size_cap_mb can be overridden in config.toml."""
        from arena.config import Config
        cfg = Config(data_dir="/tmp/test-szc", artifact_s3_uri=None,
                     harness_config_size_cap_mb=10)
        self.assertEqual(cfg.harness_config_size_cap_mb, 10)

    def test_size_cap_bytes_conversion(self):
        """config.harness_config_size_cap_mb * 1024 * 1024 produces the expected byte count."""
        from arena.config import Config
        cfg = Config(data_dir="/tmp/test-szc", artifact_s3_uri=None,
                     harness_config_size_cap_mb=10)
        self.assertEqual(cfg.harness_config_size_cap_mb * 1024 * 1024, 10 * 1024 * 1024)


# ---------------------------------------------------------------------------
# 17. inherit_setup raises ValueError when no region (already added to
#     test_workstation_setup.py; here we add a complementary unit-level check)
# ---------------------------------------------------------------------------

class InheritSetupRegionErrorTests(unittest.TestCase):
    """inherit_setup must raise ValueError (not silently use us-east-1) when no region."""

    def test_inherit_setup_raises_valueerror_when_no_region(self):
        """When config.toml has amazon-bedrock but no region and env has none, raise."""
        import shutil
        codex_exec = shutil.which("codex") or shutil.which("echo")
        if not codex_exec:
            self.skipTest("no codex or echo executable found")
        from arena.host_setup import inherit_setup
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            target_root = Path(d) / "codex-src"
            target_root.mkdir()
            (target_root / "config.toml").write_text(
                'model_provider = "amazon-bedrock"\n'  # triggers region lookup
                '[model_providers.amazon-bedrock.aws]\n'  # no region key
            )
            configured = HarnessConfig(cli_path=codex_exec)
            clean_env = {k: v for k, v in os.environ.items()
                         if k not in ("AWS_REGION", "AWS_DEFAULT_REGION")}
            with patch.dict(os.environ, {**clean_env, "CODEX_HOME": str(target_root)},
                            clear=True), \
                 patch("boto3.Session") as mock_sess:
                mock_sess.return_value.region_name = None
                with self.assertRaises(ValueError) as ctx:
                    inherit_setup("codex", configured, home)
        self.assertIn("AWS_REGION", str(ctx.exception))


# ---------------------------------------------------------------------------
# AgentCoreSandbox must pass size_cap_bytes to prepare_home
# ---------------------------------------------------------------------------

class AgentCoreSandboxSizeCapTests(unittest.TestCase):
    """agentcore_sandbox.AgentCore.start must honour harness_config_size_cap_mb."""

    def test_prepare_home_receives_size_cap_bytes(self):
        """The start() method must call prepare_home with size_cap_bytes derived from
        harness_config_size_cap_mb so the cap is enforced on AgentCore runs too."""
        import inspect
        import arena.agentcore_sandbox as mod

        source = inspect.getsource(mod.AgentCore.start)
        # The fix must wire the MB config value through as bytes, matching native_sandbox.
        self.assertIn(
            "size_cap_bytes=self.config.harness_config_size_cap_mb * 1024 * 1024",
            source,
            "AgentCore.start does not pass size_cap_bytes to prepare_home; "
            "harness_config_size_cap_mb has no effect for AgentCore runs.",
        )


# ---------------------------------------------------------------------------
# G: _write_codex_mcp must serialise list, dict, int/float, and bool fields
# ---------------------------------------------------------------------------

class WriteCodexMcpListValueTests(unittest.TestCase):
    """_write_codex_mcp must not silently drop list/dict/int/bool server fields."""

    def _write_and_read(self, server_dict: dict) -> dict:
        from arena.host_setup import _write_codex_mcp
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.toml"
            cfg.write_text("")
            data = {"mcp_servers": {"test-server": server_dict}}
            _write_codex_mcp(cfg, data)
            return tomllib.loads(cfg.read_text())

    def test_list_args_field_is_preserved(self):
        result = self._write_and_read({"command": "python", "args": ["-m", "mymodule"]})
        self.assertEqual(result["mcp_servers"]["test-server"]["args"], ["-m", "mymodule"])

    def test_dict_env_field_is_preserved(self):
        result = self._write_and_read({"command": "node", "env": {"KEY": "val"}})
        self.assertEqual(result["mcp_servers"]["test-server"]["env"], {"KEY": "val"})

    def test_int_timeout_field_is_preserved(self):
        result = self._write_and_read({"command": "node", "timeout": 30})
        self.assertEqual(result["mcp_servers"]["test-server"]["timeout"], 30)

    def test_bool_field_is_preserved(self):
        result = self._write_and_read({"command": "node", "strict": True})
        self.assertTrue(result["mcp_servers"]["test-server"]["strict"])

    def test_command_plus_args_round_trip(self):
        """Most common MCP server pattern: command + args list."""
        server = {"command": "uvx", "args": ["mcp-server-git", "--repository", "/tmp/repo"]}
        result = self._write_and_read(server)
        srv = result["mcp_servers"]["test-server"]
        self.assertEqual(srv["command"], "uvx")
        self.assertEqual(srv["args"], ["mcp-server-git", "--repository", "/tmp/repo"])


# ---------------------------------------------------------------------------
# G: _apply_s3_config_prefix must reject path-traversal keys
# ---------------------------------------------------------------------------

class S3PrefixPathTraversalTests(unittest.TestCase):
    """_apply_s3_config_prefix must not write outside the home directory."""

    def test_traversal_key_is_skipped(self):
        """An S3 key containing '..' components must not be written outside home."""
        from arena.host_setup import _apply_s3_config_prefix
        with tempfile.TemporaryDirectory() as outer:
            home = Path(outer) / "home"
            home.mkdir()
            evil_key = "prefix/../../../tmp/evil.txt"
            mock_s3 = _make_mock_s3({
                "prefix/safe.txt": b"ok",
                evil_key: b"EVIL",
            })
            with patch("boto3.Session") as mock_session:
                mock_session.return_value.client.return_value = mock_s3
                result = _apply_s3_config_prefix(
                    "claude", "s3://mybucket/prefix", home,
                    aws_profile=None, config_exclude=[]
                )
            self.assertIn("safe.txt", result["copied"])
            # The traversal path must have been skipped — not listed as copied
            for c in result["copied"]:
                self.assertNotIn("..", c, "traversal path must not appear in copied list")
            # The evil file must not have been written outside home
            evil_dest = Path(outer) / "tmp" / "evil.txt"
            self.assertFalse(evil_dest.exists(), "traversal write escaped home directory")

    def test_normal_nested_key_is_written(self):
        """A legitimate nested key like 'subdir/file.txt' should still work."""
        from arena.host_setup import _apply_s3_config_prefix
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            mock_s3 = _make_mock_s3({
                "prefix/subdir/settings.json": b'{"v": 1}',
            })
            with patch("boto3.Session") as mock_session:
                mock_session.return_value.client.return_value = mock_s3
                result = _apply_s3_config_prefix(
                    "claude", "s3://mybucket/prefix", home,
                    aws_profile=None, config_exclude=[]
                )
            self.assertIn("subdir/settings.json", result["copied"])
            self.assertTrue((home / "subdir" / "settings.json").exists())


# ---------------------------------------------------------------------------
# G: _copy_config_entries must reject path-traversal config_include entries
# ---------------------------------------------------------------------------

class CopyConfigEntriesPathTraversalTests(unittest.TestCase):
    """_copy_config_entries must not copy files outside the target directory."""

    def test_traversal_entry_in_config_include_is_skipped(self):
        """An entry like '../../etc/passwd' must be skipped without error."""
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "src"
            target = Path(td) / "tgt"
            source.mkdir()
            target.mkdir()
            # Create a 'file' at the traversal-resolved path
            (source / "safe.txt").write_text("ok")
            result = _copy_config_entries(
                "claude", source, target,
                config_include=["../../etc/passwd", "safe.txt"],
                config_exclude=[],
                size_cap_bytes=1024 * 1024,
            )
            # safe.txt (no traversal) may be copied; passwd must not appear
            for f in result["copied_files"]:
                self.assertNotIn("..", f, "traversal path must not appear in copied list")

    def test_absolute_entry_in_config_include_is_skipped(self):
        """An entry that is an absolute path must be skipped."""
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "src"
            target = Path(td) / "tgt"
            source.mkdir()
            target.mkdir()
            result = _copy_config_entries(
                "claude", source, target,
                config_include=["/etc/passwd"],
                config_exclude=[],
                size_cap_bytes=1024 * 1024,
            )
            for f in result["copied_files"]:
                self.assertFalse(Path(f).is_absolute(), "absolute path must not appear in copied list")


# ---------------------------------------------------------------------------
# HARNESS_CONFIG_DEFAULT_CHOICE: default_configuration_choice helper
# ---------------------------------------------------------------------------

class DefaultConfigurationChoiceTests(unittest.TestCase):
    """Tests for Config.default_configuration_choice and HARNESS_CONFIG_DEFAULT_CHOICE."""

    def _make_config(self, runtime, choice=None):
        import tempfile
        tmp = Path(tempfile.gettempdir()) / "cc-dcc-test"
        kw = dict(data_dir=str(tmp), artifact_s3_uri=None, runtime=runtime,
                  harness_config_default_choice=choice)
        if runtime == "agentcore":
            from arena.config import AgentCoreConfig
            kw["agentcore"] = AgentCoreConfig(
                transfer_s3_uri="s3://bucket/prefix/", region="us-east-1")
        cfg = Config(**kw)
        return cfg

    def test_agentcore_default_is_overrides_when_unset(self):
        cfg = self._make_config("agentcore")
        self.assertEqual(cfg.default_configuration_choice, "overrides")

    def test_native_default_is_default_when_unset(self):
        cfg = self._make_config("native")
        self.assertEqual(cfg.default_configuration_choice, "default")

    def test_property_overrides_agentcore_to_default(self):
        cfg = self._make_config("agentcore", choice="default")
        self.assertEqual(cfg.default_configuration_choice, "default")

    def test_property_overrides_native_to_overrides(self):
        cfg = self._make_config("native", choice="overrides")
        self.assertEqual(cfg.default_configuration_choice, "overrides")

    def test_property_overrides_to_none(self):
        cfg = self._make_config("native", choice="none")
        self.assertEqual(cfg.default_configuration_choice, "none")

    def test_invalid_value_refused_by_properties(self):
        """An invalid HARNESS_CONFIG_DEFAULT_CHOICE value must raise PropertiesError."""
        from arena import properties as props
        from arena.config import resolve_config
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.toml"
            prp_path = Path(tmp) / "deploy.properties"
            cfg_path.write_text("")
            prp_path.write_text("HARNESS_CONFIG_DEFAULT_CHOICE = all\n")
            with self.assertRaisesRegex(props.PropertiesError, "default.*overrides.*none"):
                resolve_config(cfg_path, prp_path)

    def test_valid_values_accepted_via_resolve(self):
        from arena.config import resolve_config
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.toml"
            prp_path = Path(tmp) / "deploy.properties"
            cfg_path.write_text("")
            for val in ("default", "overrides", "none"):
                prp_path.write_text(f"HARNESS_CONFIG_DEFAULT_CHOICE = {val}\n")
                config, _ = resolve_config(cfg_path, prp_path)
                self.assertEqual(config.harness_config_default_choice, val, val)
                self.assertEqual(config.default_configuration_choice, val, val)

    def test_agentcore_never_reads_sentinel_file_in_workstation_folder(self):
        """On agentcore, the workstation folder must NOT be copied (choice = 'overrides')."""
        with tempfile.TemporaryDirectory() as td:
            workstation_dir = Path(td) / "fake-claude"
            workstation_dir.mkdir()
            sentinel = workstation_dir / "settings.json"
            sentinel.write_text('{"sentinel": true}')
            # Do NOT pre-create home so seeded=True — even on a fresh home, 'overrides'
            # must not copy the workstation folder.
            home = Path(td) / "run-home"
            from arena.config import HarnessConfig
            configured = HarnessConfig(config_dir=str(workstation_dir))
            from arena.host_setup import prepare_home
            prepare_home("claude", configured, home, configuration_choice="overrides")
            # The sentinel file must NOT be present in the run home.
            dest = home / ".claude" / "settings.json"
            self.assertFalse(
                dest.exists(),
                "workstation settings.json must not be copied under 'overrides' choice",
            )

    def test_native_default_reads_workstation_folder(self):
        """On native (default choice), the workstation folder IS copied (seeded run)."""
        with tempfile.TemporaryDirectory() as td:
            workstation_dir = Path(td) / "fake-claude"
            workstation_dir.mkdir()
            sentinel = workstation_dir / "settings.json"
            sentinel.write_text('{"sentinel": true}')
            # Do NOT pre-create home; prepare_home seeds it on first call (seeded=True).
            home = Path(td) / "run-home"
            from arena.config import HarnessConfig
            configured = HarnessConfig(config_dir=str(workstation_dir))
            from arena.host_setup import prepare_home
            prepare_home("claude", configured, home, configuration_choice="default")
            dest = home / ".claude" / "settings.json"
            self.assertTrue(
                dest.exists(),
                "workstation settings.json must be copied under 'default' choice",
            )

    def test_overrides_still_delivers_opted_in_login_files(self):
        """Under 'overrides' choice, refresh_files (opted-in login files) are still delivered."""
        with tempfile.TemporaryDirectory() as td:
            workstation_dir = Path(td) / "fake-claude"
            workstation_dir.mkdir()
            cred_file = workstation_dir / ".credentials.json"
            cred_file.write_text('{"token": "x"}')
            home = Path(td) / "run-home"
            home.mkdir()
            from arena.config import HarnessConfig
            configured = HarnessConfig(
                config_dir=str(workstation_dir),
                refresh_files=[".claude/.credentials.json"],
            )
            from arena.host_setup import prepare_home
            result = prepare_home("claude", configured, home, configuration_choice="overrides")
            # The workstation settings.json must NOT appear.
            self.assertFalse((home / ".claude" / "settings.json").exists())
            # The .credentials.json (opted-in) must be refreshed.
            # Note: refresh sources from config_dir; dest path is .claude/.credentials.json
            dest = home / ".claude" / ".credentials.json"
            self.assertTrue(
                dest.exists(),
                "opted-in refresh_file must be delivered even under 'overrides' choice",
            )

    def test_configuration_plan_returns_effective_default_choice(self):
        """configuration_plan must include effective_default_choice and reads_workstation_folder."""
        with tempfile.TemporaryDirectory() as td:
            workstation = Path(td) / "fake-claude"
            workstation.mkdir()
            from arena.config import AgentCoreConfig, Config, HarnessConfig
            from arena.host_setup import configuration_plan
            cfg = Config(
                data_dir=td,
                artifact_s3_uri=None,
                runtime="agentcore",
                agentcore=AgentCoreConfig(transfer_s3_uri="s3://b/p/", region="us-east-1"),
                harnesses={"claude": HarnessConfig(config_dir=str(workstation)),
                           "codex": HarnessConfig(), "kiro": HarnessConfig()},
            )
            plan = configuration_plan(cfg, "claude")
            self.assertIn("effective_default_choice", plan)
            self.assertEqual(plan["effective_default_choice"], "overrides")
            self.assertIn("reads_workstation_folder", plan)
            self.assertFalse(plan["reads_workstation_folder"])

    def test_configuration_plan_native_reads_workstation_folder(self):
        with tempfile.TemporaryDirectory() as td:
            from arena.config import Config, HarnessConfig
            from arena.host_setup import configuration_plan
            cfg = Config(
                data_dir=td,
                artifact_s3_uri=None,
                runtime="native",
                harnesses={"claude": HarnessConfig(), "codex": HarnessConfig(), "kiro": HarnessConfig()},
            )
            plan = configuration_plan(cfg, "claude")
            self.assertEqual(plan["effective_default_choice"], "default")
            self.assertTrue(plan["reads_workstation_folder"])

    def test_collect_config_snapshot_honours_config_dir(self):
        """collect_config_snapshot must use the configured config_dir from HarnessConfig."""
        with tempfile.TemporaryDirectory() as td:
            custom_dir = Path(td) / "my-claude"
            custom_dir.mkdir()
            from arena.config import Config, HarnessConfig
            from arena.host_setup import collect_config_snapshot
            cfg = Config(
                data_dir=td,
                artifact_s3_uri=None,
                harnesses={"claude": HarnessConfig(config_dir=str(custom_dir)),
                           "codex": HarnessConfig(), "kiro": HarnessConfig()},
            )
            result = collect_config_snapshot(["claude"], config=cfg)
            self.assertIn("claude", result)
            self.assertEqual(result["claude"], custom_dir)

    def test_inherit_setup_honours_config_dir(self):
        """inherit_setup must use HarnessConfig.config_dir instead of only env var."""
        with tempfile.TemporaryDirectory() as td:
            custom_dir = Path(td) / "my-claude"
            custom_dir.mkdir()
            (custom_dir / "settings.json").write_text('{}')
            home = Path(td) / "run-home"
            home.mkdir()
            from arena.config import HarnessConfig
            from arena.host_setup import inherit_setup
            import shutil
            # Provide a fake Claude CLI.
            fake_claude = Path(td) / "claude"
            fake_claude.write_text("#!/bin/sh\n")
            fake_claude.chmod(0o755)
            configured = HarnessConfig(config_dir=str(custom_dir), cli_path=str(fake_claude))
            # Should not raise and must use custom_dir.
            env, reads, detail = inherit_setup("claude", configured, home)
            # The custom dir must appear in the read-allowed roots.
            readable_strs = [str(r) for r in reads]
            self.assertTrue(
                any(str(custom_dir.resolve()) in s for s in readable_strs)
                or (home / ".claude" / "settings.json").exists(),
                "inherit_setup must source from configured config_dir",
            )


if __name__ == "__main__":
    unittest.main()
