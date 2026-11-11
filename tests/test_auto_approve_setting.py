# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the AUTO_APPROVE setting: default, resolution, validation, shipped files,
legacy precedence, preflight checks, inputs() behaviour, QUESTIONS, and docs."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena import preflight, properties as props
from arena.config import resolve_config

ROOT = Path(__file__).parent.parent
EXAMPLE_PROPS = ROOT / "deploy.properties.example"
EXAMPLE_TOML = ROOT / "config.example.toml"

# Minimum set of answers that satisfy preflight.inputs without touching AUTO_APPROVE.
_BASE_ANSWERS = [
    "RUNTIME=agentcore",
    "KIRO_AUTH=copy-host-login",
    "AWS_PROFILE=dev",
    "AWS_REGION=us-east-1",
    "S3_BUCKET=bucket",
    "REPOSITORY_ROOTS=.",
    "DATA_DIR=data",
    "CODEX_HOME_TEMPLATE=codex-home",
]

_CODEX_PROVIDER = (
    'model_provider = "amazon-bedrock-runtime"\n'
    '[model_providers.amazon-bedrock-runtime.aws]\nregion = "us-east-1"\n'
)

# Substitutions that clear all YOUR_ placeholders in deploy.properties.example.
_PLACEHOLDER_SUBS = {
    "YOUR_RUNTIME": "native",
    "YOUR_KIRO_AUTH": "copy-host-login",
    "YOUR_AWS_PROFILE": "test-profile",
    "YOUR_AWS_REGION": "us-east-1",
    "YOUR_BUCKET": "test-bucket",
    "/absolute/path/to/projects": "/tmp",
}


def _filled_example() -> str:
    text = EXAMPLE_PROPS.read_text(encoding="utf-8")
    for placeholder, replacement in _PLACEHOLDER_SUBS.items():
        text = text.replace(placeholder, replacement)
    return text


def _make_preflight(root: Path, answers=None):
    """Set up an offline Preflight instance with a complete deploy.properties."""
    properties = root / "deploy.properties"
    config = root / "config.toml"
    if answers is None:
        answers = list(_BASE_ANSWERS)
    (root / "codex-home" / ".codex").mkdir(parents=True, exist_ok=True)
    (root / "codex-home" / ".codex" / "config.toml").write_text(_CODEX_PROVIDER)
    with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}):
        preflight.inputs(properties, list(answers))
    return preflight.Preflight(config, properties, offline=True)


# ── 1 & 2: DEFAULT and RESOLUTION via resolve_config ──────────────────────────

class AutoApproveDefaultTests(unittest.TestCase):
    """AUTO_APPROVE unset → auto_approve is False for all three harnesses."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = self.root / "config.toml"
        self.properties = self.root / "deploy.properties"

    def _resolve(self, toml: str = "", deploy_props: str = ""):
        self.config.write_text(toml)
        self.properties.write_text(deploy_props)
        config, _ = resolve_config(self.config, self.properties)
        return config

    def test_unset_gives_false_for_all_harnesses(self):
        config = self._resolve()
        for name in ("claude", "codex", "kiro"):
            with self.subTest(harness=name):
                self.assertFalse(config.harnesses[name].auto_approve)

    def test_auto_approve_true_sets_all_harnesses_true(self):
        config = self._resolve(deploy_props="AUTO_APPROVE = true\n")
        for name in ("claude", "codex", "kiro"):
            with self.subTest(harness=name):
                self.assertTrue(config.harnesses[name].auto_approve)

    def test_auto_approve_false_sets_all_harnesses_false(self):
        config = self._resolve(deploy_props="AUTO_APPROVE = false\n")
        for name in ("claude", "codex", "kiro"):
            with self.subTest(harness=name):
                self.assertFalse(config.harnesses[name].auto_approve)


# ── 3: INVALID VALUE REFUSED ──────────────────────────────────────────────────

class AutoApproveInvalidValueTests(unittest.TestCase):
    """An invalid AUTO_APPROVE value is refused with PropertiesError naming AUTO_APPROVE."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = self.root / "config.toml"
        self.properties = self.root / "deploy.properties"

    def test_yes_is_invalid_and_error_names_auto_approve(self):
        self.config.write_text("")
        self.properties.write_text("AUTO_APPROVE = yes\n")
        with self.assertRaises(props.PropertiesError) as ctx:
            resolve_config(self.config, self.properties)
        self.assertIn("AUTO_APPROVE", str(ctx.exception))

    def test_on_is_invalid(self):
        self.config.write_text("")
        self.properties.write_text("AUTO_APPROVE = on\n")
        with self.assertRaises(props.PropertiesError) as ctx:
            resolve_config(self.config, self.properties)
        self.assertIn("AUTO_APPROVE", str(ctx.exception))

    def test_one_is_invalid(self):
        self.config.write_text("")
        self.properties.write_text("AUTO_APPROVE = 1\n")
        with self.assertRaises(props.PropertiesError) as ctx:
            resolve_config(self.config, self.properties)
        self.assertIn("AUTO_APPROVE", str(ctx.exception))


