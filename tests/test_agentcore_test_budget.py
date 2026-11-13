# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for fix L4: AgentCore InvokeAgentRuntimeCommand timeout derives from the
test budget (test_active_seconds / remaining_test_budget_seconds), never from the
development active_seconds, for test-stage phases.

Covers:
  - start() limits dict reports test_active_seconds for test phases, active_seconds otherwise
  - launch() derives timeout from test_active_seconds for test phases
  - launch() derives timeout from remaining_test_budget_seconds when set in the job
  - launch() still uses active_seconds for development phases
"""
import asyncio
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from arena.agentcore_sandbox import (
    AgentCore, _effective_command_timeout, _relay_thread,
)
from arena.bridge import atomic_json
from arena.config import AgentCoreConfig, Config


# ── helpers ──────────────────────────────────────────────────────────────────

def _agentcore_config(**kwargs):
    defaults = dict(transfer_s3_uri="s3://test-bucket/code/")
    defaults.update(kwargs)
    return AgentCoreConfig(**defaults)


def _config(active_seconds=1800, test_active_seconds=3600, **ac_kwargs):
    ac = _agentcore_config(**ac_kwargs)
    cfg = Config(runtime="agentcore", agentcore=ac)
    cfg.active_seconds = active_seconds
    cfg.test_active_seconds = test_active_seconds
    return cfg


def _make_root(base: Path, run_id: str) -> Path:
    root = base / run_id
    for d in ("workspace", "bridge/requests", "bridge/answers", "control", "home"):
        (root / d).mkdir(parents=True, exist_ok=True)
    atomic_json(root / "control" / "state.json", {"seq": 0, "signal": None})
    return root


def _make_agentcore(base: Path, active_seconds=1800, test_active_seconds=3600) -> AgentCore:
    cfg = _config(active_seconds=active_seconds, test_active_seconds=test_active_seconds,
                  data_dir=str(base / "data"))
    ac = AgentCore(cfg)
    # Stub out all network clients so no real AWS calls happen.
    s3 = Mock()
    s3.put_object = Mock()
    s3.list_object_versions.return_value = {"Versions": [], "DeleteMarkers": []}
    ac._data_client = lambda: Mock()
    ac._control_client = lambda: Mock()
    ac._s3_client = lambda: s3
    # Pre-populate the boto3 session for check() / STS calls.
    sess = Mock()
    sess.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
    ac._boto_session = sess
    return ac


def _inject_job(ac: AgentCore, run_id: str, phase: str,
                remaining_test_budget_seconds=None) -> str:
    """Inject a pre-built job dict into ac._jobs and return the container name."""
    name = "arena-" + run_id
    root = Path(ac.config.root) / "runs" / run_id
    for d in ("workspace", "bridge/requests", "bridge/answers", "control", "home"):
        (root / d).mkdir(parents=True, exist_ok=True)
    atomic_json(root / "bridge" / "turn.json", {"turn_key": "turn-001", "phase": phase})
    atomic_json(root / "control" / "state.json", {"seq": 0, "signal": None})
    job = {
        "run": {"id": run_id, "harness": "claude"},
        "root": root,
        "phase": phase,
        "harness": "claude",
        "session_id": "sid-abc",
        "arn": "arn:aws:bedrock-agentcore:us-east-1:123:agent-runtime/x",
        "qualifier": "DEFAULT",
        "jobdir": "/var/tmp/mchc/" + run_id,
        "home_remote": "/var/tmp/mchc/" + run_id + "/home",
        "tools_uri": None,
    }
    if remaining_test_budget_seconds is not None:
        job["remaining_test_budget_seconds"] = remaining_test_budget_seconds
    ac._jobs[name] = job
    return name


# ── start() limits dict ───────────────────────────────────────────────────────

class StartLimitsDictTests(unittest.IsolatedAsyncioTestCase):
    """start() must report the correct active_seconds in the returned limits dict."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def _fake_resolve(self):
        async def _inner(harness):
            return (
                "arn:aws:bedrock-agentcore:us-east-1:123:agent-runtime/x",
                "DEFAULT",
                {"status": "READY", "agentRuntimeArtifact": {},
                 "agentRuntimeVersion": "1",
                 "environmentVariables": {}},
            )
        return _inner

    async def _do_start(self, phase, active_seconds=1800, test_active_seconds=3600):
        ac = _make_agentcore(self.base, active_seconds=active_seconds,
                             test_active_seconds=test_active_seconds)
        ac._resolve_runtime = self._fake_resolve()
        run = {"id": "run-s1", "harness": "claude"}
        root = _make_root(self.base, "run-s1")
        return await ac.start(run, root, phase)

    async def test_start_non_test_phase_reports_active_seconds(self):
        info = await self._do_start("develop", active_seconds=1800, test_active_seconds=3600)
        self.assertEqual(info["limits"]["active_seconds"], 1800)

    async def test_start_clarify_phase_reports_active_seconds(self):
        info = await self._do_start("clarify", active_seconds=900, test_active_seconds=3600)
        self.assertEqual(info["limits"]["active_seconds"], 900)

    async def test_start_test_phase_reports_test_active_seconds(self):
        info = await self._do_start("test", active_seconds=1800, test_active_seconds=3600)
        self.assertEqual(info["limits"]["active_seconds"], 3600)

    async def test_start_repair_phase_reports_test_active_seconds(self):
        info = await self._do_start("repair", active_seconds=1800, test_active_seconds=4000)
        self.assertEqual(info["limits"]["active_seconds"], 4000)

    async def test_start_fix_phase_reports_test_active_seconds(self):
        info = await self._do_start("fix", active_seconds=1800, test_active_seconds=5000)
        self.assertEqual(info["limits"]["active_seconds"], 5000)

    async def test_start_command_timeout_uses_test_budget_for_test_phases(self):
        """The command_timeout in the limits dict must reflect test_active_seconds."""
        # With active_seconds=1800: effective = min(3600, 1800+600) = 2400
        # With test_active_seconds=3600: effective = min(3600, 3600+600) = 3600
        info = await self._do_start("test", active_seconds=1800, test_active_seconds=3600)
        self.assertEqual(info["limits"]["command_timeout"], 3600)

    async def test_start_command_timeout_uses_dev_budget_for_dev_phases(self):
        """The command_timeout for development phases uses active_seconds."""
        # With active_seconds=1800: effective = min(3600, 1800+600) = 2400
        info = await self._do_start("develop", active_seconds=1800, test_active_seconds=3600)
        self.assertEqual(info["limits"]["command_timeout"], 2400)


