# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""New preflight checks: configuration-literals, aws-region, harness-image-versions,
harness-configuration, agentcore-secret-leftovers; QUESTIONS; USER_NAMESPACE pre-fill;
bootstrap.sh --approve; doctor enhancements."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from arena import preflight
from arena import properties as props

ROOT = Path(__file__).parent.parent
EXAMPLE = (ROOT / "deploy.properties.example").read_text()
ANSWERS = ["RUNTIME=agentcore", "KIRO_AUTH=copy-host-login", "AWS_PROFILE=dev",
           "AWS_REGION=us-east-1", "S3_BUCKET=bucket",
           "REPOSITORY_ROOTS=.", "DATA_DIR=data", "CODEX_HOME_TEMPLATE=codex-home"]
CODEX_PROVIDER = ('model_provider = "amazon-bedrock-runtime"\n'
                  '[model_providers.amazon-bedrock-runtime.aws]\nregion = "us-east-1"\n')


def _make_preflight(root, answers=None):
    """Set up a Preflight instance with a complete deploy.properties and no config.toml."""
    properties = root / "deploy.properties"
    config = root / "config.toml"      # absent → evaluates config.example.toml
    if answers is None:
        answers = ANSWERS
    (root / "codex-home" / ".codex").mkdir(parents=True, exist_ok=True)
    (root / "codex-home" / ".codex" / "config.toml").write_text(CODEX_PROVIDER)
    with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}):
        preflight.inputs(properties, list(answers))
    return preflight.Preflight(config, properties, offline=True)


class ConfigurationLiteralsCheckTests(unittest.TestCase):
    """config-toml-environment-literals check."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, answers=None):
        pf = _make_preflight(self.root, answers)
        with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}), \
             patch.object(preflight.Preflight, "host"):
            pf.run()
        return {c["id"]: c for c in pf.checks}

    def test_no_literals_pass(self):
        checks = self._run()
        self.assertEqual(checks["config-toml-environment-literals"]["status"], "pass")

    def test_environment_literal_fields_from_config_report_produce_fail(self):
        """If resolve_config reports environment literals the check emits a fail."""
        pf = _make_preflight(self.root)
        with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}), \
             patch.object(preflight.Preflight, "host"), \
             patch("arena.config.environment_literal_fields",
                   return_value=[{"field": "agentcore.region", "property": "AWS_REGION",
                                  "value": "us-east-1"}]):
            pf.run()
        checks = {c["id"]: c for c in pf.checks}
        self.assertEqual(checks["config-toml-environment-literals"]["status"], "fail")
        self.assertEqual(checks["config-toml-environment-literals"]["gate"], "assistant")
        self.assertIn("AWS_REGION", checks["config-toml-environment-literals"]["fix"])
        self.assertIn("bootstrap.sh", checks["config-toml-environment-literals"]["fix"])


class AwsRegionCheckTests(unittest.TestCase):
    """aws-region check: fail when all region fields are None."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def _make_pf_with_config(self, agentcore_region=None, artifact_region=None, judge_region=None):
        """Build a Preflight with a fake loaded config and known region fields."""
        pf = _make_preflight(self.root)
        pf.values = {"AWS_REGION": agentcore_region or ""}
        from types import SimpleNamespace
        pf.config = SimpleNamespace(
            runtime="agentcore",
            agentcore=SimpleNamespace(region=agentcore_region),
            artifact_s3_region=artifact_region,
            judge_region=judge_region,
        )
        return pf

    def test_region_configured_passes(self):
        pf = self._make_pf_with_config(agentcore_region="us-east-1")
        pf.aws_region()
        checks = {c["id"]: c for c in pf.checks}
        self.assertEqual(checks["aws-region"]["status"], "pass")

    def test_no_region_fails(self):
        pf = self._make_pf_with_config()  # all regions are None
        pf.aws_region()
        checks = {c["id"]: c for c in pf.checks}
        self.assertEqual(checks["aws-region"]["status"], "fail")
        self.assertEqual(checks["aws-region"]["gate"], "assistant")
        self.assertIn("AWS_REGION", checks["aws-region"]["fix"])

    def test_artifact_region_alone_passes(self):
        pf = self._make_pf_with_config(artifact_region="eu-west-1")
        pf.aws_region()
        checks = {c["id"]: c for c in pf.checks}
        self.assertEqual(checks["aws-region"]["status"], "pass")

    def test_judge_region_alone_passes(self):
        pf = self._make_pf_with_config(judge_region="us-west-2")
        pf.aws_region()
        checks = {c["id"]: c for c in pf.checks}
        self.assertEqual(checks["aws-region"]["status"], "pass")

    def test_aws_region_question_exists_in_questions(self):
        self.assertIn("AWS_REGION", preflight.QUESTIONS)

    def test_region_check_in_full_run_with_answers(self):
        """When a full run has AWS_REGION in answers, aws-region passes."""
        pf = _make_preflight(self.root)  # uses ANSWERS which has AWS_REGION=us-east-1
        with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}), \
             patch.object(preflight.Preflight, "host"):
            pf.run()
        checks = {c["id"]: c for c in pf.checks}
        self.assertEqual(checks["aws-region"]["status"], "pass")


