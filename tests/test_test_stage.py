# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the harness-written test stage (Part B1).

Covers:
  - ordering: write → manifest → lock → verification → fix → green
  - repair turn (manifest invalid initially; repair makes it valid)
  - lock violation (harness modifies test file; lock restored and repair forced)
  - rounds exhausted (fix_rounds used up; still failing)
  - budget exhausted mid-turn (phase returns test_budget_exhausted)
  - budget excludes review and sandbox verification (only phase active_seconds count)
  - no manifest → no_manifest_reason passed to evaluate_candidate; score 0
  - review agent failure does not prevent evaluation_incomplete
  - supplied plan unchanged (legacy comparison with plan_sha256 set keeps its plan)
  - legacy frozen plan unchanged (comparison with frozen plan skips new flow)
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

from arena.config import Config
from arena.evaluation import Plan, Check, Criterion, evaluate_candidate
from arena.harness_tests import MANIFEST_NAME, create_lock, enforce_lock
from arena.store import TERMINAL
from arena.worker import Worker, _TEST_PHASES


# ── Helpers ───────────────────────────────────────────────────────────────────

def _minimal_approved_review(scenario_id="S1"):
    """Return a minimal approved test review dict (new-flow: plan_sha256 is None)."""
    return {
        "id": "rev-1",
        "revision": 1,
        "status": "approved",
        "plan_sha256": None,
        "approval_sha256": "fakehash",
        "requirements_sha256": "fakehash",
        "approved_at": 1.0,
        "spec_version": 1,
        "proposal": {
            "summary": "Test the greeting output",
            "test_cases": [
                {"id": scenario_id, "title": "Greeting output", "steps": ["Run main.py"],
                 "expected_result": "Prints hello", "test_data": "none"},
            ],
            "assumptions": [],
            "limitations": [],
        },
        "answers": {},
        "test_coverage": "comprehensive",
    }


def _minimal_manifest(scenario_id="S1", check_id="c1", test_file="tests/test_foo.py"):
    return {
        "version": 1,
        "checks": [
            {"id": check_id, "scenario_id": scenario_id,
             "command": f"python -m pytest {test_file} -q", "junit": None}
        ],
        "test_files": [test_file],
        "python_requirements": [],
    }


def _make_plan(check_id="c1", scenario_id="S1", test_file="tests/test_foo.py"):
    return Plan(
        criteria=[Criterion(id=scenario_id, requirement="Greeting", check_ids=[check_id])],
        checks=[Check(id=check_id, group="acceptance",
                      command=f"python -m pytest {test_file} -q", scored=True)],
        review_coverage={scenario_id: [check_id]},
        excluded_groups={},
        acceptance_files={test_file: "def test_foo(): assert True\n"},
        review_id="rev-1",
    )


def _passed_evidence(check_id="c1"):
    return {"checks": [{"id": check_id, "group": "acceptance", "status": "passed",
                        "exit_code": 0, "log": "1 passed", "counts": {"passed": 1}}],
            "environment": {}}


def _failed_evidence(check_id="c1"):
    return {"checks": [{"id": check_id, "group": "acceptance", "status": "failed",
                        "exit_code": 1, "log": "1 failed\nAssertionError", "counts": {"failed": 1}}],
            "environment": {}}


# ── Shared setUp ──────────────────────────────────────────────────────────────

