# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""AgentCore runtime backend tests — fake boto3, no real AWS, no network."""
import asyncio
import base64
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from arena.agentcore_sandbox import (
    BOOTSTRAP, DELIVER, PROBE, AgentCore, RemoteProcess, _build_command,
    _is_retryable_exc, _is_session_start_failure, _relay_thread, _session_epoch, _session_id,
)
from arena.agentcore_agent import _result_archive, _staged_tools_roots, _staged_tools_target
from arena.adapters.common import HarnessTurnError
from arena.bridge import atomic_json
from arena.config import AgentCoreConfig, Config
from arena.isolation import execution_runtime


# ── helpers ──────────────────────────────────────────────────────────────────

def _agentcore_config(**kwargs):
    defaults = dict(transfer_s3_uri="s3://test-bucket/code/ai-coding-assistant-agcore-runtime/")
    defaults.update(kwargs)
    return AgentCoreConfig(**defaults)


def _config(runtime="agentcore", **ac_kwargs):
    ac = _agentcore_config(**ac_kwargs)
    return Config(runtime=runtime, agentcore=ac)


def _run(harness="claude", run_id="run-abc"):
    return {"id": run_id, "harness": harness}


def _workspace_tar(files: dict) -> bytes:
    """Build a minimal result tarball: workspace/<file> entries."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            ti = tarfile.TarInfo(name=f"workspace/{name}")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _event_stream(events):
    """Wrap list of dicts as the fake stream returned by invoke_agent_runtime_command."""
    return {"stream": events}


def _content_start():
    return {"chunk": {"contentStart": {}}}


def _content_delta(stdout=None, stderr=None):
    delta = {}
    if stdout is not None:
        delta["stdout"] = stdout
    if stderr is not None:
        delta["stderr"] = stderr
    return {"chunk": {"contentDelta": delta}}


def _content_stop(exit_code=0, status="COMPLETED"):
    return {"chunk": {"contentStop": {"exitCode": exit_code, "status": status}}}


def _fake_client(stream_events, *, activation_response=None, s3_get_body=None,
                 runtime_info=None, runtime_status="READY"):
    """Return a fake bedrock-agentcore/s3 client pair."""
    ac_client = Mock()

    def _invoke_cmd(**kwargs):
        return _event_stream(stream_events)

    ac_client.invoke_agent_runtime_command.side_effect = _invoke_cmd
    ac_client.invoke_agent_runtime.return_value = {
        "response": io.BytesIO(b"{}"), "statusCode": 200}
    # Session lifecycle is a data-plane operation, like the invoke APIs.
    ac_client.stop_runtime_session.return_value = {}

    ctrl_client = Mock()
    ctrl_client.get_agent_runtime.return_value = {
        "status": runtime_status,
        "agentRuntimeArtifact": {},
        "agentRuntimeVersion": "1",
        **(runtime_info or {}),
    }
    ctrl_client.list_agent_runtimes.return_value = {
        "agentRuntimes": [{"agentRuntimeName": "mchc_claude",
                           "agentRuntimeId": "mchc_claude-fake",
                           "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:123:runtime/mchc_claude-fake"}]
    }

    s3_client = Mock()
    s3_client.put_object.return_value = {}
    s3_client.delete_object.return_value = {}
    s3_client.delete_objects.return_value = {}
    body_bytes = s3_get_body or b""
    s3_obj = {"Body": io.BytesIO(body_bytes)}
    s3_client.get_object.return_value = s3_obj
    s3_client.list_objects_v2.return_value = {"Contents": []}
    # list_object_versions is called by _delete_all_versions_sync.
    # An empty response causes it to fall back to delete_object, keeping tests green.
    s3_client.list_object_versions.return_value = {"Versions": [], "DeleteMarkers": []}

    return ac_client, ctrl_client, s3_client


def _patch_clients(ac, ctrl, s3):
    """Context manager: patch AgentCore's three client factories."""
    def _wrap(self):
        return ac

    def _wrap_ctrl(self):
        return ctrl

    def _wrap_s3(self):
        return s3

    return (
        patch("arena.agentcore_sandbox.AgentCore._data_client", _wrap),
        patch("arena.agentcore_sandbox.AgentCore._control_client", _wrap_ctrl),
        patch("arena.agentcore_sandbox.AgentCore._s3_client", _wrap_s3),
    )


# ── Config validation ─────────────────────────────────────────────────────────

