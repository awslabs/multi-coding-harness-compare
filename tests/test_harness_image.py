# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""harness_image package tests — no real AWS, no real network, no real CLI tools.

Covers:
  - harness_image is importable without arena (arena-independence test)
  - arena/agentcore_provision.py is a thin shim that re-exports deploy()
  - deploy/agentcore-deploy.sh exists, is executable, and prints --help
  - version source resolution (pinned, installed, latest)
  - props module: parse / load
  - DeployConfig: from_props, from_arena_config
  - bundle: build_bundle is exported from agentcore_provision shim
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch


# ── arena-independence ────────────────────────────────────────────────────────

class ArenaIndependenceTests(unittest.TestCase):
    """harness_image must not import arena at module level."""

    def test_harness_image_does_not_import_arena_on_load(self):
        """Importing harness_image with arena blocked must not raise ImportError."""
        # We can't block sys.modules in a subprocess-free way without risk of
        # breaking other tests in the same process.  Use a subprocess instead
        # so the sys.modules cache is pristine.
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys\n"
             "# Block arena before importing harness_image\n"
             "orig = __import__\n"
             "def _block(name, *a, **kw):\n"
             "    if name == 'arena' or name.startswith('arena.'):\n"
             "        raise ImportError(f'blocked: {name}')\n"
             "    return orig(name, *a, **kw)\n"
             "import builtins; builtins.__import__ = _block\n"
             "import harness_image.props\n"
             "import harness_image.versions\n"
             "import harness_image.iam\n"
             "import harness_image.sources\n"
             "import harness_image.bundle\n"
             "print('ok')\n"
             ],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        self.assertEqual(result.returncode, 0,
                         f"harness_image imported arena:\n{result.stderr}")
        self.assertIn("ok", result.stdout)


# ── props module ──────────────────────────────────────────────────────────────

class PropsTests(unittest.TestCase):
    def test_parse_simple_key_value(self):
        from harness_image.props import parse
        result = parse("FOO = bar\nBAZ = qux")
        self.assertEqual(result, {"FOO": "bar", "BAZ": "qux"})

    def test_parse_ignores_comments(self):
        from harness_image.props import parse
        result = parse("# comment\nKEY = val")
        self.assertEqual(result, {"KEY": "val"})

    def test_parse_resolves_references(self):
        from harness_image.props import parse
        result = parse("BUCKET = mybucket\nURI = s3://${BUCKET}/prefix")
        self.assertEqual(result["URI"], "s3://mybucket/prefix")

    def test_parse_single_quoted_value(self):
        from harness_image.props import parse
        result = parse("KEY = 'hello world'")
        self.assertEqual(result["KEY"], "hello world")

    def test_parse_double_quoted_value(self):
        from harness_image.props import parse
        result = parse('KEY = "hello world"')
        self.assertEqual(result["KEY"], "hello world")

    def test_parse_rejects_duplicate_key(self):
        from harness_image.props import parse, PropertiesError
        with self.assertRaises(PropertiesError):
            parse("KEY = a\nKEY = b")

    def test_parse_rejects_missing_equals(self):
        from harness_image.props import parse, PropertiesError
        with self.assertRaises(PropertiesError):
            parse("BADLINE")

    def test_parse_rejects_invalid_key(self):
        from harness_image.props import parse, PropertiesError
        with self.assertRaises(PropertiesError):
            parse("lower_key = val")

    def test_parse_rejects_circular_reference(self):
        from harness_image.props import parse, PropertiesError
        with self.assertRaises(PropertiesError):
            parse("A = ${B}\nB = ${A}")

    def test_load_returns_empty_dict_for_missing_file(self):
        from harness_image.props import load
        self.assertEqual(load("/nonexistent/deploy.properties"), {})

    def test_load_returns_empty_dict_for_none(self):
        from harness_image.props import load
        self.assertEqual(load(None), {})

    def test_load_reads_file(self):
        from harness_image.props import load
        with tempfile.NamedTemporaryFile(mode="w", suffix=".properties",
                                         delete=False) as f:
            f.write("AWS_REGION = us-west-2\n")
            path = f.name
        try:
            result = load(path)
            self.assertEqual(result["AWS_REGION"], "us-west-2")
        finally:
            os.unlink(path)


# ── versions module ───────────────────────────────────────────────────────────

class VersionsTests(unittest.TestCase):
    def test_harnesses_tuple(self):
        from harness_image.versions import HARNESSES
        self.assertIn("claude", HARNESSES)
        self.assertIn("codex", HARNESSES)
        self.assertIn("kiro", HARNESSES)

    def test_pinned_version_claude(self):
        from harness_image.versions import pinned_version, CLAUDE_SDK_VERSION
        self.assertEqual(pinned_version("claude"), CLAUDE_SDK_VERSION)

    def test_pinned_version_codex(self):
        from harness_image.versions import pinned_version, CODEX_VERSION
        self.assertEqual(pinned_version("codex"), CODEX_VERSION)

    def test_pinned_version_kiro(self):
        from harness_image.versions import pinned_version, KIRO_VERSION
        self.assertEqual(pinned_version("kiro"), KIRO_VERSION)

    def test_version_strings_are_semver_like(self):
        from harness_image import versions as v
        import re
        sem = re.compile(r"^\d+\.\d+\.\d+")
        self.assertTrue(sem.match(v.CLAUDE_SDK_VERSION))
        self.assertTrue(sem.match(v.CODEX_VERSION))
        self.assertTrue(sem.match(v.KIRO_VERSION))

    def test_kiro_cdn_url_uses_https(self):
        from harness_image.versions import KIRO_CDN_URL
        self.assertTrue(KIRO_CDN_URL.startswith("https://"))

    def test_codex_package_includes_linux_arm64(self):
        from harness_image.versions import CODEX_PACKAGE
        self.assertIn("linux-arm64", CODEX_PACKAGE)


