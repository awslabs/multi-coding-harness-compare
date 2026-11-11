# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Documentation sync tests.

Every key in preflight.QUESTIONS appears in the README 'For AI coding
    assistants' section.  A test regenerates the expected row set and compares.

Every key in arena/properties.py::KEYS appears somewhere in the README
    deploy.properties table.  A missing key means the README is out of date.

AGENTS.md must not require a specific assistant (no 'Codex or Claude Code'
    wording that would exclude other tools).

README must describe the two-component architecture.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
README = ROOT / "README.md"
AGENTS_MD = ROOT / "AGENTS.md"
CLAUDE_MD = ROOT / "CLAUDE.md"
GEMINI_MD = ROOT / "GEMINI.md"


def _readme() -> str:
    return README.read_text()


def _agents() -> str:
    return AGENTS_MD.read_text()


class QuestionsTableTests(unittest.TestCase):
    """README must contain every key listed in preflight.QUESTIONS."""

    def _questions(self):
        from arena.preflight import QUESTIONS
        return QUESTIONS

    def test_readme_has_questions_section(self):
        self.assertIn("For AI coding assistants: what to ask the user", _readme())

    def test_every_question_key_appears_in_readme(self):
        readme = _readme()
        questions = self._questions()
        for key in questions:
            with self.subTest(key=key):
                self.assertIn(
                    f"`{key}`",
                    readme,
                    f"Key {key} from preflight.QUESTIONS not found in README",
                )

    def test_readme_questions_section_has_table_header(self):
        readme = _readme()
        section_start = readme.find("For AI coding assistants: what to ask the user")
        self.assertGreater(section_start, -1)
        section = readme[section_start: section_start + 3000]
        self.assertIn("| Key |", section)
        self.assertIn("Question", section)
        self.assertIn("Required", section)
        self.assertIn("How to discover candidates", section)
        self.assertIn("Example", section)

    def test_runtime_question_mentions_agentcore_option(self):
        questions = self._questions()
        self.assertIn("RUNTIME", questions)
        self.assertIn("agentcore", questions["RUNTIME"])

    def test_kiro_auth_question_mentions_both_options(self):
        questions = self._questions()
        self.assertIn("KIRO_AUTH", questions)
        q = questions["KIRO_AUTH"]
        self.assertIn("copy-host-login", q)
        self.assertIn("login-in-home", q)

    def test_user_namespace_question_present_and_ends_with_question_mark(self):
        questions = self._questions()
        self.assertIn("USER_NAMESPACE", questions)
        # All QUESTIONS entries should end with a '?' per spec
        q = questions["USER_NAMESPACE"]
        self.assertTrue(q.rstrip().endswith("?"), f"USER_NAMESPACE question should end with '?': {q!r}")

    def _parse_questions_table(self):
        """Extract {key: question_text} from the README 'For AI coding assistants' table.

        Returns a dict with backtick-stripped question text for each key found.
        Strips surrounding markdown backticks from inline code spans (`` `foo` `` -> ``foo``).
        """
        readme = _readme()
        section_start = readme.find("For AI coding assistants: what to ask the user")
        if section_start < 0:
            return {}
        # Grab the table block (up to the next blank line after the separator row)
        section = readme[section_start: section_start + 5000]
        rows = {}
        for line in section.splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cols = [c.strip() for c in line.split("|") if c.strip()]
            if len(cols) < 2:
                continue
            key_col = cols[0]
            # Key column format: `KEY`
            if key_col.startswith("`") and key_col.endswith("`"):
                key = key_col[1:-1]
                question_col = cols[1]
                # Normalise: strip backtick code spans
                import re as _re
                question_plain = _re.sub(r"`([^`]+)`", r"\1", question_col)
                rows[key] = question_plain
        return rows

    def test_question_text_matches_questions_dict(self):
        """Golden-file sync: each README question column must match QUESTIONS[key].

        If a question is edited in arena/preflight.py QUESTIONS, update README
        to match (the question text is generated from QUESTIONS — not the other way).
        """
        questions = self._questions()
        readme_rows = self._parse_questions_table()
        for key, expected_q in questions.items():
            with self.subTest(key=key):
                self.assertIn(key, readme_rows,
                              f"Key {key!r} not found in README questions table")
                readme_q = readme_rows[key]
                self.assertEqual(
                    readme_q, expected_q,
                    f"Question text mismatch for {key!r}.\n"
                    f"  README:    {readme_q!r}\n"
                    f"  QUESTIONS: {expected_q!r}\n"
                    "Update README.md to match arena/preflight.py QUESTIONS."
                )


