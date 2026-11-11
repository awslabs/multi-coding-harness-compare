# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for Kiro authentication fixes (Points 1-5).

Point 1: copy-host-login copies login from host data dir regardless of home_template.
Point 2: A configured *_HOME_TEMPLATE that does not exist raises immediately.
Point 3: _require_kiro_login() blocks with an actionable message when no login.
Point 4: Preflight home_templates() and overlay_paths() checks.
Point 5: _expand_globs(), _apply_skills_dirs, _apply_steering_files handle globs/dirs.
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arena.host_setup import (
    DEFAULT_HOME_SOURCES,
    KIRO_LOGIN_FILE,
    _apply_skills_dirs,
    _apply_steering_files,
    _expand_globs,
    _require_kiro_login,
    authentication_plan,
    prepare_home,
)
from arena.config import Config, HarnessConfig


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _config(**overrides):
    return Config(data_dir=str(Path(tempfile.gettempdir()) / "kiro-tests"),
                  artifact_s3_uri=None, **overrides)


def _kiro_config(runtime="agentcore", **harness_kw):
    return _config(runtime=runtime,
                   agentcore={"transfer_s3_uri": "s3://bucket/prefix/"},
                   harnesses={"kiro": HarnessConfig(**harness_kw)})


# ──────────────────────────────────────────────────────────────────────────────
# Point 1: copy-host-login uses host data dir even when home_template is set
# ──────────────────────────────────────────────────────────────────────────────

class CopyHostLoginWithTemplateTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _fake_kiro_data(self):
        """Create a fake kiro data dir containing a login database."""
        data_dir = self.root / "fake-kiro-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(data_dir / "data.sqlite3").close()
        return data_dir

    def test_copy_host_login_reads_from_host_data_dir_not_template(self):
        """copy-host-login must copy data.sqlite3 from the host, not from the template."""
        template = self.root / "kiro-template"
        template.mkdir()
        # The template has NO login file.
        host_data = self._fake_kiro_data()
        home = self.root / "home"

        kiro_cfg = HarnessConfig(
            home_template=str(template),
            refresh_files=[KIRO_LOGIN_FILE],
        )
        with patch("arena.host_setup.KIRO_CONFIG_SOURCE", (None, str(self.root / "no-kiro"), ".kiro")), \
             patch.dict(DEFAULT_HOME_SOURCES,
                        {"kiro": (None, str(host_data), ".local/share/kiro-cli")}):
            detail = prepare_home("kiro", kiro_cfg, home)

        # The login must have been placed from the host data dir.
        self.assertTrue((home / KIRO_LOGIN_FILE).is_file(),
                        "login should be copied from host even when template is configured")
        self.assertIn(KIRO_LOGIN_FILE, detail["refreshed"])

    def test_authentication_plan_copy_host_login_with_template_and_host_login(self):
        """blocked=None when copy-host-login is set, host login exists, and a template is set."""
        template = self.root / "kiro-tpl"
        template.mkdir()
        host_data = self._fake_kiro_data()
        config = _kiro_config(home_template=str(template),
                              refresh_files=[KIRO_LOGIN_FILE])
        with patch.dict(DEFAULT_HOME_SOURCES,
                        {"kiro": (None, str(host_data), ".local/share/kiro-cli")}):
            plan = authentication_plan(config, "kiro")
        self.assertIsNone(plan["blocked"])
        self.assertIn("copied from this workstation", plan["method"])

    def test_authentication_plan_copy_host_login_with_template_but_no_host_login(self):
        """blocked is set when copy-host-login is configured but the host login is absent."""
        template = self.root / "kiro-tpl"
        template.mkdir()
        config = _kiro_config(home_template=str(template),
                              refresh_files=[KIRO_LOGIN_FILE])
        with patch.dict(DEFAULT_HOME_SOURCES,
                        {"kiro": (None, str(self.root / "nonexistent"), ".local/share/kiro-cli")}):
            plan = authentication_plan(config, "kiro")
        self.assertIsNotNone(plan["blocked"])
        self.assertIn("no login exists", plan["blocked"])
        self.assertIn("kiro-cli login", plan["blocked"])


# ──────────────────────────────────────────────────────────────────────────────
# Point 2: configured template must exist
# ──────────────────────────────────────────────────────────────────────────────

class TemplateMustExistTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_missing_kiro_template_raises_on_first_run(self):
        """prepare_home raises ValueError when kiro template dir does not exist."""
        missing = self.root / "no-such-dir"
        kiro_cfg = HarnessConfig(home_template=str(missing))
        home = self.root / "home"
        with self.assertRaises(ValueError) as ctx:
            prepare_home("kiro", kiro_cfg, home)
        self.assertIn("KIRO_HOME_TEMPLATE", str(ctx.exception))
        self.assertIn(str(missing), str(ctx.exception))

    def test_missing_codex_template_raises(self):
        """prepare_home raises ValueError when codex template dir does not exist."""
        missing = self.root / "no-codex"
        codex_cfg = HarnessConfig(home_template=str(missing))
        home = self.root / "home"
        with self.assertRaises(ValueError) as ctx:
            with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "no-codex-src")}):
                prepare_home("codex", codex_cfg, home)
        self.assertIn("CODEX_HOME_TEMPLATE", str(ctx.exception))

    def test_existing_template_does_not_raise(self):
        """prepare_home succeeds when the template directory exists."""
        template = self.root / "kiro-tpl"
        template.mkdir()
        home = self.root / "home"
        kiro_cfg = HarnessConfig(home_template=str(template))
        with patch("arena.host_setup.KIRO_CONFIG_SOURCE", (None, str(self.root / "no-kiro"), ".kiro")):
            # No login → _require_kiro_login may raise; we only care template doesn't.
            try:
                prepare_home("kiro", kiro_cfg, home)
            except (ValueError, RuntimeError) as exc:
                self.assertNotIn("HOME_TEMPLATE", str(exc),
                                 "template existence check must not raise for existing template")


# ──────────────────────────────────────────────────────────────────────────────
# Point 3: _require_kiro_login blocks when no login
# ──────────────────────────────────────────────────────────────────────────────

class RequireKiroLoginTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_passes_when_login_present(self):
        home = self.root / "home"
        login = home / KIRO_LOGIN_FILE
        login.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(login).close()
        cfg = HarnessConfig()
        _require_kiro_login(cfg, home)  # must not raise

    def test_no_login_configured_does_not_raise(self):
        """No login mechanism configured = operator's deliberate choice; no raise."""
        home = self.root / "home"
        home.mkdir()
        cfg = HarnessConfig()  # no refresh_files, no template
        _require_kiro_login(cfg, home)  # must not raise

    def test_raises_copy_host_login_but_host_missing(self):
        home = self.root / "home"
        home.mkdir()
        cfg = HarnessConfig(refresh_files=[KIRO_LOGIN_FILE])
        with patch.dict(DEFAULT_HOME_SOURCES,
                        {"kiro": (None, str(self.root / "nonexistent"), ".local/share/kiro-cli")}):
            with self.assertRaises(RuntimeError) as ctx:
                _require_kiro_login(cfg, home)
        self.assertIn("copy-host-login", str(ctx.exception))
        self.assertIn("kiro-cli login", str(ctx.exception))

    def test_passes_copy_host_login_when_host_login_exists(self):
        home = self.root / "home"
        home.mkdir()
        host_data = self.root / "kiro-data"
        host_data.mkdir()
        sqlite3.connect(host_data / "data.sqlite3").close()
        cfg = HarnessConfig(refresh_files=[KIRO_LOGIN_FILE])
        with patch.dict(DEFAULT_HOME_SOURCES,
                        {"kiro": (None, str(host_data), ".local/share/kiro-cli")}):
            _require_kiro_login(cfg, home)  # must not raise

    def test_template_without_login_raises(self):
        """A template that has no login raises when no copy-host-login."""
        home = self.root / "home"
        home.mkdir()
        tpl = self.root / "kiro-tpl"
        tpl.mkdir()
        cfg = HarnessConfig(home_template=str(tpl))
        with self.assertRaises(RuntimeError) as ctx:
            _require_kiro_login(cfg, home)
        self.assertIn("KIRO_HOME_TEMPLATE", str(ctx.exception))

    def test_template_with_login_passes(self):
        """A template that contains the login file passes."""
        home = self.root / "home"
        home.mkdir()
        tpl = self.root / "kiro-tpl"
        login_path = tpl / KIRO_LOGIN_FILE
        login_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(login_path).close()
        cfg = HarnessConfig(home_template=str(tpl))
        _require_kiro_login(cfg, home)  # must not raise


# ──────────────────────────────────────────────────────────────────────────────
# Point 4: preflight home_templates() and authentication_plan note
# ──────────────────────────────────────────────────────────────────────────────

class PreflightHomeTemplatesTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _pf_with_harnesses(self, harnesses_overrides=None):
        """Build a Preflight with a mock config that has home_template on each harness."""
        from arena import preflight as pf_mod

        def _harness(home_template=None):
            return SimpleNamespace(home_template=home_template)

        kiro_tpl = harnesses_overrides.get("kiro_template") if harnesses_overrides else None
        codex_tpl = harnesses_overrides.get("codex_template") if harnesses_overrides else None
        pf = pf_mod.Preflight(self.root / "config.toml", self.root / "dp", offline=True)
        pf.config = SimpleNamespace(harnesses={
            "claude": _harness(),
            "codex": _harness(home_template=codex_tpl),
            "kiro": _harness(home_template=kiro_tpl),
        })
        return pf

    def test_no_templates_configured_skips_check(self):
        pf = self._pf_with_harnesses()
        pf.home_templates()
        ids = [c["id"] for c in pf.checks]
        self.assertFalse(any("-home-template" in i for i in ids))

    def test_missing_kiro_template_fails(self):
        pf = self._pf_with_harnesses({"kiro_template": str(self.root / "no-such")})
        pf.home_templates()
        checks = {c["id"]: c for c in pf.checks}
        self.assertIn("kiro-home-template", checks)
        self.assertEqual(checks["kiro-home-template"]["status"], "fail")
        self.assertEqual(checks["kiro-home-template"]["gate"], "human")

    def test_existing_kiro_template_passes(self):
        tpl = self.root / "kiro-tpl"
        tpl.mkdir()
        pf = self._pf_with_harnesses({"kiro_template": str(tpl)})
        pf.home_templates()
        checks = {c["id"]: c for c in pf.checks}
        self.assertIn("kiro-home-template", checks)
        self.assertEqual(checks["kiro-home-template"]["status"], "pass")

    def test_authentication_plan_copy_host_login_with_template_adds_note(self):
        """authentication_plan includes a note when copy-host-login + template is fine."""
        tpl = self.root / "kiro-tpl"
        tpl.mkdir()
        host_data = self.root / "kiro-data"
        host_data.mkdir()
        sqlite3.connect(host_data / "data.sqlite3").close()
        config = _kiro_config(home_template=str(tpl),
                              refresh_files=[KIRO_LOGIN_FILE])
        with patch.dict(DEFAULT_HOME_SOURCES,
                        {"kiro": (None, str(host_data), ".local/share/kiro-cli")}):
            plan = authentication_plan(config, "kiro")
        self.assertIsNone(plan["blocked"])
        self.assertIn("note", plan)
        self.assertIn("home_template", plan["note"])


# ──────────────────────────────────────────────────────────────────────────────
# Point 4: preflight overlay_paths()
# ──────────────────────────────────────────────────────────────────────────────

class PreflightOverlayPathsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _pf_with_config(self, harnesses_cfg):
        from arena import preflight as pf_mod
        pf = pf_mod.Preflight(self.root / "config.toml", self.root / "dp", offline=True)
        pf.config = SimpleNamespace(harnesses={
            "claude": SimpleNamespace(skills_dirs=[], steering_files=[], mcp_config=None),
            "codex": SimpleNamespace(skills_dirs=[], steering_files=[], mcp_config=None),
            "kiro": SimpleNamespace(skills_dirs=[], steering_files=[], mcp_config=None),
            **harnesses_cfg,
        })
        return pf

    def test_all_paths_exist_passes(self):
        existing = self.root / "skills"
        existing.mkdir()
        pf = self._pf_with_config({
            "kiro": SimpleNamespace(
                skills_dirs=[str(existing)], steering_files=[], mcp_config=None),
        })
        pf.overlay_paths()
        checks = {c["id"]: c for c in pf.checks}
        self.assertEqual(checks["overlay-paths"]["status"], "pass")

    def test_missing_path_warns(self):
        pf = self._pf_with_config({
            "kiro": SimpleNamespace(
                skills_dirs=[str(self.root / "no-such-skills")],
                steering_files=[], mcp_config=None),
        })
        pf.overlay_paths()
        checks = {c["id"]: c for c in pf.checks}
        self.assertEqual(checks["overlay-paths"]["status"], "warn")
        self.assertEqual(checks["overlay-paths"]["gate"], "human")

    def test_no_overlay_configured_passes(self):
        pf = self._pf_with_config({})
        pf.overlay_paths()
        checks = {c["id"]: c for c in pf.checks}
        self.assertEqual(checks["overlay-paths"]["status"], "pass")