class ConfigValidationTests(unittest.TestCase):
    def test_agentcore_requires_transfer_s3_uri(self):
        with self.assertRaisesRegex(Exception, "transfer_s3_uri"):
            Config(runtime="agentcore")

    def test_agentcore_requires_well_formed_s3_uri(self):
        for bad in ("not-s3://b/p", "s3://bucket", "s3:///prefix"):
            with self.subTest(uri=bad):
                with self.assertRaises(Exception):
                    Config(runtime="agentcore", agentcore=AgentCoreConfig(transfer_s3_uri=bad))

    def test_agentcore_workspace_mount_must_be_under_mnt(self):
        with self.assertRaisesRegex(Exception, "workspace_mount"):
            Config(runtime="agentcore",
                   agentcore=AgentCoreConfig(transfer_s3_uri="s3://b/p",
                                             workspace_mount="/tmp/notmnt"))

    def test_agentcore_job_root_must_not_be_under_mnt(self):
        with self.assertRaisesRegex(Exception, "job_root"):
            Config(runtime="agentcore",
                   agentcore=AgentCoreConfig(transfer_s3_uri="s3://b/p",
                                             job_root="/mnt/overlay"))

    def test_agentcore_job_root_must_be_absolute(self):
        with self.assertRaisesRegex(Exception, "job_root"):
            Config(runtime="agentcore",
                   agentcore=AgentCoreConfig(transfer_s3_uri="s3://b/p",
                                             job_root="relative/path"))

    def test_agentcore_job_root_must_be_writable_by_the_runtime_user(self):
        # Commands run as an unprivileged user, so the image roots are read-only. A
        # job_root there used to fail at mkdir seconds into every run, in all harnesses
        # at once, reported only as "Native process exited 1".
        for bad in ("/opt/mchc", "/mchc", "/var/task/job", "/usr/local/cc"):
            with self.subTest(job_root=bad):
                with self.assertRaisesRegex(Exception, "job_root"):
                    Config(runtime="agentcore",
                           agentcore=AgentCoreConfig(transfer_s3_uri="s3://b/p", job_root=bad))

    def test_agentcore_job_root_accepts_the_writable_overlay(self):
        for good in ("/tmp/mchc", "/var/tmp/mchc", "/home/agent/jobs"):
            with self.subTest(job_root=good):
                c = Config(runtime="agentcore",
                           agentcore=AgentCoreConfig(transfer_s3_uri="s3://b/p", job_root=good))
                self.assertEqual(c.agentcore.job_root, good)

    def test_agentcore_default_job_root_is_writable_and_not_a_temp_dir(self):
        # Codex refuses to create its PATH alias helpers when HOME sits under /tmp
        # ("Refusing to create helper binaries under temporary dir"), so the default
        # has to be writable without being the path that trips that check.
        job_root = AgentCoreConfig().job_root
        self.assertTrue(job_root.startswith("/var/tmp/"))
        self.assertFalse(job_root.startswith("/tmp/"))

    def test_cache_root_sits_on_the_session_storage_mount(self):
        cfg = AgentCoreConfig(transfer_s3_uri="s3://b/p", workspace_mount="/mnt/workspace",
                              cache_dir="mchc-cache")
        self.assertEqual(cfg.cache_root, "/mnt/workspace/mchc-cache")

    def test_session_epoch_zero_keeps_the_original_identifier(self):
        # A run already holding a warm session must not be moved off it by an upgrade.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = "mchc-run-abc-" + __import__("hashlib").sha256(
                ("run-abc" + str(root)).encode()).hexdigest()[:32]
            self.assertEqual(_session_id("run-abc", root, 0), legacy)

    def test_rotating_the_epoch_changes_the_session(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ids = {_session_id("run-abc", root, e) for e in range(3)}
            self.assertEqual(len(ids), 3)

    def test_session_epoch_survives_a_worker_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(_session_epoch(root), 0)
            atomic_json(root / "session-epoch.json", {"epoch": 2})
            self.assertEqual(_session_epoch(root), 2)

    def test_unreadable_epoch_falls_back_to_zero(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "session-epoch.json").write_text("not json")
            self.assertEqual(_session_epoch(root), 0)

    def test_a_refused_session_start_is_recognised(self):
        # RuntimeClientError with this wording means the service declined to start the
        # session; a fresh session ID on the same runtime starts normally.
        refused = Exception("An error occurred (RuntimeClientError) when calling the "
                            "InvokeAgentRuntime operation: An error occurred when starting "
                            "the runtime. Please check your CloudWatch logs for more information.")
        self.assertTrue(_is_session_start_failure(refused))
        self.assertFalse(_is_retryable_exc(refused))
        for other in (Exception("ThrottlingException: slow down"),
                      Exception("ValidationException: bad input")):
            with self.subTest(exc=str(other)[:20]):
                self.assertFalse(_is_session_start_failure(other))

    def test_staged_tools_prefer_the_durable_cache(self):
        with tempfile.TemporaryDirectory() as td:
            cache, fallback = Path(td) / "durable", Path(td) / "job"
            target = _staged_tools_target(
                {"cache_root": str(cache), "cache_fallback": str(fallback)}, "kiro", "2.21.3")
            self.assertEqual(target, cache / "tools" / "kiro-2.21.3")

    def test_staged_tools_fall_back_when_the_mount_is_unusable(self):
        with tempfile.TemporaryDirectory() as td:
            blocked = Path(td) / "blocked"
            blocked.write_text("not a directory")
            fallback = Path(td) / "job"
            target = _staged_tools_target(
                {"cache_root": str(blocked), "cache_fallback": str(fallback)}, "kiro", "2.21.3")
            self.assertEqual(target, fallback / "tools" / "kiro-2.21.3")

    def test_staged_tools_skip_a_cache_root_without_headroom(self):
        # The 1.07 GB sessionStorage mount cannot hold Kiro's ~1 GB unpacked tree.
        # Choosing it filled the filesystem mid-extract and left the harness with no
        # CLI: "[Errno 2] No such file or directory: 'kiro-cli'".
        from collections import namedtuple
        usage = namedtuple("usage", "total used free")
        with tempfile.TemporaryDirectory() as td:
            small, roomy = Path(td) / "mount", Path(td) / "job"
            small.mkdir()
            roomy.mkdir()

            def fake_usage(path):
                return usage(0, 0, 1 * 1024 ** 3 if Path(path) == small else 9 * 1024 ** 3)

            with patch("arena.agentcore_agent.shutil.disk_usage", fake_usage):
                roots = _staged_tools_roots(
                    {"cache_root": str(small), "cache_fallback": str(roomy)})
            self.assertNotIn(str(small), roots)
            self.assertEqual(roots[0], str(roomy))

    def test_staged_tools_use_the_mount_when_it_is_large_enough(self):
        from collections import namedtuple
        usage = namedtuple("usage", "total used free")
        with tempfile.TemporaryDirectory() as td:
            mount = Path(td) / "mount"
            mount.mkdir()
            with patch("arena.agentcore_agent.shutil.disk_usage",
                       lambda path: usage(0, 0, 9 * 1024 ** 3)):
                roots = _staged_tools_roots({"cache_root": str(mount)})
            self.assertEqual(roots[0], str(mount))

    def test_staged_tools_never_target_a_read_only_image_root(self):
        target = _staged_tools_target({}, "kiro", "2.21.3")
        self.assertFalse(str(target).startswith("/opt/"))
        self.assertTrue(str(target).startswith("/tmp/"))

    def test_cache_dir_must_stay_inside_the_mount(self):
        for bad in ("", "/", "../escape"):
            with self.subTest(cache_dir=bad):
                with self.assertRaisesRegex(Exception, "cache_dir"):
                    Config(runtime="agentcore",
                           agentcore=AgentCoreConfig(transfer_s3_uri="s3://b/p", cache_dir=bad))

    def test_native_config_unchanged(self):
        c = Config(runtime="native")
        self.assertEqual(c.runtime, "native")

    def test_docker_runtime_is_rejected(self):
        with self.assertRaisesRegex(Exception, "runtime"):
            Config(runtime="docker")

    def test_inherit_host_setup_with_agentcore_raises(self):
        from arena.config import HarnessConfig
        h = HarnessConfig(inherit_host_setup=True)
        with self.assertRaisesRegex(Exception, "Existing CLI setup"):
            Config(runtime="agentcore",
                   agentcore=AgentCoreConfig(transfer_s3_uri="s3://b/p"),
                   harnesses={"claude": h, "codex": HarnessConfig(), "kiro": HarnessConfig()})

    def test_valid_agentcore_config_accepted(self):
        c = _config()
        self.assertEqual(c.runtime, "agentcore")
        self.assertTrue(c.agentcore.transfer_s3_uri.startswith("s3://"))

    def test_remote_ceiling_follows_the_active_budget(self):
        # The remote clock runs while a turn is paused for a human answer, so a
        # ceiling pinned to 600s killed harnesses that asked a question. It must
        # track active_seconds, and an explicit command_timeout must not be
        # clamped by a ceiling computed from a zero budget.
        from arena.agentcore_sandbox import _effective_command_timeout
        c = _config()
        c.active_seconds = 1800
        self.assertEqual(_effective_command_timeout(c.agentcore, c.active_seconds), 2400)
        self.assertEqual(_effective_command_timeout(c.agentcore, 0), 600)
        explicit = _config(command_timeout=2000)
        self.assertEqual(_effective_command_timeout(explicit.agentcore, 1800), 2000)
        self.assertEqual(_effective_command_timeout(explicit.agentcore, 0), 600)
        self.assertEqual(_effective_command_timeout(_config().agentcore, 100_000), 3600)


class ExecutionRuntimeDispatchTests(unittest.TestCase):
    def test_agentcore_runtime_returns_agentcore_instance(self):
        from arena.agentcore_sandbox import AgentCore
        rt = execution_runtime(_config())
        self.assertIsInstance(rt, AgentCore)

    def test_native_runtime_returns_native(self):
        from arena.native_sandbox import Native
        rt = execution_runtime(Config(runtime="native"))
        self.assertIsInstance(rt, Native)


# ── BOOTSTRAP / DELIVER codec ─────────────────────────────────────────────────

class CodecTests(unittest.TestCase):
    def _decode_code(self, b64_str):
        return zlib.decompress(base64.b64decode(b64_str)).decode()

    def _decode_payload(self, command_str):
        # Extract CC_PAYLOAD=<b64> from the command string
        for token in command_str.split():
            if token.startswith("CC_PAYLOAD="):
                raw = token[len("CC_PAYLOAD="):]
                return json.loads(zlib.decompress(base64.b64decode(raw)))
        raise AssertionError("CC_PAYLOAD not found")

    def test_bootstrap_decodes_to_valid_python(self):
        src = self._decode_code(BOOTSTRAP)
        compile(src, "<bootstrap>", "exec")

    def test_bootstrap_reextracts_over_read_only_files(self):
        # A resumed turn in the same session extracts over the previous attempt's
        # tree, where a harness left read-only files (git pack .idx is 0444).
        src = self._decode_code(BOOTSTRAP)
        with tempfile.TemporaryDirectory() as td:
            jobdir = Path(td) / "job"
            stale = jobdir / "home" / ".codex" / "pack.idx"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"old")
            stale.chmod(0o444)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                ti = tarfile.TarInfo("home/.codex/pack.idx")
                ti.size, ti.mode = 3, 0o444
                tf.addfile(ti, io.BytesIO(b"new"))
            fake_boto3 = SimpleNamespace(client=lambda *a, **k: SimpleNamespace(
                get_object=lambda **kw: {"Body": io.BytesIO(buf.getvalue())}))
            payload = {"bucket": "b", "key": "k", "jobdir": str(jobdir),
                       "home": str(jobdir / "home"), "region": "us-east-1"}
            cmd_env = {"CC_PAYLOAD": base64.b64encode(
                zlib.compress(json.dumps(payload).encode())).decode()}

            class Execed(Exception):
                pass

            def fake_execve(*args):
                raise Execed

            with patch.dict(os.environ, cmd_env), \
                    patch.dict(sys.modules, {"boto3": fake_boto3}), \
                    patch.object(sys, "path", list(sys.path)), \
                    patch("os.execve", fake_execve):
                with self.assertRaises(Execed):
                    exec(src, {"__builtins__": __builtins__})  # noqa: S102
            self.assertEqual(stale.read_bytes(), b"new")

    def test_deliver_decodes_to_valid_python(self):
        src = self._decode_code(DELIVER)
        compile(src, "<deliver>", "exec")

    def test_probe_decodes_to_valid_python(self):
        compile(self._decode_code(PROBE), "<probe>", "exec")

    def test_probe_reports_writability_and_locking_per_path(self):
        src = self._decode_code(PROBE)
        with tempfile.TemporaryDirectory() as td:
            payload = {"paths": [td, str(Path(td) / "missing" / "nested")]}
            cmd_env = {"CC_PAYLOAD": base64.b64encode(
                zlib.compress(json.dumps(payload).encode())).decode()}
            out = io.StringIO()
            with patch.dict(os.environ, cmd_env), patch("sys.stdout", out):
                exec(src, {"__builtins__": __builtins__})  # noqa: S102
            line = next(l for l in out.getvalue().splitlines()
                        if l.startswith("MCHC_PROBE "))
            report = json.loads(line[len("MCHC_PROBE "):])
            self.assertIn("uid", report)
            first = report["paths"][0]
            self.assertTrue(first["write"])
            self.assertTrue(first["sqlite"])
            # The scratch directory must not be left behind.
            self.assertFalse((Path(td) / ".mchc-probe").exists())

    def test_build_command_payload_roundtrips(self):
        payload = {"bucket": "b", "key": "k", "jobdir": "/opt/cc/r", "home": "/mnt/h",
                   "pythonpath": "/opt/cc/r:/var/task", "region": "us-east-1"}
        cmd = _build_command(BOOTSTRAP, json.dumps(payload).encode())
        recovered = self._decode_payload(cmd)
        self.assertEqual(recovered, payload)

    def test_command_is_under_64kb(self):
        payload = {"bucket": "b", "key": "x" * 256, "jobdir": "/opt/cc/r",
                   "home": "/mnt/workspace/h", "pythonpath": "/opt/cc/r:/var/task",
                   "region": "us-east-1"}
        cmd = _build_command(BOOTSTRAP, json.dumps(payload).encode())
        self.assertLess(len(cmd.encode()), 64 * 1024)

    def test_deliver_refuses_absolute_paths(self):
        src = self._decode_code(DELIVER)
        with tempfile.TemporaryDirectory() as td:
            env = {"__builtins__": __builtins__}
            payload = {"dir": td, "files": {"/etc/passwd": "bad"}}
            cmd_env = {"CC_PAYLOAD": base64.b64encode(
                zlib.compress(json.dumps(payload).encode())).decode()}
            with patch.dict(os.environ, cmd_env):
                with self.assertRaises((ValueError, Exception)):
                    exec(src, {"__builtins__": __builtins__,  # noqa: S102
                                "os": os, "json": json, "base64": base64, "zlib": zlib})

    def test_deliver_refuses_dotdot_paths(self):
        src = self._decode_code(DELIVER)
        with tempfile.TemporaryDirectory() as td:
            payload = {"dir": td, "files": {"../escape.txt": "bad"}}
            cmd_env = {"CC_PAYLOAD": base64.b64encode(
                zlib.compress(json.dumps(payload).encode())).decode()}
            with patch.dict(os.environ, cmd_env):
                with self.assertRaises((ValueError, Exception)):
                    exec(src, {"__builtins__": __builtins__,  # noqa: S102
                                "os": os, "json": json, "base64": base64, "zlib": zlib})

    def test_deliver_writes_valid_relative_path(self):
        src = self._decode_code(DELIVER)
        with tempfile.TemporaryDirectory() as td:
            payload = {"dir": td, "files": {"sub/hello.txt": "world"}}
            cmd_env = {"CC_PAYLOAD": base64.b64encode(
                zlib.compress(json.dumps(payload).encode())).decode()}
            with patch.dict(os.environ, cmd_env):
                exec(src, {"__builtins__": __builtins__,  # noqa: S102
                            "os": os, "json": json, "base64": base64, "zlib": zlib})
            self.assertEqual((Path(td) / "sub" / "hello.txt").read_text(), "world")