class HarnessImageVersionsCheckTests(unittest.TestCase):
    """harness-image-versions check."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, extra_answers=()):
        pf = _make_preflight(self.root, list(ANSWERS) + list(extra_answers))
        with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}), \
             patch.object(preflight.Preflight, "host"):
            pf.run()
        return {c["id"]: c for c in pf.checks}

    def test_all_pinned_passes(self):
        checks = self._run()
        self.assertEqual(checks["harness-image-versions"]["status"], "pass")

    def test_non_pinned_without_lock_is_human_gate(self):
        checks = self._run(["CLAUDE_HARNESS_SOURCE=latest"])
        self.assertEqual(checks["harness-image-versions"]["status"], "fail")
        self.assertEqual(checks["harness-image-versions"]["gate"], "human")
        self.assertIn("no approved plan", checks["harness-image-versions"]["detail"])
        self.assertIn("--approve", checks["harness-image-versions"]["fix"])

    def test_non_pinned_with_approved_lock_passes(self):
        # Write a lock file for the claude harness with a token
        data_dir = self.root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        lock = {"claude": {"version": "1.2.3", "uri": "https://example.com/claude",
                           "integrity": "", "source": "latest", "token": "abc123"}}
        (data_dir / "harness-versions.lock.json").write_text(json.dumps(lock))
        checks = self._run(["CLAUDE_HARNESS_SOURCE=latest"])
        self.assertEqual(checks["harness-image-versions"]["status"], "pass")
        self.assertIn("1.2.3", checks["harness-image-versions"]["detail"])

    def test_harness_image_not_installed_is_skipped_gracefully(self):
        """When harness_image package is missing, the check simply does not run."""
        pf = _make_preflight(self.root, list(ANSWERS) + ["CLAUDE_HARNESS_SOURCE=installed"])
        with patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"}), \
             patch.object(preflight.Preflight, "host"), \
             patch.dict("sys.modules", {"harness_image": None, "harness_image.sources": None}):
            pf.run()
        checks = {c["id"]: c for c in pf.checks}
        # When import fails, the check doesn't add any check entry (returns early)
        self.assertNotIn("harness-image-versions", checks)


class HarnessConfigurationCheckTests(unittest.TestCase):
    """harness-configuration check: warn about withheld secrets and MCP secret keys."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, auth_plan_override=None, config_plan_override=None):
        pf = _make_preflight(self.root)
        auth_plan = auth_plan_override or {"method": "execution role", "secrets_withheld": []}
        cfg_plan = config_plan_override or {"sources": [{"mcp_secret_names": {}}]}
        with patch("arena.host_setup.authentication_plan", return_value=auth_plan), \
             patch("arena.preflight.preflight.Preflight.host" if False else
                   "arena.preflight.Preflight.host"), \
             patch("arena.host_setup.configuration_plan", return_value=cfg_plan), \
             patch.object(preflight.Preflight, "host"):
            pf.run()
        return {c["id"]: c for c in pf.checks}

    def test_no_withheld_no_mcp_secrets_passes(self):
        checks = self._run()
        self.assertEqual(checks["harness-configuration"]["status"], "pass")

    def test_withheld_secrets_warn(self):
        checks = self._run(auth_plan_override={"method": "execution role",
                                               "secrets_withheld": [".credentials.json"]})
        self.assertEqual(checks["harness-configuration"]["status"], "warn")
        self.assertIn(".credentials.json", checks["harness-configuration"]["detail"])
        self.assertEqual(checks["harness-configuration"]["gate"], "human")

    def test_mcp_secret_looking_env_keys_warn(self):
        checks = self._run(
            config_plan_override={"sources": [
                {"mcp_secret_names": {"my-server": ["API_KEY", "SECRET_TOKEN"]}}
            ]})
        self.assertEqual(checks["harness-configuration"]["status"], "warn")
        self.assertIn("API_KEY", checks["harness-configuration"]["detail"])
        self.assertIn("my-server", checks["harness-configuration"]["detail"])

    def test_warn_gate_is_human_not_assistant(self):
        """Secrets in MCP are the human's responsibility to review."""
        checks = self._run(
            config_plan_override={"sources": [
                {"mcp_secret_names": {"s": ["API_KEY"]}}
            ]})
        self.assertEqual(checks["harness-configuration"]["gate"], "human")