# ── launch() timeout ─────────────────────────────────────────────────────────

class LaunchTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """launch() must pass the right timeout to the relay thread.

    The relay thread args are (arn, qualifier, session_id, command, timeout, loop,
    remote, client).  We capture args[4] (the timeout) by replacing threading.Thread
    with a fake that records what it was constructed with and does not actually start.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def _captured_timeout(self, ac, name) -> int:
        """Run launch() with mocked Thread/task and return the captured timeout."""
        captured = []

        class _FakeThread:
            def __init__(self_th, target=None, args=(), daemon=False, name=None):
                # Relay thread: (arn, qualifier, session_id, command, TIMEOUT, loop, remote, client)
                if target is _relay_thread:
                    captured.append(args[4])  # timeout is arg 4

            def start(self_th):
                pass

            def join(self_th, timeout=None):
                pass

        # Replace _mirror_loop with a coroutine that immediately returns to prevent
        # "coroutine never awaited" warnings when create_task receives the real one.
        async def _noop_mirror(*args, **kwargs):
            return

        ac._mirror_loop = _noop_mirror

        loop = asyncio.get_event_loop()

        async def _run():
            with patch("arena.agentcore_sandbox.threading.Thread", _FakeThread):
                with patch("asyncio.to_thread", new=AsyncMock(return_value=None)):
                    await ac.launch(name)

        loop.run_until_complete(_run())
        self.assertEqual(len(captured), 1, "Expected exactly one relay Thread call")
        return captured[0]

    def _launch_timeout(self, phase, active_seconds=1800, test_active_seconds=3600,
                        remaining=None):
        ac = _make_agentcore(self.base, active_seconds=active_seconds,
                             test_active_seconds=test_active_seconds)
        name = _inject_job(ac, f"run-{phase}", phase,
                           remaining_test_budget_seconds=remaining)
        return self._captured_timeout(ac, name)

    def test_develop_phase_uses_active_seconds(self):
        # active_seconds=1800 → timeout = min(3600, 1800+600) = 2400
        t = self._launch_timeout("develop", active_seconds=1800)
        self.assertEqual(t, 2400)

    def test_clarify_phase_uses_active_seconds(self):
        t = self._launch_timeout("clarify", active_seconds=1800)
        self.assertEqual(t, 2400)

    def test_test_phase_uses_test_active_seconds(self):
        # test_active_seconds=3600 → timeout = min(3600, 3600+600) = 3600
        t = self._launch_timeout("test", active_seconds=1800, test_active_seconds=3600)
        self.assertEqual(t, 3600)

    def test_repair_phase_uses_test_active_seconds(self):
        t = self._launch_timeout("repair", active_seconds=1800, test_active_seconds=3600)
        self.assertEqual(t, 3600)

    def test_fix_phase_uses_test_active_seconds(self):
        t = self._launch_timeout("fix", active_seconds=1800, test_active_seconds=3600)
        self.assertEqual(t, 3600)

    def test_test_phase_not_capped_by_active_seconds(self):
        """When test_active_seconds > active_seconds the ceiling is NOT active_seconds."""
        # active_seconds=900, test_active_seconds=3600
        # Without the fix: min(3600, 900+600) = 1500
        # With the fix:    min(3600, 3600+600) = 3600
        t = self._launch_timeout("test", active_seconds=900, test_active_seconds=3600)
        self.assertGreater(t, 900 + 600)
        self.assertEqual(t, 3600)

    def test_remaining_test_budget_overrides_full_test_budget(self):
        """When job['remaining_test_budget_seconds'] is set, that value drives the timeout."""
        # remaining=1200 → timeout = min(3600, 1200+600) = 1800
        t = self._launch_timeout("test", active_seconds=1800, test_active_seconds=3600,
                                 remaining=1200)
        self.assertEqual(t, _effective_command_timeout(_agentcore_config(), 1200))

    def test_remaining_test_budget_for_repair_phase(self):
        """remaining_test_budget_seconds applies to repair turns too."""
        t = self._launch_timeout("repair", active_seconds=1800, test_active_seconds=3600,
                                 remaining=600)
        self.assertEqual(t, _effective_command_timeout(_agentcore_config(), 600))

    def test_remaining_test_budget_for_fix_phase(self):
        t = self._launch_timeout("fix", active_seconds=1800, test_active_seconds=3600,
                                 remaining=800)
        self.assertEqual(t, _effective_command_timeout(_agentcore_config(), 800))

    def test_develop_phase_ignores_remaining_test_budget(self):
        """remaining_test_budget_seconds in a develop job has no effect on the timeout."""
        # Even if the job carries the key, develop phases must use active_seconds.
        t = self._launch_timeout("develop", active_seconds=1800, test_active_seconds=3600,
                                 remaining=600)
        # Should use active_seconds=1800, not remaining=600
        self.assertEqual(t, _effective_command_timeout(_agentcore_config(), 1800))

    def test_timeout_never_derived_from_active_seconds_for_test_phase(self):
        """Test phase timeout must NOT equal what active_seconds would produce
        when test_active_seconds differs from active_seconds."""
        dev_timeout = _effective_command_timeout(_agentcore_config(), 1800)   # 2400
        test_timeout = _effective_command_timeout(_agentcore_config(), 3600)  # 3600
        t = self._launch_timeout("test", active_seconds=1800, test_active_seconds=3600)
        self.assertNotEqual(t, dev_timeout)
        self.assertEqual(t, test_timeout)