# ── RemoteProcess ─────────────────────────────────────────────────────────────

class RemoteProcessTests(unittest.IsolatedAsyncioTestCase):
    def _make(self, tmp):
        root = Path(tmp)
        (root / "bridge" / "requests").mkdir(parents=True, exist_ok=True)
        return RemoteProcess(root=root)

    async def test_stdout_fed_line_by_line(self):
        with tempfile.TemporaryDirectory() as td:
            rp = self._make(td)
            rp._feed_stdout_chunk(b"hello\nworld\n")
            rp._eof()
            line1 = await rp.stdout.readline()
            line2 = await rp.stdout.readline()
            self.assertEqual(line1, b"hello\n")
            self.assertEqual(line2, b"world\n")

    async def test_control_envelope_stripped_from_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            rp = self._make(td)
            ctrl = json.dumps({"type": "__mchc", "data": {"kind": "started", "pid": 42}})
            rp._feed_stdout_chunk(ctrl.encode() + b"\n")
            rp._feed_stdout_chunk(b"harness line\n")
            rp._eof()
            line = await rp.stdout.readline()
            self.assertEqual(line, b"harness line\n")
            self.assertEqual(rp.pid, 42)

    async def test_heartbeat_holds_the_stream_open_without_becoming_an_event(self):
        # AgentCore closes a command stream after 15 minutes without a byte, so the
        # supervisor keeps writing during a long silent turn. Those bytes must not
        # reach the worker, which turns every stdout line into a stored event.
        with tempfile.TemporaryDirectory() as td:
            rp = self._make(td)
            beat = json.dumps({"type": "__mchc",
                               "data": {"kind": "heartbeat", "elapsed": 120}})
            rp._feed_stdout_chunk(beat.encode() + b"\n")
            rp._feed_stdout_chunk(b"harness line\n")
            rp._eof()
            self.assertEqual(await rp.stdout.readline(), b"harness line\n")
            self.assertEqual(await rp.stdout.readline(), b"")
            self.assertIsNone(rp._result)

    async def test_bridge_request_written_to_file(self):
        with tempfile.TemporaryDirectory() as td:
            rp = self._make(td)
            ctrl = json.dumps({"type": "__mchc",
                                "data": {"kind": "bridge_request",
                                         "name": "ready.json",
                                         "content": {"type": "ready"}}})
            rp._feed_stdout_chunk(ctrl.encode() + b"\n")
            rp._eof()
            req = Path(td) / "bridge" / "requests" / "ready.json"
            self.assertTrue(req.exists())
            self.assertEqual(json.loads(req.read_text()), {"type": "ready"})

    async def test_result_envelope_captured(self):
        with tempfile.TemporaryDirectory() as td:
            rp = self._make(td)
            ctrl = json.dumps({"type": "__mchc",
                                "data": {"kind": "result",
                                         "bucket": "b", "key": "k"}})
            rp._feed_stdout_chunk(ctrl.encode() + b"\n")
            rp._eof()
            self.assertEqual(rp._result, {"kind": "result", "bucket": "b", "key": "k"})

    async def test_returncode_from_exit_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            rp = self._make(td)
            ctrl = json.dumps({"type": "__mchc",
                                "data": {"kind": "exit", "code": 7}})
            rp._feed_stdout_chunk(ctrl.encode() + b"\n")
            rp._eof()
            self.assertEqual(await asyncio.wait_for(rp.wait(), 2), 7)

    async def test_large_line_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            rp = self._make(td)
            big = b"x" * (65 * 1024) + b"\n"
            rp._feed_stdout_chunk(big)
            rp._eof()
            line = await rp.stdout.readline()
            self.assertEqual(len(line), 65 * 1024 + 1)

    async def test_split_mid_line_chunks_reassembled(self):
        with tempfile.TemporaryDirectory() as td:
            rp = self._make(td)
            rp._feed_stdout_chunk(b"hel")
            rp._feed_stdout_chunk(b"lo\n")
            rp._eof()
            line = await rp.stdout.readline()
            self.assertEqual(line, b"hello\n")


