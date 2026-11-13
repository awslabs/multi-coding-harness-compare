# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""stop: end this installation's API and worker, never a run in progress unless forced."""
import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arena import cli
from arena.store import Store


class StopTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(root=Path(self.dir.name), port=8799)

    def tearDown(self):
        self.dir.cleanup()

    def stop(self, **kwargs):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.stop_services(self.config, **kwargs)
        return code, json.loads(output.getvalue())

    def sleeper(self) -> int:
        # Not a child of this process, like the services start launches: launchd reaps it.
        pid = int(subprocess.run(["/bin/sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"],
                                 capture_output=True, text=True, check=True).stdout)
        self.addCleanup(lambda: cli._alive(pid) and os.kill(pid, signal.SIGKILL))
        return pid

    def test_runs_in_progress_are_never_interrupted_without_force(self):
        Store(self.config.root).put("run", {"id": "r1", "harness": "codex", "status": "developing",
                                            "comparison_id": "c0ffee0123"})
        with patch("arena.cli._own_service_pids") as find:
            code, report = self.stop()
        self.assertEqual(code, 1)
        self.assertEqual(report["active_runs"], ["codex developing (comparison c0ffee01)"])
        find.assert_not_called()

    def test_the_found_services_are_stopped_and_reported(self):
        Store(self.config.root).put("run", {"id": "r1", "harness": "codex", "status": "completed",
                                            "comparison_id": "c0ffee0123"})
        pid = self.sleeper()
        with patch("arena.cli._own_service_pids", return_value={"worker": pid}):
            code, report = self.stop()
        self.assertEqual((code, report["stopped"], report["killed_after_90s"]), (0, {"worker": pid}, {}))
        self.assertEqual(report["interrupted_runs"], [])
        self.assertFalse(cli._alive(pid))

    def test_a_service_that_exits_before_it_is_signalled_is_still_reported(self):
        gone = subprocess.Popen(["/usr/bin/true"])
        gone.wait()
        with patch("arena.cli._own_service_pids", return_value={"worker": gone.pid}):
            code, report = self.stop()
        self.assertEqual((code, report["stopped"], report["killed_after_90s"]), (0, {"worker": gone.pid}, {}))

    def test_force_stops_and_names_the_interrupted_runs(self):
        Store(self.config.root).put("run", {"id": "r1", "harness": "kiro", "status": "evaluating",
                                            "comparison_id": "abcdef0123"})
        pid = self.sleeper()
        with patch("arena.cli._own_service_pids", return_value={"serve": pid}):
            code, report = self.stop(force=True)
        self.assertEqual(code, 0)
        self.assertEqual(report["interrupted_runs"], ["kiro evaluating (comparison abcdef01)"])
        self.assertFalse(cli._alive(pid))

    def test_without_a_store_or_services_there_is_nothing_to_stop(self):
        with patch("arena.cli._own_service_pids", return_value={}):
            self.assertEqual(self.stop(), (0, {"ok": True, "stopped": {}, "killed_after_90s": {},
                                              "interrupted_runs": []}))

    def test_only_this_applications_own_service_commands_are_matched(self):
        commands = {"101": "python -m mchc serve", "102": "node server.js",
                    "201": "/x/.venv/bin/python -m mchc worker"}

        def run(argv, **kwargs):
            if argv[0] == "lsof":
                out = "101\n102\n" if any(a.startswith("-iTCP:") for a in argv) else "201\n"
            else:
                out = commands[argv[2]]
            return SimpleNamespace(stdout=out)

        with patch("arena.cli.subprocess.run", side_effect=run) as probe:
            self.assertEqual(cli._own_service_pids(self.config), {"serve": 101, "worker": 201})
        self.assertIn(["lsof", "-nP", "-t", "-iTCP:8799", "-sTCP:LISTEN"], [c.args[0] for c in probe.call_args_list])
        self.assertIn(["lsof", "-t", str(self.config.root / "worker.lock")], [c.args[0] for c in probe.call_args_list])

    def test_without_ps_only_the_pids_start_recorded_are_trusted(self):
        # A sandboxed shell may refuse to run ps; never guess, and never crash.
        (self.config.root / "services.json").write_text(json.dumps({"pids": {"serve": 101, "worker": 999}}))

        def run(argv, **kwargs):
            if argv[0] == "lsof":
                return SimpleNamespace(stdout="101\n102\n" if any(a.startswith("-iTCP:") for a in argv) else "201\n")
            raise PermissionError(1, "Operation not permitted", "ps")

        with patch("arena.cli.subprocess.run", side_effect=run):
            self.assertEqual(cli._own_service_pids(self.config), {"serve": 101})


if __name__ == "__main__":
    unittest.main()