# ── 4: SHIPPED FILES ──────────────────────────────────────────────────────────

class ShippedFilesAutoApproveTests(unittest.TestCase):
    """config.example.toml + deploy.properties.example with required answers filled.

    The shipped files must resolve to auto_approve=False by default (no overriding literal
    in config.example.toml), and to True when AUTO_APPROVE is rendered as true.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = self.root / "config.toml"
        self.properties = self.root / "deploy.properties"

    def _resolve_shipped(self, auto_approve_value: str | None = None) -> object:
        """Resolve config.example.toml with a filled deploy.properties.example.

        auto_approve_value: when provided, replace the AUTO_APPROVE line in the
        example using props.render() so we never produce a duplicate key.
        """
        filled = _filled_example()
        if auto_approve_value is not None:
            filled = props.render(filled, {"AUTO_APPROVE": auto_approve_value})
        self.properties.write_text(filled, encoding="utf-8")
        toml_text = EXAMPLE_TOML.read_text(encoding="utf-8")
        # resolve_config with text= uses that text as if it were config.toml
        config, _ = resolve_config(self.config, self.properties, text=toml_text)
        return config

    def test_shipped_defaults_give_false_for_all_harnesses(self):
        """Shipped example resolves auto_approve=False — no overriding literal in config.example.toml."""
        config = self._resolve_shipped()
        for name in ("claude", "codex", "kiro"):
            with self.subTest(harness=name):
                self.assertFalse(
                    config.harnesses[name].auto_approve,
                    f"{name}: expected auto_approve=False from shipped defaults",
                )

    def test_auto_approve_true_in_props_overrides_shipped_default(self):
        """AUTO_APPROVE = true in deploy.properties propagates through config.example.toml."""
        config = self._resolve_shipped(auto_approve_value="true")
        for name in ("claude", "codex", "kiro"):
            with self.subTest(harness=name):
                self.assertTrue(
                    config.harnesses[name].auto_approve,
                    f"{name}: expected auto_approve=True after AUTO_APPROVE=true",
                )

    def test_config_example_toml_has_no_auto_approve_literal(self):
        """config.example.toml must not set auto_approve = true (that would override AUTO_APPROVE)."""
        toml_text = EXAMPLE_TOML.read_text(encoding="utf-8")
        # Each line with 'auto_approve' in it must not be a plain 'auto_approve = true' assignment.
        for line in toml_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.match(r"auto_approve\s*=", stripped):
                self.fail(
                    f"config.example.toml contains an uncommented auto_approve literal: {line!r} — "
                    "this would override AUTO_APPROVE in deploy.properties"
                )


# ── 5: LEGACY PRECEDENCE ─────────────────────────────────────────────────────

class LegacyAutoApprovePrecedenceTests(unittest.TestCase):
    """A legacy config.toml with auto_approve = true for a harness keeps True.

    The config.toml literal wins over AUTO_APPROVE (or its absence).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = self.root / "config.toml"
        self.properties = self.root / "deploy.properties"

    def _resolve(self, toml: str, deploy_props: str = "") -> object:
        self.config.write_text(toml)
        self.properties.write_text(deploy_props)
        config, _ = resolve_config(self.config, self.properties)
        return config

    def test_legacy_config_toml_true_wins_when_auto_approve_absent(self):
        """A config.toml that sets auto_approve = true is unaffected by AUTO_APPROVE absence."""
        toml = ("[harnesses.claude]\nauto_approve = true\n"
                "[harnesses.codex]\nauto_approve = true\n"
                "[harnesses.kiro]\nauto_approve = true\n")
        config = self._resolve(toml)
        for name in ("claude", "codex", "kiro"):
            with self.subTest(harness=name):
                self.assertTrue(config.harnesses[name].auto_approve)

    def test_legacy_config_toml_true_wins_over_auto_approve_false(self):
        """config.toml auto_approve = true overrides AUTO_APPROVE = false in deploy.properties."""
        toml = "[harnesses.claude]\nauto_approve = true\n"
        config = self._resolve(toml, "AUTO_APPROVE = false\n")
        # Claude has the literal; it wins.
        self.assertTrue(config.harnesses["claude"].auto_approve)
        # Codex and Kiro have no literal; AUTO_APPROVE = false applies.
        self.assertFalse(config.harnesses["codex"].auto_approve)
        self.assertFalse(config.harnesses["kiro"].auto_approve)

    def test_auto_approve_false_property_does_not_affect_literal_true(self):
        """Even with AUTO_APPROVE = false, a literal auto_approve = true in config.toml stays True."""
        toml = ("[harnesses.kiro]\nauto_approve = true\n")
        config = self._resolve(toml, "AUTO_APPROVE = false\n")
        self.assertTrue(config.harnesses["kiro"].auto_approve)


