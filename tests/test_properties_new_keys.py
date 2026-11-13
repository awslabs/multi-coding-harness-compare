# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for new deploy.properties keys, typed parsing, Config fields, region resolution,
environment_literals report, harness_keys helper, and remove_tree move.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena import properties as props
from arena.config import (
    AgentCoreConfig, Config, HarnessConfig, HarnessImageConfig,
    HARNESS_TRUSTED_SOURCE_DEFAULTS, environment_literal_fields,
    resolve_config,
)
from arena.isolation import remove_tree


# ---------------------------------------------------------------------------
# remove_tree lives in isolation, re-exported from artifact_storage
# ---------------------------------------------------------------------------

class RemoveTreeLocationTests(unittest.TestCase):
    def test_remove_tree_is_defined_in_isolation_not_artifact_storage(self):
        import arena.isolation as iso_mod
        import arena.artifact_storage as as_mod
        # Must be defined in isolation (its __module__ is arena.isolation)
        self.assertIn("remove_tree", iso_mod.__dict__)
        self.assertEqual(iso_mod.remove_tree.__module__, "arena.isolation")
        # artifact_storage imports it (re-export), but does not define it there
        self.assertIn("remove_tree", as_mod.__dict__)
        self.assertEqual(as_mod.remove_tree.__module__, "arena.isolation")
        # Both names resolve to the same function object
        self.assertIs(iso_mod.remove_tree, as_mod.remove_tree)

    def test_remove_tree_deletes_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "x.txt"
            f.write_text("hi")
            remove_tree(f)
            self.assertFalse(f.exists())

    def test_remove_tree_deletes_a_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tree"
            (root / "sub").mkdir(parents=True)
            (root / "sub" / "file.txt").write_text("data")
            remove_tree(root)
            self.assertFalse(root.exists())

    def test_remove_tree_is_silent_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            remove_tree(Path(tmp) / "nonexistent")  # must not raise

    def test_agentcore_sandbox_still_imports_from_artifact_storage(self):
        """agentcore_sandbox continues to import remove_tree from artifact_storage
        until the agentcore sandbox is migrated; the re-export keeps that import working."""
        from arena.artifact_storage import remove_tree as art_rt
        from arena.agentcore_sandbox import remove_tree as acrt  # noqa: F401
        self.assertIs(art_rt, acrt)


# ---------------------------------------------------------------------------
# New typed parsing kinds
# ---------------------------------------------------------------------------

class NewKindParseTests(unittest.TestCase):
    def _parse(self, text):
        return props.parse(text)

    def test_harness_source_accepts_valid_values(self):
        for val in ("pinned", "installed", "latest"):
            parsed = self._parse(f"CLAUDE_HARNESS_SOURCE = {val}\n")
            self.assertEqual(parsed["CLAUDE_HARNESS_SOURCE"], val)

    def test_harness_source_rejects_unknown_value(self):
        # Typed validation fires when resolve_config calls bound_defaults/_typed
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            prp = Path(tmp) / "deploy.properties"
            cfg.write_text("")
            prp.write_text("CLAUDE_HARNESS_SOURCE = rolling\n")
            with self.assertRaisesRegex(props.PropertiesError, "pinned.*installed.*latest"):
                resolve_config(cfg, prp)

    def test_bool_accepts_true_false_via_resolve(self):
        # Typed bool conversion fires in bound_defaults/_typed
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            prp = Path(tmp) / "deploy.properties"
            cfg.write_text("")
            prp.write_text("CLAUDE_INHERIT_HOST_SETUP = true\nKIRO_INHERIT_HOST_SETUP = false\n")
            config, _ = resolve_config(cfg, prp)
            self.assertTrue(config.harnesses["claude"].inherit_host_setup)
            self.assertFalse(config.harnesses["kiro"].inherit_host_setup)

    def test_bool_rejects_bad_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            prp = Path(tmp) / "deploy.properties"
            cfg.write_text("")
            prp.write_text("HARNESS_IMAGE_WORKSTATION_CONFIG = yes\n")
            with self.assertRaisesRegex(props.PropertiesError, "true.*false"):
                resolve_config(cfg, prp)

    def test_isolation_accepts_valid_values_via_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            prp = Path(tmp) / "deploy.properties"
            cfg.write_text("")
            for val in ("shared", "per-user"):
                prp.write_text(f"AGENTCORE_ISOLATION = {val}\n")
                config, _ = resolve_config(cfg, prp)
                self.assertEqual(config.agentcore.isolation, val)

    def test_isolation_rejects_unknown_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            prp = Path(tmp) / "deploy.properties"
            cfg.write_text("")
            prp.write_text("AGENTCORE_ISOLATION = exclusive\n")
            with self.assertRaisesRegex(props.PropertiesError, "shared.*per-user"):
                resolve_config(cfg, prp)

    def test_comma_list_kind_via_resolve(self):
        # comma_list is typed in bound_defaults; verify the split+strip
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            prp = Path(tmp) / "deploy.properties"
            cfg.write_text("")
            prp.write_text("HARNESS_TRUSTED_SOURCES = a.example.com , b.example.com,c.example.com\n")
            config, _ = resolve_config(cfg, prp)
            self.assertEqual(config.harness_image.trusted_sources,
                             ["a.example.com", "b.example.com", "c.example.com"])

    def test_namespace_accepts_valid_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            prp = Path(tmp) / "deploy.properties"
            cfg.write_text("")
            for slug in ("alice", "bob42", "team-a", "a1b2c3"):
                prp.write_text(f"USER_NAMESPACE = {slug}\n")
                config, _ = resolve_config(cfg, prp)
                self.assertEqual(config.user_namespace, slug)

    def test_namespace_rejects_invalid_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            prp = Path(tmp) / "deploy.properties"
            cfg.write_text("")
            for bad in ("Alice", "bob_x", "-start"):
                with self.subTest(bad=bad), self.assertRaisesRegex(props.PropertiesError, r"\[a-z0-9-\]"):
                    prp.write_text(f"USER_NAMESPACE = {bad}\n")
                    resolve_config(cfg, prp)