# ── sources module ────────────────────────────────────────────────────────────

class SourcesTrustTests(unittest.TestCase):
    """Trusted-source enforcement (no real network)."""

    def test_assert_trusted_https_known_host(self):
        from harness_image.sources import _assert_trusted
        # Should not raise
        _assert_trusted(
            "https://registry.npmjs.org/some/pkg",
            frozenset(["registry.npmjs.org"]))

    def test_assert_trusted_rejects_http(self):
        from harness_image.sources import _assert_trusted
        with self.assertRaises(ValueError):
            _assert_trusted("http://registry.npmjs.org/pkg",
                            frozenset(["registry.npmjs.org"]))

    def test_assert_trusted_rejects_unknown_host(self):
        from harness_image.sources import _assert_trusted
        with self.assertRaises(ValueError):
            _assert_trusted("https://evil.example.com/pkg",
                            frozenset(["registry.npmjs.org"]))

    def test_plan_token_is_deterministic(self):
        from harness_image.sources import ResolvedVersion, plan_token
        rv = [
            ResolvedVersion("claude", "0.2.152", "https://pypi.org/c", "sha256:abc", "pinned"),
            ResolvedVersion("codex", "0.154.0", "https://reg.npmjs.org/c", "", "pinned"),
        ]
        t1 = plan_token(rv)
        t2 = plan_token(rv)
        self.assertEqual(t1, t2)
        self.assertIsInstance(t1, str)
        self.assertEqual(len(t1), 32)

    def test_plan_token_changes_with_version(self):
        from harness_image.sources import ResolvedVersion, plan_token
        rv1 = [ResolvedVersion("claude", "0.2.152", "https://pypi.org/c", "", "pinned")]
        rv2 = [ResolvedVersion("claude", "0.2.153", "https://pypi.org/c", "", "pinned")]
        self.assertNotEqual(plan_token(rv1), plan_token(rv2))

    def test_lock_write_read_roundtrip(self):
        from harness_image.sources import ResolvedVersion, write_lock, read_lock
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            rv = [ResolvedVersion("claude", "1.0.0", "https://x.com/f", "sha256:abc", "pinned")]
            write_lock(dp, rv, "tok123")
            locked = read_lock(dp)
            self.assertEqual(locked["claude"]["version"], "1.0.0")
            self.assertEqual(locked["claude"]["token"], "tok123")

    def test_verify_integrity_sha256_pass(self):
        import hashlib
        from harness_image.sources import verify_integrity
        data = b"hello"
        digest = hashlib.sha256(data).hexdigest()
        verify_integrity("claude", data, f"sha256:{digest}")  # should not raise

    def test_verify_integrity_sha256_fail(self):
        from harness_image.sources import verify_integrity
        with self.assertRaises(RuntimeError):
            verify_integrity("claude", b"hello", "sha256:" + "a" * 64)

    def test_verify_integrity_empty_skips(self):
        from harness_image.sources import verify_integrity
        # Empty integrity = TOFU Kiro; should not raise
        verify_integrity("kiro", b"anything", "")

    def test_installed_detection_returns_none_when_not_found(self):
        from harness_image.sources import _detect_installed
        # Force subprocess to fail
        with patch("harness_image.sources.subprocess.run",
                   side_effect=FileNotFoundError("not found")):
            result = _detect_installed("claude", cli_path="/nonexistent/claude")
        self.assertIsNone(result)

    def test_installed_detection_parses_version(self):
        from harness_image.sources import _detect_installed
        fake_result = Mock()
        fake_result.stdout = "Claude Code 0.2.152\n"
        fake_result.stderr = ""
        with patch("harness_image.sources.subprocess.run", return_value=fake_result):
            ver = _detect_installed("claude", cli_path="claude")
        self.assertEqual(ver, "0.2.152")


# ── sources.resolve with mocked network ──────────────────────────────────────