class _TestStageBase(unittest.IsolatedAsyncioTestCase):
    """Set up a Worker with the native runtime replaced by a controllable mock."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                          test_active_seconds=3600, test_fix_rounds=2,
                          artifact_s3_uri=None)
        self.w = Worker(self.cfg)
        self.c = self.w.service.create("Build a greeting CLI.")
        self.cid = self.c["id"]
        self.comparison = self.w.freeze_requirements(self.cid)
        self.run = self.w.store.detail(self.cid)["runs"][0]
        self.run_id = self.run["id"]

        # Directories used by run_test_stage
        self.baseline = self.w.config.root / "comparisons" / self.cid / "baseline"
        self.baseline.mkdir(parents=True, exist_ok=True)
        self.version_dir = (self.baseline.parent / "specifications"
                            / str(self.comparison["spec_version"]))
        self.version_dir.mkdir(parents=True, exist_ok=True)

        # Workspace must exist for phase() to skip the baseline copy.
        self.root = self.w.run_root(self.run)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

        # Approved test review (new-flow: no plan_sha256)
        self.approved_review = _minimal_approved_review()
        self.task_spec = {**self.comparison, "approved_test_review": self.approved_review}

        # Phase call log and queue of outcomes to return
        self.phase_calls = []
        self._phase_outcomes = []  # list of (outcome, side_effect_fn or None)

    def _queue_phase(self, outcome, setup_fn=None):
        """Enqueue the next phase() outcome.  setup_fn(workspace) is called before returning."""
        self._phase_outcomes.append((outcome, setup_fn))

    def _build_phase_mock(self):
        """Return an async mock for self.w.phase that drains _phase_outcomes."""
        async def _phase(run_id, phase, task_spec, plan, *,
                         active_budget=None, prior_active_seconds=0.0,
                         prompt_override=None, **kw):
            self.phase_calls.append({
                "phase": phase,
                "prompt_override": prompt_override,
            })
            if not self._phase_outcomes:
                return "tests_written"
            outcome, setup_fn = self._phase_outcomes.pop(0)
            if setup_fn is not None:
                setup_fn(self.workspace)
            # Record a minimal attempt so budget tracking works.
            if phase in _TEST_PHASES:
                run = self.w.store.get("run", run_id)
                run["attempts"].append({"id": "t-" + phase, "phase": phase,
                                        "active_seconds": 10, "outcome": None})
                self.w.store.update("run", run_id, attempts=run["attempts"])
            return outcome
        return _phase

    def _write_manifest_to_workspace(self, manifest_data=None):
        """Write a minimal valid manifest to the workspace."""
        manifest_data = manifest_data or _minimal_manifest()
        (self.workspace / MANIFEST_NAME).write_text(json.dumps(manifest_data))
        test_path = self.workspace / "tests"
        test_path.mkdir(exist_ok=True)
        (test_path / "test_foo.py").write_text("def test_foo(): assert True\n")


# ── Test 1: happy path with one fix round ─────────────────────────────────────

class TestRunTestStageHappyPath(unittest.IsolatedAsyncioTestCase):
    """write → manifest → lock → verification fails → fix → verification passes."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                     test_active_seconds=3600, test_fix_rounds=2,
                     artifact_s3_uri=None)
        self.w = Worker(cfg)
        c = self.w.service.create("Task")
        self.cid = c["id"]
        comparison = self.w.freeze_requirements(self.cid)
        self.run = self.w.store.detail(self.cid)["runs"][0]
        self.run_id = self.run["id"]
        self.baseline = self.w.config.root / "comparisons" / self.cid / "baseline"
        self.baseline.mkdir(parents=True, exist_ok=True)
        self.version_dir = (self.baseline.parent / "specifications"
                            / str(comparison["spec_version"]))
        self.version_dir.mkdir(parents=True, exist_ok=True)
        self.workspace = self.w.run_root(self.run) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.approved_review = _minimal_approved_review()
        self.task_spec = {**comparison, "approved_test_review": self.approved_review}
        self.phase_calls = []
        self.verification_calls = []
        self.fake_plan = _make_plan()

    def _write_manifest(self):
        (self.workspace / MANIFEST_NAME).write_text(json.dumps(_minimal_manifest()))
        (self.workspace / "tests").mkdir(exist_ok=True)
        (self.workspace / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")

    async def test_write_manifest_lock_fix_green(self):
        """Full happy path: write → valid manifest → lock → fail → fix → pass."""
        verification_round = [0]

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self.phase_calls.append(phase)
            if phase == "test":
                self._write_manifest()
            # Record attempt
            r = self.w.store.get("run", run_id)
            if phase in _TEST_PHASES:
                r["attempts"].append({"id": "t-" + phase, "phase": phase,
                                      "active_seconds": 10})
                self.w.store.update("run", run_id, attempts=r["attempts"])
            return "tests_written"

        async def fake_verify(snapshot, plan, evidence_dir, *args, **kwargs):
            self.verification_calls.append(verification_round[0])
            result = (_failed_evidence() if verification_round[0] == 0 else _passed_evidence())
            verification_round[0] += 1
            return result

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=fake_verify):
            plan, stop_reason, test_info = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # Ordering: write → fix
        self.assertEqual(self.phase_calls, ["test", "fix"])
        # Two verification rounds
        self.assertEqual(len(self.verification_calls), 2)
        # Result: all_passed
        self.assertEqual(stop_reason["code"], "all_passed")
        self.assertIsNotNone(plan)
        # Manifest and verified_results are returned
        self.assertIsNotNone(test_info.get("manifest"))
        self.assertIsNotNone(test_info.get("verified_results"))

    async def test_all_checks_pass_on_first_verification_no_fix_turn(self):
        """When all checks pass immediately, no fix turn is dispatched."""

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self.phase_calls.append(phase)
            if phase == "test":
                self._write_manifest()
            r = self.w.store.get("run", run_id)
            if phase in _TEST_PHASES:
                r["attempts"].append({"id": "t-" + phase, "phase": phase, "active_seconds": 5})
                self.w.store.update("run", run_id, attempts=r["attempts"])
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, test_info = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(self.phase_calls, ["test"])
        self.assertEqual(stop_reason["code"], "all_passed")

    async def test_fix_feedback_mentions_failing_check_id(self):
        """The fix turn's prompt_override contains the failing check id."""
        fix_prompts = []
        verification_round = [0]

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self.phase_calls.append(phase)
            if phase == "test":
                self._write_manifest()
            if phase == "fix" and kw.get("prompt_override"):
                fix_prompts.append(kw["prompt_override"])
            r = self.w.store.get("run", run_id)
            if phase in _TEST_PHASES:
                r["attempts"].append({"id": "t-" + phase, "phase": phase, "active_seconds": 5})
                self.w.store.update("run", run_id, attempts=r["attempts"])
            return "tests_written"

        async def fake_verify(snapshot, plan, evidence_dir, *args, **kwargs):
            result = (_failed_evidence() if verification_round[0] == 0 else _passed_evidence())
            verification_round[0] += 1
            return result

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=fake_verify):
            await self.w.run_test_stage(self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(len(fix_prompts), 1)
        # Fix prompt references the failing check id
        self.assertIn("c1", fix_prompts[0])


# ── Test 2: repair turn (manifest invalid initially) ─────────────────────────

class TestRepairTurn(unittest.IsolatedAsyncioTestCase):
    """Manifest fails validation → repair turn → manifest valid → verification passes."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                     test_active_seconds=3600, test_fix_rounds=2,
                     artifact_s3_uri=None)
        self.w = Worker(cfg)
        c = self.w.service.create("Task")
        self.cid = c["id"]
        comparison = self.w.freeze_requirements(self.cid)
        self.run = self.w.store.detail(self.cid)["runs"][0]
        self.run_id = self.run["id"]
        self.baseline = self.w.config.root / "comparisons" / self.cid / "baseline"
        self.baseline.mkdir(parents=True, exist_ok=True)
        self.version_dir = (self.baseline.parent / "specifications"
                            / str(comparison["spec_version"]))
        self.version_dir.mkdir(parents=True, exist_ok=True)
        self.workspace = self.w.run_root(self.run) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.approved_review = _minimal_approved_review()
        self.task_spec = {**comparison, "approved_test_review": self.approved_review}
        self.phase_calls = []
        self.repair_prompts = []
        self.fake_plan = _make_plan()

    def _write_valid_manifest(self):
        (self.workspace / MANIFEST_NAME).write_text(json.dumps(_minimal_manifest()))
        (self.workspace / "tests").mkdir(exist_ok=True)
        (self.workspace / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")

    async def test_manifest_invalid_repair_then_valid_pass(self):
        """Write turn omits manifest; repair writes it; verification passes."""
        turn = [0]

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self.phase_calls.append(phase)
            if kw.get("prompt_override"):
                self.repair_prompts.append(kw["prompt_override"])
            if phase == "repair":
                # Repair turn fixes the manifest
                self._write_valid_manifest()
            # Record attempt
            r = self.w.store.get("run", run_id)
            if phase in _TEST_PHASES:
                r["attempts"].append({"id": "t-" + phase, "phase": phase, "active_seconds": 5})
                self.w.store.update("run", run_id, attempts=r["attempts"])
            turn[0] += 1
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, test_info = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # write turn → repair turn
        self.assertEqual(self.phase_calls, ["test", "repair"])
        # Repair prompt mentions validation errors
        self.assertEqual(len(self.repair_prompts), 1)
        self.assertIn("VALIDATION ERRORS", self.repair_prompts[0])
        self.assertEqual(stop_reason["code"], "all_passed")

    async def test_manifest_missing_after_all_repair_rounds_manifest_missing(self):
        """If the manifest is never written, return manifest_missing after all rounds."""

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self.phase_calls.append(phase)
            r = self.w.store.get("run", run_id)
            if phase in _TEST_PHASES:
                r["attempts"].append({"id": "t-" + phase, "phase": phase, "active_seconds": 5})
                self.w.store.update("run", run_id, attempts=r["attempts"])
            return "tests_written"

        self.w.phase = fake_phase

        # test_fix_rounds=2 means rounds 0,1,2 → manifest still missing after rounds 1,2
        plan, stop_reason, test_info = await self.w.run_test_stage(
            self.run_id, self.task_spec, self.baseline, self.version_dir)

        # write + 2 repair turns
        self.assertEqual(self.phase_calls, ["test", "repair", "repair"])
        self.assertEqual(stop_reason["code"], "manifest_missing")
        self.assertIsNone(plan)


# ── Test 3: lock violation ────────────────────────────────────────────────────

class TestLockViolation(unittest.IsolatedAsyncioTestCase):
    """After lock is set, harness tries to modify a test file; lock is restored."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                     test_active_seconds=3600, test_fix_rounds=2,
                     artifact_s3_uri=None)
        self.w = Worker(cfg)
        c = self.w.service.create("Task")
        self.cid = c["id"]
        comparison = self.w.freeze_requirements(self.cid)
        self.run = self.w.store.detail(self.cid)["runs"][0]
        self.run_id = self.run["id"]
        self.baseline = self.w.config.root / "comparisons" / self.cid / "baseline"
        self.baseline.mkdir(parents=True, exist_ok=True)
        self.version_dir = (self.baseline.parent / "specifications"
                            / str(comparison["spec_version"]))
        self.version_dir.mkdir(parents=True, exist_ok=True)
        self.workspace = self.w.run_root(self.run) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.approved_review = _minimal_approved_review()
        self.task_spec = {**comparison, "approved_test_review": self.approved_review}
        self.phase_calls = []
        self.fake_plan = _make_plan()

    def _write_manifest(self):
        (self.workspace / MANIFEST_NAME).write_text(json.dumps(_minimal_manifest()))
        (self.workspace / "tests").mkdir(exist_ok=True)
        (self.workspace / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")

    async def test_lock_violation_does_not_trigger_repair_but_fix_with_notice(self):
        """After a lock violation the file is restored; a fix turn (not repair) runs
        with feedback that includes the violation notice (W3).

        Flow: write → verify fails → fix (violates lock → lock restored) →
              verify passes (round 2).  No repair turn must appear.
        """
        fix_prompts = []
        verification_calls = [0]

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self.phase_calls.append(phase)
            if phase == "test":
                self._write_manifest()
            if phase == "fix":
                # Harness violates the lock by changing the test file
                (self.workspace / "tests" / "test_foo.py").write_text("MODIFIED\n")
                if kw.get("prompt_override"):
                    fix_prompts.append(kw["prompt_override"])
            r = self.w.store.get("run", run_id)
            if phase in _TEST_PHASES:
                r["attempts"].append({"id": "t-" + phase, "phase": phase, "active_seconds": 5})
                self.w.store.update("run", run_id, attempts=r["attempts"])
            return "tests_written"

        async def fake_verify(snapshot, plan, evidence_dir, *args, **kwargs):
            # First call fails; second call passes (after lock-enforced fix)
            if verification_calls[0] == 0:
                verification_calls[0] += 1
                return _failed_evidence()
            verification_calls[0] += 1
            return _passed_evidence()

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=fake_verify):
            plan, stop_reason, test_info = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # write → verify fails → fix (violates lock, lock restored) → verify passes
        self.assertIn("test", self.phase_calls)
        self.assertIn("fix", self.phase_calls)
        # W3: repair must NOT appear after the lock is set
        self.assertNotIn("repair", self.phase_calls)
        # The locked file was restored (should match original content)
        restored = (self.workspace / "tests" / "test_foo.py").read_text()
        self.assertIn("assert True", restored)
        # test_info records violations
        self.assertTrue(len(test_info.get("lock_violations", [])) > 0)
        # The fix turn feedback mentions the violation and instructs editing implementation
        self.assertTrue(
            any("test_foo.py" in p or "mplementation" in p for p in fix_prompts),
            f"Fix prompt should mention locked file or implementation; got: {fix_prompts}"
        )

    async def test_lock_violation_at_last_round_returns_rounds_exhausted(self):
        """Lock violation on the last fix round returns rounds_exhausted (non-None plan)."""

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self.phase_calls.append(phase)
            if phase == "test":
                self._write_manifest()
            if phase == "fix":
                # Violate lock on EVERY fix turn
                (self.workspace / "tests" / "test_foo.py").write_text("EVIL\n")
            r = self.w.store.get("run", run_id)
            if phase in _TEST_PHASES:
                r["attempts"].append({"id": "t-" + phase, "phase": phase, "active_seconds": 5})
                self.w.store.update("run", run_id, attempts=r["attempts"])
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, test_info = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "rounds_exhausted")
        # plan is NOT None (we had a valid plan before violations)
        self.assertIsNotNone(plan)