# ---------------------------------------------------------------------------
# New KEYS bind to Config fields
# ---------------------------------------------------------------------------

class NewKeyBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.config.write_text("")

    def resolve(self, props_text):
        p = self.root / "deploy.properties"
        p.write_text(props_text)
        return resolve_config(self.config, p)

    # Image build keys
    def test_harness_source_fills_harness_config(self):
        cfg, _ = self.resolve("CLAUDE_HARNESS_SOURCE = installed\nCODEX_HARNESS_SOURCE = latest\n")
        self.assertEqual(cfg.harnesses["claude"].harness_source, "installed")
        self.assertEqual(cfg.harnesses["codex"].harness_source, "latest")
        self.assertIsNone(cfg.harnesses["kiro"].harness_source)  # default

    def test_harness_version_fills_harness_config(self):
        cfg, _ = self.resolve("CLAUDE_HARNESS_VERSION = 1.2.3\nKIRO_HARNESS_VERSION = 0.9.0\n")
        self.assertEqual(cfg.harnesses["claude"].harness_version, "1.2.3")
        self.assertEqual(cfg.harnesses["kiro"].harness_version, "0.9.0")

    def test_harness_trusted_sources_fills_harness_image(self):
        cfg, _ = self.resolve("HARNESS_TRUSTED_SOURCES = pypi.org,registry.npmjs.org\n")
        self.assertEqual(cfg.harness_image.trusted_sources, ["pypi.org", "registry.npmjs.org"])

    def test_kiro_download_url_fills_harness_image(self):
        url = "https://desktop-release.q.us-east-1.amazonaws.com/kiro/{version}/{arch}.tar.gz"
        cfg, _ = self.resolve(f"KIRO_DOWNLOAD_URL = {url}\n")
        self.assertEqual(cfg.harness_image.kiro_download_url, url)

    def test_workstation_config_bool_fills_harness_image(self):
        cfg, _ = self.resolve("HARNESS_IMAGE_WORKSTATION_CONFIG = true\n")
        self.assertTrue(cfg.harness_image.workstation_config)

    # Harness config keys
    def test_config_dir_fills_harness_config(self):
        cfg, _ = self.resolve(f"CLAUDE_CONFIG_DIR = {self.root}\n")
        self.assertEqual(cfg.harnesses["claude"].config_dir, str(self.root))

    def test_config_include_exclude_fills_harness_config(self):
        cfg, _ = self.resolve(
            "CLAUDE_CONFIG_INCLUDE = plugins/,custom/\n"
            "CODEX_CONFIG_EXCLUDE = agents/,mcp:big-server\n"
        )
        self.assertEqual(cfg.harnesses["claude"].config_include, ["plugins/", "custom/"])
        self.assertEqual(cfg.harnesses["codex"].config_exclude, ["agents/", "mcp:big-server"])

    def test_skills_dirs_fills_harness_config(self):
        cfg, _ = self.resolve(f"CLAUDE_SKILLS_DIRS = {self.root}\n")
        self.assertEqual(cfg.harnesses["claude"].skills_dirs, [str(self.root)])

    def test_steering_files_fills_harness_config(self):
        cfg, _ = self.resolve(f"KIRO_STEERING_FILES = {self.root}\n")
        self.assertEqual(cfg.harnesses["kiro"].steering_files, [str(self.root)])

    def test_mcp_config_fills_harness_config(self):
        f = self.root / "mcp.json"
        f.write_text("{}")
        cfg, _ = self.resolve(f"CLAUDE_MCP_CONFIG = {f}\n")
        self.assertEqual(cfg.harnesses["claude"].mcp_config, str(f))

    def test_config_s3_uri_fills_harness_config(self):
        cfg, _ = self.resolve("CLAUDE_CONFIG_S3_URI = s3://bucket/prefix/\n")
        self.assertEqual(cfg.harnesses["claude"].config_s3_uri, "s3://bucket/prefix/")

    def test_inherit_host_setup_bool_fills_harness_config(self):
        cfg, _ = self.resolve("CLAUDE_INHERIT_HOST_SETUP = true\n")
        self.assertTrue(cfg.harnesses["claude"].inherit_host_setup)

    def test_env_passthrough_fills_harness_env(self):
        cfg, _ = self.resolve("CLAUDE_ENV_PASSTHROUGH = MY_VAR,OTHER_VAR\n")
        self.assertEqual(cfg.harnesses["claude"].env, ["MY_VAR", "OTHER_VAR"])

    def test_runtime_arn_fills_agentcore_runtimes(self):
        arn = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x"
        cfg, _ = self.resolve(f"CLAUDE_RUNTIME_ARN = {arn}\n")
        self.assertEqual(cfg.agentcore.runtimes["claude"].arn, arn)

    # Other keys
    def test_codex_provider_fills_harness_config(self):
        cfg, _ = self.resolve("CODEX_PROVIDER = amazon-bedrock-runtime\n")
        self.assertEqual(cfg.harnesses["codex"].provider, "amazon-bedrock-runtime")

    def test_aws_config_file_fills_config(self):
        f = self.root / "aws_config"
        f.write_text("")
        cfg, _ = self.resolve(f"AWS_CONFIG_FILE = {f}\n")
        self.assertEqual(cfg.aws_config_file, str(f))

    def test_aws_shared_creds_file_fills_config(self):
        f = self.root / "aws_creds"
        f.write_text("")
        cfg, _ = self.resolve(f"AWS_SHARED_CREDS_FILE = {f}\n")
        self.assertEqual(cfg.aws_shared_creds_file, str(f))

    def test_user_namespace_fills_config(self):
        cfg, _ = self.resolve("USER_NAMESPACE = alice\n")
        self.assertEqual(cfg.user_namespace, "alice")

    def test_agentcore_isolation_fills_agentcore_config(self):
        cfg, _ = self.resolve("AGENTCORE_ISOLATION = per-user\n")
        self.assertEqual(cfg.agentcore.isolation, "per-user")

    def test_agentcore_transfer_kms_key_arn_fills_agentcore_config(self):
        arn = "arn:aws:kms:us-east-1:1:key/abc"
        cfg, _ = self.resolve(f"AGENTCORE_TRANSFER_KMS_KEY_ARN = {arn}\n")
        self.assertEqual(cfg.agentcore.transfer_kms_key_arn, arn)

    def test_agentcore_transfer_kms_key_arn_is_not_blocked_by_secret_guard(self):
        # KEY_ARN must not trip the SECRET guard (which matches ACCESS_KEY, not KEY)
        arn = "arn:aws:kms:us-east-1:1:key/abc"
        parsed = props.parse(f"AGENTCORE_TRANSFER_KMS_KEY_ARN = {arn}\n")
        self.assertEqual(parsed["AGENTCORE_TRANSFER_KMS_KEY_ARN"], arn)

    def test_aws_creds_file_keys_are_not_blocked_by_secret_guard(self):
        # CREDS is not CREDENTIAL; these keys must pass the guard
        parsed = props.parse("AWS_SHARED_CREDS_FILE = /path/to/file\n")
        self.assertEqual(parsed["AWS_SHARED_CREDS_FILE"], "/path/to/file")