# ── Stream exception → HarnessTurnError mapping ───────────────────────────────

class RelayExceptionMappingTests(unittest.IsolatedAsyncioTestCase):
    async def _run_relay(self, events):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bridge" / "requests").mkdir(parents=True, exist_ok=True)
            remote = RemoteProcess(root=root)
            loop = asyncio.get_running_loop()
            ac_client = Mock()
            ac_client.invoke_agent_runtime_command.return_value = _event_stream(events)
            t = threading.Thread(
                target=_relay_thread,
                args=("arn", "DEFAULT", "sid", "echo hi", 30, loop, remote, ac_client),
                daemon=True)
            t.start()
            await asyncio.wait_for(remote.wait(), 5)
            t.join(timeout=3)
            return remote

    async def test_access_denied_is_terminal(self):
        rp = await self._run_relay(
            [_content_start(), {"accessDeniedException": {"message": "denied"}}])
        self.assertIsInstance(rp._error, HarnessTurnError)
        self.assertTrue(rp._error.terminal)

    async def test_resource_not_found_is_terminal(self):
        rp = await self._run_relay(
            [_content_start(), {"resourceNotFoundException": {"message": "gone"}}])
        self.assertIsInstance(rp._error, HarnessTurnError)
        self.assertTrue(rp._error.terminal)

    async def test_validation_exception_is_terminal(self):
        rp = await self._run_relay(
            [_content_start(), {"validationException": {"message": "bad input"}}])
        self.assertIsInstance(rp._error, HarnessTurnError)
        self.assertTrue(rp._error.terminal)

    async def test_throttling_is_non_terminal(self):
        rp = await self._run_relay(
            [_content_start(), {"throttlingException": {"message": "slow down"}}])
        self.assertIsInstance(rp._error, HarnessTurnError)
        self.assertFalse(rp._error.terminal)

    async def test_internal_server_error_is_non_terminal(self):
        rp = await self._run_relay(
            [_content_start(), {"internalServerException": {"message": "oops"}}])
        self.assertIsInstance(rp._error, HarnessTurnError)
        self.assertFalse(rp._error.terminal)

    async def test_runtime_client_error_is_non_terminal(self):
        rp = await self._run_relay(
            [_content_start(), {"runtimeClientError": {"message": "500"}}])
        self.assertIsInstance(rp._error, HarnessTurnError)
        self.assertFalse(rp._error.terminal)

    async def test_content_stop_sets_returncode_from_exitcode(self):
        rp = await self._run_relay(
            [_content_start(),
             _content_delta(stdout="done\n"),
             _content_stop(exit_code=3)])
        self.assertIsNone(rp._error)
        self.assertEqual(rp.returncode, 3)

    async def test_retryable_conflict_is_retried(self):
        call_count = [0]

        class RetryableConflictException(Exception):
            pass

        def _invoke(**kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise RetryableConflictException("Session operation in progress")
            return _event_stream([_content_start(), _content_stop(exit_code=0)])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bridge" / "requests").mkdir(parents=True, exist_ok=True)
            remote = RemoteProcess(root=root)
            loop = asyncio.get_running_loop()
            ac_client = Mock()
            ac_client.invoke_agent_runtime_command.side_effect = _invoke
            # Patch sleep so test doesn't actually wait
            with patch("arena.agentcore_sandbox.time.sleep"):
                t = threading.Thread(
                    target=_relay_thread,
                    args=("arn", "DEFAULT", "sid", "echo hi", 30, loop, remote, ac_client),
                    daemon=True)
                t.start()
                await asyncio.wait_for(remote.wait(), 5)
                t.join(timeout=3)
            self.assertIsNone(remote._error)
            self.assertGreaterEqual(call_count[0], 3)


# ── Full simulated turn ───────────────────────────────────────────────────────

class FullTurnSimulationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def _make_agentcore(self, stream_events, result_tar=None):
        data_dir = str(self.base / ("data-" + str(id(stream_events))))
        cfg = Config(runtime="agentcore",
                     data_dir=data_dir,
                     agentcore=_agentcore_config())
        ac_client, ctrl_client, s3_client = _fake_client(
            stream_events, s3_get_body=result_tar or b"")
        ac = AgentCore(cfg)
        ac._data_client = lambda: ac_client
        ac._control_client = lambda: ctrl_client
        ac._s3_client = lambda: s3_client
        # Prevent the real STS get_caller_identity call inside check().
        # Pre-populate the boto3 session so _session() never creates a real one.
        _sess = Mock()
        _sess.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
        ac._boto_session = _sess
        return ac, ac_client, ctrl_client, s3_client

    def _make_root(self, run_id="run-sim"):
        root = self.base / run_id
        for d in ("workspace", "bridge/requests", "bridge/answers", "control", "home"):
            (root / d).mkdir(parents=True, exist_ok=True)
        atomic_json(root / "bridge" / "turn.json", {"turn_key": "turn-001", "phase": "develop"})
        atomic_json(root / "control" / "state.json", {"seq": 0, "signal": None})
        return root

    async def test_session_activated_before_first_command(self):
        events = [_content_start(), _content_stop(exit_code=0)]
        ac, ac_client, ctrl_client, _ = self._make_agentcore(events)
        run = _run()
        root = self._make_root(run["id"])

        # Patch _resolve_runtime to return a fake arn
        async def _fake_resolve(harness):
            return ("arn:aws:bedrock-agentcore:us-east-1:123:agent-runtime/x",
                    "DEFAULT",
                    {"agentRuntimeStatus": "READY", "agentRuntimeArtifact": {},
                     "agentRuntimeVersion": "1"})

        ac._resolve_runtime = _fake_resolve
        info = await ac.start(run, root, "develop")
        self.assertEqual(info["harness"], "claude")
        # invoke_agent_runtime called for session activation
        ac_client.invoke_agent_runtime.assert_called_once()

    async def test_full_turn_stdout_strips_control_and_bridge_request_written(self):
        # A realistic event stream: start, split harness lines, bridge_request, result, stop
        bridge_req = json.dumps({"type": "__mchc",
                                  "data": {"kind": "bridge_request",
                                           "name": "ask.json",
                                           "content": {"type": "ask", "q": "hello"}}})
        result_evt = json.dumps({"type": "__mchc",
                                  "data": {"kind": "result",
                                           "bucket": "test-bucket",
                                           "key": "code/ai-coding-assistant-agcore-runtime/sessions/sid/t1/result.tar.gz"}})
        exit_evt = json.dumps({"type": "__mchc",
                                "data": {"kind": "exit", "code": 0}})
        harness_line = b'{"type":"session","data":{"session_id":"s1"}}\n'

        result_tar = _workspace_tar({"main.py": "print('ok')\n"})
        events = [
            _content_start(),
            _content_delta(stdout=harness_line.decode()),
            # split mid-line for bridge_request
            _content_delta(stdout=bridge_req[:30]),
            _content_delta(stdout=bridge_req[30:] + "\n"),
            _content_delta(stdout=result_evt + "\n"),
            _content_delta(stdout=exit_evt + "\n"),
            _content_stop(exit_code=0),
        ]

        # s3 get_object returns the result tarball
        cfg = Config(runtime="agentcore",
                     data_dir=str(self.base / "data-full-turn"),
                     agentcore=_agentcore_config())
        ac_client, ctrl_client, s3_client = _fake_client(events, s3_get_body=result_tar)
        s3_client.get_object.return_value = {"Body": io.BytesIO(result_tar)}
        ac = AgentCore(cfg)
        ac._data_client = lambda: ac_client
        ac._control_client = lambda: ctrl_client
        ac._s3_client = lambda: s3_client
        # Prevent the real STS get_caller_identity call inside check().
        _sess = Mock()
        _sess.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
        ac._boto_session = _sess

        run = _run()
        root = self._make_root(run["id"])

        async def _fake_resolve(harness):
            return ("arn:fake", "DEFAULT",
                    {"agentRuntimeStatus": "READY", "agentRuntimeArtifact": {},
                     "agentRuntimeVersion": "1"})

        ac._resolve_runtime = _fake_resolve
        await ac.start(run, root, "develop")

        name = "arena-" + run["id"]
        remote = await ac.launch(name, args=["python", "-m", "arena.native_runner"])

        # Read the one worker-visible stdout line
        line = await asyncio.wait_for(remote.stdout.readline(), 5)
        self.assertEqual(line, harness_line)

        rc = await asyncio.wait_for(remote.wait(), 5)
        self.assertEqual(rc, 0)

        # Bridge request file written
        ask = root / "bridge" / "requests" / "ask.json"
        self.assertTrue(ask.exists())
        self.assertEqual(json.loads(ask.read_text())["type"], "ask")

        # stop() downloads workspace
        await ac.stop(name)
        self.assertTrue((root / "workspace" / "main.py").exists())

    async def test_stop_is_idempotent_when_launch_never_ran(self):
        events = [_content_stop(exit_code=0)]
        ac, ac_client, _, _ = self._make_agentcore(events)
        # stop a name that was never in _jobs — must not raise
        await ac.stop("arena-nonexistent")
        ac_client.stop_runtime_session.assert_not_called()

    async def test_cleanup_owned_calls_stop_runtime_session(self):
        events = [_content_stop(exit_code=0)]
        ac, ac_client, _, _ = self._make_agentcore(events)

        # Plant a session record
        sessions_dir = ac.config.root / "agentcore-sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(sessions_dir / "sid-x.json", {
            "session_id": "sid-x", "run_id": "r1", "harness": "claude",
            "arn": "arn:fake", "qualifier": "DEFAULT",
            "bucket": "test-bucket", "s3_prefix": "code/ai-coding-assistant-agcore-runtime/sessions/sid-x/",
            "owner": ac.config.container_owner,
            "started_at": time.time(),
        })

        await ac.cleanup_owned()
        ac_client.stop_runtime_session.assert_called_once()