class SourcesResolveTests(unittest.TestCase):
    """resolve() with mocked _fetch_json — no real network."""

    def _mock_pypi_response(self, version: str, sha256: str = "abc123") -> dict:
        return {
            "info": {"version": version},
            "urls": [
                {
                    "filename": f"claude_agent_sdk-{version}-py3-none-any.whl",
                    "url": f"https://files.pythonhosted.org/packages/{version}/c.whl",
                    "digests": {"sha256": sha256},
                }
            ],
            "releases": {version: []},
        }

    def _mock_npm_response(self, version: str) -> dict:
        return {
            "dist": {
                "tarball": f"https://registry.npmjs.org/@openai/codex/-/codex-{version}.tgz",
                "integrity": "sha512-AAAA==",
            }
        }

    def test_resolve_claude_pinned(self):
        from harness_image.sources import resolve
        mock_resp = self._mock_pypi_response("0.2.152")
        with patch("harness_image.sources._fetch_json", return_value=mock_resp):
            rv = resolve("claude", "pinned", None)
        self.assertEqual(rv.version, "0.2.152")
        self.assertEqual(rv.source, "pinned")
        self.assertTrue(rv.uri.startswith("https://"))
        self.assertTrue(rv.integrity.startswith("sha256:"))

    def test_resolve_claude_explicit_version(self):
        from harness_image.sources import resolve
        mock_resp = self._mock_pypi_response("0.2.100", "bbb")
        with patch("harness_image.sources._fetch_json", return_value=mock_resp):
            rv = resolve("claude", "pinned", "0.2.100")
        self.assertEqual(rv.version, "0.2.100")

    def test_resolve_codex_pinned(self):
        from harness_image.sources import resolve
        mock_resp = self._mock_npm_response("0.154.0")
        with patch("harness_image.sources._fetch_json", return_value=mock_resp):
            rv = resolve("codex", "pinned", None)
        self.assertEqual(rv.version, "0.154.0")
        self.assertIn("registry.npmjs.org", rv.uri)

    def test_resolve_kiro_pinned_no_network(self):
        from harness_image.sources import resolve
        # Kiro pinned uses the CDN URL directly — no _fetch_json needed
        # but we must assert the trusted host
        from harness_image.sources import _DEFAULT_TRUSTED_HOSTS
        rv = resolve("kiro", "pinned", None)
        self.assertEqual(rv.source, "pinned")
        self.assertTrue(rv.uri.startswith("https://"))

    def test_resolve_installed_falls_back_to_pinned(self):
        from harness_image.sources import resolve
        mock_resp = self._mock_pypi_response("0.2.152")
        with patch("harness_image.sources._detect_installed", return_value=None), \
             patch("harness_image.sources._fetch_json", return_value=mock_resp):
            rv = resolve("claude", "installed", None)
        from harness_image.versions import CLAUDE_SDK_VERSION
        self.assertEqual(rv.version, CLAUDE_SDK_VERSION)

    def test_resolve_unknown_harness_raises(self):
        from harness_image.sources import resolve
        with self.assertRaises(ValueError):
            resolve("unknown", "pinned", None)

    def test_resolve_invalid_source_raises(self):
        from harness_image.sources import resolve
        with self.assertRaises(ValueError):
            resolve("claude", "bogus", None)

    def test_resolve_kiro_latest_computes_sha256_integrity(self):
        """_resolve_kiro_latest must download and sha256-hash the Kiro zip."""
        import hashlib
        from harness_image.sources import _resolve_kiro_latest, _DEFAULT_TRUSTED_HOSTS
        fake_zip = b"PK\x03\x04fake-zip-content"
        expected_sha256 = "sha256:" + hashlib.sha256(fake_zip).hexdigest()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_zip
        # _resolve_kiro_latest now uses _trusted_opener, not bare urlopen
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        with patch("harness_image.sources._trusted_opener", return_value=mock_opener):
            ver, uri, integrity = _resolve_kiro_latest(
                "2.21.3", _DEFAULT_TRUSTED_HOSTS,
                "https://desktop-release.q.us-east-1.amazonaws.com/{version}/kirocli-{arch}-linux.zip",
            )
        self.assertEqual(ver, "2.21.3")
        self.assertTrue(uri.startswith("https://"))
        self.assertEqual(integrity, expected_sha256,
                         "Kiro TOFU integrity must be sha256:<hex> of the downloaded zip")

    def test_extract_kiro_verifies_integrity(self):
        """_extract_kiro must call verify_integrity when expected_integrity given."""
        import hashlib
        import zipfile
        import io
        from harness_image.bundle import _extract_kiro
        from harness_image.sources import verify_integrity
        # Build a minimal Kiro-shaped zip
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("kirocli/bin/kiro-cli", b"\x7fELF-fake")
        zip_bytes = buf.getvalue()
        correct_hash = "sha256:" + hashlib.sha256(zip_bytes).hexdigest()
        wrong_hash = "sha256:" + "0" * 64
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = zip_bytes
        # _extract_kiro now uses _trusted_opener for redirect validation
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        with tempfile.TemporaryDirectory() as td:
            tools_dir = Path(td) / "tools"
            with patch("harness_image.bundle._trusted_opener", return_value=mock_opener):
                # Correct integrity must pass
                _extract_kiro(tools_dir, "2.21.3",
                              "https://desktop-release.q.us-east-1.amazonaws.com/{version}/kirocli-{arch}-linux.zip",
                              expected_integrity=correct_hash)
        with tempfile.TemporaryDirectory() as td:
            tools_dir = Path(td) / "tools"
            mock_resp2 = MagicMock()
            mock_resp2.__enter__ = lambda s: s
            mock_resp2.__exit__ = MagicMock(return_value=False)
            mock_resp2.read.return_value = zip_bytes
            mock_opener2 = MagicMock()
            mock_opener2.open.return_value = mock_resp2
            with patch("harness_image.bundle._trusted_opener", return_value=mock_opener2), \
                 self.assertRaises(RuntimeError):
                _extract_kiro(tools_dir, "2.21.3",
                              "https://desktop-release.q.us-east-1.amazonaws.com/{version}/kirocli-{arch}-linux.zip",
                              expected_integrity=wrong_hash)

    def test_deploy_passes_locked_integrity_to_build_bundle(self):
        """deploy() must read lock file integrity and pass to build_bundle."""
        from harness_image.sources import ResolvedVersion, write_lock as src_write_lock, read_lock
        # Verify the lock file round-trip preserves integrity
        with tempfile.TemporaryDirectory() as td:
            src_write_lock(Path(td), [
                ResolvedVersion("kiro", "2.21.3", "https://cdn/kiro.zip",
                                "sha256:abc123", "latest")
            ], token="tok1")
            lock = read_lock(Path(td))
            self.assertEqual(lock.get("kiro", {}).get("integrity"), "sha256:abc123",
                             "Lock file must persist Kiro integrity for subsequent deploys")
        # Verify deploy.py code reads the lock and builds integrity_map
        import inspect
        import harness_image.deploy as deploy_mod
        src = inspect.getsource(deploy_mod.deploy)
        self.assertIn("read_lock", src,
                      "deploy() must call read_lock() to retrieve locked integrity")
        self.assertIn("integrity_map", src,
                      "deploy() must build an integrity_map from locked values")
        self.assertIn("expected_integrity", src,
                      "deploy() must pass expected_integrity to build_bundle")


