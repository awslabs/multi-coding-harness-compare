# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests that deploy and discovery agree on runtime names for shared and per-user modes.

Covers:
  - runtime_name helper: shared, per-user, hyphen mapping, length validation, prefix validation
  - harness_image/deploy.py: _ensure_runtime uses the helper (shared and per-user)
  - arena/agentcore_sandbox.py: _resolve_runtime path 3 uses the helper (shared and per-user)
  - arena/preflight.py: agentcore check uses the helper and fails clearly without USER_NAMESPACE
  - arena/cli.py: configuration_drift uses the helper for per-user names
  - Shared mode: existing names mchc_{harness} are preserved (live install safety)
"""
import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from harness_image.iam import runtime_name


# ── runtime_name unit tests ───────────────────────────────────────────────────

class RuntimeNameSharedTests(unittest.TestCase):
    def test_shared_produces_prefix_harness(self):
        self.assertEqual(runtime_name("mchc", "claude", None), "mchc_claude")
        self.assertEqual(runtime_name("mchc", "codex", None), "mchc_codex")
        self.assertEqual(runtime_name("mchc", "kiro", None), "mchc_kiro")

    def test_shared_preserves_existing_live_names(self):
        # The live installation has exactly these three names; they must never change.
        for harness in ("claude", "codex", "kiro"):
            self.assertEqual(runtime_name("mchc", harness, None),
                             f"mchc_{harness}")

    def test_shared_no_trailing_underscore(self):
        name = runtime_name("pfx", "h", None)
        self.assertFalse(name.endswith("_"))


class RuntimeNamePerUserTests(unittest.TestCase):
    def test_per_user_appends_namespace(self):
        self.assertEqual(runtime_name("mchc", "claude", "alice"),
                         "mchc_claude_alice")

    def test_per_user_maps_hyphens_to_underscores(self):
        self.assertEqual(runtime_name("mchc", "claude", "my-team"),
                         "mchc_claude_my_team")

    def test_per_user_allows_underscores_in_namespace(self):
        self.assertEqual(runtime_name("mchc", "claude", "my_team"),
                         "mchc_claude_my_team")


class RuntimeNameValidationTests(unittest.TestCase):
    def test_valid_name_passes(self):
        # Should not raise
        runtime_name("abc", "d", None)

    def test_too_long_raises(self):
        # 48 chars is the limit; construct a prefix+harness that exceeds it
        long_ns = "x" * 40
        with self.assertRaises(ValueError) as cm:
            runtime_name("mchc", "claude", long_ns)
        self.assertIn("48", str(cm.exception))

    def test_exact_48_chars_is_valid(self):
        # 'a_b_' + 44 chars = 48 exactly
        ns = "x" * 44
        name = runtime_name("a", "b", ns)
        self.assertEqual(len(name), 48)

    def test_49_chars_raises(self):
        ns = "x" * 45
        with self.assertRaises(ValueError):
            runtime_name("a", "b", ns)

    def test_invalid_start_char_raises(self):
        with self.assertRaises(ValueError):
            runtime_name("1abc", "harness", None)

    def test_hyphens_in_prefix_would_raise(self):
        # Hyphens in prefix are NOT sanitised (only namespace hyphens are mapped).
        # Callers must use valid prefixes; this documents the contract.
        with self.assertRaises(ValueError):
            runtime_name("my-prefix", "claude", None)


# ── deploy: _ensure_runtime uses runtime_name ─────────────────────────────────

class EnsureRuntimeNamingTests(unittest.TestCase):
    """_ensure_runtime must call list_agent_runtimes and match on runtime_name(...) output."""

    def _make_cfg(self, isolation="shared", namespace=None):
        from harness_image.deploy import DeployConfig
        return DeployConfig(
            transfer_s3_uri="s3://test-bucket/test-prefix/",
            region="us-east-1",
            isolation=isolation,
            user_namespace=namespace,
        )

    def _make_control(self, existing_name=None):
        ctrl = Mock()
        if existing_name:
            ctrl.list_agent_runtimes.return_value = {
                "agentRuntimes": [{"agentRuntimeName": existing_name,
                                   "agentRuntimeId": f"{existing_name}-id",
                                   "agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-east-1:123:runtime/{existing_name}-id"}]
            }
            ctrl.update_agent_runtime.return_value = {"status": "UPDATING"}
        else:
            ctrl.list_agent_runtimes.return_value = {"agentRuntimes": []}
            ctrl.create_agent_runtime.return_value = {"agentRuntimeArn": "arn:fake"}
        return ctrl

    def test_shared_creates_with_correct_name(self):
        from harness_image.deploy import _ensure_runtime
        cfg = self._make_cfg()
        ctrl = self._make_control()
        _ensure_runtime("claude", cfg, "bundle/key", "arn:role", {}, ctrl)
        call_kwargs = ctrl.create_agent_runtime.call_args.kwargs
        self.assertEqual(call_kwargs["agentRuntimeName"], "mchc_claude")

    def test_per_user_creates_with_namespaced_name(self):
        from harness_image.deploy import _ensure_runtime
        cfg = self._make_cfg(isolation="per-user", namespace="alice")
        ctrl = self._make_control()
        _ensure_runtime("claude", cfg, "bundle/key", "arn:role", {}, ctrl)
        call_kwargs = ctrl.create_agent_runtime.call_args.kwargs
        self.assertEqual(call_kwargs["agentRuntimeName"], "mchc_claude_alice")

    def test_per_user_with_hyphen_namespace_sanitises_to_underscore(self):
        from harness_image.deploy import _ensure_runtime
        cfg = self._make_cfg(isolation="per-user", namespace="my-team")
        ctrl = self._make_control()
        _ensure_runtime("claude", cfg, "bundle/key", "arn:role", {}, ctrl)
        call_kwargs = ctrl.create_agent_runtime.call_args.kwargs
        self.assertEqual(call_kwargs["agentRuntimeName"], "mchc_claude_my_team")

    def test_shared_finds_existing_by_correct_name(self):
        from harness_image.deploy import _ensure_runtime
        cfg = self._make_cfg()
        ctrl = self._make_control(existing_name="mchc_claude")
        _ensure_runtime("claude", cfg, "bundle/key", "arn:role", {}, ctrl)
        # Should update, not create
        ctrl.update_agent_runtime.assert_called_once()
        ctrl.create_agent_runtime.assert_not_called()

    def test_per_user_finds_existing_by_namespaced_name(self):
        from harness_image.deploy import _ensure_runtime
        cfg = self._make_cfg(isolation="per-user", namespace="bob")
        ctrl = self._make_control(existing_name="mchc_claude_bob")
        _ensure_runtime("claude", cfg, "bundle/key", "arn:role", {}, ctrl)
        ctrl.update_agent_runtime.assert_called_once()
        ctrl.create_agent_runtime.assert_not_called()

    def test_per_user_does_not_match_shared_name(self):
        """Per-user deploy must not accidentally update a shared runtime."""
        from harness_image.deploy import _ensure_runtime
        cfg = self._make_cfg(isolation="per-user", namespace="carol")
        # Only the shared name exists in the account
        ctrl = self._make_control(existing_name="mchc_claude")
        _ensure_runtime("claude", cfg, "bundle/key", "arn:role", {}, ctrl)
        # Should create a new namespaced runtime, not update the shared one
        ctrl.create_agent_runtime.assert_called_once()
        created_name = ctrl.create_agent_runtime.call_args.kwargs["agentRuntimeName"]
        self.assertEqual(created_name, "mchc_claude_carol")


# ── agentcore_sandbox: _resolve_runtime path 3 uses runtime_name ─────────────

class AgentcoreSandboxDiscoveryTests(unittest.TestCase):
    """Path 3 in _resolve_runtime (list scan) must use the same name as deploy."""

    def setUp(self):
        # An empty data directory of its own: path 2 reads agentcore-runtimes.json from
        # config.root, and the default (.arena in the working directory) holds a deployed
        # checkout's real runtime records, which would answer before the list scan runs.
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        self.data_dir = data.name

    def _make_config(self, isolation="shared", namespace=None):
        from arena.config import AgentCoreConfig, Config
        ac = AgentCoreConfig(
            transfer_s3_uri="s3://test-bucket/test-prefix/",
            isolation=isolation,
        )
        return Config(runtime="agentcore", agentcore=ac, user_namespace=namespace,
                      data_dir=self.data_dir)

    def _make_ctrl(self, runtime_name_in_account):
        ctrl = Mock()
        ctrl.list_agent_runtimes.return_value = {
            "agentRuntimes": [{"agentRuntimeName": runtime_name_in_account,
                               "agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-east-1:123:runtime/x",
                               "agentRuntimeId": "x"}]
        }
        ctrl.get_agent_runtime.return_value = {"status": "READY"}
        return ctrl

    def _run(self, config, ctrl):
        from arena.agentcore_sandbox import AgentCore
        ac = AgentCore(config)
        with patch.object(ac, "_control_client", return_value=ctrl):
            return asyncio.run(ac._resolve_runtime("claude"))

    def test_shared_finds_shared_name(self):
        config = self._make_config()
        ctrl = self._make_ctrl("mchc_claude")
        arn, qualifier, info = self._run(config, ctrl)
        self.assertIn("arn:", arn)

    def test_per_user_finds_namespaced_name(self):
        config = self._make_config(isolation="per-user", namespace="alice")
        ctrl = self._make_ctrl("mchc_claude_alice")
        arn, qualifier, info = self._run(config, ctrl)
        self.assertIn("arn:", arn)

    def test_per_user_does_not_find_shared_name(self):
        """Per-user discovery must NOT accidentally adopt a shared runtime."""
        config = self._make_config(isolation="per-user", namespace="bob")
        ctrl = self._make_ctrl("mchc_claude")  # only shared exists
        with self.assertRaises(RuntimeError) as cm:
            self._run(config, ctrl)
        self.assertIn("No AgentCore runtime found", str(cm.exception))

    def test_shared_does_not_find_namespaced_name(self):
        """Shared discovery must NOT accidentally adopt a per-user runtime."""
        config = self._make_config()  # shared
        ctrl = self._make_ctrl("mchc_claude_alice")  # only per-user exists
        with self.assertRaises(RuntimeError) as cm:
            self._run(config, ctrl)
        self.assertIn("No AgentCore runtime found", str(cm.exception))


# ── preflight: agentcore check uses namespaced names and fails without namespace

class PreflightAgentcoreNamingTests(unittest.TestCase):
    def _make_config(self, isolation="shared", namespace=None):
        from arena.config import AgentCoreConfig, Config
        ac = AgentCoreConfig(
            transfer_s3_uri="s3://test-bucket/test-prefix/",
            region="us-east-1",
            isolation=isolation,
        )
        return Config(runtime="agentcore", agentcore=ac, user_namespace=namespace)

    def _make_session(self, runtime_names=()):
        session = Mock()
        ctrl = Mock()
        ctrl.list_agent_runtimes.return_value = {
            "agentRuntimes": [{"agentRuntimeName": n} for n in runtime_names]
        }
        s3 = Mock()
        s3.list_objects_v2.return_value = {"Contents": []}
        session.client.side_effect = lambda svc, **kw: ctrl if "agentcore" in svc else s3
        return session

    def _run_agentcore_check(self, config, session):
        from arena import preflight
        pf = preflight.Preflight.__new__(preflight.Preflight)
        pf.checks = []
        pf.config = config
        pf.values = {}
        pf.account = None
        pf._config_report = None
        pf.config_path = None
        pf.properties_path = None
        pf.offline = False
        pf.agentcore(session, None)
        return {c["id"]: c for c in pf.checks}

    def test_shared_expects_three_plain_names(self):
        config = self._make_config()
        session = self._make_session(["mchc_claude", "mchc_codex", "mchc_kiro"])
        checks = self._run_agentcore_check(config, session)
        self.assertEqual(checks["agentcore-runtimes"]["status"], "pass")

    def test_shared_reports_missing_names(self):
        config = self._make_config()
        session = self._make_session(["mchc_claude"])  # codex+kiro missing
        checks = self._run_agentcore_check(config, session)
        self.assertEqual(checks["agentcore-runtimes"]["status"], "fail")
        self.assertIn("mchc_codex", checks["agentcore-runtimes"]["detail"])

    def test_per_user_expects_namespaced_names(self):
        config = self._make_config(isolation="per-user", namespace="alice")
        session = self._make_session([
            "mchc_claude_alice",
            "mchc_codex_alice",
            "mchc_kiro_alice",
        ])
        checks = self._run_agentcore_check(config, session)
        self.assertEqual(checks["agentcore-runtimes"]["status"], "pass")

    def test_per_user_does_not_accept_shared_names(self):
        """Per-user preflight must NOT pass when only the shared runtimes exist."""
        config = self._make_config(isolation="per-user", namespace="alice")
        session = self._make_session(["mchc_claude", "mchc_codex", "mchc_kiro"])
        checks = self._run_agentcore_check(config, session)
        self.assertEqual(checks["agentcore-runtimes"]["status"], "fail")
        self.assertIn("_alice", checks["agentcore-runtimes"]["detail"])

    def test_per_user_without_namespace_fails_before_aws_call(self):
        """Deploying per-user without USER_NAMESPACE must fail at preflight, not silently use shared names."""
        config = self._make_config(isolation="per-user", namespace=None)
        session = self._make_session()
        checks = self._run_agentcore_check(config, session)
        self.assertEqual(checks["agentcore-runtimes"]["status"], "fail")
        # Must mention USER_NAMESPACE
        self.assertIn("USER_NAMESPACE", checks["agentcore-runtimes"]["detail"])
        # Must not have called AWS (no namespace = fail immediately)
        session.client.assert_not_called()

    def test_per_user_with_hyphen_namespace_expects_underscore_names(self):
        config = self._make_config(isolation="per-user", namespace="my-team")
        # AgentCore names would have underscore, not hyphen
        session = self._make_session([
            "mchc_claude_my_team",
            "mchc_codex_my_team",
            "mchc_kiro_my_team",
        ])
        checks = self._run_agentcore_check(config, session)
        self.assertEqual(checks["agentcore-runtimes"]["status"], "pass")


# ── cli: configuration_drift uses per-user runtime_name ──────────────────────

class CliDriftNamingTests(unittest.TestCase):
    def _make_config(self, isolation="shared", namespace=None):
        from arena.config import AgentCoreConfig, Config
        ac = AgentCoreConfig(
            transfer_s3_uri="s3://test-bucket/test-prefix/",
            region="us-east-1",
            isolation=isolation,
        )
        return Config(runtime="agentcore", agentcore=ac, user_namespace=namespace)

    def _run_drift(self, config, records, root):
        from arena.cli import configuration_drift
        (root / "agentcore-runtimes.json").write_text(json.dumps(records))
        # Monkey-patch config.root to point at our temp dir
        config = type("_C", (), {
            "runtime": config.runtime,
            "agentcore": config.agentcore,
            "user_namespace": config.user_namespace,
            "root": root,
        })()
        return configuration_drift(config, {"AWS_ACCOUNT_ID": "123456789012"})

    def test_shared_no_drift_for_correct_name(self):
        config = self._make_config()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            records = {"claude": {
                "arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/mchc_claude-x",
                "id": "mchc_claude-x",
                "bundle": "s3://test-bucket/test-prefix/bundle.zip",
            }}
            issues = self._run_drift(config, records, root)
        self.assertEqual(issues, [])

    def test_per_user_no_drift_for_namespaced_name(self):
        config = self._make_config(isolation="per-user", namespace="alice")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            records = {"claude": {
                "arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/mchc_claude_alice-x",
                "id": "mchc_claude_alice-x",
                "bundle": "s3://test-bucket/test-prefix/bundle.zip",
            }}
            issues = self._run_drift(config, records, root)
        self.assertEqual(issues, [])

    def test_per_user_drift_when_id_has_shared_name(self):
        """Drift must be flagged when the cached id was deployed in shared mode but config is per-user."""
        config = self._make_config(isolation="per-user", namespace="alice")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            records = {"claude": {
                "arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/mchc_claude-x",
                "id": "mchc_claude-x",  # shared name, not namespaced
                "bundle": "s3://test-bucket/test-prefix/bundle.zip",
            }}
            issues = self._run_drift(config, records, root)
        self.assertTrue(any("mchc_claude-x" in i for i in issues),
                        f"Expected drift issue for shared-named runtime, got: {issues}")


# ── agentcore_provision re-export ─────────────────────────────────────────────

class ProvisionReExportTests(unittest.TestCase):
    def test_runtime_name_importable_from_agentcore_provision(self):
        from arena.agentcore_provision import runtime_name as prov_runtime_name
        self.assertIs(prov_runtime_name, runtime_name)

    def test_shared_via_provision_shim(self):
        from arena.agentcore_provision import runtime_name as prov_runtime_name
        self.assertEqual(prov_runtime_name("mchc", "claude", None), "mchc_claude")

    def test_per_user_via_provision_shim(self):
        from arena.agentcore_provision import runtime_name as prov_runtime_name
        self.assertEqual(prov_runtime_name("mchc", "claude", "alice"),
                         "mchc_claude_alice")


if __name__ == "__main__":
    unittest.main()