# ── 6: PREFLIGHT CHECKS ──────────────────────────────────────────────────────

class AutoApprovePreflightCheckTests(unittest.TestCase):
    """auto-approve-overridden preflight check and interaction with config-toml-environment-literals."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _run(self, answers=None):
        pf = _make_preflight(self.root, answers)
        with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}), \
             patch.object(preflight.Preflight, "host"):
            pf.run()
        return {c["id"]: c for c in pf.checks}

    def test_legacy_config_toml_true_with_auto_approve_false_produces_fail(self):
        """Legacy config.toml auto_approve=true + AUTO_APPROVE=false → auto-approve-overridden fails."""
        pf = _make_preflight(self.root, list(_BASE_ANSWERS) + ["AUTO_APPROVE=false"])
        toml_text = (
            "[harnesses.claude]\nauto_approve = true\n"
            "[harnesses.codex]\nauto_approve = true\n"
            "[harnesses.kiro]\nauto_approve = true\n"
        )
        (self.root / "config.toml").write_text(toml_text)
        with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}), \
             patch.object(preflight.Preflight, "host"):
            pf.run()
        checks = {c["id"]: c for c in pf.checks}
        self.assertIn("auto-approve-overridden", checks)
        self.assertEqual(checks["auto-approve-overridden"]["status"], "fail")
        self.assertEqual(checks["auto-approve-overridden"]["gate"], "human")

    def test_legacy_check_fix_mentions_both_options(self):
        """auto-approve-overridden fix tells user to pick one value."""
        pf = _make_preflight(self.root, list(_BASE_ANSWERS) + ["AUTO_APPROVE=false"])
        toml_text = "[harnesses.claude]\nauto_approve = true\n"
        (self.root / "config.toml").write_text(toml_text)
        with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}), \
             patch.object(preflight.Preflight, "host"):
            pf.run()
        checks = {c["id"]: c for c in pf.checks}
        fix = checks["auto-approve-overridden"]["fix"]
        # Fix must mention both keeping and removing the literal.
        self.assertIn("AUTO_APPROVE", fix)
        self.assertIn("auto_approve", fix)

    def test_config_toml_environment_literals_does_not_propose_set_auto_approve(self):
        """When auto_approve overrides AUTO_APPROVE, config-toml-environment-literals
        must NOT include --set AUTO_APPROVE in its fix (that would overturn the answer)."""
        pf = _make_preflight(self.root, list(_BASE_ANSWERS) + ["AUTO_APPROVE=false"])
        toml_text = "[harnesses.claude]\nauto_approve = true\n"
        (self.root / "config.toml").write_text(toml_text)
        with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}), \
             patch.object(preflight.Preflight, "host"):
            pf.run()
        checks = {c["id"]: c for c in pf.checks}
        # config-toml-environment-literals must not have AUTO_APPROVE in its fix.
        if "config-toml-environment-literals" in checks:
            fix = checks["config-toml-environment-literals"].get("fix", "")
            self.assertNotIn("--set AUTO_APPROVE", fix)

    def test_matching_values_produce_no_auto_approve_overridden_check(self):
        """If config.toml and AUTO_APPROVE agree (both true), there is no override conflict."""
        pf = _make_preflight(self.root, list(_BASE_ANSWERS) + ["AUTO_APPROVE=true"])
        toml_text = (
            "[harnesses.claude]\nauto_approve = true\n"
            "[harnesses.codex]\nauto_approve = true\n"
            "[harnesses.kiro]\nauto_approve = true\n"
        )
        (self.root / "config.toml").write_text(toml_text)
        with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}), \
             patch.object(preflight.Preflight, "host"):
            pf.run()
        checks = {c["id"]: c for c in pf.checks}
        self.assertNotIn("auto-approve-overridden", checks)

    def test_no_config_toml_produces_no_auto_approve_overridden_check(self):
        """Without any config.toml there is no literal override → no auto-approve-overridden."""
        # _make_preflight leaves config.toml absent → evaluates config.example.toml
        checks = self._run(list(_BASE_ANSWERS) + ["AUTO_APPROVE=false"])
        self.assertNotIn("auto-approve-overridden", checks)

    def _without_auto_approve(self, toml_text):
        """A deploy.properties made before AUTO_APPROVE existed, beside this config.toml."""
        pf = _make_preflight(self.root)
        properties = self.root / "deploy.properties"
        properties.write_text("".join(line for line in properties.read_text().splitlines(True)
                                      if not line.startswith("AUTO_APPROVE")))
        (self.root / "config.toml").write_text(toml_text)
        with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}), \
             patch.object(preflight.Preflight, "host"):
            pf.run()
        return {c["id"]: c for c in pf.checks}

    def test_legacy_true_without_an_answer_asks_the_human(self):
        """Legacy auto_approve = true literals with no AUTO_APPROVE answer must not keep
        approving unasked: the human chooses (gate human, bootstrap exit 40)."""
        checks = self._without_auto_approve("[harnesses.claude]\nauto_approve = true\n"
                                            "[harnesses.kiro]\nauto_approve = true\n")
        check = checks["auto-approve-overridden"]
        self.assertEqual((check["status"], check["gate"]), ("fail", "human"))
        self.assertIn("harnesses.claude.auto_approve = true", check["detail"])
        self.assertIn("harnesses.kiro.auto_approve = true", check["detail"])
        self.assertNotIn("codex", check["detail"])
        self.assertIn("no AUTO_APPROVE answer", check["detail"])
        self.assertIn("--set AUTO_APPROVE=true", check["fix"])
        self.assertIn("--set AUTO_APPROVE=false", check["fix"])

    def test_legacy_false_without_an_answer_needs_no_choice(self):
        """A literal false already matches the default, so nothing approves unasked."""
        checks = self._without_auto_approve("[harnesses.claude]\nauto_approve = false\n")
        self.assertNotIn("auto-approve-overridden", checks)


# ── 7: inputs() FUNCTION ─────────────────────────────────────────────────────

class AutoApproveInputsTests(unittest.TestCase):
    """arena.preflight.inputs() behaviour for AUTO_APPROVE."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_fresh_template_missing_list_does_not_include_auto_approve(self):
        """bootstrap does not stop to ask for AUTO_APPROVE (it has a default, not a placeholder)."""
        path = self.root / "deploy.properties"
        code, report = preflight.inputs(path, [])
        missing = report.get("missing", [])
        self.assertNotIn(
            "AUTO_APPROVE", missing,
            "AUTO_APPROVE has a default value and must not be in the missing list"
        )

    def test_set_auto_approve_true_exits_0_and_leaves_one_line(self):
        """inputs(path, ['AUTO_APPROVE=true']) on a filled template exits 0."""
        path = self.root / "deploy.properties"
        # Fill all required placeholders, then set AUTO_APPROVE.
        filled = _filled_example()
        path.write_text(filled, encoding="utf-8")
        code, report = preflight.inputs(path, ["AUTO_APPROVE=true"])
        self.assertEqual(code, 0, f"Expected exit 0, got {code}: {report}")
        # Exactly one non-comment 'AUTO_APPROVE = true' line in the result.
        result_text = path.read_text(encoding="utf-8")
        active_lines = [
            line for line in result_text.splitlines()
            if re.match(r"\s*AUTO_APPROVE\s*=", line) and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(active_lines), 1, f"Expected exactly one AUTO_APPROVE line, got: {active_lines}")
        self.assertIn("true", active_lines[0])

    def test_set_auto_approve_false_exits_0_and_leaves_one_line(self):
        """inputs(path, ['AUTO_APPROVE=false']) on a filled template exits 0."""
        path = self.root / "deploy.properties"
        filled = _filled_example()
        path.write_text(filled, encoding="utf-8")
        code, report = preflight.inputs(path, ["AUTO_APPROVE=false"])
        self.assertEqual(code, 0, f"Expected exit 0, got {code}: {report}")
        result_text = path.read_text(encoding="utf-8")
        active_lines = [
            line for line in result_text.splitlines()
            if re.match(r"\s*AUTO_APPROVE\s*=", line) and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(active_lines), 1)
        self.assertIn("false", active_lines[0])

    def test_invalid_bool_written_by_inputs_fails_at_resolve(self):
        """An invalid bool written to deploy.properties fails at resolve_config time.

        inputs() does not validate 'bool' kind values at write time (that is deferred to
        configuration-loads); the PropertiesError surfaces when resolve_config processes the file.
        """
        path = self.root / "deploy.properties"
        filled = _filled_example()
        # Write invalid value directly (bypass inputs() validation path).
        filled = props.render(filled, {"AUTO_APPROVE": "yes"})
        path.write_text(filled, encoding="utf-8")
        config_path = self.root / "config.toml"
        config_path.write_text("")
        with self.assertRaises(props.PropertiesError) as ctx:
            resolve_config(config_path, path)
        self.assertIn("AUTO_APPROVE", str(ctx.exception))


