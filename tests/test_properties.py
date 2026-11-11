# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""deploy.properties: one file for every user-, machine- and account-specific value."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from arena import properties as props
from arena.config import Config, load_config, resolve_config


class ParseTests(unittest.TestCase):
    def test_key_value_lines_comments_quotes_and_equals_in_values(self):
        values = props.parse('# comment\n\nAWS_PROFILE = p1\nAWS_REGION="eu-west-1"\nURL = a=b=c\n')
        self.assertEqual(values, {"AWS_PROFILE": "p1", "AWS_REGION": "eu-west-1", "URL": "a=b=c"})

    def test_keys_reference_other_keys(self):
        values = props.parse("S3_BUCKET = b\nARTIFACT_S3_URI = s3://${S3_BUCKET}/out/\n")
        self.assertEqual(values["ARTIFACT_S3_URI"], "s3://b/out/")

    def test_malformed_lines_and_keys_are_refused_with_their_line_number(self):
        for text, message in (("NO_SEPARATOR\n", ":1: expected KEY = VALUE"),
                              ("aws_profile = p\n", "UPPER_SNAKE_CASE"),
                              ("A = 1\nA = 2\n", ":2: A is defined twice"),
                              ("A = ${MISSING}\n", r"references \$\{MISSING\}"),
                              ("A = ${B}\nB = ${A}\n", "circular reference")):
            with self.subTest(text=text), self.assertRaisesRegex(props.PropertiesError, message):
                props.parse(text)

    def test_credentials_are_refused(self):
        for name in ("AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "KIRO_TOKEN", "DB_PASSWORD"):
            with self.subTest(name=name), self.assertRaisesRegex(props.PropertiesError, "credential"):
                props.parse(f"{name} = x\n")


class InterpolationTests(unittest.TestCase):
    def setUp(self):
        self.props = props.Properties(Path("/p/deploy.properties"),
                                      {"AWS_REGION": "eu-west-1", "MODEL": "m1", "EMPTY": ""})

    def test_strings_lists_and_keys_are_substituted_after_parsing(self):
        data = {"a": "${AWS_REGION}", "b": ["x-${MODEL}"], "c": {"${MODEL}": "${AWS_REGION}"}, "n": 3}
        resolved, references = props.interpolate(data, self.props)
        self.assertEqual(resolved, {"a": "eu-west-1", "b": ["x-m1"], "c": {"m1": "eu-west-1"}, "n": 3})
        self.assertEqual(references["a"], ["AWS_REGION"])

    def test_a_value_cannot_change_the_toml_structure(self):
        hostile = props.Properties(None, {"V": 'x"\n[agentcore]\nregion = "evil'})
        resolved, _ = props.interpolate({"judge_profile": "${V}"}, hostile)
        self.assertEqual(resolved, {"judge_profile": 'x"\n[agentcore]\nregion = "evil'})

    def test_a_whole_reference_to_an_empty_property_means_unset(self):
        resolved, _ = props.interpolate({"profile": "${EMPTY}", "uri": "s3://${EMPTY}x"}, self.props)
        self.assertEqual(resolved, {"profile": None, "uri": "s3://x"})

    def test_an_undefined_reference_names_the_key_and_the_file(self):
        with self.assertRaisesRegex(props.PropertiesError, r"agentcore.region references \$\{NOPE\}.*deploy.properties"):
            props.interpolate({"agentcore": {"region": "${NOPE}"}}, self.props)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.properties = self.root / "deploy.properties"

    def resolve(self, toml, properties):
        self.config.write_text(toml)
        self.properties.write_text(properties)
        return resolve_config(self.config, self.properties)

    def test_one_property_fills_every_field_it_binds(self):
        config, report = self.resolve("", "AWS_PROFILE = p1\nAWS_REGION = eu-west-1\n"
                                          "ARTIFACT_S3_URI = s3://b/out/\nSERVICE_PORT = 9001\n")
        self.assertEqual((config.artifact_s3_profile, config.judge_profile, config.agentcore.profile),
                         ("p1", "p1", "p1"))
        self.assertEqual((config.artifact_s3_region, config.judge_region, config.agentcore.region),
                         ("eu-west-1",) * 3)
        self.assertEqual((config.artifact_s3_uri, config.port), ("s3://b/out/", 9001))
        self.assertEqual(report["fields"]["agentcore.profile"]["source"], "deploy.properties:AWS_PROFILE")

    def test_a_config_toml_literal_wins_and_is_reported(self):
        config, report = self.resolve('judge_profile = "literal"\n', "AWS_PROFILE = p1\n")
        self.assertEqual((config.judge_profile, config.artifact_s3_profile), ("literal", "p1"))
        self.assertEqual(report["overridden"], [{"field": "judge_profile", "property": "AWS_PROFILE",
                                                 "config_toml": "literal", "properties": "p1"}])

    def test_a_reference_in_config_toml_is_reported_as_such(self):
        config, report = self.resolve('[agentcore]\ntransfer_s3_uri = "s3://${S3_BUCKET}/x/"\n',
                                      "S3_BUCKET = b\nAGENTCORE_TRANSFER_S3_URI = s3://other/y/\n")
        self.assertEqual(config.agentcore.transfer_s3_uri, "s3://b/x/")
        self.assertIn("via ${S3_BUCKET}", report["fields"]["agentcore.transfer_s3_uri"]["source"])

    def test_defaults_remain_when_nothing_sets_a_field(self):
        config, _ = self.resolve("", "")
        self.assertIsNone(config.artifact_s3_uri)
        self.assertEqual(config.agentcore.execution_role_name, "CodeCouncilBedrockAgentCoreRuntimeRole")
        self.assertEqual(config.agentcore.inline_policy_name, "CodeCouncilAgentCoreRuntime")

    def test_harness_values_keep_the_built_in_harness_defaults(self):
        config, _ = self.resolve("", "CLAUDE_MODEL = c1\n")
        self.assertEqual(sorted(config.harnesses), ["claude", "codex", "kiro"])
        self.assertEqual(config.harnesses["claude"].model, "c1")
        self.assertEqual(config.harnesses["claude"].env, ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"])
        config, _ = self.resolve('[harnesses.claude]\nenv = []\nauto_approve = false\n'
                                 '[harnesses.codex]\n[harnesses.kiro]\n', "CLAUDE_MODEL = c1\n")
        self.assertEqual((config.harnesses["claude"].model, config.harnesses["claude"].env,
                          config.harnesses["claude"].auto_approve), ("c1", [], False))

    def test_relative_paths_resolve_beside_the_properties_file(self):
        config, _ = self.resolve("", "REPOSITORY_ROOTS = projects, /abs/two\n"
                                     "KIRO_HOME_TEMPLATE = .arena/credential-templates/kiro\n")
        base = self.properties.resolve().parent
        self.assertEqual(config.repository_roots, [str(base / "projects"), "/abs/two"])
        self.assertEqual(config.harnesses["kiro"].home_template,
                         str(base / ".arena/credential-templates/kiro"))

    def test_a_bad_integer_is_refused(self):
        with self.assertRaisesRegex(props.PropertiesError, "SERVICE_PORT must be an integer"):
            self.resolve("", "SERVICE_PORT = eighty\n")

    def test_a_relative_path_in_config_toml_equal_to_the_property_is_not_an_override(self):
        _, report = self.resolve('data_dir = ".arena"\n', "DATA_DIR = .arena\n")
        self.assertEqual(report["overridden"], [])
        _, report = self.resolve('data_dir = "elsewhere"\n', "DATA_DIR = .arena\n")
        self.assertEqual([o["field"] for o in report["overridden"]], ["data_dir"])

    def test_a_key_mentioned_only_in_a_comment_is_still_unused(self):
        _, report = self.resolve("", "# see ${STALE}\nSTALE = x\n")
        self.assertEqual(report["unused"], ["STALE"])

    def test_unknown_keys_nothing_refers_to_are_reported(self):
        _, report = self.resolve('judge_model = "${MY_MODEL}"\n',
                                 "MY_MODEL = m\nHELPER = h\nAWS_PROFLE = typo\nS3_BUCKET = b\n")
        self.assertEqual(report["unused"], ["AWS_PROFLE", "HELPER"])

    def test_without_a_properties_file_config_toml_loads_as_before(self):
        # Region fields are no longer silently defaulted to "us-east-1"; they are None
        # when not set via deploy.properties or an AWS_REGION env var. Patch the env
        # so the two sides are comparable: both get region from the toml literal only.
        toml = 'artifact_s3_uri = "s3://b/p/"\nport = 9\n[agentcore]\nregion = "eu-west-1"\n'
        self.config.write_text(toml)
        import tomllib
        with patch.dict(os.environ, {}, clear=False) as env_copy:
            env_copy.pop("AWS_REGION", None)
            os.environ.pop("AWS_REGION", None)
            self.assertEqual(load_config(self.config, self.root / "absent.properties").model_dump(),
                             Config(**tomllib.loads(toml)).model_dump())

    def test_the_default_location_is_beside_config_toml_unless_overridden(self):
        self.config.write_text("")
        self.properties.write_text("AWS_PROFILE = beside\n")
        override = self.root / "elsewhere.properties"
        override.write_text("AWS_PROFILE = override\n")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(props.PROPERTIES_ENV, None)
            self.assertEqual(load_config(self.config).judge_profile, "beside")
            os.environ[props.PROPERTIES_ENV] = str(override)
            self.assertEqual(load_config(self.config).judge_profile, "override")


class RenderTests(unittest.TestCase):
    def test_render_fills_set_and_commented_keys_and_keeps_everything_else(self):
        example = "# header\nAWS_PROFILE = YOUR_AWS_PROFILE\n# AWS_ACCOUNT_ID = 123\nKEEP = me\n"
        text = props.render(example, {"AWS_PROFILE": "p", "AWS_ACCOUNT_ID": "999", "NEW_KEY": "n"})
        self.assertEqual(text, "# header\nAWS_PROFILE = p\nAWS_ACCOUNT_ID = 999\nKEEP = me\n"
                               "\n# Additional values\nNEW_KEY = n\n")


class DriftTests(unittest.TestCase):
    def config(self, records, **agentcore):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        (Path(temp.name) / "agentcore-runtimes.json").write_text(json.dumps(records))
        return Config(runtime="agentcore", data_dir=temp.name,
                      agentcore={"transfer_s3_uri": "s3://b/xfer/", **agentcore})

    def test_cached_runtimes_from_another_region_account_prefix_or_bucket_are_flagged(self):
        from arena.cli import configuration_drift
        record = {"arn": "arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/codecouncil_claude-X",
                  "id": "codecouncil_claude-X", "bundle": "s3://old/xfer/bundles/claude/deployment_package.zip"}
        issues = configuration_drift(self.config({"claude": record}, region="eu-west-1",
                                                 runtime_name_prefix="team"),
                                     {"AWS_ACCOUNT_ID": "222222222222"})
        self.assertEqual(len(issues), 4, issues)
        for fragment in ("us-east-1", "account 111111111111", "AGENTCORE_RUNTIME_NAME_PREFIX", "outside"):
            self.assertTrue(any(fragment in issue for issue in issues), fragment)

    def test_matching_records_and_the_native_runtime_report_nothing(self):
        from arena.cli import configuration_drift
        record = {"arn": "arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/codecouncil_claude-X",
                  "id": "codecouncil_claude-X", "bundle": "s3://b/xfer/bundles/claude/deployment_package.zip"}
        # Region must match the ARN region; set it explicitly since it no longer defaults to us-east-1.
        self.assertEqual(configuration_drift(self.config({"claude": record}, region="us-east-1"),
                                             {"AWS_ACCOUNT_ID": "111111111111"}), [])
        self.assertEqual(configuration_drift(Config(runtime="native"), {}), [])


class ProvisioningNameTests(unittest.TestCase):
    def test_role_policy_and_tags_follow_the_configured_names(self):
        from arena.agentcore_provision import ensure_execution_role
        from arena.config import AgentCoreConfig
        cfg = AgentCoreConfig(transfer_s3_uri="s3://b/xfer/", execution_role_name="TeamRole",
                              inline_policy_name="TeamPolicy", application_tag="TeamApp")
        iam = Mock()
        iam.exceptions = SimpleNamespace(EntityAlreadyExistsException=type("Exists", (Exception,), {}))
        iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/TeamRole"}}
        with patch("arena.agentcore_provision.time.sleep"):
            ensure_execution_role(cfg, "1", iam, "us-east-1")
        created = iam.create_role.call_args.kwargs
        self.assertEqual((created["RoleName"], created["Tags"]), ("TeamRole", [{"Key": "Application", "Value": "TeamApp"}]))
        self.assertEqual(iam.put_role_policy.call_args.kwargs["PolicyName"], "TeamPolicy")


if __name__ == "__main__":
    unittest.main()