# ---------------------------------------------------------------------------
# Region resolution: None default, env var fallback
# ---------------------------------------------------------------------------

class RegionResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.config.write_text("")

    def test_region_defaults_to_none_when_not_set(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AWS_REGION", None)
            cfg = Config()
            self.assertIsNone(cfg.artifact_s3_region)
            self.assertIsNone(cfg.judge_region)
            self.assertIsNone(cfg.agentcore.region)

    def test_region_filled_from_properties_aws_region(self):
        p = self.root / "deploy.properties"
        p.write_text("AWS_REGION = ap-southeast-1\n")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AWS_REGION", None)
            cfg, rep = resolve_config(self.config, p)
        self.assertEqual(cfg.artifact_s3_region, "ap-southeast-1")
        self.assertEqual(cfg.judge_region, "ap-southeast-1")
        self.assertEqual(cfg.agentcore.region, "ap-southeast-1")

    def test_region_filled_from_env_when_not_in_properties(self):
        p = self.root / "deploy.properties"
        p.write_text("")
        with patch.dict(os.environ, {"AWS_REGION": "eu-central-1"}):
            cfg, rep = resolve_config(self.config, p)
        self.assertEqual(cfg.artifact_s3_region, "eu-central-1")
        self.assertEqual(cfg.judge_region, "eu-central-1")
        self.assertEqual(cfg.agentcore.region, "eu-central-1")
        # Report shows env-sourced entries
        for dotted in ("artifact_s3_region", "judge_region", "agentcore.region"):
            self.assertIn("env:AWS_REGION", rep["fields"][dotted]["source"])

    def test_properties_region_takes_precedence_over_env(self):
        p = self.root / "deploy.properties"
        p.write_text("AWS_REGION = us-east-2\n")
        with patch.dict(os.environ, {"AWS_REGION": "us-west-1"}):
            cfg, _ = resolve_config(self.config, p)
        self.assertEqual(cfg.artifact_s3_region, "us-east-2")

    def test_config_toml_literal_takes_precedence_over_env(self):
        self.config.write_text('[agentcore]\nregion = "sa-east-1"\n')
        p = self.root / "deploy.properties"
        p.write_text("")
        with patch.dict(os.environ, {"AWS_REGION": "us-west-2"}):
            cfg, _ = resolve_config(self.config, p)
        self.assertEqual(cfg.agentcore.region, "sa-east-1")

    def test_region_none_when_env_absent_and_no_properties(self):
        p = self.root / "deploy.properties"
        p.write_text("")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AWS_REGION", None)
            cfg, _ = resolve_config(self.config, p)
        self.assertIsNone(cfg.artifact_s3_region)
        self.assertIsNone(cfg.judge_region)
        self.assertIsNone(cfg.agentcore.region)


# ---------------------------------------------------------------------------
# environment_literals report
# ---------------------------------------------------------------------------

class EnvironmentLiteralsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"

    def resolve(self, toml_text, props_text=""):
        self.config.write_text(toml_text)
        p = self.root / "deploy.properties"
        p.write_text(props_text)
        return resolve_config(self.config, p)

    def test_literal_for_key_bound_field_is_reported(self):
        # judge_profile is bound by AWS_PROFILE; setting it literally in config.toml is an env literal
        cfg, rep = self.resolve('judge_profile = "hardcoded-profile"\n', "AWS_PROFILE = p1\n")
        lits = environment_literal_fields(rep)
        fields = [e["field"] for e in lits]
        self.assertIn("judge_profile", fields)

    def test_reference_in_config_toml_is_not_an_environment_literal(self):
        # ${AWS_PROFILE} in config.toml is not a literal — it uses the property
        cfg, rep = self.resolve('judge_profile = "${AWS_PROFILE}"\n', "AWS_PROFILE = p1\n")
        lits = environment_literal_fields(rep)
        fields = [e["field"] for e in lits]
        self.assertNotIn("judge_profile", fields)

    def test_no_literals_produces_empty_list(self):
        _, rep = self.resolve("", "AWS_PROFILE = p1\n")
        self.assertEqual(environment_literal_fields(rep), [])

    def test_multiple_literals_all_reported(self):
        _, rep = self.resolve(
            'judge_profile = "p"\nartifact_s3_profile = "q"\n',
            "AWS_PROFILE = p1\n"
        )
        fields = [e["field"] for e in environment_literal_fields(rep)]
        self.assertIn("judge_profile", fields)
        self.assertIn("artifact_s3_profile", fields)

    def test_report_entry_contains_field_property_and_value(self):
        _, rep = self.resolve('judge_profile = "myprofile"\n', "AWS_PROFILE = other\n")
        entry = next(e for e in environment_literal_fields(rep) if e["field"] == "judge_profile")
        self.assertEqual(entry["property"], "AWS_PROFILE")
        self.assertEqual(entry["value"], "myprofile")

    def test_literal_reported_when_deploy_properties_key_absent(self):
        # When deploy.properties is EMPTY (AWS_PROFILE not set at all),
        # a plain literal artifact_s3_profile in config.toml must still be reported.
        _, rep = self.resolve('artifact_s3_profile = "hardcoded"\n', "")
        lits = environment_literal_fields(rep)
        fields = [e["field"] for e in lits]
        self.assertIn(
            "artifact_s3_profile", fields,
            "environment_literals must include fields with literals even when "
            "the corresponding deploy.properties key is absent",
        )

    def test_reference_in_config_toml_not_reported_even_when_key_absent(self):
        # A ${KEY} reference in config.toml when deploy.properties HAS the key
        # should not appear as an environment_literal (covered by other tests).
        # When deploy.properties doesn't have the key, the literal check still
        # works correctly for plain values (not references).
        # Test: even when AWS_PROFILE IS present, a reference is NOT an env_literal.
        _, rep = self.resolve('artifact_s3_profile = "${AWS_PROFILE}"\n', "AWS_PROFILE = dev\n")
        fields = [e["field"] for e in environment_literal_fields(rep)]
        self.assertNotIn("artifact_s3_profile", fields)


# ---------------------------------------------------------------------------
# MCHC_CODEX_PROVIDER env var fallback
# ---------------------------------------------------------------------------

class CodexProviderEnvFallbackTests(unittest.TestCase):
    """MCHC_CODEX_PROVIDER must be honoured as a fallback when CODEX_PROVIDER is unset."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.config.write_text("")
        self.props = self.root / "deploy.properties"
        self.props.write_text("")

    def _resolve(self, props_text="", env_extra=None):
        self.props.write_text(props_text)
        env = {k: v for k, v in os.environ.items() if k != "MCHC_CODEX_PROVIDER"}
        if env_extra:
            env.update(env_extra)
        with patch.dict(os.environ, env, clear=True):
            return resolve_config(self.config, self.props)

    def test_env_var_used_when_codex_provider_unset_in_properties(self):
        cfg, rep = self._resolve(
            props_text="",
            env_extra={"MCHC_CODEX_PROVIDER": "amazon-bedrock-runtime"},
        )
        self.assertEqual(cfg.harnesses["codex"].provider, "amazon-bedrock-runtime",
                         "MCHC_CODEX_PROVIDER env var must fill harnesses.codex.provider")

    def test_env_var_source_recorded_in_report(self):
        _, rep = self._resolve(
            props_text="",
            env_extra={"MCHC_CODEX_PROVIDER": "amazon-bedrock-runtime"},
        )
        source = rep["fields"].get("harnesses.codex.provider", {}).get("source", "")
        self.assertIn("MCHC_CODEX_PROVIDER", source,
                      "Report must note env:MCHC_CODEX_PROVIDER as the source")

    def test_deploy_properties_codex_provider_takes_precedence_over_env_var(self):
        # CODEX_PROVIDER in deploy.properties must win over the legacy env var.
        cfg, _ = self._resolve(
            props_text="CODEX_PROVIDER = amazon-bedrock\n",
            env_extra={"MCHC_CODEX_PROVIDER": "amazon-bedrock-runtime"},
        )
        self.assertEqual(cfg.harnesses["codex"].provider, "amazon-bedrock",
                         "CODEX_PROVIDER in deploy.properties must override MCHC_CODEX_PROVIDER")

    def test_config_toml_literal_takes_precedence_over_env_var(self):
        # A literal in config.toml must win over the env var fallback.
        self.config.write_text('[harnesses.codex]\nprovider = "amazon-bedrock"\n')
        cfg, _ = self._resolve(
            props_text="",
            env_extra={"MCHC_CODEX_PROVIDER": "amazon-bedrock-runtime"},
        )
        self.assertEqual(cfg.harnesses["codex"].provider, "amazon-bedrock",
                         "config.toml literal for provider must override MCHC_CODEX_PROVIDER")

    def test_provider_none_when_neither_set(self):
        cfg, _ = self._resolve(props_text="", env_extra={})
        self.assertIsNone(cfg.harnesses["codex"].provider,
                          "provider must remain None when neither CODEX_PROVIDER nor "
                          "MCHC_CODEX_PROVIDER is set")


# ---------------------------------------------------------------------------
# harness_keys helper
# ---------------------------------------------------------------------------

class HarnessKeysTests(unittest.TestCase):
    def test_harness_keys_returns_only_that_harness_prefix(self):
        from arena.properties import harness_keys
        for harness in ("claude", "codex", "kiro"):
            keys = harness_keys(harness)
            prefix = harness.upper() + "_"
            for k in keys:
                self.assertTrue(k.name.startswith(prefix), k.name)

    def test_harness_keys_returns_known_keys(self):
        from arena.properties import harness_keys, KNOWN
        for harness in ("claude", "codex", "kiro"):
            for k in harness_keys(harness):
                self.assertIn(k.name, KNOWN)

    def test_all_three_harnesses_have_harness_source_and_version(self):
        from arena.properties import harness_keys
        for h in ("claude", "codex", "kiro"):
            names = {k.name for k in harness_keys(h)}
            self.assertIn(f"{h.upper()}_HARNESS_SOURCE", names)
            self.assertIn(f"{h.upper()}_HARNESS_VERSION", names)

    def test_all_three_harnesses_have_config_keys(self):
        from arena.properties import harness_keys
        for h in ("claude", "codex", "kiro"):
            names = {k.name for k in harness_keys(h)}
            for suffix in ("CONFIG_DIR", "CONFIG_INCLUDE", "CONFIG_EXCLUDE",
                           "SKILLS_DIRS", "STEERING_FILES", "MCP_CONFIG",
                           "CONFIG_S3_URI", "INHERIT_HOST_SETUP", "ENV_PASSTHROUGH",
                           "RUNTIME_ARN"):
                self.assertIn(f"{h.upper()}_{suffix}", names, f"{h}: {suffix} missing")


# ---------------------------------------------------------------------------
# HarnessImageConfig defaults
# ---------------------------------------------------------------------------

class HarnessImageConfigTests(unittest.TestCase):
    def test_default_trusted_sources_match_documented_constant(self):
        cfg = HarnessImageConfig()
        self.assertEqual(cfg.trusted_sources, HARNESS_TRUSTED_SOURCE_DEFAULTS)

    def test_workstation_config_defaults_false(self):
        self.assertFalse(HarnessImageConfig().workstation_config)

    def test_kiro_download_url_defaults_none(self):
        self.assertIsNone(HarnessImageConfig().kiro_download_url)

    def test_harness_image_on_config_has_correct_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.harness_image.trusted_sources, HARNESS_TRUSTED_SOURCE_DEFAULTS)
        self.assertFalse(cfg.harness_image.workstation_config)


# ---------------------------------------------------------------------------
# New AgentCoreConfig fields
# ---------------------------------------------------------------------------

class AgentCoreNewFieldsTests(unittest.TestCase):
    def test_isolation_defaults_shared(self):
        self.assertEqual(AgentCoreConfig().isolation, "shared")

    def test_transfer_kms_key_arn_defaults_none(self):
        self.assertIsNone(AgentCoreConfig().transfer_kms_key_arn)

    def test_region_defaults_none(self):
        # Intentional change: no longer silently "us-east-1".
        self.assertIsNone(AgentCoreConfig().region)


# ---------------------------------------------------------------------------
# New Config fields
# ---------------------------------------------------------------------------

class ConfigNewFieldsTests(unittest.TestCase):
    def test_user_namespace_defaults_none(self):
        self.assertIsNone(Config().user_namespace)

    def test_aws_config_file_defaults_none(self):
        self.assertIsNone(Config().aws_config_file)

    def test_aws_shared_creds_file_defaults_none(self):
        self.assertIsNone(Config().aws_shared_creds_file)

    def test_artifact_s3_region_defaults_none(self):
        # Intentional change: no longer silently "us-east-1".
        self.assertIsNone(Config().artifact_s3_region)

    def test_judge_region_defaults_none(self):
        # Intentional change: no longer silently "us-east-1".
        self.assertIsNone(Config().judge_region)

    def test_harness_config_new_fields_have_safe_defaults(self):
        hc = HarnessConfig()
        self.assertIsNone(hc.harness_source)
        self.assertIsNone(hc.harness_version)
        self.assertIsNone(hc.config_dir)
        self.assertEqual(hc.config_include, [])
        self.assertEqual(hc.config_exclude, [])
        self.assertEqual(hc.skills_dirs, [])
        self.assertEqual(hc.steering_files, [])
        self.assertIsNone(hc.mcp_config)
        self.assertIsNone(hc.config_s3_uri)
        self.assertIsNone(hc.provider)


# ---------------------------------------------------------------------------
# Backward compat: environment_literals must not flag same-as-dp or
# behavioral (bool/comma_list) fields
# ---------------------------------------------------------------------------

class EnvironmentLiteralsBackwardCompatTests(unittest.TestCase):
    """Fixes for config-toml-environment-literals false positives."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"

    def resolve(self, toml_text, props_text=""):
        self.config.write_text(toml_text)
        p = self.root / "deploy.properties"
        p.write_text(props_text)
        return resolve_config(self.config, p)

    def test_first_loop_skips_when_literal_equals_dp_value(self):
        """A config.toml literal that echoes the deploy.properties value must not be flagged."""
        # agentcore.runtime_name_prefix is bound to AGENTCORE_RUNTIME_NAME_PREFIX.
        # When both have the same value, config-toml-environment-literals must not fire.
        _, rep = self.resolve(
            '[agentcore]\nruntime_name_prefix = "mchc"\n',
            "AGENTCORE_RUNTIME_NAME_PREFIX = mchc\n",
        )
        fields = [e["field"] for e in environment_literal_fields(rep)]
        self.assertNotIn(
            "agentcore.runtime_name_prefix", fields,
            "A config.toml entry that echoes the deploy.properties value must not be flagged",
        )

    def test_first_loop_still_flags_when_literal_differs_from_dp_value(self):
        """A config.toml literal that diverges from deploy.properties must still be flagged."""
        _, rep = self.resolve(
            '[agentcore]\nruntime_name_prefix = "other"\n',
            "AGENTCORE_RUNTIME_NAME_PREFIX = mchc\n",
        )
        fields = [e["field"] for e in environment_literal_fields(rep)]
        self.assertIn("agentcore.runtime_name_prefix", fields)

    def test_second_loop_skips_comma_list_kind_env_passthrough(self):
        """harnesses.claude.env customised in config.toml must not be flagged (comma_list kind)."""
        _, rep = self.resolve(
            '[harnesses.claude]\nenv = ["MY_CUSTOM_KEY"]\n',
            "",
        )
        fields = [e["field"] for e in environment_literal_fields(rep)]
        self.assertNotIn(
            "harnesses.claude.env", fields,
            "harnesses.claude.env is a behavioral (comma_list) field and must not be flagged",
        )

    def test_second_loop_skips_bool_kind_inherit_host_setup(self):
        """harnesses.claude.inherit_host_setup = true in config.toml must not be flagged."""
        _, rep = self.resolve(
            '[harnesses.claude]\ninherit_host_setup = true\n',
            "",
        )
        fields = [e["field"] for e in environment_literal_fields(rep)]
        self.assertNotIn(
            "harnesses.claude.inherit_host_setup", fields,
            "inherit_host_setup is a behavioral (bool) field and must not be flagged",
        )

    def test_second_loop_still_flags_non_behavioral_literal(self):
        """A non-behavioral literal (e.g. artifact_s3_profile) absent from deploy.properties
        must still be flagged by the second loop."""
        _, rep = self.resolve('artifact_s3_profile = "myprofile"\n', "")
        fields = [e["field"] for e in environment_literal_fields(rep)]
        self.assertIn("artifact_s3_profile", fields)


# ---------------------------------------------------------------------------
# deployer_policy should emit a specific role ARN in docs/iam/ output
# ---------------------------------------------------------------------------

class DeployerPolicyRoleArnTests(unittest.TestCase):
    """The generated deployer-policy.json must use a specific role ARN, not role/*."""

    def test_print_policy_deployer_uses_specific_role_arn(self):
        """print_policy for 'deployer' must call deployer_policy with a role_name so
        the generated document does not use the wildcard role/* resource."""
        import inspect
        import harness_image.iam as iam_mod
        source = inspect.getsource(iam_mod.print_policy)
        self.assertIn(
            "role_name=",
            source,
            "print_policy must pass role_name= to deployer_policy to avoid role/* wildcard",
        )

    def test_deployer_policy_with_role_name_produces_scoped_arn(self):
        from harness_image.iam import deployer_policy
        doc = deployer_policy("123456789012", "us-east-1", "b", "p",
                              role_name="MyRole", agentcore_prefix="mchc")
        resources = [
            s["Resource"] for s in doc["Statement"]
            if isinstance(s.get("Resource"), str) and "role" in s["Resource"]
        ]
        for r in resources:
            self.assertNotIn("role/*", r,
                             f"resource {r!r} must not be role/* when role_name is supplied")
            self.assertIn("MyRole", r)


# ---------------------------------------------------------------------------
# HARNESS_CONFIG_DEFAULT_CHOICE: parsing and validation
# ---------------------------------------------------------------------------

class HarnessConfigDefaultChoiceTests(unittest.TestCase):
    """Tests for the HARNESS_CONFIG_DEFAULT_CHOICE key."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.config.write_text("")

    def resolve(self, props_text):
        p = self.root / "deploy.properties"
        p.write_text(props_text)
        return resolve_config(self.config, p)

    def test_valid_values_accepted(self):
        for val in ("default", "overrides", "none"):
            with self.subTest(val=val):
                cfg, _ = self.resolve(f"HARNESS_CONFIG_DEFAULT_CHOICE = {val}\n")
                self.assertEqual(cfg.harness_config_default_choice, val)

    def test_invalid_value_raises_error(self):
        with self.assertRaisesRegex(props.PropertiesError, "default.*overrides.*none"):
            self.resolve("HARNESS_CONFIG_DEFAULT_CHOICE = all\n")

    def test_key_in_known_keys(self):
        from arena.properties import KNOWN
        self.assertIn("HARNESS_CONFIG_DEFAULT_CHOICE", KNOWN)

    def test_key_fills_harness_config_default_choice_field(self):
        cfg, _ = self.resolve("HARNESS_CONFIG_DEFAULT_CHOICE = overrides\n")
        self.assertEqual(cfg.harness_config_default_choice, "overrides")

    def test_unset_key_leaves_field_none(self):
        cfg, _ = self.resolve("")
        self.assertIsNone(cfg.harness_config_default_choice)

    def test_default_configuration_choice_agentcore_unset(self):
        """default_configuration_choice returns 'overrides' for agentcore when unset."""
        # Can't create an agentcore Config without transfer_s3_uri, so test the logic
        # directly on a Config with the field.
        cfg = Config()
        # native default
        self.assertEqual(cfg.default_configuration_choice, "default")

    def test_config_default_choice_field_is_documented(self):
        """harness_config_default_choice must appear in Config model fields."""
        self.assertIn("harness_config_default_choice", Config.model_fields)


# ---------------------------------------------------------------------------
# Codex preflight: agentcore with overrides choice needs template
# ---------------------------------------------------------------------------

class CodexPreflightDefaultChoiceTests(unittest.TestCase):
    """Codex provider preflight respects the effective configuration choice."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _run_preflight(self, answers, offline=True):
        from arena import preflight
        props_path = self.root / "deploy.properties"
        config_path = self.root / "config.toml"
        preflight.inputs(props_path, list(answers))
        p = preflight.Preflight(config_path, props_path, offline=offline)
        from unittest.mock import patch
        with patch.object(preflight.Preflight, "host"), patch("boto3.Session"):
            report = p.run()
        return {c["id"]: c for c in report["checks"]}

    def test_agentcore_overrides_without_template_fails(self):
        """agentcore + effective choice 'overrides' + no template must fail codex-provider-config."""
        answers = [
            "RUNTIME=agentcore", "KIRO_AUTH=copy-host-login", "AWS_PROFILE=dev",
            "AWS_REGION=us-east-1", "S3_BUCKET=bucket",
            "REPOSITORY_ROOTS=.", "DATA_DIR=data",
            # No CODEX_HOME_TEMPLATE, no CODEX_CONFIG_S3_URI
        ]
        checks = self._run_preflight(answers)
        if "codex-provider-config" in checks:
            c = checks["codex-provider-config"]
            self.assertEqual(c["status"], "fail")
            self.assertEqual(c["gate"], "assistant")

    def test_agentcore_default_choice_with_provider_config_passes(self):
        """agentcore + HARNESS_CONFIG_DEFAULT_CHOICE=default + valid workstation config passes."""
        codex_home = self.root / "codex-home"
        (codex_home / ".codex").mkdir(parents=True)
        (codex_home / ".codex" / "config.toml").write_text(
            'model_provider = "amazon-bedrock-runtime"\n'
            '[model_providers.amazon-bedrock-runtime.aws]\nregion = "us-east-1"\n'
        )
        answers = [
            "RUNTIME=agentcore", "KIRO_AUTH=copy-host-login", "AWS_PROFILE=dev",
            "AWS_REGION=us-east-1", "S3_BUCKET=bucket",
            "REPOSITORY_ROOTS=.", "DATA_DIR=data",
            "HARNESS_CONFIG_DEFAULT_CHOICE=default",
            f"CODEX_CONFIG_DIR={codex_home / '.codex'}",
        ]
        checks = self._run_preflight(answers)
        if "codex-provider-config" in checks:
            c = checks["codex-provider-config"]
            self.assertEqual(c["status"], "pass", c.get("detail", ""))


if __name__ == "__main__":
    unittest.main()