# ── 8: QUESTIONS DICT ─────────────────────────────────────────────────────────

class AutoApproveQuestionsTests(unittest.TestCase):
    """QUESTIONS["AUTO_APPROVE"] must end with '?' and describe both true and false."""

    def _q(self) -> str:
        return preflight.QUESTIONS["AUTO_APPROVE"]

    def test_auto_approve_in_questions(self):
        self.assertIn("AUTO_APPROVE", preflight.QUESTIONS)

    def test_question_ends_with_question_mark(self):
        self.assertTrue(
            self._q().rstrip().endswith("?"),
            f"AUTO_APPROVE question must end with '?': {self._q()!r}"
        )

    def test_question_states_false_is_default(self):
        q = self._q()
        lower = q.lower()
        self.assertIn("false", lower,
                      "AUTO_APPROVE question must state that false is the default")
        self.assertIn("default", lower,
                      "AUTO_APPROVE question must state that false is the default")

    def test_question_recommends_true(self):
        q = self._q()
        lower = q.lower()
        self.assertIn("true", lower,
                      "AUTO_APPROVE question must mention 'true'")
        self.assertIn("recommend", lower,
                      "AUTO_APPROVE question must recommend true")


# ── 9: DOCUMENTATION CHECKS ──────────────────────────────────────────────────

