# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for deployment name validation: runtime name prefix, IAM role/policy names.

Covers the four fix points from the preflight name-validation work package:

1. --set of a hyphenated AGENTCORE_RUNTIME_NAME_PREFIX exits 2 with a suggestion.
2. A deploy.properties file with a hyphenated prefix fails configuration-loads
   with gate "human" (not "aws-error").
3. codex-provider-config: when CODEX_HOME_TEMPLATE is set but .codex/config.toml
   is missing, the fix is an executable command that creates the template;
   executing it makes the check pass on re-run.
4. IAM name rules validation for AGENTCORE_EXECUTION_ROLE_NAME and
   AGENTCORE_INLINE_POLICY_NAME.
5. Per-user suffix length: USER_NAMESPACE combined with prefix must not exceed 48.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena import preflight
from arena import properties as props


CODEX_PROVIDER = ('model_provider = "amazon-bedrock-runtime"\n'
                  '[model_providers.amazon-bedrock-runtime.aws]\nregion = "us-east-1"\n')

# Minimum answers for a successful agentcore offline preflight
BASE_ANSWERS = [
    "RUNTIME=agentcore", "KIRO_AUTH=copy-host-login", "AWS_PROFILE=dev",
    "AWS_REGION=us-east-1", "S3_BUCKET=bucket",
    "REPOSITORY_ROOTS=.", "DATA_DIR=data",
]


# ---------------------------------------------------------------------------
# harness_image.iam validation helpers (unit tests)
# ---------------------------------------------------------------------------

class ValidateRuntimeNamePrefixTests(unittest.TestCase):
    """validate_runtime_name_prefix raises ValueError for invalid prefixes."""

    def setUp(self):
        from harness_image.iam import validate_runtime_name_prefix
        self.validate = validate_runtime_name_prefix

    def test_valid_lowercase_prefix(self):
        self.validate("mchc")          # must not raise

    def test_valid_mixed_case_prefix(self):
        self.validate("MchcRuntime")          # must not raise

    def test_valid_underscore_prefix(self):
        self.validate("testoct21_mchc")   # underscores allowed

    def test_valid_single_letter(self):
        self.validate("A")                    # single letter — valid, short

    def test_hyphen_raises(self):
        with self.assertRaises(ValueError) as cm:
            self.validate("testoct21-mchc")
        msg = str(cm.exception)
        self.assertIn("invalid characters", msg)
        # Suggestion must replace hyphens with underscores
        self.assertIn("testoct21_mchc", msg)

    def test_space_raises(self):
        with self.assertRaises(ValueError) as cm:
            self.validate("my prefix")
        self.assertIn("invalid characters", str(cm.exception))

    def test_starts_with_digit_raises(self):
        with self.assertRaises(ValueError) as cm:
            self.validate("1abc")
        self.assertIn("invalid characters", str(cm.exception))

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            self.validate("")

    def test_too_long_for_derived_name_raises(self):
        # prefix + "_claude" must fit 48 characters; 48 - 7 = 41
        long_prefix = "A" * 42
        with self.assertRaises(ValueError) as cm:
            self.validate(long_prefix)
        self.assertIn("too long", str(cm.exception))

    def test_maximum_safe_prefix_length(self):
        # 41 chars: "A" * 41 → "A"*41 + "_claude" = 48 chars exactly
        prefix = "A" * 41
        self.validate(prefix)  # must not raise


class ValidateIamRoleNameTests(unittest.TestCase):
    """validate_iam_role_name enforces IAM role-name character and length rules."""

    def setUp(self):
        from harness_image.iam import validate_iam_role_name
        self.validate = validate_iam_role_name

    def test_valid_role_name(self):
        self.validate("MchcBedrockAgentCoreRuntimeRole")

    def test_valid_with_underscores_and_hyphens(self):
        self.validate("testoct21_MchcBedrockAgentCoreRuntimeRole")

    def test_valid_with_plus_equals_at(self):
        self.validate("Role+=,.@-name")

    def test_empty_raises(self):
        with self.assertRaises(ValueError) as cm:
            self.validate("")
        self.assertIn("empty", str(cm.exception).lower())

    def test_too_long_raises(self):
        with self.assertRaises(ValueError) as cm:
            self.validate("A" * 65)
        self.assertIn("1-64", str(cm.exception))

    def test_64_chars_valid(self):
        self.validate("A" * 64)  # must not raise

    def test_invalid_char_raises(self):
        with self.assertRaises(ValueError) as cm:
            self.validate("Role/with/slash")
        self.assertIn("invalid", str(cm.exception).lower())

    def test_space_raises(self):
        with self.assertRaises(ValueError):
            self.validate("Role Name")