class KeysTableTests(unittest.TestCase):
    """README deploy.properties table must cover every key in KEYS."""

    def _keys(self):
        from arena.properties import KEYS
        return KEYS

    def test_every_key_appears_in_readme_deployment_table(self):
        readme = _readme()
        # Find the "Deployment properties" section
        section_start = readme.find("### Deployment properties")
        self.assertGreater(section_start, -1, "README must have '### Deployment properties' section")
        # Look for each key from KEYS in the README from that point onward
        # (the table extends quite far; search from section start to end)
        section = readme[section_start:]
        keys = self._keys()
        for key in keys:
            with self.subTest(key=key.name):
                self.assertIn(
                    f"`{key.name}`",
                    section,
                    f"Key {key.name!r} from properties.KEYS not found in README deployment table",
                )

    def test_readme_has_all_major_key_groups(self):
        readme = _readme()
        for group_header in (
            "Harness image build",
            "Harness configuration",
            "AgentCore",
            "Core AWS",
        ):
            with self.subTest(group=group_header):
                self.assertIn(group_header, readme)

    def test_keys_count_documented_matches_code(self):
        from arena.properties import KEYS
        # Sanity check: we have at least 73 keys
        self.assertGreaterEqual(len(KEYS), 73)


class AssistantNeutralTests(unittest.TestCase):
    """No step in AGENTS.md should be specific to a single assistant."""

    def test_agents_md_does_not_require_codex_or_claude_specifically(self):
        agents = _agents()
        # "Codex or Claude Code" as a compound requirement implies others cannot run this
        self.assertNotIn(
            "Codex or Claude Code",
            agents,
            "AGENTS.md must not say 'Codex or Claude Code' — it should be assistant-neutral",
        )

    def test_agents_md_mentions_using_another_assistant(self):
        agents = _agents()
        self.assertIn(
            "Using another assistant",
            agents,
            "AGENTS.md must include a 'Using another assistant' note",
        )

    def test_agents_md_lists_multiple_assistants(self):
        agents = _agents()
        # Should mention multiple assistants by name
        for name in ("Claude Code", "Codex", "Kiro", "Gemini"):
            with self.subTest(name=name):
                self.assertIn(name, agents)

    def test_claude_md_is_pointer_to_agents_md(self):
        content = CLAUDE_MD.read_text().strip()
        self.assertEqual(content, "@AGENTS.md",
                         "CLAUDE.md must contain exactly '@AGENTS.md'")

    def test_gemini_md_exists_and_is_pointer(self):
        self.assertTrue(GEMINI_MD.exists(), "GEMINI.md must exist")
        content = GEMINI_MD.read_text().strip()
        self.assertIn("AGENTS.md", content,
                      "GEMINI.md must point to AGENTS.md")

    def test_readme_start_section_is_assistant_neutral(self):
        readme = _readme()
        # The old wording "Codex or Claude Code, which find their instructions"
        # would exclude other assistants.
        self.assertNotIn(
            "Codex or Claude Code, which find",
            readme,
            "README Start section must not use 'Codex or Claude Code' exclusively",
        )

    def test_bootstrap_sh_mentions_or_any_other_assistant(self):
        bootstrap = (ROOT / "bootstrap.sh").read_text()
        # The script comment must not restrict to a narrow set of assistants.
        # It must say "or any other" to stay assistant-neutral per the spec.
        self.assertIn(
            "or any other",
            bootstrap,
            "bootstrap.sh must say 'or any other' in its assistant comment",
        )
        # The old narrow form should no longer be present.
        self.assertNotIn(
            "(Codex, Claude Code)",
            bootstrap,
            "bootstrap.sh must not list only Codex and Claude Code in its assistant comment",
        )