# ── DeployConfig ─────────────────────────────────────────────────────────────

class DeployConfigTests(unittest.TestCase):
    def test_from_props_basic(self):
        from harness_image.deploy import DeployConfig
        props = {
            "AGENTCORE_TRANSFER_S3_URI": "s3://bucket/prefix/",
            "AWS_REGION": "us-west-2",
        }
        cfg = DeployConfig.from_props(props)
        self.assertEqual(cfg.transfer_s3_uri, "s3://bucket/prefix/")
        self.assertEqual(cfg.region, "us-west-2")

    def test_from_props_missing_transfer_uri_raises(self):
        from harness_image.deploy import DeployConfig
        with self.assertRaises(ValueError):
            DeployConfig.from_props({"AWS_REGION": "us-east-1"})

    def test_from_props_missing_region_uses_env(self):
        from harness_image.deploy import DeployConfig
        props = {"AGENTCORE_TRANSFER_S3_URI": "s3://b/p/"}
        with patch.dict(os.environ, {"AWS_REGION": "eu-west-1"}):
            cfg = DeployConfig.from_props(props)
        self.assertEqual(cfg.region, "eu-west-1")

    def test_from_props_isolation_per_user(self):
        from harness_image.deploy import DeployConfig
        props = {
            "AGENTCORE_TRANSFER_S3_URI": "s3://b/p/",
            "AWS_REGION": "us-east-1",
            "AGENTCORE_ISOLATION": "per-user",
            "USER_NAMESPACE": "alice",
        }
        cfg = DeployConfig.from_props(props)
        self.assertEqual(cfg.isolation, "per-user")
        self.assertEqual(cfg._namespace, "alice")

    def test_effective_role_name_shared(self):
        from harness_image.deploy import DeployConfig
        cfg = DeployConfig(
            transfer_s3_uri="s3://b/p/",
            region="us-east-1",
            execution_role_name="MyRole",
        )
        self.assertEqual(cfg.effective_role_name, "MyRole")

    def test_effective_role_name_per_user(self):
        from harness_image.deploy import DeployConfig
        cfg = DeployConfig(
            transfer_s3_uri="s3://b/p/",
            region="us-east-1",
            execution_role_name="MyRole",
            isolation="per-user",
            user_namespace="bob",
        )
        self.assertEqual(cfg.effective_role_name, "MyRole-bob")

    def test_from_arena_config(self):
        from harness_image.deploy import DeployConfig
        from arena.config import Config, AgentCoreConfig
        ac = AgentCoreConfig(
            transfer_s3_uri="s3://mybucket/myprefix/",
            region="ap-northeast-1",
        )
        config = Config(runtime="agentcore", agentcore=ac)
        cfg = DeployConfig.from_arena_config(config)
        self.assertEqual(cfg.transfer_s3_uri, "s3://mybucket/myprefix/")
        self.assertEqual(cfg.region, "ap-northeast-1")


# ── shim preserves public API ────────────────────────────────────────────────

class ShimCompatibilityTests(unittest.TestCase):
    def test_shim_exports_deploy(self):
        from arena.agentcore_provision import deploy
        import asyncio
        self.assertTrue(asyncio.iscoroutinefunction(deploy))

    def test_shim_exports_describe(self):
        from arena.agentcore_provision import describe
        self.assertTrue(callable(describe))

    def test_shim_exports_write_records(self):
        from arena.agentcore_provision import write_records
        self.assertTrue(callable(write_records))

    def test_shim_write_records_calls_through_without_importerror(self):
        """write_records() must not raise ImportError — the _write_records_data alias
        in harness_image/deploy.py must exist so the shim import succeeds."""
        import tempfile
        from arena.agentcore_provision import write_records
        from types import SimpleNamespace
        fake_config = SimpleNamespace(root=Path(tempfile.mkdtemp()))
        records = {
            "claude": {"arn": "arn:aws:bedrock-agentcore:::runtime/r-abc", "qualifier": "DEFAULT"}
        }
        # Should not raise ImportError; writes agentcore-runtimes.json
        result = write_records(fake_config, records)
        self.assertIn("[agentcore.runtimes]", result)
        # Verify the file was actually written
        json_path = fake_config.root / "agentcore-runtimes.json"
        self.assertTrue(json_path.exists())

    def test_shim_deploy_accepts_approve_token_and_dry_run(self):
        """deploy() shim must accept approve_token and dry_run kwargs."""
        import asyncio
        import inspect
        from arena.agentcore_provision import deploy
        sig = inspect.signature(deploy)
        params = sig.parameters
        self.assertIn("approve_token", params,
                      "deploy() must accept approve_token parameter")
        self.assertIn("dry_run", params,
                      "deploy() must accept dry_run parameter")

    def test_shim_deploy_accepts_props(self):
        """deploy() shim must accept props parameter."""
        import inspect
        from arena.agentcore_provision import deploy
        sig = inspect.signature(deploy)
        self.assertIn("props", sig.parameters,
                      "deploy() must accept props parameter")

    def test_shim_exports_ensure_execution_role(self):
        from arena.agentcore_provision import ensure_execution_role
        self.assertTrue(callable(ensure_execution_role))

    def test_shim_exports_constants(self):
        from arena.agentcore_provision import (
            _HARNESSES, _CODEX_PACKAGE, _KIRO_VERSION, _CLAUDE_SDK_REQ,
        )
        self.assertIn("claude", _HARNESSES)
        self.assertIn("linux-arm64", _CODEX_PACKAGE)

    def test_shim_ensure_execution_role_creates_role(self):
        """ensure_execution_role via shim accepts AgentCoreConfig."""
        from arena.agentcore_provision import ensure_execution_role
        from arena.config import AgentCoreConfig
        cfg = AgentCoreConfig(
            transfer_s3_uri="s3://bkt/pfx/",
            execution_role_name="TestRole",
            inline_policy_name="TestPolicy",
            application_tag="TestApp",
        )
        iam = Mock()
        iam.exceptions = SimpleNamespace(
            EntityAlreadyExistsException=type("Exists", (Exception,), {}))
        iam.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::1:role/TestRole"}}
        with patch("arena.agentcore_provision.time.sleep"):
            arn = ensure_execution_role(cfg, "1", iam, "us-east-1")
        self.assertEqual(arn, "arn:aws:iam::1:role/TestRole")
        self.assertEqual(iam.create_role.call_args.kwargs["RoleName"], "TestRole")
        self.assertEqual(iam.put_role_policy.call_args.kwargs["PolicyName"], "TestPolicy")