class ValidateIamPolicyNameTests(unittest.TestCase):
    """validate_iam_policy_name enforces IAM policy-name character and length rules."""

    def setUp(self):
        from harness_image.iam import validate_iam_policy_name
        self.validate = validate_iam_policy_name

    def test_valid_policy_name(self):
        self.validate("MchcAgentCoreRuntime")

    def test_valid_underscore(self):
        self.validate("testoct21_MchcAgentCoreRuntime")

    def test_valid_128_chars(self):
        self.validate("A" * 128)  # must not raise

    def test_empty_raises(self):
        with self.assertRaises(ValueError) as cm:
            self.validate("")
        self.assertIn("empty", str(cm.exception).lower())

    def test_too_long_raises(self):
        with self.assertRaises(ValueError) as cm:
            self.validate("A" * 129)
        self.assertIn("1-128", str(cm.exception))

    def test_invalid_char_raises(self):
        with self.assertRaises(ValueError):
            self.validate("Policy/slash")


# ---------------------------------------------------------------------------
# --set refuses hyphenated prefix with exit 2
# ---------------------------------------------------------------------------

class SetHyphenatedPrefixTests(unittest.TestCase):
    """--set AGENTCORE_RUNTIME_NAME_PREFIX=<hyphenated> must exit 2 with suggestion."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "deploy.properties"
        self.addCleanup(self.tmp.cleanup)

    def test_hyphenated_prefix_exits_2(self):
        code, report = preflight.inputs(self.path, ["AGENTCORE_RUNTIME_NAME_PREFIX=testoct21-mchc"])
        self.assertEqual(code, 2)
        self.assertIn("error", report)
        self.assertIn("invalid characters", report["error"])

    def test_hyphenated_prefix_error_includes_suggestion(self):
        code, report = preflight.inputs(self.path, ["AGENTCORE_RUNTIME_NAME_PREFIX=my-deploy"])
        self.assertEqual(code, 2)
        # The suggestion must use underscores
        self.assertIn("my_deploy", report["error"])

    def test_valid_underscore_prefix_accepted(self):
        code, report = preflight.inputs(self.path, [
            "AGENTCORE_RUNTIME_NAME_PREFIX=testoct21_mchc",
        ])
        # may be exit 20 (missing other keys) but NOT 2
        self.assertNotEqual(code, 2, report)

    def test_nothing_written_on_bad_prefix(self):
        # Create fresh properties from example
        preflight.inputs(self.path, [])
        original = self.path.read_text()
        preflight.inputs(self.path, ["AGENTCORE_RUNTIME_NAME_PREFIX=bad-prefix"])
        self.assertEqual(self.path.read_text(), original)

    def test_iam_role_name_bad_char_exits_2(self):
        preflight.inputs(self.path, [])
        code, report = preflight.inputs(self.path, ["AGENTCORE_EXECUTION_ROLE_NAME=Role/slash"])
        self.assertEqual(code, 2)
        self.assertIn("error", report)

    def test_iam_policy_name_too_long_exits_2(self):
        preflight.inputs(self.path, [])
        code, report = preflight.inputs(self.path, ["AGENTCORE_INLINE_POLICY_NAME=" + "A" * 129])
        self.assertEqual(code, 2)
        self.assertIn("error", report)


# ---------------------------------------------------------------------------
# Existing file with hyphenated prefix fails configuration-loads with gate human
# ---------------------------------------------------------------------------

class HyphenatedPrefixConfigLoadsTests(unittest.TestCase):
    """A deploy.properties with a hyphenated AGENTCORE_RUNTIME_NAME_PREFIX must fail
    configuration-loads with gate 'human', not 'aws-error'."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.properties = self.root / "deploy.properties"
        self.config = self.root / "config.toml"
        self.addCleanup(self.tmp.cleanup)
        plan = patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"})
        plan.start()
        self.addCleanup(plan.stop)

    def _write_props(self, prefix):
        # Write a complete properties file with the given prefix, bypassing --set
        # validation so we can test how configuration-loads handles an existing file
        # with an invalid prefix (simulating a file that was written before validation
        # was added, or a direct edit).
        preflight.inputs(self.properties, BASE_ANSWERS)
        # Use render() to set the prefix in-place without triggering typed-kind
        # validation in --set (we want to write an invalid value to the FILE directly).
        existing = self.properties.read_text()
        updated = props.render(existing, {"AGENTCORE_RUNTIME_NAME_PREFIX": prefix})
        self.properties.write_text(updated)

    def _run_preflight(self):
        pf = preflight.Preflight(self.config, self.properties, offline=True)
        with patch.object(preflight.Preflight, "host"), patch("boto3.Session"):
            report = pf.run()
        return report, {c["id"]: c for c in report["checks"]}

    def test_hyphenated_prefix_fails_configuration_loads_not_aws_error(self):
        self._write_props("testoct21-mchc")
        report, checks = self._run_preflight()
        self.assertFalse(report["ok"])
        # Must be configuration-loads, NOT aws-error
        self.assertIn("configuration-loads", checks)
        self.assertNotIn("aws-error", checks)
        c = checks["configuration-loads"]
        self.assertEqual(c["status"], "fail")

    def test_hyphenated_prefix_configuration_loads_gate_is_human(self):
        self._write_props("testoct21-mchc")
        report, checks = self._run_preflight()
        c = checks["configuration-loads"]
        self.assertEqual(c["gate"], "human", c)

    def test_hyphenated_prefix_fix_suggests_underscores(self):
        self._write_props("testoct21-mchc")
        report, checks = self._run_preflight()
        c = checks["configuration-loads"]
        # Fix must mention the underscore form
        self.assertIn("testoct21_mchc", c["fix"])

    def test_valid_underscore_prefix_passes_configuration_loads(self):
        self._write_props("testoct21_mchc")
        report, checks = self._run_preflight()
        self.assertEqual(checks["configuration-loads"]["status"], "pass")

    def test_agentcore_runtime_names_check_not_aws_error_when_prefix_invalid(self):
        """The agentcore() check must emit agentcore-runtime-names (gate human), not aws-error."""
        # Write valid properties first (so configuration-loads passes)
        answers = [*BASE_ANSWERS, "CODEX_HOME_TEMPLATE=codex-home"]
        (self.root / "codex-home" / ".codex").mkdir(parents=True)
        (self.root / "codex-home" / ".codex" / "config.toml").write_text(CODEX_PROVIDER)
        preflight.inputs(self.properties, answers)
        # Directly manipulate the file to inject a bad prefix AFTER configuration loads
        # We test the agentcore() runtime-name check directly
        from arena.config import AgentCoreConfig, Config
        ac = AgentCoreConfig(
            transfer_s3_uri="s3://test-bucket/test-prefix/",
            region="us-east-1",
            runtime_name_prefix="testoct21-mchc",
        )
        cfg = Config(runtime="agentcore", agentcore=ac)

        from unittest.mock import MagicMock, Mock
        session = Mock()
        ctrl = MagicMock()
        ctrl.list_agent_runtimes.return_value = {"agentRuntimes": []}
        session.client.return_value = ctrl

        pf = preflight.Preflight.__new__(preflight.Preflight)
        pf.checks = []
        pf.config = cfg
        pf.values = {}
        pf.account = None
        pf._config_report = None
        pf.config_path = None
        pf.properties_path = self.properties
        pf.offline = False
        pf.agentcore(session, None)

        checks = {c["id"]: c for c in pf.checks}
        self.assertIn("agentcore-runtime-names", checks)
        self.assertNotIn("aws-error", checks)
        c = checks["agentcore-runtime-names"]
        self.assertEqual(c["status"], "fail")
        self.assertEqual(c["gate"], "human")
        # Fix must suggest the underscore form
        self.assertIn("testoct21_mchc", c["fix"])