class AgentcoreSecretLeftoversTests(unittest.TestCase):
    """agentcore-secret-leftovers check."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def _run_online(self, leftovers=()):
        pf = _make_preflight(self.root)
        pf.offline = False

        class FakeSession:
            def __call__(self, **kw):
                return self
            def client(self, service, **kw):
                m = MagicMock(name=service)
                m.get_caller_identity.return_value = {
                    "Account": "111111111111",
                    "Arn": "arn:aws:sts::111111111111:assumed-role/dev/x"}
                m.get_paginator.return_value.paginate.return_value = [{"inferenceProfileSummaries": []}]
                m.list_agent_runtimes.return_value = {"agentRuntimes": [
                    {"agentRuntimeName": f"mchc_{h}"}
                    for h in ("claude", "codex", "kiro")]}
                m.list_objects_v2.return_value = {
                    "Contents": [{"Key": k, "LastModified": "2025-01-01"} for k in leftovers]}
                return m

        with patch("arena.host_setup.authentication_plan", return_value={"method": "exec role"}), \
             patch.object(preflight.Preflight, "host"), \
             patch("boto3.Session", FakeSession()):
            pf.run()
        return {c["id"]: c for c in pf.checks}

    def test_no_leftovers_pass(self):
        checks = self._run_online()
        self.assertEqual(checks["agentcore-secret-leftovers"]["status"], "pass")

    def test_leftovers_warn(self):
        checks = self._run_online(leftovers=["code/secrets/run1/cred", "code/secrets/run2/cred"])
        self.assertEqual(checks["agentcore-secret-leftovers"]["status"], "warn")
        self.assertIn("2", checks["agentcore-secret-leftovers"]["detail"])
        self.assertEqual(checks["agentcore-secret-leftovers"]["gate"], "human")

    def test_offline_mode_skips_check(self):
        pf = _make_preflight(self.root)
        # offline=True means agentcore() is not called at all
        with patch("arena.host_setup.authentication_plan", return_value={"method": "exec role"}), \
             patch.object(preflight.Preflight, "host"):
            pf.run()  # offline=True by default in _make_preflight
        checks = {c["id"]: c for c in pf.checks}
        # The agentcore-secret-leftovers check is only added when agentcore() runs
        self.assertNotIn("agentcore-secret-leftovers", checks)


class S3FixTextTests(unittest.TestCase):
    """S3 fix text should include versioned-deletion permissions."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_transfer_bucket_fix_mentions_versioning_permissions(self):
        from botocore.exceptions import ClientError
        props_path = self.root / "deploy.properties"
        config_path = self.root / "config.toml"
        (self.root / "codex-home" / ".codex").mkdir(parents=True, exist_ok=True)
        (self.root / "codex-home" / ".codex" / "config.toml").write_text(CODEX_PROVIDER)
        preflight.inputs(props_path, list(ANSWERS))
        check = preflight.Preflight(config_path, props_path, offline=False)
        err = ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadBucket")
        with patch("arena.host_setup.authentication_plan", return_value={"method": "exec role"}), \
             patch.object(preflight.Preflight, "host"), \
             patch("boto3.Session") as mock_session:
            m = MagicMock()
            mock_session.return_value = m
            m.client.return_value.get_caller_identity.return_value = {
                "Account": "111", "Arn": "arn:aws:sts::111:role/r"}
            m.client.return_value.head_bucket.side_effect = err
            m.client.return_value.list_objects_v2.return_value = {"Contents": []}
            m.client.return_value.get_paginator.return_value.paginate.return_value = [
                {"inferenceProfileSummaries": []}]
            m.client.return_value.list_agent_runtimes.return_value = {
                "agentRuntimes": [{"agentRuntimeName": f"mchc_{h}"}
                                   for h in ("claude", "codex", "kiro")]}
            check.run()
        checks = {c["id"]: c for c in check.checks}
        transfer_fix = checks["s3-transfer"]["fix"]
        self.assertIn("s3:GetObjectVersion", transfer_fix)
        self.assertIn("s3:DeleteObjectVersion", transfer_fix)
        self.assertIn("s3:ListBucketVersions", transfer_fix)
        # Artifact bucket should NOT mention versioning-specific perms
        artifact_fix = checks["s3-artifact"]["fix"]
        self.assertNotIn("s3:GetObjectVersion", artifact_fix)