class ArchitectureSectionTests(unittest.TestCase):
    """README must describe the two-component architecture."""

    def test_architecture_section_exists(self):
        self.assertIn("Architecture: two components", _readme())

    def test_architecture_section_mentions_both_components(self):
        readme = _readme()
        section_start = readme.find("Architecture: two components")
        section = readme[section_start: section_start + 2000]
        self.assertIn("arena/", section)
        self.assertIn("harness_image/", section)
        self.assertIn("deploy/", section)

    def test_architecture_section_describes_interface(self):
        readme = _readme()
        section_start = readme.find("Architecture: two components")
        section = readme[section_start: section_start + 2000]
        self.assertIn("agentcore-runtimes.json", section)
        self.assertIn("deploy.properties", section)
        self.assertIn("harness-versions.lock.json", section)

    def test_harness_image_readme_exists(self):
        self.assertTrue(
            (ROOT / "harness_image" / "README.md").exists(),
            "harness_image/README.md must exist",
        )


class VersionConsistencyTests(unittest.TestCase):
    """README version mentions must match harness_image/versions.py."""

    def _versions(self):
        from harness_image.versions import CODEX_VERSION, KIRO_VERSION, CLAUDE_SDK_VERSION
        return CODEX_VERSION, KIRO_VERSION, CLAUDE_SDK_VERSION

    def test_readme_codex_version_matches_versions_py(self):
        codex_v, _, _ = self._versions()
        readme = _readme()
        # The README Integration provenance section mentions the CLI versions;
        # the exact line is several hundred chars into the section.
        section_start = readme.find("Integration provenance")
        self.assertGreater(section_start, -1)
        section = readme[section_start:]
        self.assertIn(
            f"Codex {codex_v}",
            section,
            f"README must mention Codex {codex_v} (from versions.py); "
            f"an old version reference may still be present",
        )

    def test_readme_kiro_version_matches_versions_py(self):
        _, kiro_v, _ = self._versions()
        readme = _readme()
        section_start = readme.find("Integration provenance")
        self.assertGreater(section_start, -1)
        section = readme[section_start:]
        self.assertIn(
            f"Kiro CLI {kiro_v}",
            section,
        )


class UpgradeHarnessSectionTests(unittest.TestCase):
    """README must describe the standalone upgrade / image workflow."""

    def test_upgrading_harnesses_section_exists(self):
        self.assertIn("Upgrading harness images (standalone)", _readme())

    def test_section_mentions_deploy_script(self):
        readme = _readme()
        idx = readme.find("Upgrading harness images (standalone)")
        section = readme[idx: idx + 4000]
        self.assertIn("deploy/agentcore-deploy.sh", section)

    def test_section_mentions_version_sources(self):
        readme = _readme()
        idx = readme.find("Upgrading harness images (standalone)")
        section = readme[idx: idx + 4000]
        for source in ("pinned", "installed", "latest"):
            with self.subTest(source=source):
                self.assertIn(source, section)

    def test_section_mentions_approval_token(self):
        readme = _readme()
        idx = readme.find("Upgrading harness images (standalone)")
        section = readme[idx: idx + 4000]
        self.assertIn("approval", section.lower())
        self.assertIn("--approve", section)

    def test_section_mentions_rollback(self):
        readme = _readme()
        idx = readme.find("Upgrading harness images (standalone)")
        section = readme[idx: idx + 4000]
        self.assertIn("ollback", section)  # "Rollback"

    def test_section_mentions_lock_file(self):
        readme = _readme()
        idx = readme.find("Upgrading harness images (standalone)")
        section = readme[idx: idx + 4000]
        self.assertIn("lock", section.lower())


class PrerequisitesIAMLinksTests(unittest.TestCase):
    """README Prerequisites table must link to all three IAM policy files."""

    def _prereqs_block(self) -> str:
        readme = _readme()
        idx = readme.find("**Prerequisites**")
        self.assertGreater(idx, -1, "README must have a Prerequisites block")
        return readme[idx: idx + 4000]

    def test_prerequisites_links_workstation_policy(self):
        prereqs = self._prereqs_block()
        self.assertIn(
            "workstation-policy.json",
            prereqs,
            "Prerequisites must link docs/iam/workstation-policy.json",
        )

    def test_prerequisites_links_execution_policy(self):
        prereqs = self._prereqs_block()
        self.assertIn(
            "execution-policy.json",
            prereqs,
            "Prerequisites must link docs/iam/execution-policy.json",
        )

    def test_prerequisites_nodejs_mentions_deploy(self):
        prereqs = self._prereqs_block()
        self.assertIn(
            "--deploy",
            prereqs,
            "Prerequisites Node.js row must mention --deploy (npm is needed for deploy)",
        )