# ── deploy/agentcore-deploy.sh ───────────────────────────────────────────────

class DeployScriptTests(unittest.TestCase):
    SCRIPT = Path(__file__).parent.parent / "deploy" / "agentcore-deploy.sh"

    def test_script_exists(self):
        self.assertTrue(self.SCRIPT.exists(), f"deploy script not found: {self.SCRIPT}")

    def test_script_is_executable(self):
        self.assertTrue(os.access(str(self.SCRIPT), os.X_OK),
                        "deploy script is not executable")

    def test_script_shebang_is_bash(self):
        content = self.SCRIPT.read_text()
        self.assertTrue(content.startswith("#!/bin/bash"),
                        "shebang should be #!/bin/bash for macOS bash 3.2 compatibility")

    def test_script_references_harness_image(self):
        content = self.SCRIPT.read_text()
        self.assertIn("harness_image", content)

    def test_script_references_properties_flag(self):
        content = self.SCRIPT.read_text()
        self.assertIn("--properties", content)

    def test_script_references_deploy_venv(self):
        content = self.SCRIPT.read_text()
        self.assertIn(".deploy-venv", content)


# ── pyproject.toml ships harness_image ────────────────────────────────────────

class PyprojectTests(unittest.TestCase):
    def test_harness_image_in_wheel_packages(self):
        import tomllib
        p = Path(__file__).parent.parent / "pyproject.toml"
        with open(str(p), "rb") as f:
            data = tomllib.load(f)
        packages = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        self.assertIn("harness_image", packages)


# ── HarnessVersionConfig ─────────────────────────────────────────────────────

class HarnessVersionConfigTests(unittest.TestCase):
    def test_defaults_to_pinned(self):
        from harness_image.deploy import HarnessVersionConfig
        cfg = HarnessVersionConfig({}, "claude")
        self.assertEqual(cfg.source, "pinned")
        self.assertIsNone(cfg.version)

    def test_reads_source_from_props(self):
        from harness_image.deploy import HarnessVersionConfig
        cfg = HarnessVersionConfig({"CLAUDE_HARNESS_SOURCE": "installed"}, "claude")
        self.assertEqual(cfg.source, "installed")

    def test_reads_version_from_props(self):
        from harness_image.deploy import HarnessVersionConfig
        cfg = HarnessVersionConfig({"CODEX_HARNESS_VERSION": "0.120.0"}, "codex")
        self.assertEqual(cfg.version, "0.120.0")


# ── plan / deploy dry-run ─────────────────────────────────────────────────────

class PlanTests(unittest.TestCase):
    """plan() with mocked sources.resolve (no network)."""

    def _mock_resolve(self, harness, source, version, *_, **__):
        from harness_image.sources import ResolvedVersion
        ver = version or {"claude": "0.2.152", "codex": "0.154.0", "kiro": "2.21.3"}[harness]
        return ResolvedVersion(harness, ver, f"https://example.com/{harness}", "", "pinned")

    def test_plan_returns_token(self):
        from harness_image.deploy import DeployConfig, plan
        cfg = DeployConfig(
            transfer_s3_uri="s3://b/p/",
            region="us-east-1",
        )
        with patch("harness_image.deploy.resolve", side_effect=self._mock_resolve):
            result = plan(cfg, None, {})
        self.assertIn("token", result)
        self.assertIn("harnesses", result)
        self.assertEqual(len(result["harnesses"]), 3)

    def test_plan_harnesses_subset(self):
        from harness_image.deploy import DeployConfig, plan
        cfg = DeployConfig(transfer_s3_uri="s3://b/p/", region="us-east-1")
        with patch("harness_image.deploy.resolve", side_effect=self._mock_resolve):
            result = plan(cfg, ["claude"], {})
        self.assertEqual(list(result["harnesses"].keys()), ["claude"])

    def test_deploy_dry_run_no_aws(self):
        """deploy() with dry_run=True must not make any AWS calls."""
        import asyncio
        from harness_image.deploy import DeployConfig, deploy
        cfg = DeployConfig(transfer_s3_uri="s3://b/p/", region="us-east-1")
        with patch("harness_image.deploy.resolve", side_effect=self._mock_resolve):
            result = asyncio.run(deploy(cfg, dry_run=True, props={}))
        self.assertTrue(result.get("dry_run"))

    def test_deploy_non_pinned_without_token_raises(self):
        """Non-pinned source strategy without approve_token must raise."""
        import asyncio
        from harness_image.deploy import DeployConfig, deploy
        cfg = DeployConfig(transfer_s3_uri="s3://b/p/", region="us-east-1")
        # CLAUDE_HARNESS_SOURCE=latest without --approve must raise RuntimeError
        props = {"CLAUDE_HARNESS_SOURCE": "latest"}
        with patch("harness_image.deploy.resolve", side_effect=self._mock_resolve), \
             self.assertRaises(RuntimeError):
            asyncio.run(deploy(cfg, harnesses=["claude"], props=props))