# ──────────────────────────────────────────────────────────────────────────────
# Point 5: _expand_globs, _apply_skills_dirs, _apply_steering_files
# ──────────────────────────────────────────────────────────────────────────────

class ExpandGlobsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_plain_existing_path_matched(self):
        f = self.root / "file.txt"
        f.write_text("x")
        matched, unmatched = _expand_globs([str(f)])
        self.assertEqual(matched, [f])
        self.assertEqual(unmatched, [])

    def test_plain_missing_path_unmatched(self):
        _, unmatched = _expand_globs([str(self.root / "no-file")])
        self.assertEqual(len(unmatched), 1)

    def test_glob_expands_to_matching_files(self):
        (self.root / "a.md").write_text("a")
        (self.root / "b.md").write_text("b")
        matched, unmatched = _expand_globs([str(self.root / "*.md")])
        self.assertEqual(len(matched), 2)
        self.assertEqual(unmatched, [])

    def test_glob_no_match_is_unmatched(self):
        _, unmatched = _expand_globs([str(self.root / "*.xyz")])
        self.assertEqual(len(unmatched), 1)

    def test_tilde_is_expanded(self):
        home = Path.home()
        matched, _ = _expand_globs(["~"])
        self.assertEqual(matched, [home])


class ApplySkillsDirsGlobTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_glob_skills_dirs_expanded(self):
        skills1 = self.root / "skills-a"
        skills1.mkdir()
        (skills1 / "skill-one").mkdir()
        (skills1 / "skill-one" / "README.md").write_text("s1")
        skills2 = self.root / "skills-b"
        skills2.mkdir()
        (skills2 / "skill-two").mkdir()
        (skills2 / "skill-two" / "README.md").write_text("s2")
        home = self.root / "home"
        result = _apply_skills_dirs("claude", [str(self.root / "skills-*")], home)
        self.assertIn("skill-one", " ".join(result["copied"]))
        self.assertIn("skill-two", " ".join(result["copied"]))
        self.assertEqual(result["unmatched"], [])

    def test_missing_skills_dir_reported_as_unmatched(self):
        home = self.root / "home"
        result = _apply_skills_dirs("claude", [str(self.root / "nonexistent")], home)
        self.assertEqual(result["unmatched"], [str(self.root / "nonexistent")])
        self.assertEqual(result["copied"], [])


class ApplySteeringFilesTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_directory_expanded_to_md_files(self):
        steering_dir = self.root / "rules"
        steering_dir.mkdir()
        (steering_dir / "rule1.md").write_text("rule1")
        (steering_dir / "rule2.md").write_text("rule2")
        (steering_dir / "ignore.txt").write_text("txt")
        home = self.root / "home"
        result = _apply_steering_files("claude", [str(steering_dir)], home)
        used_names = [Path(u).name for u in result["used"]]
        self.assertIn("rule1.md", used_names)
        self.assertIn("rule2.md", used_names)
        self.assertNotIn("ignore.txt", used_names)
        self.assertEqual(result["unmatched"], [])

    def test_empty_directory_is_unmatched(self):
        empty_dir = self.root / "empty"
        empty_dir.mkdir()
        home = self.root / "home"
        result = _apply_steering_files("claude", [str(empty_dir)], home)
        self.assertEqual(result["used"], [])
        self.assertEqual(len(result["unmatched"]), 1)

    def test_glob_steering_files_expanded(self):
        (self.root / "a.md").write_text("a")
        (self.root / "b.md").write_text("b")
        home = self.root / "home"
        result = _apply_steering_files("kiro", [str(self.root / "*.md")], home)
        self.assertEqual(len(result["used"]), 2)
        self.assertEqual(result["unmatched"], [])

    def test_missing_steering_file_is_unmatched(self):
        home = self.root / "home"
        result = _apply_steering_files("claude", [str(self.root / "no-file.md")], home)
        self.assertEqual(result["used"], [])
        self.assertEqual(len(result["unmatched"]), 1)

    def test_prepare_home_steering_returns_dict_with_used_key(self):
        f = self.root / "steer.md"
        f.write_text("# steering")
        home = self.root / "home"
        cfg = HarnessConfig(steering_files=[str(f)])
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "no-claude")}):
            detail = prepare_home("claude", cfg, home)
        self.assertIn("overrides", detail)
        steering = detail["overrides"]["steering_files_applied"]
        self.assertIn("used", steering)
        self.assertIn("unmatched", steering)


if __name__ == "__main__":
    unittest.main()