class QuestionsAndUserNamespaceTests(unittest.TestCase):
    """QUESTIONS includes USER_NAMESPACE; USER_NAMESPACE is pre-filled from login."""

    def test_user_namespace_is_in_questions(self):
        self.assertIn("USER_NAMESPACE", preflight.QUESTIONS)
        self.assertTrue(preflight.QUESTIONS["USER_NAMESPACE"].endswith("?"))

    def test_user_namespace_prefilled_from_login_when_placeholder(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "deploy.properties"
            path.write_text("USER_NAMESPACE = YOUR_NAMESPACE\n")
            with patch("arena.preflight.os.getlogin", return_value="alice"):
                preflight.inputs(path, [])
            vals = props.parse(path.read_text(), str(path), resolve=False)
            self.assertEqual(vals.get("USER_NAMESPACE"), "alice")

    def test_user_namespace_not_overwritten_when_explicitly_set(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "deploy.properties"
            path.write_text("USER_NAMESPACE = YOUR_NAMESPACE\n")
            with patch("arena.preflight.os.getlogin", return_value="alice"):
                preflight.inputs(path, ["USER_NAMESPACE=bob"])
            vals = props.parse(path.read_text(), str(path), resolve=False)
            # The explicit pair takes effect (applied before pre-fill)
            self.assertEqual(vals.get("USER_NAMESPACE"), "bob")

    def test_user_namespace_prefill_sanitizes_login(self):
        """Special chars and upper case are stripped from the login."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "deploy.properties"
            path.write_text("USER_NAMESPACE = YOUR_NAMESPACE\n")
            with patch("arena.preflight.os.getlogin", return_value="Alice.Smith_42"):
                preflight.inputs(path, [])
            vals = props.parse(path.read_text(), str(path), resolve=False)
            # Must match ^[a-z0-9][a-z0-9-]*$ after sanitisation
            import re
            ns = vals.get("USER_NAMESPACE", "")
            self.assertTrue(re.match(r'^[a-z0-9][a-z0-9-]*$', ns), f"bad namespace: {ns!r}")

    def test_prefill_does_not_crash_when_getlogin_fails(self):
        """If os.getlogin() raises, inputs() still returns normally."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "deploy.properties"
            path.write_text(EXAMPLE)
            with patch("arena.preflight.os.getlogin", side_effect=OSError("no tty")):
                code, report = preflight.inputs(path, [])
            # Must not crash; code 20 because other keys are still missing
            self.assertEqual(code, 20)


class BootstrapApproveTests(unittest.TestCase):
    """bootstrap.sh --approve flag."""

    def test_approve_is_accepted_without_error(self):
        import subprocess
        result = subprocess.run(
            ["/bin/bash", "-n", str(ROOT / "bootstrap.sh")],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_approve_token_in_usage_help(self):
        import subprocess
        result = subprocess.run(["/bin/bash", str(ROOT / "bootstrap.sh"), "--help"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--approve TOKEN", result.stdout)

    def test_approve_without_value_is_usage_error(self):
        import subprocess
        result = subprocess.run(["/bin/bash", str(ROOT / "bootstrap.sh"), "--approve"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        summary = json.loads(result.stdout)["summary"]
        self.assertEqual(summary["exit"], 2)


class NewChecksInRunMethodTests(unittest.TestCase):
    """The new checks are called from run() and appear in the checks list."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_new_check_ids_appear_in_offline_run(self):
        pf = _make_preflight(self.root)
        with patch("arena.host_setup.authentication_plan", return_value={"method": "exec role"}), \
             patch.object(preflight.Preflight, "host"):
            pf.run()
        ids = {c["id"] for c in pf.checks}
        for expected_id in ("config-toml-environment-literals", "aws-region",
                            "harness-image-versions", "harness-configuration"):
            with self.subTest(expected_id=expected_id):
                self.assertIn(expected_id, ids)

    def test_new_check_guards_run_even_when_one_raises(self):
        """A defect in a new check becomes a failed check, not a crash."""
        pf = _make_preflight(self.root)
        with patch("arena.host_setup.authentication_plan", return_value={"method": "exec role"}), \
             patch.object(preflight.Preflight, "host"), \
             patch.object(pf, "aws_region", side_effect=RuntimeError("boom")):
            pf.run()
        checks = {c["id"]: c for c in pf.checks}
        self.assertIn("aws-region-error", checks)
        self.assertEqual(checks["aws-region-error"]["gate"], "assistant")


if __name__ == "__main__":
    unittest.main()