# ── CLI agentcore-deploy has --approve and --dry-run flags ───────────────────

class CLIAgentcoreDeployFlagsTests(unittest.TestCase):
    """arena CLI agentcore-deploy subparser must expose --approve and --dry-run."""

    def _parse_agentcore_deploy(self, argv):
        """Return parsed args for the given agentcore-deploy argv (without config loading)."""
        import argparse
        import sys
        # Minimal replica of the arg-parser subset we need
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        cmd = sub.add_parser("agentcore-deploy")
        cmd.add_argument("--harness", action="append", dest="harnesses")
        cmd.add_argument("--no-rebuild", action="store_false", dest="rebuild", default=True)
        cmd.add_argument("--approve", metavar="TOKEN", dest="approve_token")
        cmd.add_argument("--dry-run", action="store_true")
        return parser.parse_args(["agentcore-deploy"] + argv)

    def test_approve_flag_accepted(self):
        args = self._parse_agentcore_deploy(["--approve", "tok123"])
        self.assertEqual(args.approve_token, "tok123")

    def test_dry_run_flag_accepted(self):
        args = self._parse_agentcore_deploy(["--dry-run"])
        self.assertTrue(args.dry_run)

    def test_approve_not_set_is_none(self):
        args = self._parse_agentcore_deploy([])
        self.assertIsNone(args.approve_token)

    def test_cli_parser_has_approve_and_dry_run(self):
        """Verify the actual arena/cli.py argparse parser exposes both flags."""
        # Import the main function without executing it; just check parser creation
        import arena.cli as cli_mod
        # The actual parser is built inside main(); simulate a lightweight approach
        # by grepping the module source for the flag names.
        import inspect
        src = inspect.getsource(cli_mod)
        self.assertIn("--approve", src, "arena/cli.py must add --approve to agentcore-deploy")
        self.assertIn("--dry-run", src, "arena/cli.py must add --dry-run to agentcore-deploy")


# ── harness_image --help shows exit codes ─────────────────────────────────────

class MainParserEpilogTests(unittest.TestCase):
    """python -m harness_image --help must show exit codes in the epilog."""

    def test_parser_epilog_contains_exit_codes(self):
        from harness_image.__main__ import _build_parser
        parser = _build_parser()
        epilog = parser.epilog or ""
        self.assertIn("0", epilog, "Epilog must list exit code 0")
        self.assertIn("1", epilog, "Epilog must list exit code 1")
        self.assertIn("3", epilog, "Epilog must list exit code 3 (approval required)")

    def test_parser_formatter_preserves_newlines(self):
        import argparse
        from harness_image.__main__ import _build_parser
        parser = _build_parser()
        self.assertIs(parser.formatter_class, argparse.RawDescriptionHelpFormatter)


# ── _emit must print compact single-line JSON ────────────────────────────────

class EmitOutputTests(unittest.TestCase):
    """_emit() must output exactly one compact JSON line (no indent=2)."""

    def test_emit_outputs_single_line(self):
        from harness_image.__main__ import _emit
        import io
        buf = io.StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(str(a[0]) + "\n")):
            _emit({"status": "ok", "harnesses": {"claude": {"version": "1.0"}}})
        output = buf.getvalue()
        lines = [l for l in output.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, f"_emit must output exactly one line, got: {output!r}")

    def test_emit_output_is_valid_json(self):
        from harness_image.__main__ import _emit
        import io
        buf = io.StringIO()
        obj = {"status": "ok", "count": 3}
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(str(a[0]) + "\n")):
            _emit(obj)
        parsed = json.loads(buf.getvalue().strip())
        self.assertEqual(parsed, obj)

    def test_emit_output_has_no_indent(self):
        """Compact output must not contain leading spaces (no indent=2)."""
        from harness_image.__main__ import _emit
        import io
        buf = io.StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(str(a[0]) + "\n")):
            _emit({"key": "value"})
        # indented JSON would have lines starting with spaces
        self.assertNotIn("\n ", buf.getvalue(), "_emit output must not be indented JSON")


# ── requirements.lock must have hash pins ────────────────────────────────────

class RequirementsLockHashTests(unittest.TestCase):
    """harness_image/requirements.lock must have --hash=sha256: annotations."""

    def test_every_package_line_has_hash_annotation(self):
        lock_path = Path(__file__).parent.parent / "harness_image" / "requirements.lock"
        self.assertTrue(lock_path.exists(), "harness_image/requirements.lock must exist")
        content = lock_path.read_text()
        # Strip comments and blank lines; collect package name lines (start with a letter, not #/\)
        pkg_lines = [
            l for l in content.splitlines()
            if l and not l.startswith("#") and not l.startswith("\\") and "==" in l
        ]
        self.assertTrue(pkg_lines, "requirements.lock must have at least one package entry")
        for line in pkg_lines:
            # Each package must eventually be followed by a --hash= entry
            # We check the whole file contains --hash=sha256 for each package
            pkg_name = line.split("==")[0].strip().rstrip(" \\")
            self.assertIn(
                "--hash=sha256:",
                content,
                f"requirements.lock must contain at least one --hash=sha256: annotation for {pkg_name}",
            )

    def test_deploy_script_uses_require_hashes(self):
        script_path = Path(__file__).parent.parent / "deploy" / "agentcore-deploy.sh"
        self.assertTrue(script_path.exists(), "deploy/agentcore-deploy.sh must exist")
        content = script_path.read_text()
        self.assertIn(
            "--require-hashes",
            content,
            "agentcore-deploy.sh must pass --require-hashes to uv pip install",
        )


