# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Credential-isolation tests: secrets channel, tarball filtering, S3 namespacing.

All AWS calls are mocked; no network, no real S3, no real AgentCore.
"""
import asyncio
import base64
import io
import json
import sys
import tarfile
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

from arena.agentcore_agent import _fetch_and_install_secrets
from arena.agentcore_sandbox import (
    AgentCore,
    _collect_secrets,
    _delete_all_versions_sync,
    _pack_secrets,
    _put_secrets_object,
    _s3_key,
    _secrets_filter,
)
from arena.bridge import atomic_json
from arena.config import AgentCoreConfig, Config
from arena.evaluation import Plan
from arena.worker import Worker


# ── helpers ───────────────────────────────────────────────────────────────────

def _agentcore_config(**kwargs):
    defaults = dict(transfer_s3_uri="s3://test-bucket/prefix")
    defaults.update(kwargs)
    return AgentCoreConfig(**defaults)


def _config(**ac_kwargs):
    ac = _agentcore_config(**ac_kwargs)
    return Config(runtime="agentcore", agentcore=ac)


def _config_with_ns(namespace: str, **ac_kwargs):
    """Config with USER_NAMESPACE set for multi-user isolation tests."""
    ac = _agentcore_config(**ac_kwargs)
    cfg = Config(runtime="agentcore", agentcore=ac)
    cfg.user_namespace = namespace
    return cfg


def _fake_s3(*, versions=None, delete_markers=None, list_objects_contents=None):
    """Fake S3 client wired for version-deletion tests."""
    s3 = Mock()
    s3.put_object.return_value = {}
    s3.delete_object.return_value = {}
    s3.delete_objects.return_value = {}
    s3.list_objects_v2.return_value = {"Contents": list_objects_contents or []}
    s3.list_object_versions.return_value = {
        "Versions": versions or [],
        "DeleteMarkers": delete_markers or [],
    }
    return s3


def _make_root(base: Path, run_id="run-test") -> Path:
    root = base / run_id
    for d in ("workspace", "bridge/requests", "bridge/answers", "control", "home"):
        (root / d).mkdir(parents=True, exist_ok=True)
    atomic_json(root / "bridge" / "turn.json", {"turn_key": "t1", "phase": "develop"})
    atomic_json(root / "control" / "state.json", {"seq": 0, "signal": None})
    return root


# ── _s3_key helper ────────────────────────────────────────────────────────────

class S3KeyHelperTests(unittest.TestCase):
    def test_no_namespace_produces_original_layout(self):
        key = _s3_key("pfx", None, "sessions/sid/t/job.tar.gz")
        self.assertEqual(key, "pfx/sessions/sid/t/job.tar.gz")

    def test_namespace_inserted_between_prefix_and_path(self):
        key = _s3_key("pfx", "alice", "sessions/sid/t/job.tar.gz")
        self.assertEqual(key, "pfx/alice/sessions/sid/t/job.tar.gz")

    def test_secrets_key_namespaced(self):
        key = _s3_key("pfx", "bob", "secrets/sid/t.bin")
        self.assertEqual(key, "pfx/bob/secrets/sid/t.bin")

    def test_empty_namespace_treated_as_no_namespace(self):
        # An empty string is falsy; behaves same as None.
        key = _s3_key("pfx", "", "secrets/sid/t.bin")
        self.assertEqual(key, "pfx/secrets/sid/t.bin")


# ── _secrets_filter ───────────────────────────────────────────────────────────

class SecretsFilterTests(unittest.TestCase):
    def _make_ti(self, name):
        ti = tarfile.TarInfo(name=name)
        ti.size = 0
        return ti

    def test_known_secret_basename_excluded(self):
        f = _secrets_filter(frozenset({".credentials.json"}))
        self.assertIsNone(f(self._make_ti("home/.credentials.json")))

    def test_nested_secret_excluded(self):
        f = _secrets_filter(frozenset({"data.sqlite3"}))
        self.assertIsNone(f(self._make_ti("home/.local/share/kiro-cli/data.sqlite3")))

    def test_non_secret_file_passes_through(self):
        f = _secrets_filter(frozenset({".credentials.json"}))
        ti = self._make_ti("home/settings.json")
        self.assertIs(f(ti), ti)

    def test_empty_secret_names_is_a_no_op(self):
        f = _secrets_filter(frozenset())
        ti = self._make_ti("home/anything.txt")
        self.assertIs(f(ti), ti)


# ── _collect_secrets ─────────────────────────────────────────────────────────

class CollectSecretsTests(unittest.TestCase):
    def test_returns_empty_when_home_does_not_exist(self):
        result = _collect_secrets(Path("/nonexistent-home-xyz"), "claude")
        self.assertEqual(result, {})

    def test_collects_claude_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cred = home / ".credentials.json"
            cred.write_text('{"token":"secret"}')
            result = _collect_secrets(home, "claude")
            self.assertIn(".credentials.json", result)
            self.assertEqual(result[".credentials.json"], b'{"token":"secret"}')

    def test_collects_codex_auth(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            auth = home / ".codex" / "auth.json"
            auth.parent.mkdir()
            auth.write_bytes(b"tok")
            result = _collect_secrets(home, "codex")
            self.assertIn(".codex/auth.json", result)

    def test_collects_kiro_sqlite(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            db_dir = home / ".local" / "share" / "kiro-cli"
            db_dir.mkdir(parents=True)
            (db_dir / "data.sqlite3").write_bytes(b"\x53\x51\x4c\x69")
            result = _collect_secrets(home, "kiro")
            self.assertIn(".local/share/kiro-cli/data.sqlite3", result)

    def test_non_secret_files_not_collected(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "settings.json").write_text("{}")
            result = _collect_secrets(home, "claude")
            self.assertNotIn("settings.json", result)

    def test_unknown_harness_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "anything").write_text("x")
            result = _collect_secrets(home, "unknown-harness")
            self.assertEqual(result, {})


# ── _pack_secrets / round-trip ────────────────────────────────────────────────

class PackSecretsTests(unittest.TestCase):
    def test_packed_data_is_bytes(self):
        packed = _pack_secrets({"a.txt": b"hello"})
        self.assertIsInstance(packed, bytes)

    def test_round_trip_preserves_content(self):
        secrets = {".credentials.json": b'{"tok":"s3cr3t"}', "auth.json": b"abc"}
        packed = _pack_secrets(secrets)
        payload = json.loads(zlib.decompress(packed))
        self.assertEqual(payload["v"], 1)
        for path, raw in secrets.items():
            self.assertIn(path, payload["files"])
            self.assertEqual(base64.b64decode(payload["files"][path]), raw)

    def test_packed_data_is_not_plaintext(self):
        # Content must not be readable as plain text; compression makes it opaque.
        packed = _pack_secrets({"secret": b"SUPER_SECRET_VALUE_12345"})
        self.assertNotIn(b"SUPER_SECRET_VALUE_12345", packed)


# ── _put_secrets_object ────────────────────────────────────────────────────────

class PutSecretsObjectTests(unittest.TestCase):
    def test_no_kms_uses_sse_s3(self):
        s3 = Mock()
        _put_secrets_object(s3, "b", "k", b"data", None)
        kwargs = s3.put_object.call_args[1]
        self.assertEqual(kwargs["ServerSideEncryption"], "AES256")
        self.assertNotIn("SSEKMSKeyId", kwargs)

    def test_kms_key_uses_sse_kms(self):
        s3 = Mock()
        arn = "arn:aws:kms:us-east-1:123:key/abc"
        _put_secrets_object(s3, "b", "k", b"data", arn)
        kwargs = s3.put_object.call_args[1]
        self.assertEqual(kwargs["ServerSideEncryption"], "aws:kms")
        self.assertEqual(kwargs["SSEKMSKeyId"], arn)

    def test_bucket_and_key_match_args(self):
        s3 = Mock()
        _put_secrets_object(s3, "my-bucket", "my/key.bin", b"x", None)
        kwargs = s3.put_object.call_args[1]
        self.assertEqual(kwargs["Bucket"], "my-bucket")
        self.assertEqual(kwargs["Key"], "my/key.bin")


# ── _delete_all_versions_sync ─────────────────────────────────────────────────

class DeleteAllVersionsTests(unittest.TestCase):
    def test_versioned_bucket_deletes_all_versions(self):
        s3 = _fake_s3(versions=[
            {"Key": "pfx/k", "VersionId": "v1"},
            {"Key": "pfx/k", "VersionId": "v2"},
        ])
        ok = _delete_all_versions_sync(s3, "b", "pfx/k")
        self.assertTrue(ok)
        s3.delete_objects.assert_called_once()
        objects = s3.delete_objects.call_args[1]["Delete"]["Objects"]
        version_ids = {o["VersionId"] for o in objects}
        self.assertEqual(version_ids, {"v1", "v2"})

    def test_unversioned_bucket_falls_back_to_delete_object(self):
        s3 = _fake_s3(versions=[], delete_markers=[])
        ok = _delete_all_versions_sync(s3, "b", "some/key")
        self.assertTrue(ok)
        s3.delete_object.assert_called_once_with(Bucket="b", Key="some/key")
        s3.delete_objects.assert_not_called()

    def test_deletes_delete_markers_too(self):
        s3 = _fake_s3(delete_markers=[{"Key": "k", "VersionId": "dm1"}])
        _delete_all_versions_sync(s3, "b", "k")
        objects = s3.delete_objects.call_args[1]["Delete"]["Objects"]
        self.assertEqual(objects[0]["VersionId"], "dm1")

    def test_ignores_versions_with_different_key(self):
        # list_object_versions with Prefix can return keys that start with the
        # same prefix.  Only exact-key matches should be deleted.
        s3 = _fake_s3(versions=[
            {"Key": "pfx/k", "VersionId": "v1"},
            {"Key": "pfx/k2", "VersionId": "v_other"},
        ])
        _delete_all_versions_sync(s3, "b", "pfx/k")
        objects = s3.delete_objects.call_args[1]["Delete"]["Objects"]
        keys = {o["Key"] for o in objects}
        self.assertEqual(keys, {"pfx/k"})

    def test_returns_false_on_error(self):
        s3 = Mock()
        s3.list_object_versions.side_effect = Exception("access denied")
        ok = _delete_all_versions_sync(s3, "b", "k")
        self.assertFalse(ok)

    def test_never_raises(self):
        s3 = Mock()
        s3.list_object_versions.side_effect = RuntimeError("boom")
        # Must not raise
        result = _delete_all_versions_sync(s3, "b", "k")
        self.assertFalse(result)


# ── Job tarball must not contain secret files ─────────────────────────────────

class JobTarballSecretsFilterTests(unittest.TestCase):
    """Verify the job tarball never contains known-secret file names."""

    def _tar_names(self, data: bytes) -> set[str]:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            return {m.name for m in tf.getmembers() if m.isfile()}

    def _make_agentcore(self, base: Path):
        cfg = Config(runtime="agentcore",
                     data_dir=str(base / "data"),
                     agentcore=AgentCoreConfig(
                         transfer_s3_uri="s3://test-bucket/pfx"))
        ac = AgentCore(cfg)
        s3 = Mock()
        s3.put_object.return_value = {}
        s3.list_object_versions.return_value = {"Versions": [], "DeleteMarkers": []}
        s3.delete_object.return_value = {}
        ac._s3_client = lambda: s3
        ctrl = Mock()
        ctrl.get_agent_runtime.return_value = {
            "status": "READY", "agentRuntimeArtifact": {}, "agentRuntimeVersion": "1"}
        ctrl.list_agent_runtimes.return_value = {
            "agentRuntimes": [
                {"agentRuntimeName": "mchc_claude",
                 "agentRuntimeArn": "arn:fake",
                 "agentRuntimeId": "mchc_claude-fake"}
            ]
        }
        ac._control_client = lambda: ctrl
        data = Mock()
        data.invoke_agent_runtime.return_value = {"response": io.BytesIO(b"{}"), "statusCode": 200}
        data.invoke_agent_runtime_command.return_value = {
            "stream": [{"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}]}
        ac._data_client = lambda: data
        # Prevent the real STS get_caller_identity call inside check().
        _sess = Mock()
        _sess.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
        ac._boto_session = _sess
        return ac, s3

    def _run_launch(self, harness, secret_file_name, secret_content=b"SECRET"):
        """Return the job tarball bytes after a simulated launch."""
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            ac, s3 = self._make_agentcore(base)
            root = _make_root(base, "run-sec")

            # Plant a secret file in HOME
            secret_dir = root / "home"
            secret_dir.mkdir(parents=True, exist_ok=True)
            secret_file = secret_dir / secret_file_name
            secret_file.write_bytes(secret_content)

            captured = {}

            def fake_put_object(**kwargs):
                key = kwargs.get("Key", "")
                if key.endswith("job.tar.gz"):
                    captured["job_tar"] = kwargs["Body"]
                return {}

            s3.put_object.side_effect = fake_put_object

            run = {"id": "run-sec", "harness": harness}

            async def _fake_resolve(h):
                return "arn:fake", "DEFAULT", {
                    "agentRuntimeStatus": "READY",
                    "agentRuntimeArtifact": {}, "agentRuntimeVersion": "1"}

            async def go():
                ac._resolve_runtime = _fake_resolve
                await ac.start(run, root, "develop")
                await ac.launch("arena-run-sec", args=["python", "-c", "pass"])

            asyncio.run(go())
            return captured.get("job_tar", b"")

    def test_claude_credentials_not_in_job_tar(self):
        data = self._run_launch("claude", ".credentials.json")
        self.assertNotEqual(data, b"", "Job tarball was not captured")
        names = self._tar_names(data)
        secret_paths = {n for n in names if ".credentials.json" in n}
        self.assertEqual(secret_paths, set(),
                         f"Secret file found in job tarball: {secret_paths}")

    def test_codex_auth_not_in_job_tar(self):
        data = self._run_launch("codex", "auth.json")
        names = self._tar_names(data)
        secret_paths = {n for n in names if "auth.json" in n}
        self.assertEqual(secret_paths, set(),
                         f"Secret file found in job tarball: {secret_paths}")

    def test_kiro_sqlite_not_in_job_tar(self):
        data = self._run_launch("kiro", "data.sqlite3")
        names = self._tar_names(data)
        secret_paths = {n for n in names if "data.sqlite3" in n}
        self.assertEqual(secret_paths, set(),
                         f"Secret file found in job tarball: {secret_paths}")

    def test_non_secret_files_still_present_in_job_tar(self):
        """Filtering secrets must not remove non-secret HOME files."""
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            ac, s3 = self._make_agentcore(base)
            root = _make_root(base, "run-keep")

            home = root / "home"
            home.mkdir(parents=True, exist_ok=True)
            (home / ".credentials.json").write_bytes(b"secret")
            (home / "settings.json").write_bytes(b'{"model":"claude"}')

            captured = {}

            def fake_put(**kwargs):
                if kwargs.get("Key", "").endswith("job.tar.gz"):
                    captured["tar"] = kwargs["Body"]
                return {}

            s3.put_object.side_effect = fake_put
            run = {"id": "run-keep", "harness": "claude"}

            async def _fake_resolve(h):
                return "arn:fake", "DEFAULT", {
                    "agentRuntimeStatus": "READY",
                    "agentRuntimeArtifact": {}, "agentRuntimeVersion": "1"}

            async def go():
                ac._resolve_runtime = _fake_resolve
                await ac.start(run, root, "develop")
                await ac.launch("arena-run-keep", args=["python", "-c", "pass"])

            asyncio.run(go())
            tar = captured.get("tar", b"")
            self.assertNotEqual(tar, b"")
            names = self._tar_names(tar)
            settings_paths = {n for n in names if "settings.json" in n}
            self.assertNotEqual(settings_paths, set(),
                                "Non-secret settings.json missing from tarball")


# ── Secrets object is uploaded separately ────────────────────────────────────

class SecretChannelUploadTests(unittest.TestCase):
    """When secrets are present in HOME the secrets object must be uploaded to S3."""

    def _run_with_secret(self, kms_arn=None):
        """Launch with a .credentials.json in HOME; return all put_object calls."""
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            ac_kwargs = {}
            if kms_arn:
                ac_kwargs["transfer_kms_key_arn"] = kms_arn
            cfg = Config(runtime="agentcore",
                         data_dir=str(base / "data"),
                         agentcore=AgentCoreConfig(
                             transfer_s3_uri="s3://test-bucket/pfx", **ac_kwargs))
            ac = AgentCore(cfg)
            s3 = Mock()
            put_calls = []

            def fake_put(**kwargs):
                put_calls.append(dict(kwargs))
                return {}

            s3.put_object.side_effect = fake_put
            s3.list_object_versions.return_value = {"Versions": [], "DeleteMarkers": []}
            s3.delete_object.return_value = {}
            ac._s3_client = lambda: s3
            ctrl = Mock()
            ctrl.get_agent_runtime.return_value = {
                "status": "READY", "agentRuntimeArtifact": {}, "agentRuntimeVersion": "1"}
            ctrl.list_agent_runtimes.return_value = {
                "agentRuntimes": [
                    {"agentRuntimeName": "mchc_claude",
                     "agentRuntimeArn": "arn:fake",
                     "agentRuntimeId": "fake"}
                ]
            }
            ac._control_client = lambda: ctrl
            data = Mock()
            data.invoke_agent_runtime.return_value = {
                "response": io.BytesIO(b"{}"), "statusCode": 200}
            data.invoke_agent_runtime_command.return_value = {
                "stream": [{"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}]}
            ac._data_client = lambda: data
            # Prevent the real STS get_caller_identity call inside check().
            _sess = Mock()
            _sess.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
            ac._boto_session = _sess

            root = _make_root(base, "run-chan")
            home = root / "home"
            (home / ".credentials.json").write_bytes(b'{"tok":"x"}')

            run = {"id": "run-chan", "harness": "claude"}

            async def _fake_resolve(h):
                return "arn:fake", "DEFAULT", {
                    "agentRuntimeStatus": "READY",
                    "agentRuntimeArtifact": {}, "agentRuntimeVersion": "1"}

            async def go():
                ac._resolve_runtime = _fake_resolve
                await ac.start(run, root, "develop")
                await ac.launch("arena-run-chan", args=["python", "-c", "pass"])

            asyncio.run(go())
            return put_calls

    def test_secrets_object_is_uploaded(self):
        calls = self._run_with_secret()
        secret_calls = [c for c in calls if c.get("Key", "").endswith(".bin")]
        self.assertEqual(len(secret_calls), 1,
                         f"Expected 1 secrets upload; got {secret_calls}")

    def test_secrets_object_uses_sse_s3_by_default(self):
        calls = self._run_with_secret()
        secret_call = next(c for c in calls if c.get("Key", "").endswith(".bin"))
        self.assertEqual(secret_call["ServerSideEncryption"], "AES256")
        self.assertNotIn("SSEKMSKeyId", secret_call)

    def test_secrets_object_uses_sse_kms_when_key_configured(self):
        arn = "arn:aws:kms:us-east-1:123:key/abc"
        calls = self._run_with_secret(kms_arn=arn)
        secret_call = next(c for c in calls if c.get("Key", "").endswith(".bin"))
        self.assertEqual(secret_call["ServerSideEncryption"], "aws:kms")
        self.assertEqual(secret_call["SSEKMSKeyId"], arn)

    def test_secrets_key_under_secrets_prefix(self):
        calls = self._run_with_secret()
        secret_call = next(c for c in calls if c.get("Key", "").endswith(".bin"))
        self.assertIn("/secrets/", secret_call["Key"])

    def test_no_secrets_means_no_secrets_upload(self):
        """If HOME has no secrets the secrets channel is not used."""
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = Config(runtime="agentcore",
                         data_dir=str(base / "data"),
                         agentcore=AgentCoreConfig(
                             transfer_s3_uri="s3://test-bucket/pfx"))
            ac = AgentCore(cfg)
            s3 = Mock()
            put_calls = []

            def fake_put(**kwargs):
                put_calls.append(dict(kwargs))
                return {}

            s3.put_object.side_effect = fake_put
            s3.list_object_versions.return_value = {"Versions": [], "DeleteMarkers": []}
            s3.delete_object.return_value = {}
            ac._s3_client = lambda: s3
            ctrl = Mock()
            ctrl.get_agent_runtime.return_value = {
                "status": "READY", "agentRuntimeArtifact": {}, "agentRuntimeVersion": "1"}
            ctrl.list_agent_runtimes.return_value = {"agentRuntimes": [
                {"agentRuntimeName": "mchc_claude", "agentRuntimeArn": "arn:fake",
                 "agentRuntimeId": "fake"}]}
            ac._control_client = lambda: ctrl
            data = Mock()
            data.invoke_agent_runtime.return_value = {
                "response": io.BytesIO(b"{}"), "statusCode": 200}
            data.invoke_agent_runtime_command.return_value = {
                "stream": [{"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}]}
            ac._data_client = lambda: data
            # Prevent the real STS get_caller_identity call inside check().
            _sess = Mock()
            _sess.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
            ac._boto_session = _sess

            # HOME with no secrets
            root = _make_root(base, "run-nosec")
            home = root / "home"
            (home / "settings.json").write_text("{}")

            run = {"id": "run-nosec", "harness": "claude"}

            async def _fake_resolve(h):
                return "arn:fake", "DEFAULT", {
                    "agentRuntimeStatus": "READY",
                    "agentRuntimeArtifact": {}, "agentRuntimeVersion": "1"}

            async def go():
                ac._resolve_runtime = _fake_resolve
                await ac.start(run, root, "develop")
                await ac.launch("arena-run-nosec", args=["python", "-c", "pass"])

            asyncio.run(go())
            secret_calls = [c for c in put_calls if c.get("Key", "").endswith(".bin")]
            self.assertEqual(secret_calls, [],
                             "Secrets upload unexpectedly triggered with no secrets")


# ── S3 key namespacing ────────────────────────────────────────────────────────

class S3KeyNamespacingIntegrationTests(unittest.TestCase):
    """Verify job/result/secrets keys all include the namespace segment."""

    def _run_and_capture_keys(self, namespace=None):
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = Config(runtime="agentcore",
                         data_dir=str(base / "data"),
                         agentcore=AgentCoreConfig(
                             transfer_s3_uri="s3://test-bucket/pfx"))
            if namespace:
                cfg.user_namespace = namespace
            ac = AgentCore(cfg)
            s3 = Mock()
            put_keys = []

            def fake_put(**kwargs):
                put_keys.append(kwargs.get("Key", ""))
                return {}

            s3.put_object.side_effect = fake_put
            s3.list_object_versions.return_value = {"Versions": [], "DeleteMarkers": []}
            s3.delete_object.return_value = {}
            ac._s3_client = lambda: s3
            ctrl = Mock()
            ctrl.get_agent_runtime.return_value = {
                "status": "READY", "agentRuntimeArtifact": {}, "agentRuntimeVersion": "1"}
            ctrl.list_agent_runtimes.return_value = {"agentRuntimes": [
                {"agentRuntimeName": "mchc_claude", "agentRuntimeArn": "arn:fake",
                 "agentRuntimeId": "fake"}]}
            ac._control_client = lambda: ctrl
            data = Mock()
            data.invoke_agent_runtime.return_value = {
                "response": io.BytesIO(b"{}"), "statusCode": 200}
            data.invoke_agent_runtime_command.return_value = {
                "stream": [{"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}]}
            ac._data_client = lambda: data
            # Prevent the real STS get_caller_identity call inside check().
            _sess = Mock()
            _sess.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
            ac._boto_session = _sess

            root = _make_root(base, "run-ns")
            home = root / "home"
            (home / ".credentials.json").write_bytes(b"s")

            run = {"id": "run-ns", "harness": "claude"}

            async def _fake_resolve(h):
                return "arn:fake", "DEFAULT", {
                    "agentRuntimeStatus": "READY",
                    "agentRuntimeArtifact": {}, "agentRuntimeVersion": "1"}

            async def go():
                ac._resolve_runtime = _fake_resolve
                await ac.start(run, root, "develop")
                await ac.launch("arena-run-ns", args=["python", "-c", "pass"])

            asyncio.run(go())
            return put_keys

    def test_job_key_includes_namespace(self):
        keys = self._run_and_capture_keys(namespace="alice")
        job_keys = [k for k in keys if "job.tar.gz" in k]
        self.assertTrue(all("alice" in k for k in job_keys),
                        f"Namespace missing from job keys: {job_keys}")

    def test_secrets_key_includes_namespace(self):
        keys = self._run_and_capture_keys(namespace="alice")
        sec_keys = [k for k in keys if k.endswith(".bin")]
        self.assertTrue(all("alice" in k for k in sec_keys),
                        f"Namespace missing from secrets keys: {sec_keys}")

    def test_no_namespace_keeps_original_layout(self):
        keys = self._run_and_capture_keys(namespace=None)
        job_keys = [k for k in keys if "job.tar.gz" in k]
        # Without a namespace, key starts directly with the prefix
        self.assertTrue(all(k.startswith("pfx/") for k in job_keys),
                        f"Unexpected key layout without namespace: {job_keys}")


# ── In-VM: _fetch_and_install_secrets ────────────────────────────────────────

class InVMSecretFetchTests(unittest.TestCase):
    """Tests for the in-VM agent's secrets fetch+install path."""

    def _make_secrets_payload(self, files: dict[str, bytes]) -> bytes:
        """Build a fake secrets object as the host would upload it."""
        payload = {
            "v": 1,
            "files": {p: base64.b64encode(b).decode() for p, b in files.items()},
        }
        return zlib.compress(json.dumps(payload).encode())

    def _fake_boto3(self, body: bytes, versions=None, delete_markers=None):
        s3 = Mock()
        s3.get_object.return_value = {"Body": io.BytesIO(body)}
        s3.list_object_versions.return_value = {
            "Versions": versions or [],
            "DeleteMarkers": delete_markers or [],
        }
        s3.delete_object.return_value = {}
        s3.delete_objects.return_value = {}
        boto3_mod = SimpleNamespace(client=lambda *a, **k: s3)
        return boto3_mod, s3

    def test_secrets_written_to_home_with_correct_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            body = self._make_secrets_payload({".credentials.json": b'{"tok":"x"}'})
            boto3_mod, s3 = self._fake_boto3(body)
            job = {"secrets": {"bucket": "b", "key": "pfx/secrets/sid/t.bin"}}
            with patch.dict(sys.modules, {"boto3": boto3_mod}):
                _fetch_and_install_secrets(job, home, "us-east-1")
            cred = home / ".credentials.json"
            self.assertTrue(cred.exists())
            self.assertEqual(cred.read_bytes(), b'{"tok":"x"}')
            # 0600 permissions
            self.assertEqual(oct(cred.stat().st_mode & 0o777), oct(0o600))

    def test_nested_secret_path_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            body = self._make_secrets_payload(
                {".local/share/kiro-cli/data.sqlite3": b"sqlite"})
            boto3_mod, s3 = self._fake_boto3(body)
            job = {"secrets": {"bucket": "b", "key": "k"}}
            with patch.dict(sys.modules, {"boto3": boto3_mod}):
                _fetch_and_install_secrets(job, home, "us-east-1")
            db = home / ".local" / "share" / "kiro-cli" / "data.sqlite3"
            self.assertTrue(db.exists())
            self.assertEqual(db.read_bytes(), b"sqlite")

    def test_all_versions_deleted_after_reading(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            body = self._make_secrets_payload({".credentials.json": b"s"})
            boto3_mod, s3 = self._fake_boto3(
                body,
                versions=[{"Key": "k", "VersionId": "v1"}, {"Key": "k", "VersionId": "v2"}])
            job = {"secrets": {"bucket": "b", "key": "k"}}
            with patch.dict(sys.modules, {"boto3": boto3_mod}):
                _fetch_and_install_secrets(job, home, "us-east-1")
            # delete_objects called with both version IDs
            s3.delete_objects.assert_called_once()
            obj_keys = {o["VersionId"] for o in
                        s3.delete_objects.call_args[1]["Delete"]["Objects"]}
            self.assertEqual(obj_keys, {"v1", "v2"})

    def test_no_secrets_key_in_job_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            job = {}  # no "secrets" key
            boto3_mod = SimpleNamespace(client=lambda *a, **k: Mock())
            with patch.dict(sys.modules, {"boto3": boto3_mod}):
                _fetch_and_install_secrets(job, home, "us-east-1")
            # No files written
            self.assertEqual(list(home.iterdir()), [])

    def test_unsafe_absolute_path_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            body = self._make_secrets_payload({"/etc/passwd": b"bad"})
            boto3_mod, _ = self._fake_boto3(body)
            job = {"secrets": {"bucket": "b", "key": "k"}}
            with patch.dict(sys.modules, {"boto3": boto3_mod}):
                _fetch_and_install_secrets(job, home, "us-east-1")
            # Nothing written (the absolute path was rejected)
            self.assertEqual(list(home.iterdir()), [])

    def test_dotdot_path_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            body = self._make_secrets_payload({"../escape.txt": b"bad"})
            boto3_mod, _ = self._fake_boto3(body)
            job = {"secrets": {"bucket": "b", "key": "k"}}
            with patch.dict(sys.modules, {"boto3": boto3_mod}):
                _fetch_and_install_secrets(job, home, "us-east-1")
            # No file written outside home
            self.assertFalse((Path(td) / "escape.txt").exists())

    def test_fetch_failure_does_not_raise(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            s3 = Mock()
            s3.get_object.side_effect = Exception("no access")
            boto3_mod = SimpleNamespace(client=lambda *a, **k: s3)
            job = {"secrets": {"bucket": "b", "key": "k"}}
            # Must not raise
            with patch.dict(sys.modules, {"boto3": boto3_mod}):
                _fetch_and_install_secrets(job, home, "us-east-1")


# ── Doctor: list leftover secrets ─────────────────────────────────────────────

class LeftoverSecretsTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_leftover_secrets_returns_objects(self):
        cfg = Config(runtime="agentcore",
                     agentcore=AgentCoreConfig(transfer_s3_uri="s3://b/pfx"))
        ac = AgentCore(cfg)
        s3 = Mock()
        s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "pfx/secrets/sid/t.bin", "LastModified": "2025-01-01"},
            ]
        }
        ac._s3_client = lambda: s3
        result = await ac.list_leftover_secrets()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["key"], "pfx/secrets/sid/t.bin")
        self.assertEqual(result[0]["bucket"], "b")

    async def test_list_leftover_secrets_empty_when_none(self):
        cfg = Config(runtime="agentcore",
                     agentcore=AgentCoreConfig(transfer_s3_uri="s3://b/pfx"))
        ac = AgentCore(cfg)
        s3 = Mock()
        s3.list_objects_v2.return_value = {"Contents": []}
        ac._s3_client = lambda: s3
        result = await ac.list_leftover_secrets()
        self.assertEqual(result, [])

    async def test_list_leftover_secrets_namespaced_prefix(self):
        cfg = Config(runtime="agentcore",
                     agentcore=AgentCoreConfig(transfer_s3_uri="s3://b/pfx"))
        cfg.user_namespace = "alice"
        ac = AgentCore(cfg)
        s3 = Mock()
        s3.list_objects_v2.return_value = {"Contents": []}
        ac._s3_client = lambda: s3
        await ac.list_leftover_secrets()
        prefix_arg = s3.list_objects_v2.call_args[1]["Prefix"]
        self.assertIn("alice", prefix_arg)
        self.assertIn("secrets", prefix_arg)

    async def test_list_leftover_secrets_never_raises(self):
        cfg = Config(runtime="agentcore",
                     agentcore=AgentCoreConfig(transfer_s3_uri="s3://b/pfx"))
        ac = AgentCore(cfg)
        s3 = Mock()
        s3.list_objects_v2.side_effect = Exception("network error")
        ac._s3_client = lambda: s3
        result = await ac.list_leftover_secrets()
        self.assertEqual(result, [])