class HarnessConfigSectionTests(unittest.TestCase):
    """README must document harness configuration defaults, overrides, and skills."""

    def test_harness_config_section_exists(self):
        self.assertIn("Harness configuration defaults", _readme())

    def test_section_documents_default_allowlists(self):
        readme = _readme()
        idx = readme.find("Harness configuration defaults")
        section = readme[idx: idx + 3000]
        # Must mention all three harnesses
        for harness in ("Claude Code", "Codex", "Kiro"):
            with self.subTest(harness=harness):
                self.assertIn(harness, section)

    def test_section_mentions_skills_md(self):
        readme = _readme()
        idx = readme.find("Harness configuration defaults")
        section = readme[idx: idx + 3000]
        self.assertIn("SKILL.md", section)

    def test_section_mentions_configuration_choice(self):
        readme = _readme()
        idx = readme.find("Harness configuration defaults")
        section = readme[idx: idx + 3000]
        self.assertIn("configuration_choice", section)

    def test_section_mentions_mcp_secret_detection(self):
        readme = _readme()
        idx = readme.find("Harness configuration defaults")
        section = readme[idx: idx + 3000]
        self.assertIn("secret", section.lower())
        self.assertIn("MCP", section)


class HarnessConfigTableAccuracyTests(unittest.TestCase):
    """README default-copy table must match DEFAULT_HOME_CONFIG/DIRS in code."""

    @classmethod
    def setUpClass(cls):
        from arena.host_setup import DEFAULT_HOME_CONFIG, DEFAULT_HOME_DIRS
        cls.DEFAULT_HOME_CONFIG = DEFAULT_HOME_CONFIG
        cls.DEFAULT_HOME_DIRS = DEFAULT_HOME_DIRS
        readme = _readme()
        idx = readme.find("Default copy allowlist")
        end = readme.find("Never-copy rules", idx)
        cls.section = readme[idx:end] if idx >= 0 else ""

    def test_claude_row_contains_output_styles(self):
        """output-styles/ must appear in the Claude Code row (from DEFAULT_HOME_DIRS)."""
        self.assertIn("output-styles", self.DEFAULT_HOME_DIRS["claude"],
                      "output-styles must be in DEFAULT_HOME_DIRS['claude']")
        self.assertIn("output-styles", self.section,
                      "README table must list output-styles/ in the Claude Code row")

    def test_claude_row_does_not_contain_prompts(self):
        """prompts/ must not appear in the Claude Code row — it is not in DEFAULT_HOME_DIRS."""
        self.assertNotIn("prompts", self.DEFAULT_HOME_DIRS["claude"],
                         "prompts must NOT be in DEFAULT_HOME_DIRS['claude']")
        # The table section should not have 'prompts/' in the Claude row.
        # We check that the Claude row (first data row) does not contain 'prompts/'.
        lines = self.section.splitlines()
        claude_line = next((l for l in lines if "Claude Code" in l), "")
        self.assertNotIn("prompts/", claude_line,
                         "README Claude Code row must not list prompts/ — not in DEFAULT_HOME_DIRS")

    def test_codex_row_contains_prompts_rules_skills(self):
        """Codex row must list prompts/, rules/, skills/ (not instructions/)."""
        for d in ("prompts", "rules", "skills"):
            self.assertIn(d, self.DEFAULT_HOME_DIRS["codex"],
                          f"{d} must be in DEFAULT_HOME_DIRS['codex']")
        lines = self.section.splitlines()
        codex_line = next((l for l in lines if "Codex" in l), "")
        for d in ("prompts/", "rules/", "skills/"):
            self.assertIn(d, codex_line,
                          f"README Codex row must list {d} (from DEFAULT_HOME_DIRS['codex'])")

    def test_codex_row_does_not_contain_instructions(self):
        """instructions/ must not appear in the Codex row — never in codebase."""
        self.assertNotIn("instructions", self.DEFAULT_HOME_DIRS.get("codex", ()),
                         "instructions must NOT be in DEFAULT_HOME_DIRS['codex']")
        lines = self.section.splitlines()
        codex_line = next((l for l in lines if "Codex" in l), "")
        self.assertNotIn("instructions/", codex_line,
                         "README Codex row must not list instructions/ — not in code")

    def test_kiro_row_contains_settings_mcp_json(self):
        """Kiro row must list settings/mcp.json (not .kiro/settings/)."""
        self.assertIn("settings/mcp.json", self.DEFAULT_HOME_CONFIG.get("kiro", ()),
                      "settings/mcp.json must be in DEFAULT_HOME_CONFIG['kiro']")
        lines = self.section.splitlines()
        kiro_line = next((l for l in lines if "| Kiro" in l), "")
        self.assertIn("settings/mcp.json", kiro_line,
                      "README Kiro row must list settings/mcp.json (from DEFAULT_HOME_CONFIG['kiro'])")

    def test_kiro_row_contains_skills_and_hooks(self):
        """Kiro row must list skills/ and hooks/ (from DEFAULT_HOME_DIRS['kiro'])."""
        for d in ("skills", "hooks"):
            self.assertIn(d, self.DEFAULT_HOME_DIRS["kiro"],
                          f"{d} must be in DEFAULT_HOME_DIRS['kiro']")
        lines = self.section.splitlines()
        kiro_line = next((l for l in lines if "| Kiro" in l), "")
        for d in ("skills/", "hooks/"):
            self.assertIn(d, kiro_line,
                          f"README Kiro row must list {d} (from DEFAULT_HOME_DIRS['kiro'])")