# ---------------------------------------------------------------------------
# codex-provider-config: template set but file missing → executable fix
# ---------------------------------------------------------------------------

class CodexProviderTemplateMissingTests(unittest.TestCase):
    """When CODEX_HOME_TEMPLATE is set but .codex/config.toml does not exist, the fix
    is an executable command.  Running it must make the check pass on re-run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.properties = self.root / "deploy.properties"
        self.config = self.root / "config.toml"
        # A template directory that exists but has no .codex/config.toml
        self.template_dir = self.root / "my-codex-template"
        self.template_dir.mkdir()
        self.addCleanup(self.tmp.cleanup)
        plan = patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"})
        plan.start()
        self.addCleanup(plan.stop)

    def _run_preflight(self, answers):
        preflight.inputs(self.properties, list(answers))
        pf = preflight.Preflight(self.config, self.properties, offline=True)
        with patch.object(preflight.Preflight, "host"), patch("boto3.Session"):
            report = pf.run()
        return report, {c["id"]: c for c in report["checks"]}

    def test_template_set_but_file_missing_fails(self):
        answers = [*BASE_ANSWERS, f"CODEX_HOME_TEMPLATE={self.template_dir}"]
        report, checks = self._run_preflight(answers)
        c = checks.get("codex-provider-config", {})
        self.assertEqual(c.get("status"), "fail", checks)
        self.assertEqual(c.get("gate"), "assistant", c)

    def test_template_missing_detail_mentions_template_path(self):
        answers = [*BASE_ANSWERS, f"CODEX_HOME_TEMPLATE={self.template_dir}"]
        _, checks = self._run_preflight(answers)
        c = checks["codex-provider-config"]
        self.assertIn("does not exist", c["detail"])
        self.assertIn(str(self.template_dir), c["detail"])

    def test_template_missing_fix_contains_create_command(self):
        answers = [*BASE_ANSWERS, f"CODEX_HOME_TEMPLATE={self.template_dir}"]
        _, checks = self._run_preflight(answers)
        c = checks["codex-provider-config"]
        self.assertIn("Create a dedicated template", c["fix"])

    def test_template_missing_fix_is_executable_and_creates_valid_config(self):
        """Execute the fix command in a temp dir; it must create a parseable config."""
        answers = [*BASE_ANSWERS, f"CODEX_HOME_TEMPLATE={self.template_dir}"]
        _, checks = self._run_preflight(answers)
        c = checks["codex-provider-config"]

        # Extract the shell script (same pattern as test_without_a_template test)
        script = c["fix"].split("Create a dedicated template: ", 1)[1].split(" && ./bootstrap.sh")[0]
        result = subprocess.run(script, shell=True, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

        import tomllib
        dedicated = self.root.resolve() / ".arena" / "credential-templates" / "codex"
        config_file = dedicated / ".codex" / "config.toml"
        self.assertTrue(config_file.exists(), "fix must create .codex/config.toml")
        parsed = tomllib.loads(config_file.read_text())
        self.assertEqual(parsed["model_provider"], "amazon-bedrock-runtime")
        region = parsed["model_providers"]["amazon-bedrock-runtime"]["aws"]["region"]
        self.assertEqual(region, "us-east-1")
        self.assertNotIn("profile", parsed["model_providers"]["amazon-bedrock-runtime"]["aws"])

    def test_after_fix_rerun_passes(self):
        """After applying the fix (setting CODEX_HOME_TEMPLATE to the created dir),
        a re-run of preflight must make codex-provider-config pass."""
        answers = [*BASE_ANSWERS, f"CODEX_HOME_TEMPLATE={self.template_dir}"]
        _, checks = self._run_preflight(answers)
        fix = checks["codex-provider-config"]["fix"]

        script = fix.split("Create a dedicated template: ", 1)[1].split(" && ./bootstrap.sh")[0]
        subprocess.run(script, shell=True, check=True)

        # Extract the CODEX_HOME_TEMPLATE path from the fix
        new_template = fix.rsplit(" ", 1)[1].split("CODEX_HOME_TEMPLATE=", 1)[1]
        preflight.inputs(self.properties, [f"CODEX_HOME_TEMPLATE={new_template}"])
        _, checks2 = self._run_preflight([])
        self.assertEqual(checks2["codex-provider-config"]["status"], "pass",
                         checks2["codex-provider-config"])

    def test_template_exists_but_wrong_content_keeps_prose_fix(self):
        """When the template file exists but has wrong content (profile key),
        the fix must be prose — we never overwrite the user's file."""
        (self.template_dir / ".codex").mkdir()
        (self.template_dir / ".codex" / "config.toml").write_text(
            CODEX_PROVIDER + 'profile = "dev"\n'
        )
        answers = [*BASE_ANSWERS, f"CODEX_HOME_TEMPLATE={self.template_dir}"]
        _, checks = self._run_preflight(answers)
        c = checks["codex-provider-config"]
        self.assertEqual(c["status"], "fail")
        # Prose fix: must mention "dedicated template" and NOT be an executable command
        self.assertIn("dedicated template", c["fix"])
        # The prose fix must NOT include the mkdir/printf executable parts
        self.assertNotIn("mkdir -p", c["fix"])