# ── Import location of remove_tree ────────────────────────────────────────────

class RemoveTreeImportTests(unittest.TestCase):
    def test_remove_tree_comes_from_arena_isolation(self):
        """agentcore_sandbox must import remove_tree from arena.isolation, not artifact_storage."""
        import arena.agentcore_sandbox as sandbox_mod
        import arena.isolation as iso_mod
        # The remove_tree name in the sandbox module must be the same object as
        # the one defined in isolation.
        self.assertIs(sandbox_mod.remove_tree, iso_mod.remove_tree)

    def test_artifact_storage_still_re_exports_remove_tree(self):
        """artifact_storage re-exports remove_tree for backward compat; must still work."""
        from arena.artifact_storage import remove_tree as at_rt
        from arena.isolation import remove_tree as iso_rt
        self.assertIs(at_rt, iso_rt)


# ── stop() secrets cleanup: error, cancellation, and surfacing ────────────────

class SecretsCleanupTests(unittest.IsolatedAsyncioTestCase):
    """Verify the secrets object is always deleted, and failures are surfaced."""

    def _make_ac_with_job(self, *, delete_fails=False):
        """Return (AgentCore, s3_mock) with a pre-registered job containing secrets."""
        cfg = _config()
        ac = AgentCore(cfg)
        if delete_fails:
            s3 = Mock()
            s3.list_object_versions.side_effect = Exception("simulated S3 error")
        else:
            s3 = _fake_s3(versions=[])
        ac._s3_client = lambda: s3
        # Directly register a job as launch() would after uploading secrets.
        ac._jobs["arena-test"] = {
            "run": {"id": "test-run"},
            "root": None,
            "remote": None,
            "bucket": "test-bucket",
            "secrets_s3_key": "secrets/sid/t.bin",
            "s3_key": None,
            "mirror_task": None,
            "relay_thread": None,
        }
        return ac, s3

    async def test_stop_returns_warning_when_deletion_fails(self):
        """stop() returns a non-None warning string when secrets deletion fails."""
        ac, _ = self._make_ac_with_job(delete_fails=True)
        warning = await ac.stop("arena-test")
        self.assertIsNotNone(warning,
                             "Expected a warning string when deletion fails")
        self.assertIsInstance(warning, str)
        # Warning must name the inspection command and the key.
        self.assertIn("multi-coding-harness-compare preflight", warning)
        self.assertIn("list_leftover_secrets", warning)
        self.assertIn("secrets/sid/t.bin", warning)

    async def test_stop_returns_none_on_successful_deletion(self):
        """stop() returns None when secrets deletion succeeds (no false warnings)."""
        ac, _ = self._make_ac_with_job(delete_fails=False)
        warning = await ac.stop("arena-test")
        self.assertIsNone(warning)

    async def test_secrets_deleted_despite_prior_exception(self):
        """Secrets deletion is attempted even when an earlier step in stop() raises.

        Covers: a harness error, network failure, or any unexpected exception that
        interrupts stop() before reaching the secrets-deletion block.
        """
        ac, s3 = self._make_ac_with_job(delete_fails=False)
        # Plant a remote whose wait() raises an unexpected exception — this puts
        # stop() into the finally block before it has cleaned up secrets.
        remote = MagicMock()
        remote.wait = AsyncMock(side_effect=RuntimeError("simulated relay failure"))
        remote._result = None
        ac._jobs["arena-test"]["remote"] = remote

        with self.assertRaises(RuntimeError):
            await ac.stop("arena-test")

        # Deletion must have been attempted inside the finally block.
        self.assertTrue(
            s3.list_object_versions.called or s3.delete_object.called,
            "S3 delete should be called in the finally block even when an "
            "earlier step raises")

    async def test_secrets_deleted_on_cancellation(self):
        """Secrets deletion is attempted even when the stop() task is cancelled.

        Covers: comparison cancelled, worker stopped, or process shutdown while
        stop() is awaiting remote.wait().
        """
        ac, s3 = self._make_ac_with_job(delete_fails=False)
        # Plant a remote whose wait() blocks indefinitely so the task stays inside
        # asyncio.wait_for(remote.wait()) until cancelled.
        never_done = asyncio.Event()
        remote = MagicMock()
        remote._result = None

        async def _blocking_wait():
            await never_done.wait()

        remote.wait = _blocking_wait
        ac._jobs["arena-test"]["remote"] = remote

        task = asyncio.create_task(ac.stop("arena-test"))
        # Yield control so the task starts and enters the wait_for block.
        await asyncio.sleep(0)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        # Deletion must have been attempted in the finally block despite cancellation.
        self.assertTrue(
            s3.list_object_versions.called or s3.delete_object.called,
            "S3 delete should be called in the finally block even on CancelledError")


