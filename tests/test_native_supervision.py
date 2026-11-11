# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Process lifecycle regressions; no model calls or production database writes."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from arena.config import Config
from arena.evaluation import default_plan
from arena.isolation import command
from arena.native_sandbox import Native, _process_table
from arena.preflight import shell_restrictions
from arena.prompts import READINESS_REMINDER
from arena.worker import Worker


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_preserved_if_child_exits_before_kill(self):
        process = Mock(returncode=0)
        process.communicate = AsyncMock(side_effect=[TimeoutError(), (b"", b"")])
        process.wait = AsyncMock(return_value=0)
        process.kill.side_effect = ProcessLookupError()
        with patch("arena.isolation.asyncio.create_subprocess_exec", return_value=process):
            with self.assertRaises(TimeoutError):
                await command("unused")
        process.communicate.assert_awaited()

    async def test_cancellation_preserved_if_child_exits_before_kill(self):
        process = Mock(returncode=0)
        process.communicate = AsyncMock(side_effect=[asyncio.CancelledError(), (b"", b"")])
        process.wait = AsyncMock(return_value=0)
        process.kill.side_effect = ProcessLookupError()
        with patch("arena.isolation.asyncio.create_subprocess_exec", return_value=process):
            with self.assertRaises(asyncio.CancelledError):
                await command("unused")

    async def test_timeout_and_cancellation_reap_real_noisy_children(self):
        original = asyncio.create_subprocess_exec
        children = []

        async def launch(*args, **kwargs):
            child = await original(*args, **kwargs)
            children.append(child)
            return child

        for cancel in (False, True):
            with self.subTest(cancel=cancel), patch(
                    "arena.isolation.asyncio.create_subprocess_exec", side_effect=launch):
                task = asyncio.create_task(command(sys.executable, "-c",
                    "import os,time\nwhile True:\n os.write(1,b'x'*65536)\n time.sleep(.001)",
                    timeout=30 if cancel else .1))
                if cancel:
                    await asyncio.sleep(.1)
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                    await asyncio.wait_for(task, 3)
                self.assertIsNotNone(children[-1].returncode)

    async def test_output_input_and_nonzero_exit_are_unchanged(self):
        result = await command(sys.executable, "-c",
            "import sys; print(sys.stdin.read()); print('failure detail',file=sys.stderr); sys.exit(7)",
            input=b"input")
        self.assertEqual(result, (7, "input\n", "failure detail\n"))


class HarnessStartupTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """A stalled startup must name itself and stay retryable.

    A codex turn died reporting the single word "TimeoutError", because
    asyncio.wait_for raises one with no message and the runner falls back to the
    class name. Measured cold in an AgentCore microVM, app-server answers
    initialize in 26-51s, so the adapter's hardcoded 30s was always marginal.
    """

    async def _silent_rpc(self):
        from arena.adapters.common import RPC
        rpc = await RPC([sys.executable, "-c", "import time; time.sleep(30)"],
                        AsyncMock(), AsyncMock()).start()
        self.addAsyncCleanup(rpc.close)
        return rpc

    async def test_stalled_call_names_the_method_and_stays_retryable(self):
        from arena.adapters.common import HarnessTurnError
        rpc = await self._silent_rpc()
        with self.assertRaises(HarnessTurnError) as caught:
            await rpc.call("initialize", {}, 0.2)
        self.assertFalse(caught.exception.terminal)
        message = str(caught.exception)
        self.assertIn("initialize", message)
        self.assertIn("0.2s", message)
        self.assertIn("still running", message)
        # The bare class name is what reached the operator before; never again.
        self.assertNotEqual(message, "TimeoutError")

    async def test_a_dead_cli_is_reported_as_a_closed_stream_not_a_stall(self):
        # The two failures must stay distinguishable: the reader sees EOF and
        # resolves waiters, so a stall always means the CLI is alive but slow.
        from arena.adapters.common import RPC
        rpc = await RPC([sys.executable, "-c", "pass"], AsyncMock(), AsyncMock()).start()
        self.addAsyncCleanup(rpc.close)
        with self.assertRaises((RuntimeError, ConnectionResetError)) as caught:
            await rpc.call("initialize", {}, 5)
        self.assertNotIsInstance(caught.exception, TimeoutError)

    async def test_pending_future_is_discarded_after_a_stall(self):
        from arena.adapters.common import HarnessTurnError
        rpc = await self._silent_rpc()
        with self.assertRaises(HarnessTurnError):
            await rpc.call("initialize", {}, 0.2)
        self.assertFalse(rpc.pending)

    async def test_version_check_is_bounded_and_retryable(self):
        from arena.adapters.common import HarnessTurnError, version
        with self.assertRaises(HarnessTurnError) as caught:
            await version(sys.executable, "-c", "import time; time.sleep(30)", timeout=0.2)
        self.assertFalse(caught.exception.terminal)
        self.assertIn("did not report its version", str(caught.exception))

    async def test_version_check_without_a_timeout_still_works(self):
        from arena.adapters.common import version
        self.assertEqual(await version(sys.executable, "-c", "print('v1')"), "v1")

    async def test_codex_startup_spends_the_configured_setup_budget(self):
        from arena.adapters.codex import CodexAdapter
        for turn, expected in (({"setup_seconds": 300}, 300), ({}, 90)):
            with self.subTest(turn=turn):
                adapter = CodexAdapter({**turn, "config": {"auto_approve": True}, "prompt": "p"})
                calls = []

                async def record(method, params, timeout=None):
                    calls.append((method, timeout))
                    raise RuntimeError("stop after initialize")

                rpc = Mock(call=record, close=AsyncMock())
                with patch("arena.adapters.codex.version", AsyncMock(return_value="codex-cli 0.154.0")), \
                     patch("arena.adapters.codex.RPC") as constructor:
                    constructor.return_value.start = AsyncMock(return_value=rpc)
                    with self.assertRaises(RuntimeError):
                        await adapter.run()
                self.assertEqual(calls, [("initialize", expected)])

    def test_a_missing_session_is_told_apart_from_a_real_error(self):
        from arena.adapters.common import session_missing
        # The wording the CLIs actually produce, wrapped by RPC.read as JSON text.
        gone = RuntimeError('{"code": -32603, "message": "Internal error", "data": '
                            '"Failed to start session: Session not found: 7479c161"}')
        self.assertTrue(session_missing(gone))
        for other in (RuntimeError('{"message": "permission denied"}'),
                      RuntimeError("Native protocol stream closed")):
            with self.subTest(exc=str(other)[:24]):
                self.assertFalse(session_missing(other))

    async def test_kiro_starts_fresh_when_its_session_is_gone(self):
        # A turn killed before uploading never returns HOME, so the session it created
        # is unreachable while the host still offers its id. Failing forever on that is
        # worse than losing context that did not survive anyway.
        from arena.adapters.kiro import KiroAdapter
        adapter = KiroAdapter({"setup_seconds": 90, "config": {}, "prompt": "p",
                               "session_id": "7479c161"})
        calls = []

        async def record(method, params, timeout=None):
            calls.append(method)
            if method == "initialize":
                return {"agentCapabilities": {"loadSession": True}}
            if method == "session/load":
                raise RuntimeError('{"data": "Failed to start session: Session not found: 7479c161"}')
            if method == "session/new":
                return {"sessionId": "fresh-session"}
            raise RuntimeError("stop after session setup")

        with patch("arena.adapters.kiro.version", AsyncMock(return_value="kiro 2.21.3")), \
             patch("arena.adapters.kiro.RPC") as constructor:
            constructor.return_value.start = AsyncMock(
                return_value=Mock(call=record, close=AsyncMock()))
            with self.assertRaises(RuntimeError):
                await adapter.run()
        self.assertEqual(calls[:3], ["initialize", "session/load", "session/new"])
        self.assertEqual(adapter.session_id, "fresh-session")

    async def test_kiro_does_not_mask_a_real_session_load_failure(self):
        from arena.adapters.kiro import KiroAdapter
        adapter = KiroAdapter({"setup_seconds": 90, "config": {}, "prompt": "p",
                               "session_id": "7479c161"})
        calls = []

        async def record(method, params, timeout=None):
            calls.append(method)
            if method == "initialize":
                return {"agentCapabilities": {"loadSession": True}}
            raise RuntimeError('{"message": "permission denied"}')

        with patch("arena.adapters.kiro.version", AsyncMock(return_value="kiro 2.21.3")), \
             patch("arena.adapters.kiro.RPC") as constructor:
            constructor.return_value.start = AsyncMock(
                return_value=Mock(call=record, close=AsyncMock()))
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                await adapter.run()
        self.assertNotIn("session/new", calls)

    async def test_codex_starts_a_new_thread_when_the_old_one_is_gone(self):
        from arena.adapters.codex import CodexAdapter
        adapter = CodexAdapter({"setup_seconds": 90, "config": {"auto_approve": True},
                                "prompt": "p", "session_id": "thread-1"})
        calls = []

        async def record(method, params, timeout=None):
            calls.append((method, params.get("threadId")))
            if method in ("initialize", "account/read"):
                return {"requiresOpenaiAuth": False, "account": {"type": "amazonBedrock"}}
            if method == "thread/resume":
                raise RuntimeError('{"data": "thread not found: thread-1"}')
            if method == "thread/start":
                return {"thread": {"id": "thread-2"}}
            raise RuntimeError("stop after thread setup")

        with patch("arena.adapters.codex.version", AsyncMock(return_value="codex-cli 0.154.0")), \
             patch("arena.adapters.codex.RPC") as constructor:
            constructor.return_value.start = AsyncMock(
                return_value=Mock(call=record, notify=AsyncMock(), close=AsyncMock()))
            with self.assertRaises(RuntimeError):
                await adapter.run()
        self.assertIn(("thread/resume", "thread-1"), calls)
        self.assertIn(("thread/start", None), calls)
        self.assertEqual(adapter.session_id, "thread-2")

    async def test_kiro_startup_spends_the_configured_setup_budget(self):
        from arena.adapters.kiro import KiroAdapter
        adapter = KiroAdapter({"setup_seconds": 300, "config": {}, "prompt": "p"})
        calls = []

        async def record(method, params, timeout=None):
            calls.append((method, timeout))
            raise RuntimeError("stop after initialize")

        rpc = Mock(call=record, close=AsyncMock())
        with patch("arena.adapters.kiro.version", AsyncMock(return_value="kiro 1.0")), \
             patch("arena.adapters.kiro.RPC") as constructor:
            constructor.return_value.start = AsyncMock(return_value=rpc)
            with self.assertRaises(RuntimeError):
                await adapter.run()
        self.assertEqual(calls, [("initialize", 300)])


class InspectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.native = Native(Config(data_dir=self.temp.name))
        self.rows = {
            110: {"parent": 1, "group": 110, "rss": 1024, "start": "root"},
            111: {"parent": 110, "group": 110, "rss": 2048, "start": "child"},
            210: {"parent": 1, "group": 210, "rss": 1024, "start": "second"},
            310: {"parent": 1, "group": 310, "rss": 1024, "start": "third"},
        }
        for name, pid in (("claude", 110), ("codex", 210), ("kiro", 310)):
            self.native.jobs[name] = {"pid": pid, "known": {}}

    async def test_transient_inspection_timeout_retries_fresh_scan(self):
        result = subprocess.CompletedProcess([], 0,
            "110 1 110 1 Tue Sep 15 10:00:00 2026\n", "")
        with patch("arena.native_sandbox.subprocess.run",
                   side_effect=[subprocess.TimeoutExpired("ps", 5), result]) as run:
            rows = await _process_table()
        self.assertEqual(rows[110]["rss"], 1024)
        self.assertEqual(run.call_count, 2)

    async def test_persistent_inspection_failure_is_bounded(self):
        with patch("arena.native_sandbox.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("ps", 5)) as run:
            with self.assertRaisesRegex(RuntimeError, "supervision unavailable"):
                await _process_table()
        self.assertEqual(run.call_count, 2)
        self.assertLessEqual(sum(call.kwargs["timeout"] for call in run.call_args_list), 15)

    async def test_inspection_permission_failure_is_not_ignored(self):
        with patch("arena.native_sandbox.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, "", "Operation not permitted")):
            with self.assertRaisesRegex(RuntimeError, "Operation not permitted"):
                await _process_table()

    async def test_process_inspection_does_not_depend_on_event_loop_pipe_reads(self):
        started, finished = threading.Event(), threading.Event()
        main_thread = threading.get_ident()

        def read(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
            started.set()
            time.sleep(.03)
            finished.set()
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("arena.native_sandbox.subprocess.run", side_effect=read):
            task = asyncio.create_task(_process_table())
            while not started.is_set():
                await asyncio.sleep(.001)
            # Reproduce an occupied event loop while the monitoring child exits.
            time.sleep(.1)
            self.assertTrue(finished.is_set())
            self.assertEqual(await task, {})

    async def test_three_harnesses_share_scan_and_one_cancellation_does_not_cancel_it(self):
        gate = asyncio.Event()

        async def scan():
            await gate.wait()
            return self.rows

        with patch("arena.native_sandbox._process_table", side_effect=scan) as read:
            tasks = [asyncio.create_task(self.native.supervise(name, force=True))
                     for name in self.native.jobs]
            await asyncio.sleep(.01)
            tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
            gate.set()
            await asyncio.gather(*tasks[1:])
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.native.jobs["codex"]["known"], {210: "second"})
        self.assertEqual(self.native.jobs["kiro"]["known"], {310: "third"})

    async def test_failed_scan_does_not_poison_next_scan_or_replace_last_good_snapshot(self):
        with patch("arena.native_sandbox._process_table",
                   side_effect=[self.rows, RuntimeError("inspection unavailable"), self.rows]):
            await self.native.supervise("claude", force=True)
            with self.assertRaises(RuntimeError):
                await self.native.supervise("claude", force=True)
            self.assertEqual(self.native.process_snapshot, self.rows)
            await self.native.supervise("claude", force=True)
        self.assertEqual(self.native.jobs["claude"]["known"], {110: "root", 111: "child"})

    async def test_memory_and_process_limits_still_reject_excess(self):
        for limit in ({"memory": "1k"}, {"pids": 1}):
            with self.subTest(limit=limit), patch(
                    "arena.native_sandbox._process_table", return_value=self.rows):
                self.native.config = Config(data_dir=self.temp.name, **limit)
                with self.assertRaisesRegex(RuntimeError, "resource limit exceeded"):
                    await self.native.supervise("claude", force=True)

    async def test_signals_ignore_reused_pids_and_child_exit_races(self):
        job = self.native.jobs["claude"]
        job.update(root_start="root", known={110: "root", 111: "old child"})
        with patch("arena.native_sandbox._process_table", return_value=self.rows), \
                patch("arena.native_sandbox.os.kill", side_effect=ProcessLookupError()) as kill, \
                patch("arena.native_sandbox.os.killpg", side_effect=ProcessLookupError()) as killpg:
            await self.native._signal("claude", signal.SIGTERM)
        kill.assert_called_once_with(110, signal.SIGTERM)
        killpg.assert_called_once_with(110, signal.SIGTERM)

    async def test_group_exit_permission_race_is_ignored_only_after_fresh_scan(self):
        self.native.jobs["claude"].update(root_start="root", known={110: "root"})
        for fresh, raises in (({}, False), (self.rows, True)):
            with self.subTest(group_still_exists=raises), patch(
                    "arena.native_sandbox._process_table", side_effect=[self.rows, fresh]), \
                    patch("arena.native_sandbox.os.killpg", side_effect=PermissionError()), \
                    patch("arena.native_sandbox.os.kill", side_effect=ProcessLookupError()) as kill:
                if raises:
                    with self.assertRaises(PermissionError):
                        await self.native._signal("claude", signal.SIGTERM)
                    kill.assert_not_called()
                else:
                    await self.native._signal("claude", signal.SIGTERM)

    async def test_process_group_is_signalled_before_individual_members(self):
        self.native.jobs["claude"].update(root_start="root", known={110: "root", 111: "child"})
        calls = []
        with patch("arena.native_sandbox._process_table", return_value=self.rows), \
                patch("arena.native_sandbox.os.killpg", side_effect=lambda *args: calls.append(("group", args))), \
                patch("arena.native_sandbox.os.kill", side_effect=lambda *args: calls.append(("process", args))):
            await self.native._signal("claude", signal.SIGTERM)
        self.assertEqual(calls[0], ("group", (110, signal.SIGTERM)))
        self.assertEqual([args[0] for kind, args in calls[1:]], [111, 110])


# An assistant's sandboxed shell refuses a nested Seatbelt sandbox or /bin/ps; start must
# then run from Terminal.app anyway (bootstrap exit 42), so these cannot pass here.
SANDBOXED = "; ".join(shell_restrictions())


@unittest.skipUnless(sys.platform == "darwin", "macOS native sandbox integration")
@unittest.skipIf(SANDBOXED, f"this shell is sandboxed ({SANDBOXED}); run from Terminal.app")
class NativeProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.native = Native(Config(data_dir=str(self.base / "data"), check_seconds=2))

    async def asyncTearDown(self):
        await self.native.cleanup_owned()
        if self.native.process_scan:
            await asyncio.gather(self.native.process_scan, return_exceptions=True)

    async def check(self, name, text, cancellation=None):
        root = self.base / name
        execution, acceptance, regression = [root / folder
                                             for folder in ("workspace", "acceptance", "regression")]
        for folder in (execution, acceptance, regression):
            folder.mkdir(parents=True, exist_ok=True)
        return await self.native.execute_check(name, execution, acceptance, regression,
                                               text, cancellation)

    async def test_three_concurrent_checks_keep_exit_codes_and_clean_descendants(self):
        results = await asyncio.gather(
            self.check("claude", "sleep 30 >/dev/null 2>&1 & echo $! > child.pid; sleep 1.2; echo claude; exit 0"),
            self.check("codex", "echo failed-check >&2; exit 7"),
            self.check("kiro", "echo kiro; exit 0"),
        )
        self.assertEqual([r[0] for r in results], [0, 7, 0])
        self.assertIn("failed-check", results[1][2])
        child = int((self.base / "claude/workspace/child.pid").read_text())
        self.assertNotIn(child, await _process_table())
        self.assertFalse(self.native.jobs)
        self.assertFalse(list(self.native.records.glob("*.json")))

    async def test_timeout_and_user_cancellation_stop_live_processes(self):
        self.native.config.check_seconds = .15
        with self.assertRaises(TimeoutError):
            await self.check("timeout", "sleep 30")
        self.native.config.check_seconds = 5
        with self.assertRaisesRegex(RuntimeError, "Comparison cancelled"):
            await self.check("cancel", "sleep 30", lambda: True)
        self.assertFalse(self.native.jobs)
        self.assertFalse(list(self.native.records.glob("*.json")))

    async def test_transient_ps_timeout_during_three_real_checks_recovers(self):
        original = subprocess.run
        failures = 0

        def read(*args, **kwargs):
            nonlocal failures
            if args[0][0] == "/bin/ps" and failures == 0:
                failures += 1
                raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
            return original(*args, **kwargs)

        with patch("arena.native_sandbox.subprocess.run", side_effect=read):
            results = await asyncio.gather(*[
                self.check(name, "echo complete") for name in ("claude", "codex", "kiro")])
        self.assertEqual(failures, 1)
        self.assertEqual([r[0] for r in results], [0, 0, 0])
        self.assertFalse(self.native.jobs)

    async def test_restart_cleanup_retains_pid_identity_protection(self):
        root = self.base / "restart"
        from arena.native_sandbox import private_environment, private_tmp, profile, runtime_roots
        root.mkdir()
        home = root / "home"
        home.mkdir()
        temporary = private_tmp(root)
        policy = root / "sandbox.sb"
        policy.write_text(profile(runtime_roots(), {root, temporary}))
        self.native.jobs["restart"] = {
            "policy": policy, "env": private_environment(home, temporary, root),
            "cwd": root, "known": {}}
        process = await self.native.launch("restart", ["/bin/sleep", "30"])
        self.assertIsNone(process.returncode)
        restarted = Native(self.native.config)
        await restarted.cleanup_owned()
        self.assertEqual(await asyncio.wait_for(process.wait(), 3), -signal.SIGTERM)
        self.assertFalse(list(self.native.records.glob("*.json")))
        self.native.jobs.clear()


@unittest.skipUnless(sys.platform == "darwin", "macOS native worker integration")
@unittest.skipIf(SANDBOXED, f"this shell is sandboxed ({SANDBOXED}); run from Terminal.app")
class NativePhaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w = Worker(Config(data_dir=str(Path(self.temp.name) / "data")))
        for harness in self.w.config.harnesses.values():
            harness.env = []
        self.c = self.w.service.create("Create a local greeting CLI")
        self.spec = self.w.freeze_requirements(self.c["id"])
        self.runs = self.w.store.detail(self.c["id"])["runs"]
        self.plan = default_plan(self.spec, self.w.run_root(self.runs[0]).parent.parent / "baseline")
        self.original_launch = self.w.runtime.launch
        self.tasks = []

        async def launch(name, args=None):
            # Exercise the real sandbox, worker, mailbox and process supervisor.
            # Only the external coding CLI is replaced with a deterministic peer.
            code = """
import json, os, time
from pathlib import Path
bridge = Path(os.environ['ARENA_BRIDGE'])
turn = json.loads((bridge / 'turn.json').read_text())
if turn['phase'] == 'clarify':
    (bridge / 'requests' / 'ready.json').write_text(json.dumps({
        'type':'ready', 'turn_key':turn['turn_key'], 'assumptions':'Local test'}))
else:
    (Path(os.environ['ARENA_WORKSPACE']) / 'main.py').write_text("print('Hello')\\n")
print(json.dumps({'type':'session','data':{'session_id':'local-' + turn['harness']}}), flush=True)
deadline = time.monotonic() + 1.2
while time.monotonic() < deadline:
    marker = bridge / 'cancel.json'
    if marker.exists() and json.loads(marker.read_text()).get('turn_key') == turn['turn_key']:
        raise SystemExit(0)
    time.sleep(.02)
print(json.dumps({'type':'turn_complete','data':{}}), flush=True)
"""
            return await self.original_launch(name, [sys.executable, "-c", code])

        self.w.runtime.launch = launch

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.w.runtime.cleanup_owned()

    def start(self, run, phase="develop", spec=None):
        task = asyncio.create_task(self.w.phase(run["id"], phase, spec or self.spec, self.plan))
        self.tasks.append(task)
        return task

    async def launched(self, run):
        async with asyncio.timeout(5):
            while "pid" not in self.w.runtime.jobs.get("arena-" + run["id"], {}):
                await asyncio.sleep(.01)

    async def test_all_harness_phases_survive_one_inspection_timeout(self):
        original = subprocess.run
        injected = 0

        def read(*args, **kwargs):
            nonlocal injected
            if args[0][0] == "/bin/ps" and injected == 0:
                injected += 1
                raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
            return original(*args, **kwargs)

        with patch("arena.native_sandbox.subprocess.run", side_effect=read):
            outcomes = await asyncio.gather(*[self.start(run, "clarify") for run in self.runs])
            self.assertEqual(outcomes, ["ready"] * 3)
            outcomes = await asyncio.gather(*[self.start(run) for run in self.runs])
        self.assertEqual(injected, 1)
        self.assertEqual(outcomes, ["implemented"] * 3)
        for run in self.runs:
            current = self.w.store.get("run", run["id"])
            self.assertEqual(current["status"], "implemented")
            self.assertIsNone(current["error"])
            self.assertTrue(all(a.get("ended_at") for a in current["attempts"]))
            self.assertTrue((self.w.run_root(run) / "workspace/main.py").exists())
        self.assertFalse(self.w.runtime.jobs)

    async def test_requirements_revision_cleans_old_process_and_resumes_same_session(self):
        run = self.runs[0]
        first = self.start(run)
        await self.launched(run)
        self.w.service.revise(self.c["id"], "Use the greeting Hello")
        self.assertEqual(await first, "revised")
        current = self.w.store.get("run", run["id"])
        self.assertIsNotNone(current["attempts"][-1]["ended_at"])
        session = current["session_id"]
        spec = self.w.freeze_requirements(self.c["id"])
        self.assertEqual(await self.start(run, spec=spec), "implemented")
        current = self.w.store.get("run", run["id"])
        self.assertEqual(current["session_id"], session)
        self.assertEqual(current["spec_version"], 2)
        self.assertFalse(self.w.runtime.jobs)

    async def test_cancelled_harness_does_not_stop_other_harnesses(self):
        tasks = [self.start(run) for run in self.runs]
        await self.launched(self.runs[0])
        self.w.service.cancel(self.c["id"], self.runs[0]["id"])
        self.assertEqual(await asyncio.gather(*tasks), ["cancelled", "implemented", "implemented"])
        self.assertFalse(self.w.runtime.jobs)

    async def test_exhausted_active_budget_is_charged_atomically_and_names_the_limit(self):
        # A worker killed before the `finally` bookkeeping must not lose this
        # turn's seconds: a resume that cannot see them grants the budget again.
        self.w.config.active_seconds = 1
        committed = {}
        original = self.w.store.transition

        def transition(run_id, status, at=None, **fields):
            if status == "limit_exhausted":
                committed[run_id] = fields
            return original(run_id, status, at=at, **fields)

        with patch.object(self.w.store, "transition", side_effect=transition):
            self.assertEqual(await self.start(self.runs[0]), "limit_exhausted")
        fields = committed[self.runs[0]["id"]]
        charged = fields["attempts"][-1]
        self.assertEqual(charged["outcome"], "limit_exhausted")
        self.assertGreater(charged["active_seconds"], 0)
        self.assertIsNotNone(charged["ended_at"])
        self.assertIn("active_seconds=1", fields["error"])
        current = self.w.store.get("run", self.runs[0]["id"])
        self.assertEqual(current["status"], "limit_exhausted")
        # This is exactly the sum `phase` uses as previous_active on a resume.
        self.assertGreaterEqual(
            sum(a.get("active_seconds", 0) + a.get("unobserved_active_seconds", 0)
                for a in current["attempts"]), self.w.config.active_seconds)
        self.assertFalse(self.w.runtime.jobs)

    async def test_resource_failure_remains_failure_and_closes_attempt(self):
        self.w.config.memory = "1k"
        outcome = await self.start(self.runs[0])
        self.assertEqual(outcome, "failed")
        current = self.w.store.get("run", self.runs[0]["id"])
        self.assertIn("resource limit exceeded", current["error"])
        self.assertIsNotNone(current["attempts"][-1]["ended_at"])
        self.assertFalse(self.w.runtime.jobs)

    def readiness_peer(self, call_ready_when_reminded: bool):
        """A harness that finishes its clarification turn without calling ready."""
        # Detect the reminder through the shipped constant, not a copy of its text.
        marker = READINESS_REMINDER.strip().split(".")[0]
        self.assertTrue(marker)

        def launch(name, args=None):
            code = """
import json, os, sys
from pathlib import Path
bridge = Path(os.environ['ARENA_BRIDGE'])
turn = json.loads((bridge / 'turn.json').read_text())
if sys.argv[1] in turn['prompt'] and sys.argv[2] == 'yes':
    (bridge / 'requests' / 'ready.json').write_text(json.dumps({
        'type':'ready', 'turn_key':turn['turn_key'], 'assumptions':'Reminded'}))
print(json.dumps({'type':'session','data':{'session_id':'local-' + turn['harness']}}), flush=True)
print(json.dumps({'type':'turn_complete','data':{}}), flush=True)
"""
            return self.original_launch(name, [
                sys.executable, "-c", code, marker,
                "yes" if call_ready_when_reminded else "no"])
        self.w.runtime.launch = launch

    def failing_peer(self, message, terminal):
        """A harness whose turn dies the way an adapter reports a bad bridge."""
        def launch(name, args=None):
            code = """
import json, sys
error = {'message': sys.argv[1], 'traceback': 'synthetic'}
if sys.argv[2] != 'unset':
    error['terminal'] = sys.argv[2] == 'yes'
print(json.dumps({'type':'error','data':error}), flush=True)
raise SystemExit(1)
"""
            flag = "unset" if terminal is None else ("yes" if terminal else "no")
            return self.original_launch(name, [sys.executable, "-c", code, message, flag])
        self.w.runtime.launch = launch

    async def test_raiser_classification_outranks_the_message_wording(self):
        # "unavailable" matches the blocking word list, but a dropped bridge
        # socket is retryable and the adapter says so.
        transient = ("multi-coding-harness-compare clarification bridge unavailable: disconnected. "
                     "The task prompt was not submitted.")
        for terminal, expected in ((False, "failed"), (True, "blocked"), (None, "blocked")):
            with self.subTest(terminal=terminal):
                run = self.runs[0]
                self.failing_peer(transient, terminal)
                self.assertEqual(await self.start(run, "clarify"), expected)
                current = self.w.store.get("run", run["id"])
                self.assertEqual(current["status"], expected)
                self.assertEqual(current["error"], transient)
                self.assertFalse(self.w.runtime.jobs)

    async def test_missing_credentials_stay_blocked_without_a_classification(self):
        run = self.runs[1]
        self.failing_peer("Harness credentials are missing: run the CLI login", None)
        self.assertEqual(await self.start(run, "clarify"), "blocked")
        self.assertEqual(self.w.store.get("run", run["id"])["status"], "blocked")

    async def test_clarification_without_ready_is_reprompted_before_blocking(self):
        run = self.runs[0]
        self.readiness_peer(call_ready_when_reminded=True)
        self.assertEqual(await self.start(run, "clarify"), "readiness_unverified")
        current = self.w.store.get("run", run["id"])
        self.assertFalse(current.get("clarified"))
        self.assertIsNone(current["error"])
        task = asyncio.create_task(self.w.phase(run["id"], "clarify", self.spec, self.plan,
                                                readiness_reminder=True))
        self.tasks.append(task)
        self.assertEqual(await task, "ready")
        current = self.w.store.get("run", run["id"])
        self.assertEqual(current["status"], "ready")
        self.assertTrue(current["clarified"])
        self.assertEqual(current["assumptions"], "Reminded")
        # The reminder resumes the same session instead of restarting the work.
        self.assertEqual(current["session_id"], "local-" + run["harness"])
        self.assertTrue(current["attempts"][-1]["readiness_reminder"])
        self.assertFalse(current["attempts"][0]["readiness_reminder"])
        self.assertFalse(self.w.runtime.jobs)

    async def test_second_clarification_without_ready_blocks_the_run(self):
        run = self.runs[0]
        self.readiness_peer(call_ready_when_reminded=False)
        self.assertEqual(await self.start(run, "clarify"), "readiness_unverified")
        task = asyncio.create_task(self.w.phase(run["id"], "clarify", self.spec, self.plan,
                                                readiness_reminder=True))
        self.tasks.append(task)
        self.assertEqual(await task, "blocked")
        current = self.w.store.get("run", run["id"])
        self.assertEqual(current["status"], "blocked")
        self.assertIn("ready tool", current["error"])
        self.assertFalse(current.get("clarified"))
        self.assertFalse(self.w.runtime.jobs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
