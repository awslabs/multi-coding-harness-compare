# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""inputs and preflight: what bootstrap.sh asks for, and checks, before setup writes anything."""
import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from arena import preflight
from arena import properties as props

ROOT = Path(__file__).parent.parent
EXAMPLE = (ROOT / "deploy.properties.example").read_text()
ASKED = ["AWS_PROFILE", "AWS_REGION", "KIRO_AUTH", "REPOSITORY_ROOTS", "RUNTIME", "S3_BUCKET"]
ANSWERS = ["RUNTIME=agentcore", "KIRO_AUTH=copy-host-login", "AWS_PROFILE=dev", "AWS_REGION=us-east-1",
           "S3_BUCKET=bucket",
           "REPOSITORY_ROOTS=.", "DATA_DIR=data", "CODEX_HOME_TEMPLATE=codex-home"]
CODEX_PROVIDER = ('model_provider = "amazon-bedrock-runtime"\n'
                  '[model_providers.amazon-bedrock-runtime.aws]\nregion = "us-east-1"\n')


class InputsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "deploy.properties"

    def tearDown(self):
        self.dir.cleanup()

    def test_a_fresh_checkout_gets_the_example_and_one_question_per_missing_value(self):
        code, report = preflight.inputs(self.path, [])
        self.assertEqual(code, 20)
        self.assertTrue(report["created"])
        self.assertEqual(self.path.read_text(), EXAMPLE)
        # ARTIFACT_S3_URI and AGENTCORE_TRANSFER_S3_URI only reference S3_BUCKET: not asked.
        self.assertEqual(report["missing"], ASKED)
        self.assertEqual(sorted(report["questions"]), ASKED)
        self.assertTrue(all(question.endswith("?") for question in report["questions"].values()))

    def test_answers_complete_the_file_and_references_follow_them(self):
        code, report = preflight.inputs(self.path, ANSWERS)
        self.assertEqual((code, report["missing"], report["created"]), (0, [], True))
        values = props.load(self.path).values
        self.assertEqual(values["ARTIFACT_S3_URI"], "s3://bucket/output/mchc/")
        self.assertEqual(values["CODEX_HOME_TEMPLATE"], "codex-home")
        self.assertEqual(preflight.inputs(self.path, [])[0], 0)   # re-running asks nothing

    def test_typos_multiline_values_and_malformed_pairs_are_refused_and_nothing_is_written(self):
        preflight.inputs(self.path, [])
        for pair, message in (("AWS_PROFIL=dev", "unknown key"), ("AWS_PROFILE=a\nb", "single line"),
                              ("aws_profile=dev", "UPPER_SNAKE_CASE"), ("AWS_PROFILE", "KEY=VALUE")):
            with self.subTest(pair=pair):
                code, report = preflight.inputs(self.path, ["S3_BUCKET=bucket", pair])
                self.assertEqual(code, 2)
                self.assertIn(message, report["error"])
                self.assertEqual(self.path.read_text(), EXAMPLE)

    def test_keys_the_file_already_defines_may_be_set(self):
        self.path.write_text(EXAMPLE + "TEAM_BUCKET = old\n")
        code, report = preflight.inputs(self.path, ["TEAM_BUCKET=new"])
        self.assertEqual(code, 20)
        self.assertEqual(props.load(self.path).values["TEAM_BUCKET"], "new")

    def test_setting_a_key_with_a_commented_and_a_live_line_leaves_one_definition(self):
        self.path.write_text("# CLAUDE_MODEL = example\nCLAUDE_MODEL = old\n")
        self.assertEqual(preflight.inputs(self.path, ["CLAUDE_MODEL=new"])[0], 0)
        self.assertEqual(self.path.read_text(), "# CLAUDE_MODEL = example\nCLAUDE_MODEL = new\n")

    def test_a_malformed_file_is_reported_not_rewritten(self):
        self.path.write_text("A = 1\nA = 2\n")
        code, report = preflight.inputs(self.path, ["AWS_PROFILE=dev"])
        self.assertEqual(code, 2)
        self.assertIn("defined twice", report["error"])
        self.assertEqual(self.path.read_text(), "A = 1\nA = 2\n")