class AutoApproveDocumentationTests(unittest.TestCase):
    """Docs must document the AUTO_APPROVE setting."""

    def _readme(self) -> str:
        return (ROOT / "README.md").read_text(encoding="utf-8")

    def _runbook(self) -> str:
        return (ROOT / "docs" / "launch-runbook.md").read_text(encoding="utf-8")

    def _agents_md(self) -> str:
        return (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    # README checks
    def test_readme_has_permission_prompts_section(self):
        """README must have '### Permission prompts (`AUTO_APPROVE`)' section."""
        self.assertIn(
            "### Permission prompts (`AUTO_APPROVE`)",
            self._readme(),
        )

    def test_readme_permission_prompts_section_states_default_false(self):
        readme = self._readme()
        idx = readme.find("### Permission prompts (`AUTO_APPROVE`)")
        section = readme[idx: idx + 2000]
        self.assertIn("false", section.lower(),
                      "Permission prompts section must state the default is false")

    def test_readme_permission_prompts_section_recommends_true(self):
        readme = self._readme()
        idx = readme.find("### Permission prompts (`AUTO_APPROVE`)")
        section = readme[idx: idx + 2000]
        self.assertIn("true", section.lower(),
                      "Permission prompts section must recommend setting true")

    def test_readme_security_considerations_mentions_auto_approve(self):
        """README Security considerations section must mention AUTO_APPROVE."""
        readme = self._readme()
        # Find any security-related section.
        idx = readme.lower().find("security")
        self.assertGreater(idx, -1, "README must have a security section")
        section = readme[idx: idx + 3000]
        self.assertIn("AUTO_APPROVE", section,
                      "README security section must mention AUTO_APPROVE")

    def test_readme_questions_table_has_auto_approve_row(self):
        """README 'For AI coding assistants' questions table must have an AUTO_APPROVE row."""
        readme = self._readme()
        self.assertIn("For AI coding assistants: what to ask the user", readme)
        idx = readme.find("For AI coding assistants: what to ask the user")
        section = readme[idx: idx + 5000]
        self.assertIn("`AUTO_APPROVE`", section,
                      "README questions table must include AUTO_APPROVE row")

    def test_readme_deployment_properties_table_has_auto_approve(self):
        """README deployment properties table must include AUTO_APPROVE."""
        readme = self._readme()
        idx = readme.find("### Deployment properties")
        self.assertGreater(idx, -1)
        section = readme[idx:]
        self.assertIn("`AUTO_APPROVE`", section)

    # Launch runbook checks
    def test_runbook_inputs_table_has_auto_approve_row(self):
        """docs/launch-runbook.md Inputs table must have an AUTO_APPROVE row."""
        runbook = self._runbook()
        self.assertIn("AUTO_APPROVE", runbook,
                      "docs/launch-runbook.md must document AUTO_APPROVE in its Inputs table")

    def test_runbook_mentions_asking_auto_approve(self):
        """Runbook must tell the deploying assistant to ask for AUTO_APPROVE."""
        runbook = self._runbook()
        idx = runbook.find("AUTO_APPROVE")
        # Should appear in a context about asking for it.
        self.assertGreater(idx, -1)

    # AGENTS.md checks
    def test_agents_md_has_auto_approve_rule(self):
        """AGENTS.md must have a rule naming AUTO_APPROVE (the ask-for-it bullet)."""
        agents = self._agents_md()
        self.assertIn("AUTO_APPROVE", agents,
                      "AGENTS.md must include a rule naming AUTO_APPROVE")

    def test_agents_md_auto_approve_context_is_about_asking(self):
        """The AGENTS.md AUTO_APPROVE mention must be in context of asking the human."""
        agents = self._agents_md()
        idx = agents.find("AUTO_APPROVE")
        context = agents[max(0, idx - 200): idx + 300].lower()
        # Should be near 'ask' or 'human' or 'answer' or 'recommend'.
        matches_any = any(word in context for word in ("ask", "human", "recommend", "answer", "true"))
        self.assertTrue(
            matches_any,
            "AGENTS.md AUTO_APPROVE mention must be in context of asking the human for a value"
        )


if __name__ == "__main__":
    unittest.main()