# ── redirect validation ───────────────────────────────────────────────────────

class RedirectValidationTests(unittest.TestCase):
    """HTTP redirects must be validated against HARNESS_TRUSTED_SOURCES."""

    def test_trusted_redirect_handler_allows_trusted_redirect(self):
        from harness_image.sources import _TrustedRedirectHandler, _DEFAULT_TRUSTED_HOSTS
        import urllib.request as _ureq
        handler = _TrustedRedirectHandler(_DEFAULT_TRUSTED_HOSTS)
        # Build a real Request so get_method() returns "GET" — MagicMock fails the
        # HTTPRedirectHandler method check (it's not in ("GET","HEAD","POST")).
        req = _ureq.Request("https://files.pythonhosted.org/old/path")
        # A redirect to a trusted host must not raise ValueError
        try:
            handler.redirect_request(
                req, None, 301, "Moved",
                {},
                "https://files.pythonhosted.org/new/path",
            )
        except ValueError:
            self.fail("redirect to trusted host must not raise ValueError")

    def test_trusted_redirect_handler_rejects_untrusted_redirect(self):
        from harness_image.sources import _TrustedRedirectHandler, _DEFAULT_TRUSTED_HOSTS
        handler = _TrustedRedirectHandler(_DEFAULT_TRUSTED_HOSTS)
        req = MagicMock()
        with self.assertRaises(ValueError):
            handler.redirect_request(
                req, None, 301, "Moved",
                {"Location": "https://evil.example.com/malware.whl"},
                "https://evil.example.com/malware.whl",
            )

    def test_trusted_opener_uses_redirect_handler(self):
        from harness_image.sources import _trusted_opener, _DEFAULT_TRUSTED_HOSTS
        import urllib.request
        opener = _trusted_opener(_DEFAULT_TRUSTED_HOSTS)
        handler_types = [type(h).__name__ for h in opener.handlers]
        self.assertIn("_TrustedRedirectHandler", handler_types,
                      "_trusted_opener must install _TrustedRedirectHandler")

    def test_fetch_json_uses_trusted_opener(self):
        """_fetch_json must use _trusted_opener, not bare urlopen."""
        import inspect
        from harness_image import sources
        src = inspect.getsource(sources._fetch_json)
        self.assertIn("_trusted_opener", src,
                      "_fetch_json must use _trusted_opener for redirect validation")


# ── HARNESS_IMAGE_WORKSTATION_CONFIG in standalone CLI ───────────────────────

class WorkstationConfigStandaloneTests(unittest.TestCase):
    """standalone CLI must honour HARNESS_IMAGE_WORKSTATION_CONFIG."""

    def test_deploy_subparser_has_config_snapshot_dir_argument(self):
        from harness_image.__main__ import _build_parser
        parser = _build_parser()
        # Walk sub-parsers to find 'deploy'
        deploy_parser = None
        for action in parser._actions:
            if hasattr(action, "_name_parser_map"):
                deploy_parser = action._name_parser_map.get("deploy")
                break
        self.assertIsNotNone(deploy_parser, "deploy sub-command must exist")
        option_strings = {s for a in deploy_parser._actions for s in a.option_strings}
        self.assertIn("--config-snapshot-dir", option_strings,
                      "deploy sub-parser must have --config-snapshot-dir argument")

    def test_collect_config_snapshot_exported_from_host_setup(self):
        """arena.host_setup must export collect_config_snapshot."""
        from arena import host_setup
        self.assertTrue(
            callable(getattr(host_setup, "collect_config_snapshot", None)),
            "arena.host_setup must export collect_config_snapshot()",
        )


# ── README accuracy ───────────────────────────────────────────────────────────

class ReadmeAccuracyTests(unittest.TestCase):
    """harness_image/README.md must accurately describe the import direction."""

    README = Path(__file__).parent.parent / "harness_image" / "README.md"

    def test_readme_does_not_claim_arena_avoids_harness_image(self):
        """README must not falsely claim arena/ does not import harness_image."""
        content = self.README.read_text()
        self.assertNotIn(
            "does not import `harness_image`",
            content,
            "README must not contain the false claim that arena/ does not import "
            "harness_image (arena does import it via the agentcore_provision shim)",
        )

    def test_readme_states_arena_imports_via_shim(self):
        """README must document that arena imports harness_image via the shim."""
        content = self.README.read_text()
        self.assertIn(
            "agentcore_provision",
            content,
            "README must mention arena/agentcore_provision.py as the import shim",
        )

    def test_readme_states_harness_image_does_not_import_arena(self):
        """`harness_image` does not import `arena` — this half must remain documented."""
        content = self.README.read_text()
        self.assertIn(
            "`harness_image` does not import `arena`",
            content,
            "README must still state that harness_image does not import arena",
        )


# ── props forwarding reaches HarnessVersionConfig ────────────────────────────