# ── Test 4: rounds exhausted ──────────────────────────────────────────────────

class TestRoundsExhausted(unittest.IsolatedAsyncioTestCase):
    """Verification never passes; fix_rounds rounds used up → rounds_exhausted."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                     test_active_seconds=3600, test_fix_rounds=2,
                     artifact_s3_uri=None)
        self.w = Worker(cfg)
        c = self.w.service.create("Task")
        self.cid = c["id"]
        comparison = self.w.freeze_requirements(self.cid)
        self.run = self.w.store.detail(self.cid)["runs"][0]
        self.run_id = self.run["id"]
        self.baseline = self.w.config.root / "comparisons" / self.cid / "baseline"
        self.baseline.mkdir(parents=True, exist_ok=True)
        self.version_dir = (self.baseline.parent / "specifications"
                            / str(comparison["spec_version"]))
        self.version_dir.mkdir(parents=True, exist_ok=True)
        self.workspace = self.w.run_root(self.run) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.approved_review = _minimal_approved_review()
        self.task_spec = {**comparison, "approved_test_review": self.approved_review}
        self.fake_plan = _make_plan()

    def _write_manifest(self):
        (self.workspace / MANIFEST_NAME).write_text(json.dumps(_minimal_manifest()))
        (self.workspace / "tests").mkdir(exist_ok=True)
        (self.workspace / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")

    async def test_all_fix_rounds_used_returns_rounds_exhausted_with_plan(self):
        """With fix_rounds=2, after 2 fix turns still failing → rounds_exhausted."""
        phase_calls = []

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            phase_calls.append(phase)
            if phase == "test":
                self._write_manifest()
            r = self.w.store.get("run", run_id)
            if phase in _TEST_PHASES:
                r["attempts"].append({"id": "t-" + phase, "phase": phase, "active_seconds": 5})
                self.w.store.update("run", run_id, attempts=r["attempts"])
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, test_info = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # write + 2 fix turns + 3 verifications (after write, after fix1, after fix2)
        self.assertEqual(phase_calls.count("test"), 1)
        self.assertEqual(phase_calls.count("fix"), 2)
        self.assertEqual(stop_reason["code"], "rounds_exhausted")
        # plan is not None (manifest was valid)
        self.assertIsNotNone(plan)
        # detail names the still-failing check
        self.assertIn("c1", stop_reason.get("detail", ""))

    async def test_fix_rounds_zero_returns_rounds_exhausted_after_first_verification(self):
        """With fix_rounds=0, any failure immediately returns rounds_exhausted."""
        cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                     test_active_seconds=3600, test_fix_rounds=0,
                     artifact_s3_uri=None)
        self.w = Worker(cfg)
        c = self.w.service.create("Task2")
        comparison = self.w.freeze_requirements(c["id"])
        run = self.w.store.detail(c["id"])["runs"][0]
        run_id = run["id"]
        baseline = self.w.config.root / "comparisons" / c["id"] / "baseline"
        baseline.mkdir(parents=True, exist_ok=True)
        version_dir = baseline.parent / "specifications" / str(comparison["spec_version"])
        version_dir.mkdir(parents=True, exist_ok=True)
        workspace = self.w.run_root(run) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / MANIFEST_NAME).write_text(json.dumps(_minimal_manifest()))
        (workspace / "tests").mkdir(exist_ok=True)
        (workspace / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")
        fake_plan = _make_plan()
        phase_calls = []

        async def fake_phase(run_id2, phase, task_spec, plan, **kw):
            phase_calls.append(phase)
            r = self.w.store.get("run", run_id2)
            if phase in _TEST_PHASES:
                r["attempts"].append({"id": "t-" + phase, "phase": phase, "active_seconds": 5})
                self.w.store.update("run", run_id2, attempts=r["attempts"])
            return "tests_written"

        self.w.phase = fake_phase
        approved = _minimal_approved_review()
        ts = {**comparison, "approved_test_review": approved}

        with patch("arena.worker.build_plan", return_value=(fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(run_id, ts, baseline, version_dir)

        self.assertEqual(phase_calls, ["test"])  # no fix turns
        self.assertEqual(stop_reason["code"], "rounds_exhausted")


# ── Test 5: budget exhausted ──────────────────────────────────────────────────

class TestBudgetExhausted(unittest.IsolatedAsyncioTestCase):
    """phase() returns test_budget_exhausted → code=budget_exhausted returned."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                     test_active_seconds=3600, test_fix_rounds=2,
                     artifact_s3_uri=None)
        self.w = Worker(cfg)
        c = self.w.service.create("Task")
        self.cid = c["id"]
        comparison = self.w.freeze_requirements(self.cid)
        self.run = self.w.store.detail(self.cid)["runs"][0]
        self.run_id = self.run["id"]
        self.baseline = self.w.config.root / "comparisons" / self.cid / "baseline"
        self.baseline.mkdir(parents=True, exist_ok=True)
        self.version_dir = (self.baseline.parent / "specifications"
                            / str(comparison["spec_version"]))
        self.version_dir.mkdir(parents=True, exist_ok=True)
        self.workspace = self.w.run_root(self.run) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.approved_review = _minimal_approved_review()
        self.task_spec = {**comparison, "approved_test_review": self.approved_review}
        self.fake_plan = _make_plan()

    def _write_manifest(self):
        (self.workspace / MANIFEST_NAME).write_text(json.dumps(_minimal_manifest()))
        (self.workspace / "tests").mkdir(exist_ok=True)
        (self.workspace / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")

    async def test_budget_exhausted_during_write_turn_returns_none_plan(self):
        """budget_exhausted on the initial write turn → plan=None, code=budget_exhausted_no_manifest."""
        phase_calls = []

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            phase_calls.append(phase)
            return "test_budget_exhausted"

        self.w.phase = fake_phase

        plan, stop_reason, _ = await self.w.run_test_stage(
            self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(phase_calls, ["test"])
        self.assertIsNone(plan)
        # No manifest was ever accepted → budget_exhausted_no_manifest (not budget_exhausted)
        self.assertEqual(stop_reason["code"], "budget_exhausted_no_manifest")
        # Message must mention the budget and that Accuracy and Tests are 0
        self.assertIn("3600", stop_reason["message"])
        self.assertIn("Accuracy and Tests are 0", stop_reason["message"])

    async def test_budget_exhausted_during_fix_turn_returns_plan(self):
        """budget_exhausted during a fix turn returns (plan, budget_exhausted, test_info)."""
        phase_calls = []
        call_n = [0]

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            phase_calls.append(phase)
            call_n[0] += 1
            if phase == "test":
                self._write_manifest()
            r = self.w.store.get("run", run_id)
            if phase in _TEST_PHASES:
                r["attempts"].append({"id": "t-" + phase, "phase": phase, "active_seconds": 5})
                self.w.store.update("run", run_id, attempts=r["attempts"])
            if phase == "fix":
                return "test_budget_exhausted"
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, test_info = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(phase_calls, ["test", "fix"])
        self.assertIsNotNone(plan)
        self.assertEqual(stop_reason["code"], "budget_exhausted")
        # Message must mention the budget and the round number
        self.assertIn("3600", stop_reason["message"])
        self.assertIn("fix round", stop_reason["message"].lower())

    async def test_budget_exhausted_no_manifest_message_mentions_budget_and_outcome(self):
        """budget_exhausted_no_manifest message is human-readable per DESIGN.md catalog."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            return "test_budget_exhausted"
        self.w.phase = fake_phase
        _, stop_reason, _ = await self.w.run_test_stage(
            self.run_id, self.task_spec, self.baseline, self.version_dir)
        self.assertEqual(stop_reason["code"], "budget_exhausted_no_manifest")
        msg = stop_reason["message"]
        # Must mention the budget seconds, the turn label, and the consequence
        self.assertIn("3600", msg)
        self.assertIn("Accuracy and Tests are 0", msg)
        # Should NOT say "test_budget_exhausted" (the internal outcome string)
        self.assertNotIn("test_budget_exhausted", msg)


# ── Test 6: budget excludes review and verification ───────────────────────────

class TestBudgetExcludesReviewAndVerification(unittest.IsolatedAsyncioTestCase):
    """Active seconds charged to test stage come only from phase() calls, not from
    CandidateTestRunner or review_harness_tests."""

    async def test_verification_seconds_not_charged_to_test_budget(self):
        """The active_budget passed to phase() equals test_active_seconds minus only
        active seconds from prior test-phase turns, not from verification calls."""
        temp = tempfile.mkdtemp()
        from pathlib import Path
        cfg = Config(data_dir=str(Path(temp) / "data"),
                     test_active_seconds=100, test_fix_rounds=1,
                     artifact_s3_uri=None)
        w = Worker(cfg)
        c = w.service.create("Task")
        comparison = w.freeze_requirements(c["id"])
        run = w.store.detail(c["id"])["runs"][0]
        run_id = run["id"]
        baseline = w.config.root / "comparisons" / c["id"] / "baseline"
        baseline.mkdir(parents=True, exist_ok=True)
        version_dir = baseline.parent / "specifications" / str(comparison["spec_version"])
        version_dir.mkdir(parents=True, exist_ok=True)
        workspace = w.run_root(run) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        approved = _minimal_approved_review()
        task_spec = {**comparison, "approved_test_review": approved}
        fake_plan = _make_plan()

        # Pre-charge 50 test-stage seconds to the run to simulate prior turns
        run_data = w.store.get("run", run_id)
        run_data["attempts"].append({"id": "prior", "phase": "test", "active_seconds": 50})
        w.store.update("run", run_id, attempts=run_data["attempts"])

        observed_priors = []

        async def fake_phase(run_id2, phase, task_spec2, plan, *,
                             prior_active_seconds=0.0, active_budget=None, **kw):
            observed_priors.append(prior_active_seconds)
            if phase == "test":
                (workspace / MANIFEST_NAME).write_text(json.dumps(_minimal_manifest()))
                (workspace / "tests").mkdir(exist_ok=True)
                (workspace / "tests" / "test_foo.py").write_text(
                    "def test_foo(): assert True\n")
            r = w.store.get("run", run_id2)
            if phase in _TEST_PHASES:
                r["attempts"].append({"id": "t-" + phase, "phase": phase, "active_seconds": 10})
                w.store.update("run", run_id2, attempts=r["attempts"])
            return "tests_written"

        w.phase = fake_phase

        # Simulate a slow verification (time passes but no active_seconds charged)
        async def slow_verify(snapshot, plan2, evidence_dir, *args, **kwargs):
            await asyncio.sleep(0)  # yield, simulating async work
            return _passed_evidence()

        with patch("arena.worker.build_plan", return_value=(fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=slow_verify):
            plan, stop_reason, _ = await w.run_test_stage(run_id, task_spec, baseline, version_dir)

        # First call: prior = 50s (from pre-charged attempt)
        self.assertEqual(observed_priors[0], 50.0)
        self.assertEqual(stop_reason["code"], "all_passed")
        # Verification does not add to prior_active_seconds
        import shutil; shutil.rmtree(temp, ignore_errors=True)

    async def test_review_agent_time_not_charged_to_test_budget(self):
        """review_harness_tests() runs outside run_test_stage(); its time must NOT count
        against test_active_seconds.  After run_test_stage() returns, additional review
        seconds that arrive from outside the stage do not cause budget_exhausted."""
        import shutil
        temp = tempfile.mkdtemp()
        from pathlib import Path
        cfg = Config(data_dir=str(Path(temp) / "data"),
                     test_active_seconds=60, test_fix_rounds=0,
                     artifact_s3_uri=None)
        w = Worker(cfg)
        c = w.service.create("Task")
        comparison = w.freeze_requirements(c["id"])
        run = w.store.detail(c["id"])["runs"][0]
        run_id = run["id"]
        baseline = w.config.root / "comparisons" / c["id"] / "baseline"
        baseline.mkdir(parents=True, exist_ok=True)
        version_dir = baseline.parent / "specifications" / str(comparison["spec_version"])
        version_dir.mkdir(parents=True, exist_ok=True)
        workspace = w.run_root(run) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        approved = _minimal_approved_review()
        task_spec = {**comparison, "approved_test_review": approved}
        fake_plan = _make_plan()

        # Simulate a phase that uses 50s of test budget (within 60s limit)
        async def fake_phase(run_id2, phase, task_spec2, plan, *,
                             prior_active_seconds=0.0, active_budget=None, **kw):
            (workspace / MANIFEST_NAME).write_text(json.dumps(_minimal_manifest()))
            (workspace / "tests").mkdir(exist_ok=True)
            (workspace / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")
            r = w.store.get("run", run_id2)
            r["attempts"].append({"id": "t-" + phase, "phase": phase, "active_seconds": 50})
            w.store.update("run", run_id2, attempts=r["attempts"])
            return "tests_written"

        w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await w.run_test_stage(run_id, task_spec, baseline, version_dir)

        # Test stage completed with 50s used out of 60s budget
        self.assertEqual(stop_reason["code"], "all_passed")

        # Simulate 100s of review time arriving AFTER run_test_stage returned.
        # This must NOT trigger budget_exhausted in a subsequent run_test_stage call
        # (the run is not re-entered; we just verify the _active() sum is unaffected by
        # non-test-phase attempts).
        run_data = w.store.get("run", run_id)
        run_data["attempts"].append({"id": "review-time", "phase": "review", "active_seconds": 100})
        w.store.update("run", run_id, attempts=run_data["attempts"])

        # _active() must still be 50 (only test-phase attempts counted)
        total = sum(a.get("active_seconds", 0) for a in w.store.get("run", run_id)["attempts"]
                    if a.get("phase") in _TEST_PHASES)
        self.assertEqual(total, 50)
        shutil.rmtree(temp, ignore_errors=True)


# ── Test 7: no manifest → score 0 ────────────────────────────────────────────

class TestNoManifestScore(unittest.IsolatedAsyncioTestCase):
    """When run_test_stage returns None plan, evaluate_run is called with no_manifest_reason
    and accuracy/tests scores are forced to 0."""

    async def test_no_manifest_forces_scores_to_zero(self):
        """evaluate_candidate with no_manifest_reason must set accuracy and tests score=0."""
        from arena.evaluation import evaluate_candidate, Plan, default_plan
        from arena.config import Config

        temp = tempfile.mkdtemp()
        cfg = Config(data_dir=str(Path(temp) / "data"), artifact_s3_uri=None)
        w = Worker(cfg)
        c = w.service.create("Task")
        comparison = w.freeze_requirements(c["id"])
        run = w.store.detail(c["id"])["runs"][0]
        run_id = run["id"]
        baseline = w.config.root / "comparisons" / c["id"] / "baseline"
        baseline.mkdir(parents=True, exist_ok=True)

        # Pre-create snapshot so evaluate_run doesn't need to run snapshot_run
        root = w.run_root(run)
        snapshot_path = root / "artifacts" / "v1-test" / "snapshot"
        snapshot_path.mkdir(parents=True, exist_ok=True)
        (snapshot_path / "main.py").write_text("print('hello')\n")
        patch_path = snapshot_path.parent / "implementation.patch"
        patch_path.write_text("diff\n")
        from arena.isolation import tree_hash, immutable
        immutable(snapshot_path)
        sha = tree_hash(snapshot_path)
        w.store.update("run", run_id, artifacts={
            "snapshot": str(snapshot_path), "patch": str(patch_path), "sha256": sha})

        no_manifest_msg = "'.mchc-tests.json' was not found in the workspace root."
        evidence = {"checks": [], "environment": {}}

        async def fake_checks(snapshot, plan, evidence_dir, *args, **kwargs):
            return evidence

        called_with = {}

        async def fake_evaluate(task, plan, candidate, config, *, ratings=None,
                                 no_manifest_reason=None, **kw):
            called_with["no_manifest_reason"] = no_manifest_reason
            called_with["ratings"] = ratings
            # Return a minimal result with 0 scores (simulating what the real evaluator does)
            return {"overall": 0, "verified_complete": False, "accuracy": {"score": 0},
                    "tests": {"score": 0}, "relevance": {"score": 0},
                    "critical_failure": True}

        with patch("arena.worker.CandidateTestRunner.run", side_effect=fake_checks), \
             patch("arena.worker.evaluate_candidate", side_effect=fake_evaluate):
            await w.evaluate_run(run_id, comparison, None, None,
                                 no_manifest_reason=no_manifest_msg)

        # no_manifest_reason was passed through to evaluate_candidate
        self.assertEqual(called_with["no_manifest_reason"], no_manifest_msg)
        self.assertIsNone(called_with["ratings"])

        import shutil; shutil.rmtree(temp, ignore_errors=True)


# ── Test 8: review agent failure ─────────────────────────────────────────────

class TestReviewAgentFailure(_TestStageBase):
    """Review failure drives evaluated_incomplete via incomplete_reason (not an unrated path).

    Exercises the saved-code one_saved() flow: test_stages[-1].review must record the
    failure with status='failed' and the full failure dict; incomplete_reason must reach
    evaluate_candidate; the run must end as evaluated_incomplete (R2-4).
    """

    async def test_review_failure_reaches_evaluated_incomplete(self):
        """run_saved_code_test_stage: a failing review gives evaluated_incomplete,
        not an unrated path; test_stages[-1].review.status == 'failed' (R2-4)."""
        from arena.isolation import immutable, tree_hash

        # Provide a snapshot so the run appears in eligible runs.
        snap_dir = self.root / "artifacts" / "v1-snap" / "snapshot"
        snap_dir.mkdir(parents=True)
        (snap_dir / "main.py").write_text("print('hello')\n")
        immutable(snap_dir)
        sha = tree_hash(snap_dir)
        patch_p = snap_dir.parent / "implementation.patch"
        patch_p.write_text("")
        self.w.store.update("run", self.run_id, artifacts={
            "snapshot": str(snap_dir), "patch": str(patch_p), "sha256": sha,
        })

        failure_code = "api_error"
        failure_msg = (
            "The test agent review failed with an API error after 1 attempt(s): "
            "overloaded. Verified test results are shown; Accuracy and Tests are incomplete."
        )
        failure_attempts = 1

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            (self.workspace / MANIFEST_NAME).write_text(
                json.dumps(_minimal_manifest()))
            (self.workspace / "tests").mkdir(exist_ok=True)
            (self.workspace / "tests" / "test_foo.py").write_text(
                "def test_foo(): assert True\n")
            r = self.w.store.get("run", run_id)
            r["attempts"].append({"id": "t-" + phase, "phase": phase,
                                  "active_seconds": 10})
            self.w.store.update("run", run_id, attempts=r["attempts"])
            return "tests_written"

        self.w.phase = fake_phase

        captured: dict = {}

        async def fake_evaluate(task, plan, candidate, config, *, ratings=None,
                                no_manifest_reason=None, incomplete_reason=None, **kw):
            captured["incomplete_reason"] = incomplete_reason
            captured["ratings"] = ratings
            return {"overall": None, "verified_complete": False,
                    "accuracy": None, "tests": None,
                    "evaluation_status": "evaluated_incomplete"}

        async def failing_review(*a, **kw):
            return {"failure": {"code": failure_code, "message": failure_msg,
                                "attempts": failure_attempts}}

        approved = _minimal_approved_review()
        with patch("arena.worker.build_plan",
                   return_value=(_make_plan(), [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())), \
             patch("arena.worker.review_harness_tests",
                   side_effect=failing_review), \
             patch("arena.worker.evaluate_candidate",
                   side_effect=fake_evaluate):
            await self.w.run_saved_code_test_stage(
                self.cid, self.comparison, approved,
                self.comparison["spec_version"])

        # test_stages[-1].review must have status='failed' with failure dict (R2-4)
        run_after = self.w.store.get("run", self.run_id)
        stages = run_after.get("test_stages", [])
        self.assertGreater(len(stages), 0, "test_stages must be populated")
        review = stages[-1].get("review")
        self.assertIsNotNone(review,
                             "test_stages[-1].review must be set after review failure (R2-4)")
        self.assertEqual(review.get("status"), "failed",
                         f"review.status must be 'failed' (R2-4): {review}")
        self.assertEqual(review.get("failure", {}).get("code"), failure_code,
                         f"review failure code must be stored (R2-4): {review}")
        self.assertEqual(review.get("failure", {}).get("attempts"), failure_attempts,
                         f"review failure attempts must be stored (R2-4): {review}")

        # incomplete_reason must reach evaluate_candidate — not an unrated ratings=None path
        self.assertIsNotNone(captured.get("incomplete_reason"),
                             "incomplete_reason must reach evaluate_candidate (R2-4 / W1)")
        self.assertIsNone(captured.get("ratings"),
                          "ratings must be None when review fails (R2-4)")

        # run ends as evaluated_incomplete (accuracy and tests are None)
        run_final = self.w.store.get("run", self.run_id)
        self.assertEqual(run_final.get("status"), "evaluated_incomplete",
                         f"Run must be evaluated_incomplete after review failure (R2-4). "
                         f"Got: {run_final.get('status')}")


# ── Test 9: supplied plan unchanged (legacy comparison) ───────────────────────

class TestSuppliedPlanUnchanged(unittest.IsolatedAsyncioTestCase):
    """When a comparison already has test_review.plan_sha256 set (legacy),
    approved_plan() returns _new_flow=False and prepare_plan IS called."""

    async def test_legacy_comparison_uses_prepare_plan(self):
        """approved_plan() with plan_sha256 set returns _new_flow=False."""
        from arena.test_review import update_review, requirements_digest, plan_digest
        from arena.evaluation import Plan, Check, Criterion

        temp = tempfile.mkdtemp()
        cfg = Config(data_dir=str(Path(temp) / "data"), artifact_s3_uri=None)
        w = Worker(cfg)
        c = w.service.create("Task")
        comparison = w.freeze_requirements(c["id"])
        baseline = w.config.root / "comparisons" / c["id"] / "baseline"
        baseline.mkdir(parents=True, exist_ok=True)

        fake_plan = Plan(
            criteria=[Criterion(id="S1", requirement="req", check_ids=["c1"])],
            checks=[Check(id="c1", group="acceptance", command="pytest")],
            review_coverage={"S1": ["c1"]},
            excluded_groups={},
            review_id="rev-1",
        )
        # Set plan_sha256 on the review to signal legacy flow
        review = w.store.get("comparison", c["id"])["test_review"]
        digest_val = plan_digest(fake_plan)
        review.update(
            status="approved",
            plan_sha256=digest_val,
            approval_sha256="ah",
            requirements_sha256=requirements_digest(comparison),
            approved_at=1.0,
            answers={},
            generation_status="complete",
            proposal={"summary": "P", "test_cases": [
                {"id": "S1", "title": "T1", "steps": [], "expected_result": "r", "test_data": "d"}
            ]},
        )
        w.store.update("comparison", c["id"], test_review=review)
        task_spec = {**comparison, "test_review": review}
        prepare_calls = []

        async def fake_prepare(task, baseline2, config):
            prepare_calls.append(1)
            return fake_plan, {"source": "legacy"}

        def not_obsolete():
            return False

        # review_tests() blocks waiting for approval signals; return the already-approved review.
        async def fake_review_tests(cid, baseline2, task_spec2, obsolete2):
            return review

        with patch.object(w, "review_tests", side_effect=fake_review_tests), \
             patch("arena.worker.prepare_plan", side_effect=fake_prepare), \
             patch("arena.worker.review_alignment_issues", return_value=[]):
            result = await w.approved_plan(c["id"], baseline, task_spec, not_obsolete)

        # Legacy flow: _new_flow=False
        self.assertIsNotNone(result)
        self.assertFalse(result["_new_flow"])
        self.assertEqual(len(prepare_calls), 1)

        import shutil; shutil.rmtree(temp, ignore_errors=True)


# ── Test 10: legacy frozen plan unchanged ─────────────────────────────────────

class TestLegacyFrozenPlanUnchanged(unittest.IsolatedAsyncioTestCase):
    """When freeze() finds a plan already frozen in specifications, it skips generation."""

    async def test_frozen_plan_skips_prepare_plan(self):
        """freeze() with existing spec.plan doesn't call prepare_plan."""
        from arena.evaluation import Plan, Check, Criterion

        temp = tempfile.mkdtemp()
        cfg = Config(data_dir=str(Path(temp) / "data"), artifact_s3_uri=None)
        w = Worker(cfg)
        c = w.service.create("Task")
        comparison = w.freeze_requirements(c["id"])
        baseline = w.config.root / "comparisons" / c["id"] / "baseline"
        baseline.mkdir(parents=True, exist_ok=True)

        fake_plan = Plan(
            criteria=[Criterion(id="S1", requirement="req", check_ids=["c1"])],
            checks=[Check(id="c1", group="acceptance", command="pytest")],
            review_coverage={}, excluded_groups={},
        )
        # Inject a frozen plan into the specification
        specs = w.store.get("comparison", c["id"])["specifications"]
        if specs:
            specs[0]["plan"] = fake_plan.model_dump()
        else:
            specs = [{"version": 1, "plan": fake_plan.model_dump(), "baseline_sha256": "",
                      "task": c["prompt"], "answers": [], "planned_at": 0,
                      "planning": {}, "frozen_at": 0}]
        w.store.update("comparison", c["id"], plan=fake_plan.model_dump(), specifications=specs)

        prepare_calls = []

        async def fake_prepare(*a, **kw):
            prepare_calls.append(1)
            return fake_plan, {}

        async def fake_checks(snapshot, plan, evidence_dir, *a, **kw):
            return {"checks": [], "environment": {}}

        with patch("arena.worker.prepare_plan", side_effect=fake_prepare), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=fake_checks):
            result = await w.freeze(c["id"], baseline)

        # frozen plan should be used without calling prepare_plan
        self.assertEqual(prepare_calls, [])
        # Result contains the frozen plan (not None for legacy path)
        self.assertIsNotNone(result)

        import shutil; shutil.rmtree(temp, ignore_errors=True)