# ---------------------------------------------------------------------------
# Per-user suffix length: USER_NAMESPACE + prefix must not exceed 48 chars
# ---------------------------------------------------------------------------

class PerUserSuffixLengthTests(unittest.TestCase):
    """runtime_name raises ValueError when prefix + harness + namespace exceeds 48 chars."""

    def test_long_namespace_raises(self):
        from harness_image.iam import runtime_name
        # prefix="a", harness="b", namespace 46 chars → "a_b_" + 46 = 50 > 48
        with self.assertRaises(ValueError) as cm:
            runtime_name("a", "b", "x" * 46)
        self.assertIn("48", str(cm.exception))

    def test_prefix_plus_namespace_boundary(self):
        from harness_image.iam import runtime_name
        # "myprefix_claude_ns" = 8 + 1 + 6 + 1 + len(ns) = 16 + len(ns)
        # max len(ns) = 48 - 16 = 32
        self.assertEqual(len(runtime_name("myprefix", "claude", "x" * 32)), 48)

    def test_one_over_boundary_raises(self):
        from harness_image.iam import runtime_name
        with self.assertRaises(ValueError):
            runtime_name("myprefix", "claude", "x" * 33)

    def test_preflight_agentcore_check_emits_runtime_names_not_error(self):
        """When runtime_name raises during the agentcore check, preflight emits
        agentcore-runtime-names (gate human) rather than letting guard() catch it
        as aws-error."""
        from unittest.mock import Mock, MagicMock
        from arena.config import AgentCoreConfig, Config
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            props_path = Path(tmp) / "deploy.properties"
            ac = AgentCoreConfig(
                transfer_s3_uri="s3://b/p/",
                region="us-east-1",
                runtime_name_prefix="prefix",
                isolation="per-user",
            )
            cfg = Config(runtime="agentcore", agentcore=ac,
                         user_namespace="x" * 40)

            session = Mock()
            ctrl = MagicMock()
            ctrl.list_agent_runtimes.return_value = {"agentRuntimes": []}
            session.client.return_value = ctrl

            pf = preflight.Preflight.__new__(preflight.Preflight)
            pf.checks = []
            pf.config = cfg
            pf.values = {}
            pf.account = None
            pf._config_report = None
            pf.config_path = None
            pf.properties_path = props_path
            pf.offline = False
            pf.agentcore(session, None)

        checks = {c["id"]: c for c in pf.checks}
        self.assertIn("agentcore-runtime-names", checks)
        self.assertNotIn("aws-error", checks)
        self.assertEqual(checks["agentcore-runtime-names"]["gate"], "human")


if __name__ == "__main__":
    unittest.main()