class HarnessConfigPrecedenceTests(unittest.TestCase):
    """README must document configuration precedence order."""

    def _config_section(self):
        readme = _readme()
        idx = readme.find("Harness configuration defaults")
        return readme[idx: idx + 4000] if idx >= 0 else ""

    def test_precedence_line_present(self):
        """README must state the precedence order in the config section."""
        section = self._config_section()
        self.assertIn("precedence", section.lower(),
                      "README config section must document configuration precedence")

    def test_precedence_order_mentions_s3_and_template(self):
        """Precedence line must mention S3 prefix and home template."""
        section = self._config_section()
        lower = section.lower()
        self.assertIn("s3", lower, "Precedence documentation must mention S3 prefix")
        self.assertIn("home template", lower.replace("_", " ").replace("-", " "),
                      "Precedence documentation must mention home template")

    def test_precedence_order_mentions_defaults(self):
        """Precedence line must anchor at 'defaults' as the lowest."""
        section = self._config_section()
        self.assertIn("defaults", section.lower(),
                      "Precedence documentation must mention defaults as the lowest source")


class SkillsSectionAccuracyTests(unittest.TestCase):
    """Skills/SKILL.md paragraph must accurately describe all three harnesses."""

    def _skills_section(self):
        readme = _readme()
        idx = readme.find("Skills / SKILL.md")
        return readme[idx: idx + 1000] if idx >= 0 else ""

    def test_skills_section_does_not_say_instructions_for_codex(self):
        """Skills paragraph must not say 'instructions/' for Codex."""
        section = self._skills_section()
        self.assertNotIn("instructions/", section,
                         "Skills/SKILL.md paragraph must not mention instructions/ for Codex")

    def test_skills_section_mentions_agents_skills_for_codex(self):
        """Skills paragraph must describe .agents/skills for Codex."""
        section = self._skills_section()
        self.assertIn(".agents/skills", section,
                      "Skills paragraph must describe .agents/skills for Codex")

    def test_skills_section_mentions_kiro_skills(self):
        """Skills paragraph must mention .kiro/skills for Kiro."""
        section = self._skills_section()
        self.assertIn(".kiro/skills", section,
                      "Skills paragraph must mention .kiro/skills for Kiro")


if __name__ == "__main__":
    unittest.main()
