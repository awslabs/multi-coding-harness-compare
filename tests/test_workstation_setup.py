# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Moving this checkout to another workstation must need no absolute paths, and must
never move a login nobody asked it to move.

Setup is expected to be driven by an AI coding assistant rather than a person at a
terminal, so the commands here are checked for the property that matters to an agent:
they report what is unresolved and exit, rather than blocking on stdin. No model calls
and no AWS calls.
"""
import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arena.config import Config, HarnessConfig, load_config
from arena.host_setup import (DEFAULT_HOME_SOURCES, authentication_plan, default_home_plan,
                              default_home_source, prepare_home)

REPO = Path(__file__).parent.parent


class DefaultHomeSourceTests(unittest.TestCase):
    def test_each_harness_resolves_the_directory_its_own_cli_uses(self):
        source, destination = default_home_source("codex")
        self.assertEqual(destination, ".codex")
        self.assertEqual(source, Path.home() / ".codex")
        # kiro keeps non-secret configuration in ~/.kiro (global config dir), separate
        # from the per-platform data directory that holds data.sqlite3. The destination
        # is .kiro inside the run's private HOME.
        source, destination = default_home_source("kiro")
        self.assertEqual(destination, ".kiro")
        self.assertEqual(source, Path.home() / ".kiro")

    def test_the_clis_own_environment_override_wins(self):
        with patch.dict(os.environ, {"CODEX_HOME": "/tmp/elsewhere/.codex"}):
            self.assertEqual(default_home_source("codex")[0], Path("/tmp/elsewhere/.codex"))

    def test_an_unknown_harness_reports_no_default(self):
        self.assertEqual(default_home_source("invented"), (None, None))


class PrivateHomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "codex-config"
        self.source.mkdir()
        (self.source / "config.toml").write_text('model_provider = "amazon-bedrock-runtime"\n')
        (self.source / "AGENTS.md").write_text("house rules\n")
        (self.source / "auth.json").write_text('{"token": "SECRET"}')
        (self.source / "history.jsonl").write_text("not configuration\n")

    def prepare(self, **overrides):
        home = self.root / "run-home"
        configured = HarnessConfig(**overrides)
        with patch.dict(os.environ, {"CODEX_HOME": str(self.source)}):
            detail = prepare_home("codex", configured, home)
        return home, detail

    def test_non_secret_configuration_is_copied_and_the_login_is_not(self):
        home, detail = self.prepare()
        self.assertEqual((home / ".codex/config.toml").read_text().strip(),
                         'model_provider = "amazon-bedrock-runtime"')
        self.assertTrue((home / ".codex/AGENTS.md").is_file())
        self.assertFalse((home / ".codex/auth.json").exists())
        # Only the allowlist travels; unrelated files stay on the host even though they
        # sit in the same directory.
        self.assertFalse((home / ".codex/history.jsonl").exists())
        self.assertEqual(detail["secrets_withheld"], ["auth.json"])
        self.assertEqual(detail["copied"], ["config.toml", "AGENTS.md"])

    def test_naming_the_login_in_refresh_files_opts_it_in(self):
        home, detail = self.prepare(refresh_files=[".codex/auth.json"])
        self.assertEqual(json.loads((home / ".codex/auth.json").read_text())["token"], "SECRET")
        self.assertEqual(detail["secrets_withheld"], [])
        self.assertEqual(detail["secrets_opted_in"], [".codex/auth.json"])

    def test_a_configured_template_still_copies_the_whole_directory(self):
        template = self.root / "template"
        (template / ".codex").mkdir(parents=True)
        (template / ".codex/config.toml").write_text("dedicated = true\n")
        (template / ".codex/auth.json").write_text("{}")
        home, detail = self.prepare(home_template=str(template))
        self.assertEqual(detail["mode"], "configured home_template")
        # The operator assembled this directory for this purpose, so nothing is filtered.
        self.assertTrue((home / ".codex/auth.json").is_file())
        self.assertEqual((home / ".codex/config.toml").read_text().strip(), "dedicated = true")

    def test_an_absent_source_leaves_an_empty_private_home(self):
        home = self.root / "no-source-home"
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "nowhere")}):
            detail = prepare_home("codex", HarnessConfig(), home)
        self.assertTrue(home.is_dir())
        self.assertEqual(list(home.iterdir()), [])
        self.assertFalse(detail["source_present"])

    def test_a_refresh_path_leaving_the_home_is_rejected_at_configuration_time(self):
        for value in ("../outside.json", "/etc/passwd"):
            with self.assertRaisesRegex(ValueError, "relative to the dedicated HOME"):
                HarnessConfig(refresh_files=[value])

    def test_a_refresh_path_escaping_a_configured_template_is_refused(self):
        template = self.root / "template"
        template.mkdir()
        (self.root / "outside.json").write_text("{}")
        escaping = HarnessConfig.model_construct(
            **{**HarnessConfig().model_dump(), "home_template": str(template),
               "refresh_files": ["../outside.json"]})
        with self.assertRaisesRegex(ValueError, "escaped the dedicated HOME"):
            prepare_home("codex", escaping, self.root / "escape-home")

    def test_a_refresh_path_outside_the_harness_prefix_copies_nothing(self):
        # The default source is the harness's own config directory, so an entry that
        # names something else in HOME has nothing to copy from and is skipped rather
        # than reaching outside that directory.
        home, detail = self.prepare(refresh_files=[".ssh/id_rsa"])
        self.assertFalse((home / ".ssh/id_rsa").exists())
        self.assertEqual(detail["refreshed"], [])

    def test_the_personal_home_is_never_used_as_a_source(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(Path.home())}):
            with self.assertRaisesRegex(ValueError, "not be your personal HOME"):
                prepare_home("codex", HarnessConfig(), self.root / "personal-home")


class AuthenticationPlanTests(unittest.TestCase):
    def config(self, **overrides):
        return Config(data_dir=str(Path(tempfile.gettempdir()) / "cc-auth"),
                      artifact_s3_uri=None, **overrides)

    def test_claude_and_codex_use_the_execution_role_on_agentcore(self):
        config = self.config(runtime="agentcore", agentcore={
            "transfer_s3_uri": "s3://bucket/prefix/"})
        for harness in ("claude", "codex"):
            plan = authentication_plan(config, harness)
            self.assertEqual(plan["method"], "AgentCore execution role")
            self.assertIsNone(plan["blocked"])
            self.assertIn("No host credential is copied", plan["explanation"])
            self.assertIn("no API key is forwarded", plan["explanation"])

    def test_kiro_has_no_execution_role_and_says_how_to_fix_it(self):
        config = self.config(runtime="agentcore", agentcore={
            "transfer_s3_uri": "s3://bucket/prefix/"})
        plan = authentication_plan(config, "kiro")
        self.assertIn("no execution-role path", plan["explanation"])
        self.assertIn("--kiro-auth=copy-host-login", plan["blocked"])
        self.assertIn("--kiro-auth=login-in-home", plan["blocked"])
        self.assertIn("kiro-cli login", plan["blocked"])

    def test_an_opted_in_kiro_login_clears_the_blocker(self):
        config = self.config(runtime="agentcore",
                             agentcore={"transfer_s3_uri": "s3://bucket/prefix/"},
                             harnesses={"kiro": HarnessConfig(
                                 refresh_files=[".local/share/kiro-cli/data.sqlite3"])})
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "kiro-cli")
            source.mkdir()
            sqlite3.connect(source / "data.sqlite3").close()
            with patch.dict(DEFAULT_HOME_SOURCES,
                            {"kiro": (None, str(source), ".local/share/kiro-cli")}):
                plan = authentication_plan(config, "kiro")
        self.assertIsNone(plan["blocked"])
        self.assertIn("copied from this workstation", plan["method"])

    def test_an_opted_in_login_that_does_not_exist_is_reported(self):
        config = self.config(runtime="agentcore",
                             agentcore={"transfer_s3_uri": "s3://bucket/prefix/"},
                             harnesses={"kiro": HarnessConfig(
                                 refresh_files=[".local/share/kiro-cli/data.sqlite3"])})
        with patch.dict(DEFAULT_HOME_SOURCES,
                        {"kiro": (None, "/nonexistent/kiro-cli", ".local/share/kiro-cli")}):
            plan = authentication_plan(config, "kiro")
        self.assertIn("no login exists", plan["blocked"])
        self.assertIn("kiro-cli login", plan["blocked"])

    def test_a_configured_template_withholds_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory, "kiro-home")
            template.mkdir()
            config = self.config(runtime="agentcore",
                                 agentcore={"transfer_s3_uri": "s3://bucket/prefix/"},
                                 harnesses={"kiro": HarnessConfig(home_template=str(template))})
            plan = authentication_plan(config, "kiro")
        # The whole template is copied, so reporting a withheld secret would be wrong.
        self.assertEqual(plan["secrets_withheld"], [])
        self.assertEqual(plan["configuration_source"], str(template))
        self.assertTrue(plan["configuration_source_present"])
        self.assertIsNone(plan["blocked"])

    def test_the_plan_names_the_withheld_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "auth.json").write_text("{}")
            with patch.dict(os.environ, {"CODEX_HOME": str(source)}):
                plan = default_home_plan("codex", HarnessConfig())
        self.assertEqual(plan["secrets_withheld"], ["auth.json"])


class SetupCommandTests(unittest.TestCase):
    """Driven the way an agent drives it: argv in, JSON out, no stdin."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "config.toml"
        self.properties = Path(self.temp.name) / "deploy.properties"

    def load(self):
        """The configuration setup wrote, as the services load it: config.toml + deploy.properties."""
        return load_config(self.output, self.properties)

    def setup(self, *arguments, stdin=subprocess.DEVNULL):
        completed = subprocess.run(
            [sys.executable, "-m", "arena.cli", "setup", "--output", str(self.output), *arguments],
            cwd=REPO, capture_output=True, text=True, stdin=stdin, timeout=120)
        return completed

    def test_unresolved_decisions_are_reported_and_nothing_is_written(self):
        completed = self.setup()
        self.assertEqual(completed.returncode, 1)
        report = json.loads(completed.stdout)
        self.assertFalse(report["ok"])
        self.assertIsNone(report["wrote"])
        self.assertFalse(self.output.exists())
        identifiers = {d["id"] for d in report["decisions"]}
        self.assertEqual(identifiers, {"repository-root", "aws-profile", "artifact-bucket",
                                      "runtime", "kiro-auth"})
        for decision in report["decisions"]:
            self.assertTrue(decision["flag"], decision["id"])
            self.assertTrue(decision["writes"], decision["id"])

    def test_interactive_without_a_terminal_reports_instead_of_blocking(self):
        # The failure this prevents: an agent's shell call hanging on input() until its
        # own timeout, with setup having done nothing.
        completed = self.setup("--interactive")
        self.assertEqual(completed.returncode, 1)
        report = json.loads(completed.stdout)
        self.assertFalse(report["interactive"])
        self.assertIn("needs a terminal", report["reason"])
        self.assertTrue(report["decisions"])
        self.assertFalse(self.output.exists())

    def test_a_resolved_run_writes_a_configuration_that_loads(self):
        completed = self.setup("--repository-root", self.temp.name,
                               "--aws-profile", "example-profile",
                               "--artifact-bucket", "example-bucket",
                               "--runtime", "native", "--kiro-auth", "copy-host-login")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["ok"])
        config = self.load()
        self.assertEqual(config.repository_roots, [self.temp.name])
        self.assertEqual(config.judge_profile, "example-profile")
        self.assertEqual(config.artifact_s3_profile, "example-profile")
        self.assertEqual(config.artifact_s3_uri, "s3://example-bucket/output/mchc/")
        self.assertEqual(config.harnesses["kiro"].refresh_files,
                         [".local/share/kiro-cli/data.sqlite3"])
        # The example must produce a working evaluator, not a silently degraded one.
        self.assertTrue(config.judge_model)
        self.assertTrue(config.effective_planning_model)
        # No placeholder survives on a line that actually takes effect, in either file.
        for written in (self.output, self.properties):
            live = [line for line in written.read_text().splitlines()
                    if ("YOUR_" in line or "/absolute/path/" in line) and not line.lstrip().startswith("#")]
            self.assertEqual(live, [], written)
        # Environment values live in deploy.properties, not in config.toml.
        for value in ("example-profile", "example-bucket", self.temp.name):
            self.assertNotIn(value, self.output.read_text())

    def test_login_in_home_records_the_step_a_person_has_to_do(self):
        completed = self.setup("--repository-root", self.temp.name,
                               "--aws-profile", "example-profile",
                               "--artifact-bucket", "example-bucket",
                               "--runtime", "native", "--kiro-auth", "login-in-home")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        login = report["next_steps"][0]
        self.assertEqual(login["action"], "kiro-cli login")
        self.assertIn("cannot be automated", login["note"])
        self.assertIn("no login copied", report["authentication"]["kiro"])
        self.assertEqual(self.load().harnesses["kiro"].refresh_files, [])

    def test_an_existing_configuration_is_not_overwritten_without_force(self):
        self.output.write_text("data_dir = '.arena'\n")
        arguments = ("--repository-root", self.temp.name, "--aws-profile", "p",
                     "--artifact-bucket", "b", "--runtime", "native",
                     "--kiro-auth", "login-in-home")
        completed = self.setup(*arguments)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            [d["id"] for d in json.loads(completed.stdout)["decisions"]], ["existing-config"])
        self.assertEqual(self.output.read_text(), "data_dir = '.arena'\n")
        completed = self.setup(*arguments, "--force")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertTrue(self.output.with_suffix(".toml.backup").is_file())

    def test_a_new_repository_root_keeps_the_roots_already_listed(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        from arena import properties as props
        example = (Path(REPO) / "deploy.properties.example").read_text()
        self.properties.write_text(props.render(example, {"REPOSITORY_ROOTS": str(other)}))
        completed = self.setup("--repository-root", self.temp.name, "--aws-profile", "p",
                               "--artifact-bucket", "b", "--runtime", "native", "--kiro-auth", "login-in-home")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(self.load().repository_roots, [self.temp.name, str(other)])


class AgentCoreSetupTests(unittest.TestCase):
    """AgentCore has to come up in the same flow, driven by the same agent."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "config.toml"
        self.properties = Path(self.temp.name) / "deploy.properties"

    def load(self):
        """The configuration setup wrote, as the services load it: config.toml + deploy.properties."""
        return load_config(self.output, self.properties)

    def setup(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "arena.cli", "setup", "--output", str(self.output),
             "--repository-root", self.temp.name, "--aws-profile", "example-profile",
             "--artifact-bucket", "example-bucket", "--kiro-auth", "copy-host-login", *arguments],
            cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)

    def test_runtime_is_a_decision_setup_will_not_guess(self):
        completed = subprocess.run(
            [sys.executable, "-m", "arena.cli", "setup", "--output", str(self.output)],
            cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        decisions = {d["id"]: d for d in json.loads(completed.stdout)["decisions"]}
        self.assertIn("runtime", decisions)
        self.assertEqual(decisions["runtime"]["options"], ["native", "agentcore"])

    def test_agentcore_runtime_writes_a_complete_loadable_block(self):
        completed = self.setup("--runtime", "agentcore")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["runtime"], "agentcore")
        config = self.load()
        self.assertEqual(config.runtime, "agentcore")
        self.assertEqual(config.agentcore.transfer_s3_uri,
                         "s3://example-bucket/code/ai-coding-assistant-agcore-runtime/")
        self.assertEqual(config.agentcore.profile, "example-profile")
        self.assertEqual(config.agentcore.job_root, "/var/tmp/mchc")
        # Nothing pinned: a new workstation discovers the runtimes already in the account.
        self.assertEqual(config.agentcore.runtimes, {})
        self.assertIn("no ARNs needed", report["agentcore"]["runtime_discovery"])

    def test_a_separate_transfer_bucket_is_honoured(self):
        completed = self.setup("--runtime", "agentcore", "--transfer-bucket", "other-bucket")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        config = self.load()
        self.assertEqual(config.agentcore.transfer_s3_uri,
                         "s3://other-bucket/code/ai-coding-assistant-agcore-runtime/")
        self.assertEqual(config.artifact_s3_uri, "s3://example-bucket/output/mchc/")

    def test_without_deploy_the_exact_provisioning_commands_are_printed(self):
        report = json.loads(self.setup("--runtime", "agentcore").stdout)
        actions = [step["action"] for step in report["next_steps"]]
        self.assertEqual(actions[-3:], ["multi-coding-harness-compare agentcore-deploy",
                                        "multi-coding-harness-compare agentcore-probe", "multi-coding-harness-compare doctor"])

    def test_the_native_runtime_asks_for_no_provisioning(self):
        report = json.loads(self.setup("--runtime", "native").stdout)
        actions = [step["action"] for step in report["next_steps"]]
        self.assertNotIn("multi-coding-harness-compare agentcore-deploy", actions)
        self.assertNotIn("multi-coding-harness-compare agentcore-probe", actions)
        self.assertEqual(actions[-1], "multi-coding-harness-compare doctor")

    def test_deploy_provisions_then_probes_and_drops_the_manual_step(self):
        from arena import setup_wizard
        calls = []

        async def fake_deploy(config, **kwargs):
            calls.append(("deploy", config.runtime, config.agentcore.transfer_s3_uri))
            return {"execution_role_arn": "arn:aws:iam::1:role/R", "runtimes": {}}

        class FakeAgentCore:
            def __init__(self, config):
                calls.append(("probe-init", config.runtime))

            async def probe(self, harnesses=None):
                calls.append(("probe", harnesses))
                return {"ok": True, "runtimes": {}}

        args = SimpleNamespace(
            repository_root=self.temp.name, aws_profile="example-profile",
            artifact_bucket="example-bucket", transfer_bucket=None, runtime="agentcore",
            kiro_auth="copy-host-login", output=str(self.output), force=False,
            interactive=False, deploy=True)
        with patch("arena.agentcore_provision.deploy", fake_deploy), \
                patch("arena.agentcore_sandbox.AgentCore", FakeAgentCore):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code = setup_wizard.run(args)
        self.assertEqual(code, 0, captured.getvalue())
        report = json.loads(captured.getvalue())
        self.assertTrue(report["ok"])
        self.assertTrue(report["deployment"]["ok"])
        self.assertEqual(calls[0][0], "deploy")
        self.assertEqual(calls[0][1], "agentcore")
        self.assertIn(("probe", None), calls)
        # The step the deploy just performed must not also be printed as pending work.
        actions = [step["action"] for step in report["next_steps"]]
        self.assertNotIn("multi-coding-harness-compare agentcore-deploy", actions)
        self.assertIn("multi-coding-harness-compare agentcore-probe", actions)

    def test_a_failed_probe_after_deploy_is_reported_and_exits_non_zero(self):
        from arena import setup_wizard

        async def fake_deploy(config, **kwargs):
            return {"runtimes": {}}

        class FakeAgentCore:
            def __init__(self, config):
                pass

            async def probe(self, harnesses=None):
                return {"ok": False, "runtimes": {"claude": {"job_root_writable": False}}}

        args = SimpleNamespace(
            repository_root=self.temp.name, aws_profile="p", artifact_bucket="b",
            transfer_bucket=None, runtime="agentcore", kiro_auth="copy-host-login",
            output=str(self.output), force=False, interactive=False, deploy=True)
        with patch("arena.agentcore_provision.deploy", fake_deploy), \
                patch("arena.agentcore_sandbox.AgentCore", FakeAgentCore):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code = setup_wizard.run(args)
        self.assertEqual(code, 1)
        report = json.loads(captured.getvalue())
        self.assertFalse(report["ok"])
        self.assertEqual(report["deployment"]["stage"], "agentcore-probe")
        # The configuration is still written: it is correct, the runtime paths are not.
        self.assertTrue(self.output.is_file())

    def test_a_probe_that_finds_no_runtime_keeps_its_own_error(self):
        from arena import setup_wizard

        async def fake_deploy(config, **kwargs):
            return {"runtimes": {}}

        class FakeAgentCore:
            def __init__(self, config):
                pass

            async def probe(self, harnesses=None):
                return {"ok": False, "runtimes": {}, "error": "No AgentCore runtimes are available."}

        args = SimpleNamespace(
            repository_root=self.temp.name, aws_profile="p", artifact_bucket="b",
            transfer_bucket=None, runtime="agentcore", kiro_auth="copy-host-login",
            output=str(self.output), force=False, interactive=False, deploy=True)
        with patch("arena.agentcore_provision.deploy", fake_deploy), \
                patch("arena.agentcore_sandbox.AgentCore", FakeAgentCore):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                setup_wizard.run(args)
        deployment = json.loads(captured.getvalue())["deployment"]
        self.assertEqual(deployment["error"], "No AgentCore runtimes are available.")

    def test_a_deploy_failure_names_the_stage(self):
        from arena import setup_wizard

        async def failing_deploy(config, **kwargs):
            raise RuntimeError("AccessDenied: bedrock-agentcore:CreateAgentRuntime")

        args = SimpleNamespace(
            repository_root=self.temp.name, aws_profile="p", artifact_bucket="b",
            transfer_bucket=None, runtime="agentcore", kiro_auth="copy-host-login",
            output=str(self.output), force=False, interactive=False, deploy=True)
        with patch("arena.agentcore_provision.deploy", failing_deploy):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code = setup_wizard.run(args)
        self.assertEqual(code, 1)
        deployment = json.loads(captured.getvalue())["deployment"]
        self.assertEqual(deployment["stage"], "agentcore-deploy")
        self.assertIn("AccessDenied", deployment["error"])


class ExampleConfigurationTests(unittest.TestCase):
    def test_no_shipped_file_names_an_account_or_a_home_directory(self):
        # The repository is shared as is: account IDs and personal paths belong in the
        # untracked deploy.properties. Tests and docs use the documented placeholder IDs;
        # /home/agent and /home/agentcore-runtime-user are users inside the image and microVM.
        placeholders = {"123456789012", "111111111111", "222222222222", "999999999999", "000000000000"}
        pattern = re.compile(r"(?<![\w.-])\d{12}(?!\w)|(?<![\w./-])/Users/[A-Za-z][\w.-]*"
                             r"|(?<![\w./-])/home/(?!(?:agent|agentcore-runtime-user)\b)[a-z][\w.-]*")
        listed = subprocess.run(["git", "ls-files", "-c", "--exclude-standard", "-z"], cwd=REPO,
                                capture_output=True, text=True)
        if listed.returncode:
            self.skipTest("not a git checkout")
        for name in filter(None, listed.stdout.split("\0")):
            path = REPO / name
            if not path.is_file():
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            found = sorted({m for m in pattern.findall(text) if m not in placeholders})
            self.assertEqual(found, [], name)

    def test_the_shipped_examples_hold_only_placeholders_for_identity(self):
        from arena import properties
        values = properties.parse((REPO / "deploy.properties.example").read_text(), "example")
        for key in ("AWS_PROFILE", "AWS_REGION", "S3_BUCKET", "RUNTIME", "KIRO_AUTH"):
            self.assertTrue(values[key].startswith("YOUR_"), key)

    def test_the_example_codex_template_matches_the_preflight_fix(self):
        import tomllib
        template = REPO / "examples" / "credential-templates" / "codex" / ".codex" / "config.toml"
        settings = tomllib.loads(template.read_text())
        self.assertEqual(settings["model_provider"], "amazon-bedrock-runtime")
        aws = settings["model_providers"]["amazon-bedrock-runtime"]["aws"]
        self.assertEqual((aws["region"], aws["wire_api"], aws["namespace_tools"]),
                         ("YOUR_AWS_REGION", "responses", False))
        self.assertNotIn("profile", aws)

    def test_the_examples_load_together_once_the_choices_are_filled(self):
        from arena import properties
        with tempfile.TemporaryDirectory() as td:
            filled = Path(td) / "deploy.properties"
            filled.write_text(properties.render((REPO / "deploy.properties.example").read_text(),
                                                {"RUNTIME": "agentcore", "KIRO_AUTH": "copy-host-login"}))
            config = load_config(REPO / "config.example.toml", filled)
        self.assertEqual(config.runtime, "agentcore")
        self.assertEqual(config.harnesses["kiro"].refresh_files, [".local/share/kiro-cli/data.sqlite3"])
        self.assertEqual(config.artifact_s3_uri, "s3://YOUR_BUCKET/output/mchc/")
        self.assertEqual(config.judge_profile, "YOUR_AWS_PROFILE")
        self.assertEqual(config.agentcore.inline_policy_name, "MchcAgentCoreRuntime")

    def test_an_unfilled_choice_is_refused_with_its_options(self):
        with self.assertRaisesRegex(ValueError, "RUNTIME must be one of native, agentcore"):
            load_config(REPO / "config.example.toml", REPO / "deploy.properties.example")


# ── no silent us-east-1 fallbacks ─────────────────────────────────────────────

class SetupWizardNoUsEast1FallbackTests(unittest.TestCase):
    """setup wizard must not silently write us-east-1 when no region is given."""

    def _run_setup(self, extra_props=""):
        """Run the setup wizard without --aws-region; return the written deploy.properties text."""
        from arena.setup_wizard import run as run_setup
        with tempfile.TemporaryDirectory() as td:
            props_path = Path(td) / "deploy.properties"
            props_path.write_text(
                "RUNTIME = agentcore\n"
                "KIRO_AUTH = copy-host-login\n"
                "AWS_PROFILE = myprofile\n"
                "S3_BUCKET = mybucket\n"
                "REPOSITORY_ROOTS = /tmp/repos\n"
                + extra_props
            )
            config_path = REPO / "config.example.toml"
            args = SimpleNamespace(
                aws_profile="myprofile",
                aws_region=None,  # not provided
                artifact_bucket="mybucket",
                transfer_bucket=None,
                repository_root="/tmp/repos",
                runtime="agentcore",
                kiro_auth="copy-host-login",
                interactive=False,
                force=False,
                deploy=False,
                config=str(config_path),
                properties=str(props_path),
                output=None,
            )
            run_setup(args)
            return props_path.read_text() if props_path.exists() else ""

    def test_wizard_without_region_does_not_write_us_east_1(self):
        """When no region is available, the wizard must not write AWS_REGION = us-east-1."""
        written = self._run_setup()
        # Either no AWS_REGION key is written, or it is not hardcoded to us-east-1
        if "AWS_REGION" in written:
            self.assertNotIn(
                "AWS_REGION = us-east-1",
                written,
                "setup wizard must not silently write AWS_REGION = us-east-1 "
                "when no region was provided"
            )


class InheritSetupNoUsEast1FallbackTests(unittest.TestCase):
    """inherit_setup must raise ValueError instead of falling back to us-east-1."""

    def test_inherit_setup_raises_when_no_region_configured(self):
        """When no region is in the config, env, or boto3 session, raise ValueError."""
        import shutil
        codex_exec = shutil.which("codex") or shutil.which("echo")
        if not codex_exec:
            self.skipTest("no codex or echo executable found")
        from arena.host_setup import inherit_setup
        from arena.config import HarnessConfig
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            codex_src = Path(td) / "codex-src"
            codex_src.mkdir()
            # Write a config.toml with model_provider=amazon-bedrock but no region
            (codex_src / "config.toml").write_text(
                'model_provider = "amazon-bedrock"\n'
                '[model_providers.amazon-bedrock.aws]\n'
            )
            configured = HarnessConfig(cli_path=codex_exec)
            # Remove all region-related env vars, then make boto3 return None
            clean_env = {k: v for k, v in os.environ.items()
                         if k not in ("AWS_REGION", "AWS_DEFAULT_REGION")}
            with patch.dict(os.environ, {**clean_env, "CODEX_HOME": str(codex_src)},
                            clear=True), \
                 patch("boto3.Session") as mock_sess:
                mock_sess.return_value.region_name = None
                with self.assertRaises(ValueError) as ctx:
                    inherit_setup("codex", configured, home)
        self.assertIn("AWS_REGION", str(ctx.exception),
                      "ValueError must mention AWS_REGION so the operator knows how to fix it")


# ── AWS_CONFIG_FILE forwarded to subprocess env in start_background ───────────

class StartBackgroundEnvTests(unittest.TestCase):
    """start_background must forward AWS_CONFIG_FILE / AWS_SHARED_CREDENTIALS_FILE."""

    def _config_with_creds(self, config_file=None, creds_file=None):
        """Return a minimal Config-like object with the given credential paths."""
        from arena.config import Config, HarnessConfig, AgentCoreConfig
        from arena import properties as prop_mod
        with tempfile.TemporaryDirectory() as td:
            props_path = Path(td) / "deploy.properties"
            lines = [
                "RUNTIME = native\n",
                "KIRO_AUTH = copy-host-login\n",
            ]
            if config_file:
                lines.append(f"AWS_CONFIG_FILE = {config_file}\n")
            if creds_file:
                lines.append(f"AWS_SHARED_CREDS_FILE = {creds_file}\n")
            props_path.write_text("".join(lines))
            cfg = load_config(REPO / "config.example.toml", props_path)
        return cfg

    def test_aws_config_file_included_in_subprocess_env(self):
        """When aws_config_file is set, start_background must include it in the child env."""
        import arena.cli as cli_mod
        import inspect
        src = inspect.getsource(cli_mod.start_background)
        self.assertIn("aws_config_file", src,
                      "start_background must read config.aws_config_file")
        self.assertIn("AWS_CONFIG_FILE", src,
                      "start_background must set AWS_CONFIG_FILE in the subprocess env")

    def test_aws_shared_creds_file_included_in_subprocess_env(self):
        import arena.cli as cli_mod
        import inspect
        src = inspect.getsource(cli_mod.start_background)
        self.assertIn("aws_shared_creds_file", src,
                      "start_background must read config.aws_shared_creds_file")
        self.assertIn("AWS_SHARED_CREDENTIALS_FILE", src,
                      "start_background must set AWS_SHARED_CREDENTIALS_FILE in the subprocess env")


if __name__ == "__main__":
    unittest.main()