class ShellRestrictionTests(unittest.TestCase):
    def setUp(self):
        preflight.shell_restrictions.cache_clear()
        self.addCleanup(preflight.shell_restrictions.cache_clear)

    def test_a_refused_nested_sandbox_and_ps_are_both_reported(self):
        def run(argv, **kwargs):
            if argv[0] == "/bin/ps":
                raise PermissionError(1, "Operation not permitted", "/bin/ps")
            return SimpleNamespace(returncode=71, stdout="", stderr="sandbox_apply: Operation not permitted")

        with patch("arena.preflight.sys.platform", "darwin"), patch("arena.preflight.subprocess.run", side_effect=run):
            problems = preflight.shell_restrictions()
        self.assertEqual(len(problems), 2)
        self.assertIn("sandbox-exec exit 71: sandbox_apply", problems[0])
        self.assertIn("ps: [Errno 1] Operation not permitted", problems[1])

    def test_an_unrestricted_shell_reports_nothing(self):
        with patch("arena.preflight.sys.platform", "darwin"), \
                patch("arena.preflight.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="ok", stderr="")):
            self.assertEqual(preflight.shell_restrictions(), ())


class FakeSession:
    """boto3.Session stand-in that records every call made through it."""
    def __init__(self, account="111111111111", runtimes=(), profiles=(), s3_error=None):
        self.clients = {}
        self.account, self.runtimes, self.profiles, self.s3_error = account, runtimes, profiles, s3_error

    def __call__(self, **kwargs):
        self.session_kwargs = kwargs
        return self

    def client(self, service, region_name=None, config=None):
        client = self.clients.setdefault(service, MagicMock(name=service))
        client.get_caller_identity.return_value = {"Account": self.account,
                                                   "Arn": f"arn:aws:sts::{self.account}:assumed-role/dev/x"}
        if self.s3_error:
            client.head_bucket.side_effect = self.s3_error
        client.get_paginator.return_value.paginate.return_value = [
            {"inferenceProfileSummaries": [{"inferenceProfileId": p, "status": "ACTIVE"} for p in self.profiles]}]
        client.list_agent_runtimes.return_value = {"agentRuntimes": [{"agentRuntimeName": n} for n in self.runtimes]}
        return client

    def calls(self):
        return {(service, call[0].split("(")[0].split(".")[0])
                for service, client in self.clients.items() for call in client.mock_calls}


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.properties = self.root / "deploy.properties"
        self.config = self.root / "config.toml"     # absent: preflight evaluates config.example.toml
        (self.root / "codex-home" / ".codex").mkdir(parents=True)
        (self.root / "codex-home" / ".codex" / "config.toml").write_text(CODEX_PROVIDER)
        plan = patch("arena.host_setup.authentication_plan", return_value={"method": "execution role"})
        plan.start()
        self.addCleanup(plan.stop)

    def tearDown(self):
        self.dir.cleanup()

    def run_preflight(self, answers=ANSWERS, offline=True, session=None):
        preflight.inputs(self.properties, list(answers))
        check = preflight.Preflight(self.config, self.properties, offline=offline)
        with patch.object(preflight.Preflight, "host"), patch("boto3.Session", session or FakeSession()):
            report = check.run()
        return report, {c["id"]: c for c in report["checks"]}

    def test_placeholders_stop_preflight_before_the_configuration_is_evaluated(self):
        report, checks = self.run_preflight(answers=[])
        self.assertFalse(report["ok"])
        self.assertEqual((checks["properties-filled"]["status"], checks["properties-filled"]["gate"]), ("fail", "human"))
        self.assertNotIn("configuration-loads", checks)
        self.assertEqual(report["gates"], ["human"])

    def test_a_complete_agentcore_configuration_passes_offline(self):
        report, checks = self.run_preflight()
        self.assertTrue(report["ok"], [c for c in report["checks"] if c["status"] == "fail"])
        self.assertEqual(report["runtime"], "agentcore")
        for id in ("configuration-loads", "choice-runtime", "choice-kiro-auth", "artifact-s3-uri",
                   "agentcore-transfer-s3-uri", "repository-roots", "codex-provider-config", "agentcore-drift"):
            self.assertEqual(checks[id]["status"], "pass", id)
        self.assertEqual(checks["aws"]["status"], "skip")
        self.assertFalse(report["needs_deploy"])

    def test_an_invalid_choice_is_a_human_decision(self):
        report, checks = self.run_preflight(answers=[*ANSWERS, "RUNTIME=AgentCore"])
        self.assertFalse(report["ok"])
        self.assertEqual(checks["configuration-loads"]["status"], "fail")

    def test_without_a_template_the_fix_creates_one_and_never_edits_the_hosts_codex_config(self):
        # Behaviour intentionally changed: with RUNTIME=agentcore the effective
        # configuration choice is now "overrides" (workstation folder NOT read).
        # The fix must propose creating a dedicated template rather than pointing at
        # the host's own ~/.codex — which could never be used in a microVM anyway.
        host = self.root / "host-codex"
        host.mkdir()
        (host / "config.toml").write_text(CODEX_PROVIDER + 'profile = "dev"\n')
        answers = [a for a in ANSWERS if not a.startswith("CODEX_HOME_TEMPLATE")]
        with patch.dict("os.environ", {"CODEX_HOME": str(host)}):
            report, checks = self.run_preflight(answers=answers)
        check = checks["codex-provider-config"]
        self.assertEqual((check["status"], check["gate"]), ("fail", "assistant"))
        # The fix must NOT say "do not edit the host config" — instead it should
        # say to create a template because the workstation folder is NOT read.
        self.assertNotIn("do not edit", check["fix"])
        self.assertIn("Create a dedicated template", check["fix"])
        dedicated = self.root.resolve() / ".arena" / "credential-templates" / "codex"
        script = check["fix"].split("Create a dedicated template: ", 1)[1].split(" && ./bootstrap.sh")[0]
        import subprocess
        subprocess.run(script, shell=True, check=True)
        self.assertEqual(check["fix"].rsplit(" ", 1)[1], f"CODEX_HOME_TEMPLATE={dedicated}")
        import tomllib
        written = tomllib.loads((dedicated / ".codex" / "config.toml").read_text())
        self.assertEqual(written["model_provider"], "amazon-bedrock-runtime")
        self.assertEqual(written["model_providers"]["amazon-bedrock-runtime"]["aws"]["region"], "us-east-1")
        self.assertNotIn("profile", written["model_providers"]["amazon-bedrock-runtime"]["aws"])
        # Workstation codex config is never modified — the fix only creates a NEW template.
        self.assertIn('profile = "dev"', (host / "config.toml").read_text())
        preflight.inputs(self.properties, [f"CODEX_HOME_TEMPLATE={dedicated}"])
        report, checks = self.run_preflight(answers=[])
        self.assertEqual(checks["codex-provider-config"]["status"], "pass")

    def test_a_missing_properties_file_gets_a_quoted_copy_command(self):
        path = self.root / "with space" / "deploy.properties"
        check = preflight.Preflight(self.config, path, offline=True)
        self.assertFalse(check.inputs())
        self.assertIn(f"cp deploy.properties.example '{check.properties_path}',", check.checks[0]["fix"])

    def test_the_region_in_the_fix_command_is_shell_quoted(self):
        host = self.root / "host-codex"
        host.mkdir()
        injected = self.root / "injected"
        region = f"us-east-1'; touch {injected}; echo '"
        answers = [a for a in ANSWERS if not a.startswith(("CODEX_HOME_TEMPLATE", "AWS_REGION"))]
        with patch.dict("os.environ", {"CODEX_HOME": str(host)}):
            report, checks = self.run_preflight(answers=[*answers, f"AWS_REGION={region}"])
        script = checks["codex-provider-config"]["fix"].split("Create a dedicated template: ", 1)[1]
        import subprocess
        subprocess.run(script.split(" && ./bootstrap.sh")[0], shell=True, check=True)
        self.assertFalse(injected.exists())
        written = self.root.resolve() / ".arena" / "credential-templates" / "codex" / ".codex" / "config.toml"
        self.assertIn(f'region = "{region}"', written.read_text())

    def test_codex_provider_config_with_a_profile_is_assistant_fixable(self):
        (self.root / "codex-home" / ".codex" / "config.toml").write_text(CODEX_PROVIDER + 'profile = "dev"\n')
        report, checks = self.run_preflight()
        self.assertEqual((checks["codex-provider-config"]["status"], checks["codex-provider-config"]["gate"]),
                         ("fail", "assistant"))

    def test_cached_runtimes_from_another_region_need_an_authorized_deploy(self):
        (self.root / "data").mkdir()
        (self.root / "data" / "agentcore-runtimes.json").write_text(json.dumps({"claude": {
            "arn": "arn:aws:bedrock-agentcore:eu-west-1:111111111111:runtime/mchc_claude-x",
            "id": "mchc_claude-x"}}))
        report, checks = self.run_preflight()
        self.assertEqual((checks["agentcore-drift"]["status"], checks["agentcore-drift"]["gate"]), ("fail", "deploy"))
        self.assertIn("eu-west-1", checks["agentcore-drift"]["detail"])
        self.assertTrue(report["needs_deploy"])
        self.assertEqual(report["gates"], ["deploy"])

    def test_cached_runtimes_from_the_previous_account_are_caught_without_aws_account_id(self):
        (self.root / "data").mkdir()
        (self.root / "data" / "agentcore-runtimes.json").write_text(json.dumps({"codex": {
            "arn": "arn:aws:bedrock-agentcore:us-east-1:999999999999:runtime/mchc_codex-x"}}))
        session = FakeSession(account="111111111111", profiles=["us.anthropic.claude-sonnet-4-6",
                              "us.anthropic.claude-opus-5"], runtimes=["mchc_claude", "mchc_codex",
                                                                       "mchc_kiro"])
        report, checks = self.run_preflight(offline=False, session=session)
        self.assertEqual(checks["agentcore-drift"]["status"], "fail")
        self.assertIn("999999999999", checks["agentcore-drift"]["detail"])
        self.assertEqual(checks["agentcore-runtimes"]["status"], "pass")
        self.assertTrue(report["needs_deploy"])

    def test_aws_checks_make_only_read_only_calls_and_name_who_acts(self):
        from botocore.exceptions import ClientError
        session = FakeSession(account="111111111111", runtimes=["mchc_claude"],
                              profiles=["us.anthropic.claude-sonnet-4-6"],
                              s3_error=ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadBucket"))
        report, checks = self.run_preflight(answers=[*ANSWERS, "AWS_ACCOUNT_ID=222222222222"],
                                            offline=False, session=session)
        self.assertEqual(session.session_kwargs, {"profile_name": "dev", "region_name": "us-east-1"})
        expected = {"aws-identity": ("pass", "none"), "aws-account": ("fail", "human"),
                    "s3-artifact": ("fail", "human"), "s3-transfer": ("fail", "human"),
                    "bedrock-model-judge": ("pass", "none"), "bedrock-model-planning": ("fail", "human"),
                    "agentcore-runtimes": ("fail", "deploy")}
        self.assertEqual({id: (checks[id]["status"], checks[id]["gate"]) for id in expected}, expected)
        self.assertIn("mchc_codex", checks["agentcore-runtimes"]["detail"])
        self.assertTrue(report["needs_deploy"])
        self.assertEqual(report["gates"], ["deploy", "human"])
        # list_objects_v2 is added by the agentcore-secret-leftovers read-only check.
        self.assertEqual(session.calls(), {
            ("sts", "get_caller_identity"), ("s3", "head_bucket"), ("s3", "list_objects_v2"),
            ("bedrock", "get_paginator"), ("bedrock-agentcore-control", "list_agent_runtimes")})
        transfer = [c for c in session.clients["s3"].head_bucket.call_args_list
                    if c.kwargs.get("ExpectedBucketOwner")]
        self.assertEqual(transfer[0].kwargs["ExpectedBucketOwner"], "111111111111")

    def test_a_defect_in_a_check_is_reported_as_a_failed_check_not_a_crash(self):
        with patch.object(preflight.Preflight, "drift", side_effect=RuntimeError("boom")):
            report, checks = self.run_preflight()
        self.assertEqual((checks["agentcore-drift-error"]["status"], checks["agentcore-drift-error"]["gate"]),
                         ("fail", "assistant"))
        self.assertIn("RuntimeError: boom", checks["agentcore-drift-error"]["detail"])
        self.assertIn("report this preflight defect", checks["agentcore-drift-error"]["fix"])
        self.assertEqual(report["failed"], ["agentcore-drift-error"])

    def test_a_port_held_by_another_program_is_reported(self):
        preflight.inputs(self.properties, ANSWERS)
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            preflight.inputs(self.properties, [f"SERVICE_PORT={port}"])
            check = preflight.Preflight(self.config, self.properties, offline=True)
            check.inputs()
            with patch("arena.cli._own_service_pids", return_value={}):
                check.host()
            checks = {c["id"]: c for c in check.checks}
            self.assertEqual((checks["service-port"]["status"], checks["service-port"]["gate"]), ("fail", "assistant"))
            with patch("arena.cli._own_service_pids", return_value={"serve": 4242}):
                check.checks = []
                check.host()
            checks = {c["id"]: c for c in check.checks}
            self.assertEqual(checks["service-port"]["status"], "pass")
            self.assertIn("4242", checks["service-port"]["detail"])


if __name__ == "__main__":
    unittest.main()