# ── Worker surfaces stop() warning in the run's error field ──────────────────

class _FakeStream:
    """Async readline() that drains pre-encoded lines then returns b""."""
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeProcess:
    """Subprocess stand-in: exits immediately with returncode=0."""
    returncode = 0

    def __init__(self, events=()):
        self.stdout = _FakeStream(
            [json.dumps(e).encode() + b"\n" for e in events] + [b""])
        self.stderr = _FakeStream([b""])

    async def wait(self):
        return 0


class WorkerCleanupSurfacingTests(unittest.IsolatedAsyncioTestCase):
    """Verify that a stop() warning string is stored in the run's error field."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w = Worker(Config(data_dir=str(Path(self.temp.name) / "data")))
        self.c = self.w.service.create("Test task for surfacing")
        self.cid = self.c["id"]
        self.comparison = self.w.freeze_requirements(self.cid)
        self.runs = self.w.store.detail(self.cid)["runs"]
        self.plan = Plan.model_validate(self.comparison["development_plan"])
        # Workspace must exist so phase() skips the copy_tree step.
        for run in self.runs:
            (self.w.run_root(run) / "workspace").mkdir(parents=True, exist_ok=True)

    def _setup_runtime(self, *, stop_warning=None):
        async def fake_start(run, root, phase):
            (root / "bridge" / "requests").mkdir(parents=True, exist_ok=True)
            (root / "bridge" / "answers").mkdir(parents=True, exist_ok=True)
            return {}

        self.w.runtime.start = fake_start
        self.w.runtime.launch = AsyncMock(
            return_value=_FakeProcess([{"type": "turn_complete", "data": {}}]))
        self.w.runtime.stop = AsyncMock(return_value=stop_warning)
        self.w.runtime.supervise = AsyncMock()

    async def test_stop_warning_surfaced_in_run_error_field(self):
        """When stop() returns a warning string, it is stored in run.error and visible in the UI."""
        warning = ("Secrets object may not be deleted — key secrets/sid/t.bin. "
                   "Run multi-coding-harness-compare preflight to list leftovers.")
        self._setup_runtime(stop_warning=warning)

        run = self.runs[0]
        spec = {**self.comparison, **self.w.store.get("comparison", self.cid)}
        with patch("arena.worker.initialize_checkout", AsyncMock()):
            await self.w.phase(run["id"], "develop", spec, self.plan)

        stored = self.w.store.get("run", run["id"])
        self.assertIsNotNone(stored.get("error"),
                             "Expected run.error to be set when stop() returns a warning")
        self.assertIn(warning, stored["error"],
                      "Warning text from stop() must appear verbatim in run.error")
        # The error event keeps the warning in the run's activity after later
        # transitions (e.g. implemented) reset run.error.
        events = [e for e in self.w.store.events(self.cid)
                  if e["run_id"] == run["id"] and e["type"] == "error"]
        self.assertTrue(any(e["data"].get("message") == warning for e in events),
                        "Expected an error event carrying the warning for the run")

    async def test_no_warning_when_stop_returns_none(self):
        """When stop() returns None, run.error is not set by the cleanup block."""
        self._setup_runtime(stop_warning=None)

        run = self.runs[0]
        spec = {**self.comparison, **self.w.store.get("comparison", self.cid)}
        with patch("arena.worker.initialize_checkout", AsyncMock()):
            await self.w.phase(run["id"], "develop", spec, self.plan)

        stored = self.w.store.get("run", run["id"])
        # On a clean successful turn with no deletion failure, error must not be
        # set by the cleanup block (it may be None or absent entirely).
        self.assertFalse(stored.get("error"),
                         "Unexpected error field when stop() returns None: "
                         + repr(stored.get("error")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