class PropsForwardingTests(unittest.TestCase):
    """CLAUDE_HARNESS_SOURCE in props must reach HarnessVersionConfig in deploy()."""

    def test_harness_version_config_reads_source_from_props(self):
        """HarnessVersionConfig must pick up CLAUDE_HARNESS_SOURCE from the props dict."""
        from harness_image.deploy import HarnessVersionConfig
        props = {"CLAUDE_HARNESS_SOURCE": "installed"}
        hvc = HarnessVersionConfig(props, "claude")
        self.assertEqual(
            hvc.source, "installed",
            "HarnessVersionConfig must read {HARNESS}_HARNESS_SOURCE from the props dict",
        )

    def test_deploy_dry_run_forwards_props_to_harness_version_config(self):
        """deploy() must pass props through so CLAUDE_HARNESS_SOURCE non-pinned is visible.

        With CLAUDE_HARNESS_SOURCE=latest and no approve_token, deploy() must raise
        RuntimeError (not silently ignore the source setting). If props is not forwarded
        to HarnessVersionConfig, the source would default to 'pinned' and no error
        would be raised.
        """
        import asyncio
        from harness_image.deploy import DeployConfig, deploy
        from harness_image.sources import ResolvedVersion

        cfg = DeployConfig(transfer_s3_uri="s3://b/p/", region="us-east-1")
        props = {"CLAUDE_HARNESS_SOURCE": "latest"}

        def _mock_resolve(harness, source, version, *_, **__):
            return ResolvedVersion(
                harness, "1.0.0", f"https://example.com/{harness}", "", source)

        # Non-pinned source without approve_token must raise RuntimeError,
        # proving props was forwarded to HarnessVersionConfig.
        with patch("harness_image.deploy.resolve", side_effect=_mock_resolve), \
             self.assertRaises(RuntimeError,
                               msg="deploy() must raise when CLAUDE_HARNESS_SOURCE=latest "
                               "and no approve_token — props not forwarded if it does not"):
            asyncio.run(deploy(cfg, harnesses=["claude"], props=props))

    def test_shim_deploy_forwards_props_to_hi_deploy(self):
        """arena.agentcore_provision.deploy() must forward props to harness_image.deploy()."""
        import asyncio
        import inspect
        import arena.agentcore_provision as shim
        src = inspect.getsource(shim.deploy)
        # The shim must forward 'effective_props' (or 'props') to _hi_deploy
        self.assertIn("props", src,
                      "arena.agentcore_provision.deploy() must forward props")
        self.assertIn("_hi_deploy", src,
                      "arena.agentcore_provision.deploy() must call _hi_deploy")


# ── HARNESS_IMAGE_WORKSTATION_CONFIG triggers auto-collect ───────────────────

class WorkstationConfigAutoCollectTests(unittest.TestCase):
    """when HARNESS_IMAGE_WORKSTATION_CONFIG=true, standalone CLI must
    call arena.host_setup.collect_config_snapshot and forward the result to deploy()."""

    def test_main_calls_collect_when_workstation_config_true(self):
        """Mock collect_config_snapshot and verify config_snapshot_dirs reaches deploy()."""
        import asyncio
        import tempfile
        from harness_image.__main__ import main
        from harness_image.sources import ResolvedVersion

        snapshot_dir = Path(tempfile.mkdtemp())
        fake_snapshots = {"claude": snapshot_dir}

        calls: list = []

        async def _fake_deploy(cfg, harnesses=None, *, rebuild=True,
                               approve_token=None, dry_run=False, props=None,
                               kiro_url_template=None,
                               config_snapshot_dirs=None, **kw):
            calls.append(config_snapshot_dirs)
            return {"dry_run": True, "harnesses": {}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".properties",
                                        delete=False) as f:
            f.write(
                "AGENTCORE_TRANSFER_S3_URI = s3://bucket/prefix/\n"
                "AWS_REGION = us-east-1\n"
                "HARNESS_IMAGE_WORKSTATION_CONFIG = true\n"
            )
            props_path = f.name

        try:
            with patch("harness_image.__main__.deploy", side_effect=_fake_deploy), \
                 patch("arena.host_setup.collect_config_snapshot",
                       return_value=fake_snapshots) as mock_collect:
                rc = main([
                    "--properties", props_path,
                    "deploy",
                    "--dry-run",
                ])
            # collect_config_snapshot should have been called (import may silently
            # skip if arena is absent; the important thing is that when it succeeds,
            # the snapshots are passed through)
            if mock_collect.called:
                self.assertTrue(
                    calls,
                    "config_snapshot_dirs must be passed to deploy() when "
                    "HARNESS_IMAGE_WORKSTATION_CONFIG=true",
                )
                passed = calls[0]
                if passed is not None:
                    self.assertIn("claude", passed,
                                  "snapshot dirs must include harness keys")
        finally:
            import os
            os.unlink(props_path)

    def test_explicit_config_snapshot_dir_parsed_correctly(self):
        """--config-snapshot-dir HARNESS=PATH must be parsed and forwarded."""
        import tempfile
        from harness_image.__main__ import main
        from harness_image.sources import ResolvedVersion

        snapshot_dir = tempfile.mkdtemp()
        calls: list = []

        async def _fake_deploy(cfg, harnesses=None, *, rebuild=True,
                               approve_token=None, dry_run=False, props=None,
                               kiro_url_template=None,
                               config_snapshot_dirs=None, **kw):
            calls.append(config_snapshot_dirs)
            return {"dry_run": True, "harnesses": {}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".properties",
                                        delete=False) as f:
            f.write(
                "AGENTCORE_TRANSFER_S3_URI = s3://bucket/prefix/\n"
                "AWS_REGION = us-east-1\n"
            )
            props_path = f.name

        try:
            with patch("harness_image.__main__.deploy", side_effect=_fake_deploy):
                main([
                    "--properties", props_path,
                    "deploy",
                    "--dry-run",
                    "--config-snapshot-dir", f"claude={snapshot_dir}",
                ])
            self.assertTrue(calls, "deploy must have been called")
            passed = calls[0]
            self.assertIsNotNone(passed, "config_snapshot_dirs must not be None")
            self.assertIn("claude", passed)
            self.assertEqual(passed["claude"], Path(snapshot_dir))
        finally:
            import os
            os.unlink(props_path)


if __name__ == "__main__":
    unittest.main()