# ── pause / unpause bump state.json ──────────────────────────────────────────

class PauseUnpauseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "control").mkdir()
        (self.root / "bridge" / "requests").mkdir(parents=True, exist_ok=True)
        atomic_json(self.root / "control" / "state.json", {"seq": 0, "signal": None})
        self.ac = AgentCore(_config())
        self.name = "arena-r1"
        self.ac._jobs[self.name] = {"root": self.root}

    async def test_pause_bumps_seq_and_sets_stop(self):
        await self.ac.pause(self.name)
        st = json.loads((self.root / "control" / "state.json").read_text())
        self.assertEqual(st["seq"], 1)
        self.assertEqual(st["signal"], "stop")

    async def test_unpause_bumps_seq_and_sets_cont(self):
        await self.ac.pause(self.name)
        await self.ac.unpause(self.name)
        st = json.loads((self.root / "control" / "state.json").read_text())
        self.assertEqual(st["seq"], 2)
        self.assertEqual(st["signal"], "cont")

    async def test_pause_unknown_name_returns_zero(self):
        result = await self.ac.pause("arena-nope")
        self.assertEqual(result, (0, "", ""))


class ProbeReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_reports_missing_runtimes_as_json(self):
        ac = AgentCore(_config())
        with patch.object(AgentCore, "check", AsyncMock(side_effect=RuntimeError(
                "No AgentCore runtimes are available. Run multi-coding-harness-compare agentcore-deploy"))):
            result = await ac.probe()
        self.assertFalse(result["ok"])
        self.assertIn("No AgentCore runtimes are available", result["error"])
        self.assertEqual(result["runtimes"], {})
        json.dumps(result)


class ClarifySessionStateTests(unittest.IsolatedAsyncioTestCase):
    """A clarify turn's session must reach the host, so a later turn in a new microVM can resume it."""

    def _tree(self, root):
        (root / "workspace").mkdir(parents=True)
        (root / "workspace" / "app.py").write_text("print('baseline')")
        rollout = root / "home" / ".codex" / "sessions" / "rollout-thread.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}")
        return rollout

    def _names(self, data):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            return set(tf.getnames())

    def test_clarify_returns_home_but_never_the_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._tree(root)
            names = self._names(_result_archive(root / "workspace", root / "home", read_only=True))
        self.assertIn("home/.codex/sessions/rollout-thread.jsonl", names)
        self.assertFalse(any(n.startswith("workspace") for n in names))

    def test_develop_returns_workspace_and_home(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._tree(root)
            names = self._names(_result_archive(root / "workspace", root / "home", read_only=False))
        self.assertIn("workspace/app.py", names)
        self.assertIn("home/.codex/sessions/rollout-thread.jsonl", names)

    def test_clarify_without_home_uploads_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_result_archive(Path(td) / "workspace", Path(td) / "home", read_only=True))

    async def test_host_installs_a_home_only_result_without_touching_the_workspace(self):
        with tempfile.TemporaryDirectory() as remote_td, tempfile.TemporaryDirectory() as host_td:
            remote, host = Path(remote_td), Path(host_td)
            self._tree(remote)
            data = _result_archive(remote / "workspace", remote / "home", read_only=True)
            (host / "workspace").mkdir()
            (host / "workspace" / "app.py").write_text("print('host copy')")
            (host / "home").mkdir()
            ac = AgentCore(_config())
            s3 = Mock()
            s3.get_object.return_value = {"Body": io.BytesIO(data)}
            with patch.object(AgentCore, "_s3_client", lambda self: s3):
                await ac._download_workspace(host, "bucket", "key")
            self.assertEqual((host / "workspace" / "app.py").read_text(), "print('host copy')")
            self.assertTrue((host / "home" / ".codex" / "sessions" / "rollout-thread.jsonl").is_file())
            self.assertFalse((host / "workspace.incoming").exists())

    def test_resume_starts_a_new_session_when_codex_has_no_rollout(self):
        from arena.adapters.common import session_missing
        self.assertTrue(session_missing(RuntimeError(
            '{"code": -32600, "message": "no rollout found for thread id 01a1455f"}')))


# ── In-VM supervisor (agentcore_agent) ───────────────────────────────────────

class SupervisorTests(unittest.TestCase):
    def _run_supervisor(self, child_argv, poll_seconds=0.05):
        """Run the supervisor against a real child in a temporary jobdir."""
        import subprocess as sp
        with tempfile.TemporaryDirectory() as td:
            jobdir = Path(td)
            workspace = jobdir / "workspace"
            bridge = jobdir / "bridge"
            control = jobdir / "control"
            home = jobdir / "home"
            tmp = jobdir / "tmp"
            for d in (workspace, bridge / "requests", bridge / "answers", control, tmp, home):
                d.mkdir(parents=True, exist_ok=True)
            atomic_json(control / "state.json", {"seq": 0, "signal": None})

            job = {
                "run_id": "r1", "harness": "claude", "phase": "develop",
                "workspace": str(workspace), "bridge": str(bridge),
                "control": str(control), "home": str(home), "tmp": str(tmp),
                "argv": child_argv, "env": {}, "read_only": False,
                "result": None, "poll_seconds": poll_seconds,
                "region": "us-east-1",
            }
            (jobdir / "job.json").write_text(json.dumps(job))

            import sys
            result = sp.run(
                [sys.executable, "-m", "arena.agentcore_agent", str(jobdir)],
                cwd=str(jobdir), capture_output=True,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)},
                timeout=15,
            )
            stdout = result.stdout.decode(errors="replace")
            stderr = result.stderr.decode(errors="replace")
            return result.returncode, stdout, stderr, jobdir

    def test_supervisor_forwards_child_stdout_verbatim(self):
        import sys
        code = "import sys; sys.stdout.write('hello from child\\n'); sys.stdout.flush()"
        rc, out, _err, _ = self._run_supervisor([sys.executable, "-c", code])
        self.assertIn("hello from child", out)
        self.assertEqual(rc, 0)

    def test_supervisor_emits_started_envelope(self):
        import sys
        code = "pass"
        rc, out, _err, _ = self._run_supervisor([sys.executable, "-c", code])
        lines = [l for l in out.splitlines() if '"__mchc"' in l or '"type":"__mchc"' in l]
        kinds = []
        for l in out.splitlines():
            try:
                m = json.loads(l)
                if isinstance(m, dict) and m.get("type") == "__mchc":
                    kinds.append(m["data"]["kind"])
            except Exception:
                pass
        self.assertIn("started", kinds)
        self.assertIn("exit", kinds)

    def test_supervisor_bridge_request_emitted_when_file_appears(self):
        import sys
        code = (
            "import json, os, time\n"
            "from pathlib import Path\n"
            "br = Path(os.environ['ARENA_BRIDGE']) / 'requests' / 'rq1.json'\n"
            "br.write_text(json.dumps({'type':'ask','q':'hi'}))\n"
            "time.sleep(0.15)\n"
        )
        rc, out, _err, _ = self._run_supervisor([sys.executable, "-c", code])
        kinds = []
        for l in out.splitlines():
            try:
                m = json.loads(l)
                if isinstance(m, dict) and m.get("type") == "__mchc":
                    kinds.append(m["data"]["kind"])
            except Exception:
                pass
        self.assertIn("bridge_request", kinds)

    def test_supervisor_exits_with_child_returncode(self):
        import sys
        code = "raise SystemExit(5)"
        rc, _out, _err, _ = self._run_supervisor([sys.executable, "-c", code])
        # Supervisor maps negative signals to 128+abs; 5 is direct
        self.assertEqual(rc, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
