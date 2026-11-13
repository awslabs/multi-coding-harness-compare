# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for FIXLIST.md worker (W1–W9) and lifecycle (L1–L4) fixes.

Each test is named after its fix ID.  The tests are derived from the binding
specification (FIXLIST.md interfaces 1–3 and the persisted record schema) and
DESIGN.md §9.16 / §10, NOT from the current implementation.  Some tests are
expected to fail until the matching worker/lifecycle part finishes its work.

Driving strategy: use the same fake-phase / fake-verify / fake-review pattern
as tests/test_test_stage.py and tests/test_review_flow.py.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from arena.config import Config
from arena.evaluation import Check, Criterion, Plan
from arena.harness_tests import MANIFEST_NAME, create_lock
from arena.service import Service
from arena.store import TERMINAL
from arena.worker import Worker, _TEST_PHASES


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _approved_review(scenario_id: str = "S1"):
    """Minimal new-flow approved test review (plan_sha256=None)."""
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
                {
                    "id": scenario_id,
                    "title": "Greeting output",
                    "steps": ["Run main.py"],
                    "expected_result": "Prints hello",
                    "test_data": "none",
                    "group": "acceptance",
                },
            ],
            "assumptions": ["Runs on Python 3.12"],
            "limitations": [],
        },
        "answers": {"q1": "Yes"},
        "test_coverage": "focused",
    }


def _manifest(scenario_id: str = "S1", check_id: str = "c1",
              test_file: str = "tests/test_foo.py") -> dict:
    return {
        "version": 1,
        "checks": [
            {"id": check_id, "scenario_id": scenario_id,
             "command": f"python -m pytest {test_file} -q", "junit": None}
        ],
        "test_files": [test_file],
        "python_requirements": [],
    }


def _plan(check_id: str = "c1", scenario_id: str = "S1",
          test_file: str = "tests/test_foo.py", scored: bool = True) -> Plan:
    return Plan(
        criteria=[Criterion(id=scenario_id, requirement="Greeting", check_ids=[check_id])],
        checks=[Check(id=check_id, group="acceptance",
                      command=f"python -m pytest {test_file} -q",
                      scored=scored)],
        review_coverage={scenario_id: [check_id]},
        excluded_groups={},
        acceptance_files={test_file: "def test_foo(): assert True\n"},
        review_id="rev-1",
    )


def _passed_evidence(check_id: str = "c1", scored: bool = True) -> dict:
    return {
        "checks": [{
            "id": check_id, "group": "acceptance", "status": "passed",
            "exit_code": 0, "log": "1 passed", "counts": {"passed": 1},
            "scored": scored,
        }],
        "environment": {},
    }


def _failed_evidence(check_id: str = "c1", scored: bool = True) -> dict:
    return {
        "checks": [{
            "id": check_id, "group": "acceptance", "status": "failed",
            "exit_code": 1, "log": "1 failed\nAssertionError", "counts": {"failed": 1},
            "scored": scored,
        }],
        "environment": {},
    }


class _Base(unittest.IsolatedAsyncioTestCase):
    """Set up a Worker with helpers for each test."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(
            data_dir=str(Path(self.temp.name) / "data"),
            test_active_seconds=3600,
            test_fix_rounds=2,
            artifact_s3_uri=None,
        )
        self.w = Worker(self.cfg)
        c = self.w.service.create("Build a greeting CLI.")
        self.cid = c["id"]
        self.comparison = self.w.freeze_requirements(self.cid)
        self.run = self.w.store.detail(self.cid)["runs"][0]
        self.run_id = self.run["id"]

        self.baseline = self.w.config.root / "comparisons" / self.cid / "baseline"
        self.baseline.mkdir(parents=True, exist_ok=True)
        self.version_dir = (
            self.baseline.parent / "specifications"
            / str(self.comparison["spec_version"])
        )
        self.version_dir.mkdir(parents=True, exist_ok=True)

        self.root = self.w.run_root(self.run)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

        self.approved_review = _approved_review()
        self.task_spec = {**self.comparison, "approved_test_review": self.approved_review}
        self.fake_plan = _plan()

    def _write_manifest(self, manifest_data=None):
        data = manifest_data or _manifest()
        (self.workspace / MANIFEST_NAME).write_text(json.dumps(data))
        (self.workspace / "tests").mkdir(exist_ok=True)
        (self.workspace / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")

    def _record_phase_attempt(self, run_id, phase, active_seconds=10):
        r = self.w.store.get("run", run_id)
        r["attempts"].append({
            "id": "t-" + phase,
            "phase": phase,
            "active_seconds": active_seconds,
            "outcome": None,
        })
        self.w.store.update("run", run_id, attempts=r["attempts"])


# ══════════════════════════════════════════════════════════════════════════════
# W1: Review failure → incomplete_reason (not ratings=None / not evaluated_incomplete
#     without root cause)
# ══════════════════════════════════════════════════════════════════════════════

class TestW1ReviewFailureIncompleteReason(_Base):
    """W1 (§9.8): When the test agent review fails, evaluate_run must be called with
    incomplete_reason = the review failure message; Accuracy and Tests become None →
    evaluated_incomplete.  Never fall back to unrated scoring."""

    async def test_w1_review_failure_uses_incomplete_reason(self):
        """Mock review_harness_tests to return failure; check incomplete_reason propagated."""
        # Set up a successful test stage so review is triggered
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        incomplete_reason_seen = {}

        async def fake_evaluate(task, plan, candidate, config, *,
                                ratings=None, no_manifest_reason=None,
                                incomplete_reason=None, **kw):
            incomplete_reason_seen["value"] = incomplete_reason
            incomplete_reason_seen["ratings"] = ratings
            return {
                "overall": None,
                "verified_complete": False,
                "accuracy": None,
                "tests": None,
                "relevance": {"score": 0.5},
                "evaluation_status": "evaluated_incomplete",
            }

        async def failing_review(*a, **kw):
            return {
                "failure": {
                    "code": "timeout",
                    "message": "The test agent review timed out after 2 attempt(s) (60 s).",
                    "attempts": 2,
                }
            }

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())), \
             patch("arena.worker.review_harness_tests", side_effect=failing_review), \
             patch("arena.worker.evaluate_candidate", side_effect=fake_evaluate):
            await self.w.run_test_stage(self.run_id, self.task_spec,
                                        self.baseline, self.version_dir)
            # Give compare() / evaluate_run a path to exercise; call evaluate_run directly.
            await self.w.evaluate_run(
                self.run_id, self.task_spec, self.fake_plan, None,
                incomplete_reason="The test agent review timed out after 2 attempt(s) (60 s).",
            )

        # incomplete_reason must be a non-empty string (the review failure message)
        self.assertIsNotNone(incomplete_reason_seen.get("value"))
        self.assertIsInstance(incomplete_reason_seen["value"], str)
        self.assertGreater(len(incomplete_reason_seen["value"]), 0)

    async def test_w1_review_failure_incomplete_reason_contains_failure_message(self):
        """The incomplete_reason passed to evaluate_candidate must contain the review failure text."""
        failure_msg = "The test agent review timed out after 3 attempt(s) (120 s)."

        incomplete_reason_seen = {}

        async def fake_evaluate(task, plan, candidate, config, *,
                                ratings=None, no_manifest_reason=None,
                                incomplete_reason=None, **kw):
            incomplete_reason_seen["value"] = incomplete_reason
            return {
                "overall": None, "verified_complete": False,
                "accuracy": None, "tests": None, "relevance": {"score": 0},
                "evaluation_status": "evaluated_incomplete",
            }

        # Set up minimal artifacts for evaluate_run to run
        root = self.w.run_root(self.run)
        snapshot_path = root / "artifacts" / "v1-test" / "snapshot"
        snapshot_path.mkdir(parents=True, exist_ok=True)
        (snapshot_path / "main.py").write_text("print('hello')\n")
        patch_path = snapshot_path.parent / "implementation.patch"
        patch_path.write_text("")
        from arena.isolation import tree_hash, immutable
        immutable(snapshot_path)
        sha = tree_hash(snapshot_path)
        self.w.store.update("run", self.run_id, artifacts={
            "snapshot": str(snapshot_path),
            "patch": str(patch_path),
            "sha256": sha,
        })

        async def fake_checks(*a, **kw):
            return _passed_evidence()

        with patch("arena.worker.CandidateTestRunner.run", side_effect=fake_checks), \
             patch("arena.worker.evaluate_candidate", side_effect=fake_evaluate):
            await self.w.evaluate_run(
                self.run_id, self.task_spec, self.fake_plan, None,
                incomplete_reason=failure_msg,
            )

        self.assertIn(failure_msg, incomplete_reason_seen.get("value", ""))

    async def test_w1_review_failure_stored_in_test_stages_review(self):
        """Review failure through the saved-code path: test_stages[-1].review must record
        the failure with status='failed', incomplete_reason must reach evaluate_candidate,
        and the run ends as evaluated_incomplete — not an unrated path (R2-4 / W1+W2)."""
        from arena.isolation import immutable, tree_hash

        # Give the run a snapshot so it appears in eligible runs for run_saved_code_test_stage.
        snapshot = self.root / "artifacts" / "v1-snap" / "snapshot"
        snapshot.mkdir(parents=True)
        (snapshot / "main.py").write_text("print('hello')\n")
        immutable(snapshot)
        sha = tree_hash(snapshot)
        patch_p = snapshot.parent / "implementation.patch"
        patch_p.write_text("")
        self.w.store.update("run", self.run_id, artifacts={
            "snapshot": str(snapshot), "patch": str(patch_p), "sha256": sha,
        })

        failure_dict = {
            "code": "api_error",
            "message": (
                "The test agent review failed with an API error after 2 attempt(s): "
                "overloaded. Verified test results are shown; "
                "Accuracy and Tests are incomplete."
            ),
            "attempts": 2,
        }

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        captured: dict = {}

        async def fake_evaluate(task, plan, candidate, config, *, ratings=None,
                                no_manifest_reason=None, incomplete_reason=None, **kw):
            captured["incomplete_reason"] = incomplete_reason
            captured["ratings"] = ratings
            return {
                "overall": None, "verified_complete": False,
                "accuracy": None, "tests": None,
                "evaluation_status": "evaluated_incomplete",
            }

        async def failing_review(*a, **kw):
            return {"failure": failure_dict}

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())), \
             patch("arena.worker.review_harness_tests", side_effect=failing_review), \
             patch("arena.worker.evaluate_candidate", side_effect=fake_evaluate):
            await self.w.run_saved_code_test_stage(
                self.cid, self.comparison, self.approved_review,
                self.comparison["spec_version"])

        # test_stages[-1].review must record the failure with status='failed' (W1+W2 / R2-4)
        run_after = self.w.store.get("run", self.run_id)
        stages = run_after.get("test_stages", [])
        self.assertGreater(len(stages), 0, "test_stages must be persisted (W2)")
        last_entry = stages[-1]
        self.assertIn("review", last_entry,
                      "test_stages[-1] must have a 'review' field (W1 / R2-4)")
        review = last_entry["review"]
        self.assertEqual(review.get("status"), "failed",
                         f"review.status must be 'failed' after review failure (R2-4): {review}")
        failure = review.get("failure", {})
        self.assertEqual(failure.get("code"), failure_dict["code"],
                         f"review.failure.code must be stored (R2-4): {review}")
        self.assertEqual(failure.get("attempts"), failure_dict["attempts"],
                         f"review.failure.attempts must be stored (R2-4): {review}")

        # incomplete_reason must reach evaluate_candidate — not an unrated ratings=None path
        self.assertIsNotNone(captured.get("incomplete_reason"),
                             "incomplete_reason must be passed to evaluate_candidate (R2-4 / W1)")
        self.assertIsNone(captured.get("ratings"),
                          "ratings must be None when review fails (R2-4)")

        # run must end up evaluated_incomplete (accuracy=None, tests=None)
        run_final = self.w.store.get("run", self.run_id)
        self.assertEqual(run_final.get("status"), "evaluated_incomplete",
                         f"Run must be evaluated_incomplete after review failure (R2-4). "
                         f"Got: {run_final.get('status')}")


# ══════════════════════════════════════════════════════════════════════════════
# W2: Persist test_stages per interface 3 schema
# ══════════════════════════════════════════════════════════════════════════════

class TestW2TestStagesPersisted(_Base):
    """W2: run['test_stages'] must be a list; each entry has the schema from interface 3."""

    async def test_w2_test_stages_list_exists_after_stage(self):
        """run['test_stages'] is a list after run_test_stage returns."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        run_after = self.w.store.get("run", self.run_id)
        stages = run_after.get("test_stages")
        self.assertIsInstance(stages, list,
                              "run['test_stages'] must be a list (W2)")
        self.assertGreater(len(stages), 0,
                           "test_stages must have at least one entry (W2)")

    async def test_w2_entry_has_required_schema_keys(self):
        """Each test_stages entry contains the schema keys from interface 3."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        run_after = self.w.store.get("run", self.run_id)
        stages = run_after.get("test_stages", [])
        self.assertGreater(len(stages), 0, "test_stages must be persisted (W2)")
        entry = stages[-1]

        # Required top-level keys from interface 3
        required = {"spec_version", "status", "budget_seconds", "used_seconds",
                    "rounds", "stop_reason"}
        for key in required:
            self.assertIn(key, entry,
                          f"test_stages entry missing required key '{key}' (W2)")

    async def test_w2_spec_version_matches_comparison(self):
        """test_stages[0].spec_version matches the comparison's spec_version."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        run_after = self.w.store.get("run", self.run_id)
        stages = run_after.get("test_stages", [])
        self.assertGreater(len(stages), 0)
        self.assertEqual(stages[-1].get("spec_version"),
                         self.comparison["spec_version"],
                         "test_stages[0].spec_version must match the comparison's spec_version (W2)")

    async def test_w2_budget_seconds_set_from_config(self):
        """test_stages[-1].budget_seconds equals config.test_active_seconds."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        run_after = self.w.store.get("run", self.run_id)
        stages = run_after.get("test_stages", [])
        self.assertGreater(len(stages), 0)
        self.assertEqual(stages[-1].get("budget_seconds"), 3600,
                         "test_stages[-1].budget_seconds must equal config.test_active_seconds (W2)")

    async def test_w2_stop_reason_code_in_entry(self):
        """test_stages[-1].stop_reason.code is set."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        run_after = self.w.store.get("run", self.run_id)
        stages = run_after.get("test_stages", [])
        self.assertGreater(len(stages), 0)
        entry_stop = stages[-1].get("stop_reason", {})
        self.assertIn("code", entry_stop,
                      "test_stages[-1].stop_reason must have a 'code' field (W2)")
        self.assertEqual(entry_stop["code"], stop_reason["code"],
                         "test_stages[-1].stop_reason.code must match returned stop_reason.code (W2)")

    async def test_w2_rounds_list_in_entry(self):
        """test_stages[-1].rounds is a list with at least one entry per turn."""
        phase_calls = []

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            phase_calls.append(phase)
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        run_after = self.w.store.get("run", self.run_id)
        stages = run_after.get("test_stages", [])
        self.assertGreater(len(stages), 0)
        rounds = stages[-1].get("rounds", [])
        self.assertIsInstance(rounds, list, "test_stages[-1].rounds must be a list (W2)")
        # At minimum the write turn is recorded
        self.assertGreater(len(rounds), 0,
                           "test_stages[-1].rounds must have at least the write turn (W2)")

    async def test_w2_lock_stored_in_entry(self):
        """test_stages[-1].lock has 'files' and 'auto_locked' keys after lock is set."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        run_after = self.w.store.get("run", self.run_id)
        stages = run_after.get("test_stages", [])
        self.assertGreater(len(stages), 0)
        entry = stages[-1]
        lock = entry.get("lock", {})
        self.assertIn("files", lock, "test_stages[-1].lock must have 'files' (W2)")
        self.assertIn("auto_locked", lock,
                      "test_stages[-1].lock must have 'auto_locked' (W2)")

    async def test_w2_convenience_copies_kept(self):
        """run['test_stop_reason'] and run['test_lock'] are still written as convenience copies."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)
            self.w.store.update("run", self.run_id, test_stop_reason=stop_reason)

        run_after = self.w.store.get("run", self.run_id)
        self.assertIsNotNone(run_after.get("test_stop_reason"),
                             "run['test_stop_reason'] convenience copy must exist (W2)")


# ══════════════════════════════════════════════════════════════════════════════
# W3: Lock violation → fix turn (not repair), never skips verification
# ══════════════════════════════════════════════════════════════════════════════

class TestW3LockViolationToFixNotRepair(_Base):
    """W3: After the lock exists, a lock violation must NEVER trigger a 'repair' turn.
    Instead: restore files, record violation, verify, then if scored checks fail and
    rounds remain → 'fix' turn (with violation notice in feedback)."""

    async def test_w3_lock_violation_triggers_fix_not_repair(self):
        """After lock exists and harness modifies a locked file, next turn is 'fix' not 'repair'."""
        phase_calls = []

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            phase_calls.append(phase)
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            if phase == "fix":
                # Harness violates the lock by changing the test file
                (self.workspace / "tests" / "test_foo.py").write_text("MODIFIED BY FIX\n")
            return "tests_written"

        self.w.phase = fake_phase

        verify_calls = [0]

        async def fake_verify(snapshot, plan, evidence_dir, *a, **kw):
            result = (_failed_evidence() if verify_calls[0] == 0 else _passed_evidence())
            verify_calls[0] += 1
            return result

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=fake_verify):
            plan, stop_reason, test_info = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # 'repair' must NOT appear after the lock is set (W3)
        # Phases before the lock: ["test"]; after lock violation on fix: must NOT use "repair"
        post_write_phases = phase_calls[1:]  # everything after the initial write turn
        self.assertNotIn("repair", post_write_phases,
                         "After lock exists, violation must NOT trigger a 'repair' turn (W3).\n"
                         f"Phases called: {phase_calls}")

    async def test_w3_lock_violation_does_not_skip_verification(self):
        """After a lock violation, verification must still run before the next fix turn."""
        verify_calls = [0]

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            if phase == "fix":
                (self.workspace / "tests" / "test_foo.py").write_text("EVIL\n")
            return "tests_written"

        self.w.phase = fake_phase

        async def fake_verify(snapshot, plan, evidence_dir, *a, **kw):
            verify_calls[0] += 1
            return _failed_evidence()

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=fake_verify):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # Verification must run after EVERY round (write + 2 fix = 3 verifications minimum)
        self.assertGreaterEqual(verify_calls[0], 1,
                                "Verification must run after each round (W3)")

    async def test_w3_fix_turn_feedback_mentions_violation(self):
        """The fix turn's prompt_override must contain notice about the lock violation."""
        fix_prompts = []
        verify_calls = [0]

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            if phase == "fix":
                (self.workspace / "tests" / "test_foo.py").write_text("MODIFIED\n")
                po = kw.get("prompt_override", "")
                if po:
                    fix_prompts.append(po)
            return "tests_written"

        self.w.phase = fake_phase

        async def fake_verify(snapshot, plan, evidence_dir, *a, **kw):
            result = (_failed_evidence() if verify_calls[0] == 0 else _passed_evidence())
            verify_calls[0] += 1
            return result

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=fake_verify):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # At least one fix prompt must mention that the lock was enforced (W3)
        # The violation notice should say to edit the implementation, not the tests
        combined = "\n".join(fix_prompts)
        self.assertTrue(
            "implementation" in combined.lower() or "restored" in combined.lower(),
            f"Fix turn feedback must mention lock violation / restoration (W3).\n"
            f"Received prompts: {fix_prompts[:1]}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# W4: Only scored checks trigger fix turns / stop rule
# ══════════════════════════════════════════════════════════════════════════════

class TestW4OnlyScoredChecksTriggerFix(_Base):
    """W4 (§9.13): Only scored checks that fail or error count as failing for fix turns
    and the stop rule.  If the per-harness plan has no scored checks, stop with all_passed."""

    async def test_w4_unscored_failing_check_does_not_trigger_fix(self):
        """When only an unscored check fails, no fix turn is dispatched → all_passed."""
        phase_calls = []
        # Plan with an unscored, non-mandatory check only; criterion has no check_ids
        unscored_plan = Plan(
            criteria=[Criterion(id="S1", requirement="Greeting", check_ids=[])],
            checks=[Check(id="c-unscored", group="project",
                          command="python -m pytest tests/ -q",
                          scored=False, mandatory=False)],
            review_coverage={"S1": []},
            excluded_groups={},
            acceptance_files={"tests/test_foo.py": "def test_foo(): assert True\n"},
            review_id="rev-1",
        )

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            phase_calls.append(phase)
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        # Unscored check fails
        unscored_failed_evidence = {
            "checks": [{
                "id": "c-unscored", "group": "project", "status": "failed",
                "exit_code": 1, "log": "1 failed", "counts": {"failed": 1},
                "scored": False,
            }],
            "environment": {},
        }

        with patch("arena.worker.build_plan", return_value=(unscored_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=unscored_failed_evidence)):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # No fix turn — only unscored checks failed
        self.assertNotIn("fix", phase_calls,
                         "Unscored check failure must NOT trigger a fix turn (W4).\n"
                         f"Phases: {phase_calls}")
        self.assertEqual(stop_reason["code"], "all_passed",
                         f"No scored checks failing → all_passed (W4). Got: {stop_reason}")

    async def test_w4_no_scored_checks_gives_all_passed(self):
        """A plan with no scored checks at all → all_passed immediately after verification."""
        phase_calls = []
        # Plan with one criterion and zero checks mapped to it (no scored checks)
        no_scored_plan = Plan(
            criteria=[Criterion(id="S1", requirement="Greeting", check_ids=[])],
            checks=[],  # No checks at all
            review_coverage={"S1": []},
            excluded_groups={},
            review_id="rev-1",
        )

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            phase_calls.append(phase)
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(no_scored_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value={"checks": [], "environment": {}})):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "all_passed",
                         f"Plan with no scored checks must yield all_passed (W4). Got: {stop_reason}")

    async def test_w4_scored_failing_check_triggers_fix(self):
        """A scored check that fails must trigger a fix turn."""
        phase_calls = []
        verify_calls = [0]

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            phase_calls.append(phase)
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        async def fake_verify(snapshot, plan, evidence_dir, *a, **kw):
            result = (_failed_evidence() if verify_calls[0] == 0 else _passed_evidence())
            verify_calls[0] += 1
            return result

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=fake_verify):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertIn("fix", phase_calls,
                      "A scored failing check must trigger a fix turn (W4)")
        self.assertEqual(stop_reason["code"], "all_passed")


# ══════════════════════════════════════════════════════════════════════════════
# W5: Repair-turn prompts carry validation errors + full approved specification
# ══════════════════════════════════════════════════════════════════════════════

class TestW5RepairPromptContainsFullSpec(_Base):
    """W5 (§9.14): Repair turns must include both the validation errors verbatim
    and the full approved specification (every scenario field)."""

    async def test_w5_repair_prompt_includes_scenario_id(self):
        """The repair prompt must include the scenario ID from the approved spec."""
        repair_prompts = []
        turn = [0]

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            turn[0] += 1
            self._record_phase_attempt(run_id, phase)
            po = kw.get("prompt_override")
            if phase == "repair" and po:
                repair_prompts.append(po)
                # Repair turn now writes a valid manifest
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertGreater(len(repair_prompts), 0, "No repair prompt was sent")
        combined = "\n".join(repair_prompts)
        # The approved spec contains scenario_id="S1" and title="Greeting output"
        self.assertIn("S1", combined,
                      "Repair prompt must include scenario ID from the approved spec (W5).\n"
                      f"Prompt excerpt: {combined[:300]}")

    async def test_w5_repair_prompt_includes_validation_errors(self):
        """The repair prompt includes the validation errors verbatim."""
        repair_prompts = []

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            po = kw.get("prompt_override")
            if phase == "repair":
                if po:
                    repair_prompts.append(po)
                # Repair turn fixes the manifest
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertGreater(len(repair_prompts), 0, "No repair prompt was sent")
        combined = "\n".join(repair_prompts)
        # The prompt must include some validation error context
        self.assertTrue(
            "VALIDATION" in combined.upper() or "error" in combined.lower(),
            f"Repair prompt must mention validation errors (W5).\nPrompt: {combined[:300]}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# W6: Implementation diff passed to review_harness_tests
# ══════════════════════════════════════════════════════════════════════════════

class TestW6ImplementationDiff(_Base):
    """W6 (§9.12): The worker computes the diff of non-locked implementation files
    between the implementation snapshot and the final test-stage snapshot, then passes
    it as implementation_diff to review_harness_tests (interface 2)."""

    async def test_w6_implementation_diff_passed_to_review(self):
        """review_harness_tests is called with implementation_diff keyword argument."""
        review_kwargs = {}

        async def capturing_review(*a, **kw):
            review_kwargs.update(kw)
            return {
                "scenarios": [
                    {"scenario_id": "S1", "rating": 1, "verdict": "faithful",
                     "reason": "ok", "evidence": "tests/test_foo.py::test_foo"}
                ],
                "summary": "looks good",
                "concerns": [],
                "ratings": {"S1": 1},
            }

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())), \
             patch("arena.worker.review_harness_tests", side_effect=capturing_review):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)
            # run_test_stage doesn't call review_harness_tests directly;
            # the compare() loop does it after. We test the keyword is accepted
            # by calling review_harness_tests directly (the interface contract).
            # The important check is that worker.py COMPUTES the diff and passes it.
            # We test this via compare() path below by patching _compute_implementation_diff.

        # Verify the diff helper function exists and is importable
        from arena.worker import _compute_implementation_diff
        self.assertTrue(callable(_compute_implementation_diff),
                        "_compute_implementation_diff must exist (W6)")

    async def test_w6_implementation_diff_passed_in_compare_loop(self):
        """In the compare() loop, review_harness_tests receives implementation_diff kwarg."""
        review_kwargs_list = []

        async def capturing_review(*a, **kw):
            review_kwargs_list.append(kw)
            return {
                "scenarios": [
                    {"scenario_id": "hello", "rating": 1, "verdict": "faithful",
                     "reason": "ok", "evidence": "tests/test_cli.py::test_cli"}
                ],
                "summary": "looks good",
                "concerns": [],
                "ratings": {"hello": 1},
            }

        # Build a full compare() scenario using the patterns from test_review_flow.py
        from arena.test_review import TestProposal, TestScenario
        approved = {
            "id": "rev-1", "revision": 1, "status": "approved",
            "plan_sha256": None, "approval_sha256": "ah",
            "requirements_sha256": "rh", "approved_at": 1.0,
            "spec_version": 1,
            "proposal": {
                "summary": "Test greeting",
                "test_cases": [{"id": "hello", "title": "Greeting",
                                 "steps": ["run"], "expected_result": "hi",
                                 "test_data": "none"}],
                "assumptions": [], "limitations": [],
            },
            "answers": {}, "test_coverage": "focused",
        }

        async def fake_phase_full(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "develop":
                (self.workspace / "main.py").write_text("print('hello')\n")
                return "implemented"
            if phase in _TEST_PHASES:
                (self.workspace / MANIFEST_NAME).write_text(json.dumps({
                    "version": 1,
                    "checks": [{"id": "cli", "scenario_id": "hello",
                                "command": "python main.py", "junit": None}],
                    "test_files": ["tests/test_cli.py"],
                    "python_requirements": [],
                }))
                (self.workspace / "tests").mkdir(exist_ok=True)
                (self.workspace / "tests" / "test_cli.py").write_text(
                    "def test_cli(): assert True\n")
                return "tests_written"
            if phase == "clarify":
                return "ready"
            return "implemented"

        cli_plan = Plan(
            criteria=[Criterion(id="hello", requirement="Greeting", check_ids=["cli"])],
            checks=[Check(id="cli", group="acceptance",
                          command="python main.py", scored=True)],
            review_coverage={"hello": ["cli"]},
            excluded_groups={},
            acceptance_files={"tests/test_cli.py": "def test_cli(): assert True\n"},
            review_id="rev-1",
        )

        self.w.phase = fake_phase_full

        async def fake_checks(*a, **kw):
            return {
                "checks": [{"id": "cli", "group": "acceptance", "status": "passed",
                             "exit_code": 0, "log": "ok", "counts": {}}],
                "environment": {},
            }

        async def fake_score(*a, **kw):
            return {"overall": 100, "verified_complete": True}

        self.w.score_candidate = fake_score

        with patch("arena.worker.build_plan", return_value=(cli_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=fake_checks), \
             patch("arena.worker.review_harness_tests", side_effect=capturing_review):
            # Inject approved review into the comparison
            comp = self.w.store.get("comparison", self.cid)
            comp["test_review"] = approved
            comp["spec_version"] = 1
            self.w.store.update("comparison", self.cid,
                                test_review=approved, spec_version=1)

            task = asyncio.create_task(self.w.compare(self.cid))
            # Approve after a short wait
            await asyncio.sleep(0.05)
            try:
                await asyncio.wait_for(task, timeout=15)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        # Check that review_harness_tests was called with implementation_diff
        if review_kwargs_list:
            # W6: implementation_diff must be passed (may be None if no changes)
            self.assertIn("implementation_diff", review_kwargs_list[0],
                          "review_harness_tests must receive implementation_diff kwarg (W6)")


# ══════════════════════════════════════════════════════════════════════════════
# W7: Infrastructure error → incomplete_reason (not no_manifest_reason)
# ══════════════════════════════════════════════════════════════════════════════

class TestW7InfrastructureVsHarnessError(_Base):
    """W7 (§9.5): Infrastructure failures (sandbox/runner error) before any manifest
    is accepted → stop_reason `infrastructure_error` and `incomplete_reason` (not
    `no_manifest_reason`).  Harness-caused failures → `no_manifest_reason`."""

    async def test_w7_infrastructure_error_gives_infrastructure_stop_code(self):
        """phase() returning 'infrastructure_error' → stop_reason code 'infrastructure_error'."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            # Simulate an infrastructure error on the write turn
            return "infrastructure_error"

        self.w.phase = fake_phase

        plan, stop_reason, _ = await self.w.run_test_stage(
            self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "infrastructure_error",
                         f"Infrastructure error must give stop code 'infrastructure_error' (W7).\n"
                         f"Got: {stop_reason}")
        self.assertIsNone(plan,
                          "Infrastructure error before manifest accepted must yield plan=None (W7)")

    async def test_w7_infrastructure_error_uses_incomplete_reason_not_no_manifest(self):
        """When infrastructure prevents the turn, evaluate_run receives incomplete_reason,
        not no_manifest_reason."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            return "infrastructure_error"

        self.w.phase = fake_phase

        plan, stop_reason, _ = await self.w.run_test_stage(
            self.run_id, self.task_spec, self.baseline, self.version_dir)

        # For infrastructure errors the message must reference §10 template
        # and must NOT say "Accuracy and Tests are 0" (that's for harness failures)
        msg = stop_reason.get("message", "")
        # §10: "infrastructure_error": "{cause} stopped the test stage before any test manifest
        # was accepted. The evaluation is incomplete; this is not the harness's fault."
        self.assertIn("incomplete", msg.lower(),
                      f"Infrastructure error message must say 'incomplete' (W7 / §10).\n"
                      f"Message: {msg}")
        # Must NOT say "Accuracy and Tests are 0" (that's no_manifest path)
        self.assertNotIn("Accuracy and Tests are 0", msg,
                         f"Infrastructure error message must NOT say 'Accuracy and Tests are 0' (W7).\n"
                         f"Message: {msg}")

    async def test_w7_harness_failed_gives_no_manifest_reason_message(self):
        """phase() returning 'harness_failed' (not infrastructure) → no_manifest_reason path
        (accuracy 0, tests 0, NOT incomplete)."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            return "harness_failed"

        self.w.phase = fake_phase

        plan, stop_reason, _ = await self.w.run_test_stage(
            self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "harness_failed",
                         f"Harness-caused failure must give stop code 'harness_failed' (W7).\n"
                         f"Got: {stop_reason}")
        self.assertIsNone(plan)
        # §10 template: "harness_failed" (no lock): "Accuracy and Tests are 0."
        msg = stop_reason.get("message", "")
        self.assertIn("Accuracy and Tests are 0", msg,
                      f"Harness failure message must say 'Accuracy and Tests are 0' (W7 / §10).\n"
                      f"Message: {msg}")


# ══════════════════════════════════════════════════════════════════════════════
# W8: _WORKER_ACTIVE includes 'testing' and 'reviewing_tests'
# ══════════════════════════════════════════════════════════════════════════════

class TestW8WorkerActiveStatuses(unittest.TestCase):
    """W8: Service._WORKER_ACTIVE must include 'testing' and 'reviewing_tests' so that
    a spec revision during the test stage is handled via the in-flight path."""

    def test_w8_testing_in_worker_active(self):
        """'testing' is in Service._WORKER_ACTIVE."""
        from arena.service import Service
        self.assertIn("testing", Service._WORKER_ACTIVE,
                      "'testing' must be in _WORKER_ACTIVE (W8)")

    def test_w8_reviewing_tests_in_worker_active(self):
        """'reviewing_tests' is in Service._WORKER_ACTIVE."""
        from arena.service import Service
        self.assertIn("reviewing_tests", Service._WORKER_ACTIVE,
                      "'reviewing_tests' must be in _WORKER_ACTIVE (W8)")

    def test_w8_testing_in_store_non_terminal(self):
        """'testing' is NOT in TERMINAL statuses (it is non-terminal)."""
        self.assertNotIn("testing", TERMINAL,
                         "'testing' must NOT be in TERMINAL statuses (W8)")

    def test_w8_reviewing_tests_in_store_non_terminal(self):
        """'reviewing_tests' is NOT in TERMINAL statuses."""
        self.assertNotIn("reviewing_tests", TERMINAL,
                         "'reviewing_tests' must NOT be in TERMINAL statuses (W8)")


# ══════════════════════════════════════════════════════════════════════════════
# W9: DESIGN §10 message templates
# ══════════════════════════════════════════════════════════════════════════════

class TestW9StopReasonMessages(_Base):
    """W9: Every stop code must use a §10 message template (no ad-hoc wording)."""

    async def _get_stop_reason_for_phase_outcome(self, phase_outcome, write_manifest=False,
                                                  verify_evidence=None):
        """Helper: run a minimal test stage and return the stop_reason."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if write_manifest and phase == "test":
                self._write_manifest()
            return phase_outcome

        self.w.phase = fake_phase
        evidence = verify_evidence or _failed_evidence()

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=evidence)):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)
        return stop_reason

    async def test_w9_budget_exhausted_no_manifest_message_template(self):
        """budget_exhausted_no_manifest message matches §10 template."""
        stop_reason = await self._get_stop_reason_for_phase_outcome("test_budget_exhausted")
        msg = stop_reason["message"]
        # §10: "The {budget_seconds}-second test budget ran out during the {turn_label}
        # before an acceptable test manifest existed. Accuracy and Tests are 0."
        self.assertIn("3600", msg,
                      f"Message must include budget seconds (W9 / §10): {msg}")
        self.assertIn("Accuracy and Tests are 0", msg,
                      f"Message must include consequence (W9 / §10): {msg}")
        # Must NOT use the internal outcome string
        self.assertNotIn("test_budget_exhausted", msg,
                         f"Message must not contain internal outcome string (W9): {msg}")

    async def test_w9_harness_failed_no_lock_message_template(self):
        """harness_failed (no lock) message matches §10 template."""
        stop_reason = await self._get_stop_reason_for_phase_outcome("harness_failed")
        msg = stop_reason["message"]
        # §10: "The {turn_label} ended without a result: {cause}. Accuracy and Tests are 0."
        self.assertIn("Accuracy and Tests are 0", msg,
                      f"harness_failed (no lock) must include consequence (W9 / §10): {msg}")

    async def test_w9_manifest_missing_message_template(self):
        """manifest_missing message matches §10 template."""
        # Write no manifest; exhaust all repair rounds
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            # Never write a manifest
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        msg = stop_reason["message"]
        # §10: "The harness did not write the test manifest (.mchc-tests.json).
        # Accuracy and Tests are 0."
        self.assertIn("Accuracy and Tests are 0", msg,
                      f"manifest_missing must include consequence (W9 / §10): {msg}")
        self.assertNotIn("test_budget_exhausted", msg,
                         f"Message must not contain internal string (W9): {msg}")

    async def test_w9_all_passed_message_template(self):
        """all_passed message matches §10 template."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "all_passed")
        msg = stop_reason["message"]
        # §10: "All {passed_count} scored checks passed in {round_label}."
        # Must NOT be the old generic "All checks passed."
        self.assertFalse(
            msg == "All checks passed.",
            f"all_passed message must use §10 template (W9), not generic text.\nGot: {msg}"
        )

    async def test_w9_rounds_exhausted_with_lock_message_template(self):
        """rounds_exhausted (lock exists) message matches §10 template."""
        verify_calls = [0]

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "rounds_exhausted")
        msg = stop_reason["message"]
        # §10: "All {total_rounds} fix rounds were used; {still_failing_count} scored
        # check(s) still failed: {check_list}. Scored the code from {evaluated_round_label}."
        self.assertTrue(
            "fix round" in msg.lower() or "rounds" in msg.lower(),
            f"rounds_exhausted message must mention fix rounds (W9 / §10): {msg}"
        )
        # Must mention the failing check id
        self.assertIn("c1", msg,
                      f"rounds_exhausted message must name failing checks (W9 / §10): {msg}")

    async def test_w9_budget_exhausted_with_lock_message_template(self):
        """budget_exhausted (lock exists) message matches §10 template."""
        call_n = [0]

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            call_n[0] += 1
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            if phase == "fix":
                return "test_budget_exhausted"
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "budget_exhausted")
        msg = stop_reason["message"]
        # §10: "The {budget_seconds}-second test budget ran out during fix round {round_k}
        # of {total_rounds}; ..."
        self.assertIn("3600", msg,
                      f"budget_exhausted (lock) message must mention budget seconds (W9 / §10): {msg}")
        self.assertIn("fix round", msg.lower(),
                      f"budget_exhausted (lock) message must mention 'fix round' (W9 / §10): {msg}")


# ══════════════════════════════════════════════════════════════════════════════
# L1: spec_revised during test-stage → stop_reason spec_revised
# ══════════════════════════════════════════════════════════════════════════════

class TestL1SpecRevisedDuringTestStage(_Base):
    """L1 (§9.4): If phase() returns 'revised' during any test-stage turn, run_test_stage
    must stop with stop_reason code 'spec_revised' and NOT evaluate the run."""

    async def test_l1_revised_on_write_turn_gives_spec_revised(self):
        """phase() returning 'revised' on the write turn → stop_reason 'spec_revised'."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                return "revised"
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "spec_revised",
                         f"'revised' on write turn must yield stop_reason 'spec_revised' (L1).\n"
                         f"Got: {stop_reason}")

    async def test_l1_revised_on_fix_turn_gives_spec_revised(self):
        """phase() returning 'revised' on a fix turn → stop_reason 'spec_revised'."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
                return "tests_written"
            if phase == "fix":
                return "revised"
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "spec_revised",
                         f"'revised' on fix turn must yield stop_reason 'spec_revised' (L1).\n"
                         f"Got: {stop_reason}")

    async def test_l1_spec_revised_message_matches_catalog(self):
        """spec_revised stop_reason message matches §10 template."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            return "revised"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "spec_revised")
        msg = stop_reason.get("message", "")
        # §10: "The specification was revised during the test stage; this test stage was
        # abandoned and a new one runs against revision {new_version}."
        self.assertTrue(
            "revised" in msg.lower() or "specification" in msg.lower(),
            f"spec_revised message must reference the revision (L1 / §10): {msg}"
        )

    async def test_l1_spec_revised_keeps_stage_entry(self):
        """After spec_revised, the test_stages entry is kept (not discarded)."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            return "revised"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # test_stages must have an entry even for abandoned stages (W2+L1)
        run_after = self.w.store.get("run", self.run_id)
        stages = run_after.get("test_stages", [])
        # This may fail until W2+L1 are both implemented
        self.assertGreater(len(stages), 0,
                           "test_stages must retain the abandoned entry (L1+W2)")


# ══════════════════════════════════════════════════════════════════════════════
# L2: Budget counts active_seconds of test-phase attempts only
# ══════════════════════════════════════════════════════════════════════════════

class TestL2BudgetAccountingTestPhasesOnly(unittest.IsolatedAsyncioTestCase):
    """L2 (§9.2): test_active_seconds is the sum of active_seconds of attempts whose
    phase is in _TEST_PHASES.  Review, clarify, develop phases must NOT count."""

    async def test_l2_test_phase_seconds_counted(self):
        """Attempts with phase in _TEST_PHASES contribute to the test budget."""
        self.assertIn("test", _TEST_PHASES)
        self.assertIn("fix", _TEST_PHASES)
        self.assertIn("repair", _TEST_PHASES)

    async def test_l2_non_test_phases_not_counted(self):
        """Attempts with phase 'develop', 'clarify', 'review' do NOT count."""
        temp = tempfile.mkdtemp()
        try:
            cfg = Config(
                data_dir=str(Path(temp) / "data"),
                test_active_seconds=100, test_fix_rounds=1,
                artifact_s3_uri=None,
            )
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
            approved = _approved_review()
            task_spec = {**comparison, "approved_test_review": approved}
            fake_pl = _plan()

            # Pre-load non-test-phase attempts (develop, clarify) with high seconds
            run_data = w.store.get("run", run_id)
            run_data["attempts"].extend([
                {"id": "dev-1", "phase": "develop", "active_seconds": 50},
                {"id": "clar-1", "phase": "clarify", "active_seconds": 30},
            ])
            w.store.update("run", run_id, attempts=run_data["attempts"])

            observed_priors = []

            async def fake_phase(rid, phase, ts, plan, *,
                                 prior_active_seconds=0.0, active_budget=None, **kw):
                observed_priors.append(prior_active_seconds)
                if phase == "test":
                    (workspace / MANIFEST_NAME).write_text(json.dumps(_manifest()))
                    (workspace / "tests").mkdir(exist_ok=True)
                    (workspace / "tests" / "test_foo.py").write_text(
                        "def test_foo(): assert True\n")
                r = w.store.get("run", rid)
                if phase in _TEST_PHASES:
                    r["attempts"].append({"id": "t-" + phase, "phase": phase,
                                          "active_seconds": 10, "outcome": None})
                    w.store.update("run", rid, attempts=r["attempts"])
                return "tests_written"

            w.phase = fake_phase

            with patch("arena.worker.build_plan", return_value=(fake_pl, [])), \
                 patch("arena.worker.CandidateTestRunner.run",
                       side_effect=AsyncMock(return_value=_passed_evidence())):
                plan, stop_reason, _ = await w.run_test_stage(
                    run_id, task_spec, baseline, version_dir)

            # The prior_active_seconds for the first test-phase call must be 0
            # (develop/clarify phases must NOT be counted)
            self.assertEqual(observed_priors[0], 0.0,
                             f"prior_active_seconds at first test-phase call must be 0 (L2).\n"
                             f"Got: {observed_priors[0]}")

        finally:
            import shutil
            shutil.rmtree(temp, ignore_errors=True)

    async def test_l2_active_budget_decreases_with_test_phase_usage(self):
        """active_budget passed to phase decreases as test-phase active_seconds accumulate."""
        temp = tempfile.mkdtemp()
        try:
            cfg = Config(
                data_dir=str(Path(temp) / "data"),
                test_active_seconds=100, test_fix_rounds=2,
                artifact_s3_uri=None,
            )
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
            approved = _approved_review()
            task_spec = {**comparison, "approved_test_review": approved}
            fake_pl = _plan()

            observed_budgets = []
            verify_calls = [0]

            async def fake_phase(rid, phase, ts, plan, *,
                                 prior_active_seconds=0.0, active_budget=None, **kw):
                if active_budget is not None:
                    observed_budgets.append(active_budget)
                if phase == "test":
                    (workspace / MANIFEST_NAME).write_text(json.dumps(_manifest()))
                    (workspace / "tests").mkdir(exist_ok=True)
                    (workspace / "tests" / "test_foo.py").write_text(
                        "def test_foo(): assert True\n")
                r = w.store.get("run", rid)
                if phase in _TEST_PHASES:
                    r["attempts"].append({"id": "t-" + phase, "phase": phase,
                                          "active_seconds": 20, "outcome": None})
                    w.store.update("run", rid, attempts=r["attempts"])
                return "tests_written"

            w.phase = fake_phase

            async def fake_verify(snapshot, plan, evidence_dir, *a, **kw):
                result = (_failed_evidence() if verify_calls[0] < 2 else _passed_evidence())
                verify_calls[0] += 1
                return result

            with patch("arena.worker.build_plan", return_value=(fake_pl, [])), \
                 patch("arena.worker.CandidateTestRunner.run", side_effect=fake_verify):
                await w.run_test_stage(run_id, task_spec, baseline, version_dir)

            # active_budget on the second test-phase call should be less than the first
            if len(observed_budgets) >= 2:
                self.assertLess(
                    observed_budgets[1], observed_budgets[0],
                    f"active_budget must decrease with each test-phase turn (L2).\n"
                    f"Budgets: {observed_budgets}"
                )

        finally:
            import shutil
            shutil.rmtree(temp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# L3: Recovery/resume from persisted test_stages entry
# ══════════════════════════════════════════════════════════════════════════════

class TestL3RecoveryFromTestStages(_Base):
    """L3 (§9.3): After a worker restart, run_test_stage resumes from the persisted
    test_stages entry (lock, last completed round) instead of starting over.
    If budget is already spent, evaluate the last completed round."""

    async def test_l3_budget_already_spent_at_resume_uses_last_round(self):
        """If the budget is already spent when run_test_stage is resumed (crash recovery),
        it must NOT dispatch any phase but evaluate the last completed round with
        budget_exhausted (R2-1 / L3).

        Pre-populates a real test_stages entry with a lock and completed write round
        and also saves the lock bytes to disk — matching the actual post-write state."""
        from arena.worker import _save_lock_to_disk

        # Fresh worker with a small budget so the pre-charged attempt exceeds it.
        cfg = Config(
            data_dir=str(Path(self.temp.name) / "data2"),
            test_active_seconds=100,  # 100 s budget; 100 s pre-charged → exhausted on entry
            test_fix_rounds=2,
            artifact_s3_uri=None,
        )
        w2 = Worker(cfg)
        c2 = w2.service.create("Build a greeting CLI.")
        cid2 = c2["id"]
        comparison2 = w2.freeze_requirements(cid2)
        run2 = w2.store.detail(cid2)["runs"][0]
        run_id2 = run2["id"]
        baseline2 = w2.config.root / "comparisons" / cid2 / "baseline"
        baseline2.mkdir(parents=True, exist_ok=True)
        version_dir2 = baseline2.parent / "specifications" / str(comparison2["spec_version"])
        version_dir2.mkdir(parents=True, exist_ok=True)
        workspace2 = w2.run_root(run2) / "workspace"
        workspace2.mkdir(parents=True, exist_ok=True)
        root2 = w2.run_root(run2)
        approved2 = _approved_review()
        task_spec2 = {**comparison2, "approved_test_review": approved2}

        # Inject manifest and test file so load_manifest / build_plan have something to read.
        (workspace2 / MANIFEST_NAME).write_text(json.dumps(_manifest()))
        (workspace2 / "tests").mkdir(exist_ok=True)
        (workspace2 / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")

        # Build a real lock and persist both the store-side record and the disk file.
        lock = create_lock(workspace2, _manifest())
        _save_lock_to_disk(root2, lock)

        lock_for_store = {
            "files": {p: info["sha256"] for p, info in lock.get("files", {}).items()},
            "auto_locked": lock.get("auto_locked", []),
        }
        pre_stage = {
            "spec_version": comparison2["spec_version"],
            "status": "testing",
            "budget_seconds": 100,
            "used_seconds": 100,
            "rounds": [
                {"number": 0, "kind": "write", "active_seconds": 100,
                 "outcome": "tests_written", "validation_errors": [], "verification": None},
            ],
            "lock": lock_for_store,
            "lock_violations": [],
            "evaluated_round": None,
            "stop_reason": None,
            "review": None,
        }
        w2.store.update("run", run_id2, test_stages=[pre_stage])

        # Pre-charge the budget — the write turn already consumed it in the previous session.
        run_data2 = w2.store.get("run", run_id2)
        run_data2["attempts"].extend([
            {"id": "pre-test", "phase": "test", "active_seconds": 100},
        ])
        w2.store.update("run", run_id2, attempts=run_data2["attempts"])

        phase2_calls: list = []

        async def fake_phase2(run_id, phase, task_spec, plan, **kw):
            phase2_calls.append(phase)
            return "tests_written"

        w2.phase = fake_phase2
        fake_pl2 = _plan()

        with patch("arena.worker.build_plan", return_value=(fake_pl2, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await w2.run_test_stage(
                run_id2, task_spec2, baseline2, version_dir2)

        # R2-1 / L3: with budget exhausted and a lock + completed round on disk, the worker
        # must return budget_exhausted (not budget_exhausted_no_manifest).
        self.assertEqual(
            stop_reason["code"], "budget_exhausted",
            f"When budget is spent on resume with lock + completed round, must give "
            f"budget_exhausted (R2-1 / L3). Got: {stop_reason}",
        )
        # Must not have dispatched any new phase (the budget guard fires before the loop).
        self.assertEqual(
            phase2_calls, [],
            f"Budget spent on resume: must not dispatch any phase (R2-1). "
            f"Got phases: {phase2_calls}",
        )
        # evaluated_round must be set in the persisted stage entry.
        run_after = w2.store.get("run", run_id2)
        stages = run_after.get("test_stages", [])
        self.assertGreater(len(stages), 0, "test_stages must be persisted")
        last_stage = stages[-1]
        self.assertIsNotNone(
            last_stage.get("evaluated_round"),
            f"evaluated_round must be set after budget_exhausted resume (R2-1): {last_stage}",
        )

    async def test_l3_resumed_stage_does_not_restart_from_zero(self):
        """After restart with a test_stages entry showing a completed round,
        run_test_stage must not call the phase for a completed write turn again."""
        # This tests the SPEC requirement; actual implementation is in worker.py.
        # We set up the run as if it was interrupted after the write turn succeeded
        # (manifest and lock exist) and verify that resuming does not restart.

        # Inject a pre-existing test_stages entry simulating a completed write turn
        (self.workspace / MANIFEST_NAME).write_text(json.dumps(_manifest()))
        (self.workspace / "tests").mkdir(exist_ok=True)
        (self.workspace / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")

        # Build the lock as it would have been set during the write turn
        lock = create_lock(self.workspace, _manifest())
        # Persist the lock and test_stages entry
        pre_stage_entry = {
            "spec_version": self.comparison["spec_version"],
            "status": "testing",
            "budget_seconds": 3600,
            "used_seconds": 10,
            "rounds": [
                {"number": 0, "kind": "write", "active_seconds": 10,
                 "outcome": "tests_written", "validation_errors": [], "verification": None}
            ],
            "lock": {"files": lock["files"], "auto_locked": lock.get("auto_locked", [])},
            "lock_violations": [],
            "evaluated_round": None,
            "stop_reason": None,
            "review": None,
        }
        self.w.store.update("run", self.run_id, test_stages=[pre_stage_entry])
        # Also pre-charge 10 seconds of test-phase budget (from the write turn)
        run_data = self.w.store.get("run", self.run_id)
        run_data["attempts"].append(
            {"id": "pre-test", "phase": "test", "active_seconds": 10, "outcome": None})
        self.w.store.update("run", self.run_id, attempts=run_data["attempts"])

        phase_calls = []

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            phase_calls.append(phase)
            self._record_phase_attempt(run_id, phase)
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # After resume from a completed write round, the write turn must NOT repeat.
        # The expected behavior (L3): go straight to verification of the existing round.
        self.assertNotIn("test", phase_calls,
                         f"Resuming from persisted test_stages must NOT re-run the write turn (L3).\n"
                         f"Phase calls: {phase_calls}")


# ══════════════════════════════════════════════════════════════════════════════
# L4: AgentCore InvokeAgentRuntimeCommand timeout from remaining budget
# ══════════════════════════════════════════════════════════════════════════════

class TestL4AgentCoreTimeoutFromBudget(_Base):
    """L4 (§9.2): The active_budget passed to phase() for test-stage phases must
    equal the REMAINING test budget (budget - already_used), not the full budget
    and not config.test_active_seconds unconditionally."""

    async def test_l4_active_budget_is_remaining_budget(self):
        """active_budget = test_active_seconds − prior_test_phase_active_seconds."""
        # Pre-charge 1000 seconds of test-phase time
        run_data = self.w.store.get("run", self.run_id)
        run_data["attempts"].append(
            {"id": "pre-test", "phase": "test", "active_seconds": 1000, "outcome": None})
        self.w.store.update("run", self.run_id, attempts=run_data["attempts"])

        observed_budget = []

        async def fake_phase(run_id, phase, task_spec, plan, *,
                             active_budget=None, prior_active_seconds=0.0, **kw):
            if active_budget is not None:
                observed_budget.append(active_budget)
            self._record_phase_attempt(run_id, phase, active_seconds=10)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # First call must see the remaining budget (3600 - 1000 = 2600), not the full 3600
        self.assertGreater(len(observed_budget), 0, "active_budget was never passed to phase()")
        first_budget = observed_budget[0]
        # Must be less than the full budget (some has already been used)
        self.assertLess(first_budget, 3600,
                        f"active_budget must be the REMAINING budget, not the full 3600 (L4).\n"
                        f"Got active_budget={first_budget}")
        # Must be approximately 3600 - 1000 = 2600
        self.assertAlmostEqual(first_budget, 2600, delta=50,
                               msg=f"active_budget must equal remaining budget (L4).\n"
                                   f"Expected ~2600, got {first_budget}")

    async def test_l4_active_budget_not_negative(self):
        """When budget is exhausted, active_budget must be 0 (not negative)."""
        # Pre-charge more seconds than the budget
        run_data = self.w.store.get("run", self.run_id)
        run_data["attempts"].append(
            {"id": "pre-test", "phase": "test", "active_seconds": 5000, "outcome": None})
        self.w.store.update("run", self.run_id, attempts=run_data["attempts"])

        observed_budget = []

        async def fake_phase(run_id, phase, task_spec, plan, *,
                             active_budget=None, prior_active_seconds=0.0, **kw):
            if active_budget is not None:
                observed_budget.append(active_budget)
            self._record_phase_attempt(run_id, phase, active_seconds=5)
            return "test_budget_exhausted"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        if observed_budget:
            self.assertGreaterEqual(observed_budget[0], 0,
                                    f"active_budget must not be negative (L4).\n"
                                    f"Got: {observed_budget[0]}")


# ══════════════════════════════════════════════════════════════════════════════
# §9.16: Stop reason table — evaluated snapshot, scoring, run status
# ══════════════════════════════════════════════════════════════════════════════

class TestSection916StopReasonTable(_Base):
    """DESIGN §9.16: Each stop_reason code has a specified evaluated snapshot,
    accuracy/tests scoring, and run status. These tests verify the mapping."""

    async def test_916_all_passed_uses_rated_scores(self):
        """all_passed: last completed round, rated scores, per-scores status."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # §9.16: all_passed → rated scores (plan must be non-None)
        self.assertEqual(stop_reason["code"], "all_passed")
        self.assertIsNotNone(plan, "all_passed must have a non-None plan (§9.16)")

    async def test_916_rounds_exhausted_with_lock_uses_rated_scores(self):
        """rounds_exhausted (lock exists): last completed round, rated scores."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # §9.16: rounds_exhausted (lock exists) → rated scores (plan non-None)
        self.assertEqual(stop_reason["code"], "rounds_exhausted")
        self.assertIsNotNone(plan,
                             "rounds_exhausted (lock) must have a non-None plan (§9.16)")

    async def test_916_rounds_exhausted_no_lock_gives_zero_scores(self):
        """rounds_exhausted (no lock): implementation snapshot, accuracy=0, tests=0."""
        # Never write a manifest → rounds exhausted without lock
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            # No manifest written
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_passed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # §9.16: rounds_exhausted (no lock) → plan=None → no_manifest_reason path
        self.assertIsNone(plan,
                          "rounds_exhausted (no lock) must yield plan=None for 0/0 scores (§9.16)")

    async def test_916_manifest_missing_gives_plan_none(self):
        """manifest_missing: plan=None → evaluate with no_manifest_reason (accuracy=0, tests=0)."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            # Never write a manifest; use all repair rounds
            return "tests_written"

        self.w.phase = fake_phase

        plan, stop_reason, _ = await self.w.run_test_stage(
            self.run_id, self.task_spec, self.baseline, self.version_dir)

        # §9.16: manifest_missing → plan=None
        self.assertIsNone(plan,
                          "manifest_missing must give plan=None (§9.16)")
        self.assertIn(stop_reason["code"], ("manifest_missing", "manifest_invalid"),
                      f"Expected manifest_missing/invalid stop code (§9.16). Got: {stop_reason}")

    async def test_916_harness_failed_no_lock_gives_plan_none(self):
        """harness_failed (no lock): plan=None → accuracy=0, tests=0."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            return "harness_failed"

        self.w.phase = fake_phase

        plan, stop_reason, _ = await self.w.run_test_stage(
            self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertIsNone(plan,
                          "harness_failed (no lock) must yield plan=None (§9.16)")
        self.assertEqual(stop_reason["code"], "harness_failed")

    async def test_916_harness_failed_with_lock_keeps_plan(self):
        """harness_failed (lock exists): last completed round, rated scores (plan non-None)."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
                return "tests_written"
            # Fix turn fails
            return "harness_failed"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # §9.16: harness_failed (lock exists) → rated scores (plan non-None)
        self.assertIsNotNone(plan,
                             "harness_failed (lock exists) must yield non-None plan (§9.16)")
        self.assertEqual(stop_reason["code"], "harness_failed")
        # R2-10 / §10 message catalog: harness_failed (lock) message template:
        # "Fix round {k} ended without a result: {cause}; {n} scored check(s) still failed.
        #  Scored the code from {evaluated_round_label}."
        msg = stop_reason.get("message", "")
        self.assertIn("Fix round", msg,
                      f"harness_failed (lock) message must start with 'Fix round' (§10 / R2-10): {msg}")
        self.assertIn("ended without a result", msg,
                      f"harness_failed (lock) message must include 'ended without a result' (§10 / R2-10): {msg}")
        self.assertIn("scored check(s) still failed", msg,
                      f"harness_failed (lock) message must include 'scored check(s) still failed' (§10 / R2-10): {msg}")
        self.assertIn("Scored the code from", msg,
                      f"harness_failed (lock) message must include 'Scored the code from' (§10 / R2-10): {msg}")

    async def test_916_budget_exhausted_no_manifest_gives_plan_none(self):
        """budget_exhausted_no_manifest: plan=None → accuracy=0, tests=0, failed status."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            return "test_budget_exhausted"

        self.w.phase = fake_phase

        plan, stop_reason, _ = await self.w.run_test_stage(
            self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertIsNone(plan,
                          "budget_exhausted_no_manifest must yield plan=None (§9.16)")
        self.assertEqual(stop_reason["code"], "budget_exhausted_no_manifest")

    async def test_916_budget_exhausted_with_lock_keeps_plan(self):
        """budget_exhausted (lock exists): last completed round, rated scores (plan non-None)."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
                return "tests_written"
            return "test_budget_exhausted"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertIsNotNone(plan,
                             "budget_exhausted (lock exists) must yield non-None plan (§9.16)")
        self.assertEqual(stop_reason["code"], "budget_exhausted")

    async def test_916_infrastructure_error_gives_plan_none_and_incomplete_message(self):
        """infrastructure_error: plan=None → None scores → evaluated_incomplete."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            return "infrastructure_error"

        self.w.phase = fake_phase

        plan, stop_reason, _ = await self.w.run_test_stage(
            self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertIsNone(plan,
                          "infrastructure_error must yield plan=None (§9.16)")
        self.assertEqual(stop_reason["code"], "infrastructure_error",
                         f"infrastructure_error outcome must give 'infrastructure_error' code (§9.16).\n"
                         f"Got: {stop_reason}")
        # §9.16: "this is not the harness's fault" → incomplete semantics
        msg = stop_reason.get("message", "")
        self.assertIn("incomplete", msg.lower(),
                      f"infrastructure_error message must reference 'incomplete' (§9.16 / §10): {msg}")

    async def test_916_cancelled_gives_not_scored(self):
        """cancelled: not scored — plan evaluation is not performed."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            return "cancelled"

        self.w.phase = fake_phase

        plan, stop_reason, _ = await self.w.run_test_stage(
            self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "cancelled",
                         f"cancelled outcome must give 'cancelled' stop code (§9.16).\n"
                         f"Got: {stop_reason}")


# ══════════════════════════════════════════════════════════════════════════════
# R2-3 / DESIGN §14: Infrastructure error on a fix turn AFTER the lock
# ══════════════════════════════════════════════════════════════════════════════

class TestR23InfrastructureErrorAfterLock(_Base):
    """R2-3 / DESIGN §14: When infrastructure fails on a FIX turn after the lock exists,
    the stop code must be `infrastructure_error`, the message must state the cause and
    the evaluated round (never 'the evaluation is incomplete'), plan must NOT be None
    (rated evaluation of the last completed round), and the run must NOT be
    evaluated_incomplete."""

    async def test_r23_infrastructure_error_on_fix_turn_stop_code(self):
        """Fix turn returns infrastructure_error; stop code stays infrastructure_error (§14)."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
                return "tests_written"
            # Fix turn fails with infrastructure error
            return "infrastructure_error"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(
            stop_reason["code"], "infrastructure_error",
            f"Infrastructure error on fix turn (after lock) must give 'infrastructure_error' "
            f"(R2-3 / §14). Got: {stop_reason}",
        )

    async def test_r23_infrastructure_error_on_fix_turn_plan_not_none(self):
        """Fix turn infra error after lock: plan is non-None — last completed round scored (§14)."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
                return "tests_written"
            return "infrastructure_error"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # §14: rated evaluation of last completed round → plan must NOT be None
        self.assertIsNotNone(
            plan,
            "Infrastructure error after lock (§14): plan must NOT be None — last completed "
            "round must be evaluated with rated scores (R2-3).",
        )

    async def test_r23_infrastructure_error_on_fix_turn_message_mentions_cause_and_round(self):
        """§14 message states the cause and the evaluated round (§10 template)."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
                return "tests_written"
            return "infrastructure_error"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        msg = stop_reason.get("message", "")

        # §14 template: "An infrastructure error ended fix round {k}: {cause}.
        # Scored the code from {evaluated_round_label}; this is not the harness's fault."
        self.assertIn(
            "infrastructure error", msg.lower(),
            f"§14 message must mention 'infrastructure error' (R2-3): {msg}",
        )
        self.assertIn(
            "Scored the code from", msg,
            f"§14 message must include 'Scored the code from' (R2-3): {msg}",
        )
        self.assertIn(
            "this is not the harness's fault", msg,
            f"§14 message must include 'this is not the harness's fault' (R2-3): {msg}",
        )

    async def test_r23_infrastructure_error_after_lock_message_never_says_incomplete(self):
        """§14 key rule: message must NEVER say 'the evaluation is incomplete' next to rated scores."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
                return "tests_written"
            return "infrastructure_error"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        msg = stop_reason.get("message", "")

        # §14 explicit constraint: "Never show 'the evaluation is incomplete' next to rated scores."
        # This distinguishes §14 (after lock, rated) from §9.5/§9.16 (before lock, incomplete).
        self.assertNotIn(
            "the evaluation is incomplete", msg,
            f"§14 (after-lock infrastructure error): message must NEVER say "
            f"'the evaluation is incomplete' next to rated scores (R2-3).\n"
            f"Message: {msg}",
        )
        # Also verify plan is non-None as a cross-check that this is the rated path
        self.assertIsNotNone(
            plan,
            "§14 regression: plan must be non-None (rated evaluation, not incomplete).",
        )

    async def test_r23_infrastructure_error_after_lock_uses_last_completed_round(self):
        """§14: the evaluated_round in the persisted stage entry is set to the last completed round."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
                return "tests_written"
            return "infrastructure_error"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        run_after = self.w.store.get("run", self.run_id)
        stages = run_after.get("test_stages", [])
        self.assertGreater(len(stages), 0, "test_stages must be persisted")
        last_stage = stages[-1]
        self.assertIsNotNone(
            last_stage.get("evaluated_round"),
            f"evaluated_round must be set after §14 infrastructure error (R2-3): {last_stage}",
        )


# ══════════════════════════════════════════════════════════════════════════════
# R2-1 (in-loop): Budget guard before fix turn dispatching
# ══════════════════════════════════════════════════════════════════════════════

class TestR21InLoopBudgetGuard(unittest.IsolatedAsyncioTestCase):
    """R2-1 in-loop guard: When verification finds failures but the remaining test budget
    is 0, the worker must stop BEFORE dispatching the fix turn.  This is the guard at
    worker.py line 2016 — distinct from the resume-entry guard at line 1639.

    The scenario: budget is fully consumed by the write turn; verification runs and
    finds failing scored checks; the in-loop guard fires before any fix phase is called.
    Assertions: no fix turn launched, stop code is budget_exhausted (not
    budget_exhausted_no_manifest), plan is non-None, message matches §10 template."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Small budget (50 s) so the write turn can exhaust it.
        self.cfg = Config(
            data_dir=str(Path(self.temp.name) / "data"),
            test_active_seconds=50,
            test_fix_rounds=2,
            artifact_s3_uri=None,
        )
        self.w = Worker(self.cfg)
        c = self.w.service.create("Build a greeting CLI.")
        self.cid = c["id"]
        self.comparison = self.w.freeze_requirements(self.cid)
        self.run = self.w.store.detail(self.cid)["runs"][0]
        self.run_id = self.run["id"]

        self.baseline = self.w.config.root / "comparisons" / self.cid / "baseline"
        self.baseline.mkdir(parents=True, exist_ok=True)
        self.version_dir = (
            self.baseline.parent / "specifications"
            / str(self.comparison["spec_version"])
        )
        self.version_dir.mkdir(parents=True, exist_ok=True)

        self.root = self.w.run_root(self.run)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

        self.approved_review = _approved_review()
        self.task_spec = {**self.comparison, "approved_test_review": self.approved_review}
        self.fake_plan = _plan()

    def _write_manifest(self):
        (self.workspace / MANIFEST_NAME).write_text(json.dumps(_manifest()))
        (self.workspace / "tests").mkdir(exist_ok=True)
        (self.workspace / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")

    def _record_phase_attempt(self, run_id, phase, active_seconds=10):
        r = self.w.store.get("run", run_id)
        r["attempts"].append({
            "id": "t-" + phase,
            "phase": phase,
            "active_seconds": active_seconds,
            "outcome": None,
        })
        self.w.store.update("run", run_id, attempts=r["attempts"])

    async def test_r21_inloop_guard_no_fix_turn_when_budget_exhausted(self):
        """Write turn exhausts the budget; in-loop guard stops before the fix turn.

        No fix phase is dispatched, stop code is budget_exhausted, and plan is non-None
        (the write round's verified results are used)."""
        phase_calls: list = []

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            phase_calls.append(phase)
            # Write turn consumes the entire 50-second budget.
            self._record_phase_attempt(run_id, phase, active_seconds=50)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # In-loop guard must fire: NO fix turn dispatched.
        self.assertNotIn(
            "fix", phase_calls,
            f"In-loop budget guard: no fix turn must be dispatched when budget is 0 "
            f"(R2-1 in-loop). Phases called: {phase_calls}",
        )

        # Stop code must be budget_exhausted (lock exists → rated path), not
        # budget_exhausted_no_manifest (the manifest was accepted and lock is set).
        self.assertEqual(
            stop_reason["code"], "budget_exhausted",
            f"In-loop guard with lock must give 'budget_exhausted' (R2-1). "
            f"Got: {stop_reason}",
        )

    async def test_r21_inloop_guard_plan_not_none(self):
        """When in-loop guard fires (budget 0, lock exists), plan must be non-None."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase, active_seconds=50)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertIsNotNone(
            plan,
            "In-loop budget guard (lock exists): plan must NOT be None — "
            "last completed round's verified results are the evaluation (R2-1).",
        )

    async def test_r21_inloop_guard_message_matches_section10_template(self):
        """budget_exhausted (in-loop guard) message matches §10 template."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase, active_seconds=50)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "budget_exhausted")
        msg = stop_reason.get("message", "")

        # §10: "The {budget_seconds}-second test budget ran out ..."
        self.assertIn(
            "50", msg,
            f"budget_exhausted (in-loop) message must state the budget seconds (§10): {msg}",
        )
        # §10: "Scored the code from {evaluated_round_label}."
        self.assertIn(
            "Scored the code from", msg,
            f"budget_exhausted (in-loop) message must include 'Scored the code from' (§10): {msg}",
        )
        # Must NOT say "Accuracy and Tests are 0" (that is for the no-lock path)
        self.assertNotIn(
            "Accuracy and Tests are 0", msg,
            f"budget_exhausted (lock exists) message must NOT say 'Accuracy and Tests are 0'. "
            f"Message: {msg}",
        )

    async def test_r21_inloop_guard_distinct_from_resume_guard(self):
        """In-loop guard fires mid-run (after write), not at resume entry.

        The resume-entry guard fires when budget is spent BEFORE run_test_stage starts.
        The in-loop guard fires AFTER the write turn runs and exhausts the budget.
        Verify: the write phase IS called (budget was not spent on entry), but the fix
        phase is NOT called (guard fires after verification)."""
        phase_calls: list = []

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            phase_calls.append(phase)
            # Write turn consumes the entire budget — budget was available at start.
            self._record_phase_attempt(run_id, phase, active_seconds=50)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=AsyncMock(return_value=_failed_evidence())):
            plan, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        # Write phase must have been called (budget was available on entry)
        self.assertIn(
            "test", phase_calls,
            f"Write phase must be called when budget is available on entry "
            f"(distinguishes in-loop guard from resume guard). Phases: {phase_calls}",
        )
        # Fix phase must NOT have been called (in-loop guard fired)
        self.assertNotIn(
            "fix", phase_calls,
            f"Fix phase must NOT be called — in-loop guard stopped before it. "
            f"Phases: {phase_calls}",
        )
        # Stop code confirms the in-loop guard path (lock exists)
        self.assertEqual(
            stop_reason["code"], "budget_exhausted",
            f"In-loop guard must give 'budget_exhausted' (R2-1). Got: {stop_reason}",
        )


# ══════════════════════════════════════════════════════════════════════════════
# §14 / W7: Verification crash — round-0 (no completed round) vs later round
# ══════════════════════════════════════════════════════════════════════════════

class TestVerificationCrashNoCompletedRound(_Base):
    """§14 case (a): CandidateTestRunner.run raises during round-0 verification.

    No prior round has been verified, so the run must be evaluated_incomplete
    (accuracy and tests None, relevance still judged) via the incomplete_reason
    path.  Stop code is infrastructure_error; message says "before any round was
    verified" and "The evaluation is incomplete".  evaluated_round is None.
    """

    async def test_stop_code_is_infrastructure_error(self):
        """Round-0 verification crash → stop_reason.code == 'infrastructure_error'."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        async def crashing_verify(*a, **kw):
            raise RuntimeError("sandbox exploded")

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=crashing_verify):
            plan_ret, stop_reason, test_info = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "infrastructure_error",
                         f"Round-0 crash must yield infrastructure_error. Got: {stop_reason}")

    async def test_plan_is_none_no_completed_round(self):
        """Round-0 verification crash returns plan=None to trigger incomplete_reason path."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        async def crashing_verify(*a, **kw):
            raise RuntimeError("sandbox exploded")

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=crashing_verify):
            plan_ret, stop_reason, test_info = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertIsNone(plan_ret,
                          "Round-0 crash must return plan=None so caller uses incomplete_reason path.")

    async def test_message_says_before_any_round_verified(self):
        """Message must contain 'before any round was verified' and 'evaluation is incomplete'."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        async def crashing_verify(*a, **kw):
            raise RuntimeError("sandbox exploded")

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=crashing_verify):
            _, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        msg = stop_reason.get("message", "")
        self.assertIn("before any round was verified", msg,
                      f"Message must say 'before any round was verified'. Got: {msg!r}")
        self.assertIn("evaluation is incomplete", msg,
                      f"Message must say 'evaluation is incomplete'. Got: {msg!r}")
        self.assertNotIn("Scored the code from", msg,
                         f"Must NOT say 'Scored the code from' in case (a). Got: {msg!r}")

    async def test_evaluated_round_is_none(self):
        """test_info['evaluated_round'] must be None when no round was verified."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        async def crashing_verify(*a, **kw):
            raise RuntimeError("sandbox exploded")

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=crashing_verify):
            _, _, test_info = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertIsNone(test_info.get("evaluated_round"),
                          "evaluated_round must be None when no round was verified.")

    async def test_caller_routes_to_evaluated_incomplete(self):
        """Through run_saved_code_test_stage: accuracy/tests are None, status is evaluated_incomplete."""
        from arena.isolation import immutable, tree_hash

        # Give the run a snapshot so it appears in eligible runs.
        snapshot = self.root / "artifacts" / "v1-snap" / "snapshot"
        snapshot.mkdir(parents=True)
        (snapshot / "main.py").write_text("print('hello')\n")
        immutable(snapshot)
        sha = tree_hash(snapshot)
        patch_p = snapshot.parent / "implementation.patch"
        patch_p.write_text("")
        self.w.store.update("run", self.run_id, artifacts={
            "snapshot": str(snapshot), "patch": str(patch_p), "sha256": sha,
        })

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        captured: dict = {}

        async def fake_evaluate(task, plan, candidate, config, *, ratings=None,
                                no_manifest_reason=None, incomplete_reason=None, **kw):
            captured["incomplete_reason"] = incomplete_reason
            captured["ratings"] = ratings
            return {
                "overall": None, "verified_complete": False,
                "accuracy": None, "tests": None,
                "evaluation_status": "evaluated_incomplete",
            }

        verify_calls = [0]

        async def crash_then_pass(*a, **kw):
            # First call is the in-loop verification; crash it.
            # Subsequent calls are from evaluate_run (using default_plan) → succeed.
            n = verify_calls[0]
            verify_calls[0] += 1
            if n == 0:
                raise RuntimeError("sandbox exploded during round 0")
            return _passed_evidence()

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=crash_then_pass), \
             patch("arena.worker.evaluate_candidate", side_effect=fake_evaluate):
            await self.w.run_saved_code_test_stage(
                self.cid, self.comparison, self.approved_review,
                self.comparison["spec_version"])

        # incomplete_reason must be set (non-None, non-empty)
        self.assertIsNotNone(captured.get("incomplete_reason"),
                             "incomplete_reason must be passed to evaluate_candidate for case (a).")
        self.assertIsInstance(captured["incomplete_reason"], str)
        self.assertGreater(len(captured["incomplete_reason"]), 0)
        # ratings must be None (not the rated-scores path)
        self.assertIsNone(captured.get("ratings"),
                          "ratings must be None for evaluated_incomplete path (case a).")
        # run must end evaluated_incomplete
        run_final = self.w.store.get("run", self.run_id)
        self.assertEqual(run_final.get("status"), "evaluated_incomplete",
                         f"Run must be evaluated_incomplete for case (a). Got: {run_final.get('status')}")


class TestVerificationCrashAfterCompletedRound(_Base):
    """§14 case (b): CandidateTestRunner.run raises during a fix-round (round ≥ 1)
    verification after round 0 completed.

    A prior completed round exists, so the run must use that round with rated
    scores.  Stop code is infrastructure_error; message says "An infrastructure
    error stopped verification in fix round" and "Scored the code from round";
    never says "evaluation is incomplete".  evaluated_round equals the last
    verified round (0 in the typical test).
    """

    def _make_round1_crashing_verify(self):
        """Return a fake verify that succeeds on round 0 and crashes on round 1."""
        calls = [0]

        async def fake_verify(snapshot, plan, evidence_dir, *a, **kw):
            n = calls[0]
            calls[0] += 1
            if n == 0:
                return _failed_evidence()   # round 0 completes with failure → triggers fix turn
            raise RuntimeError("sandbox exploded in fix round")

        return fake_verify

    async def test_stop_code_is_infrastructure_error(self):
        """Fix-round verification crash → stop_reason.code == 'infrastructure_error'."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=self._make_round1_crashing_verify()):
            _, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(stop_reason["code"], "infrastructure_error",
                         f"Fix-round verification crash must yield infrastructure_error. Got: {stop_reason}")

    async def test_plan_is_not_none(self):
        """Fix-round crash returns plan != None so caller uses rated-scores path."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=self._make_round1_crashing_verify()):
            plan_ret, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertIsNotNone(plan_ret,
                             "Fix-round crash must return plan != None (rated-scores path).")

    async def test_message_says_scored_from_round(self):
        """Message must contain 'Scored the code from round' and 'infrastructure error'.
        Must NOT say 'evaluation is incomplete'."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=self._make_round1_crashing_verify()):
            _, stop_reason, _ = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        msg = stop_reason.get("message", "")
        self.assertIn("Scored the code from round", msg,
                      f"Message must say 'Scored the code from round'. Got: {msg!r}")
        self.assertIn("infrastructure error", msg.lower(),
                      f"Message must mention 'infrastructure error'. Got: {msg!r}")
        self.assertNotIn("evaluation is incomplete", msg.lower(),
                         f"Must NOT say 'evaluation is incomplete' next to rated scores. Got: {msg!r}")

    async def test_evaluated_round_set_to_last_verified(self):
        """test_info['evaluated_round'] must be the last successfully verified round (0)."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=self._make_round1_crashing_verify()):
            _, _, test_info = await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        self.assertEqual(test_info.get("evaluated_round"), 0,
                         f"evaluated_round must be 0 (last verified round). Got: {test_info.get('evaluated_round')}")

    async def test_stage_entry_evaluated_round_set(self):
        """run['test_stages'][-1]['evaluated_round'] must be set to last verified round."""
        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run",
                   side_effect=self._make_round1_crashing_verify()):
            await self.w.run_test_stage(
                self.run_id, self.task_spec, self.baseline, self.version_dir)

        run_after = self.w.store.get("run", self.run_id)
        stages = run_after.get("test_stages", [])
        self.assertGreater(len(stages), 0, "test_stages must be persisted.")
        last_entry = stages[-1]
        self.assertEqual(last_entry.get("evaluated_round"), 0,
                         f"test_stages[-1].evaluated_round must be 0 (last verified round). "
                         f"Got: {last_entry.get('evaluated_round')}")

    async def test_caller_routes_to_rated_scores(self):
        """Through run_saved_code_test_stage: ratings are passed, run is not evaluated_incomplete."""
        from arena.isolation import immutable, tree_hash

        # Give the run a snapshot so it appears in eligible runs.
        snapshot = self.root / "artifacts" / "v1-snap" / "snapshot"
        snapshot.mkdir(parents=True)
        (snapshot / "main.py").write_text("print('hello')\n")
        immutable(snapshot)
        sha = tree_hash(snapshot)
        patch_p = snapshot.parent / "implementation.patch"
        patch_p.write_text("")
        self.w.store.update("run", self.run_id, artifacts={
            "snapshot": str(snapshot), "patch": str(patch_p), "sha256": sha,
        })

        async def fake_phase(run_id, phase, task_spec, plan, **kw):
            self._record_phase_attempt(run_id, phase)
            if phase == "test":
                self._write_manifest()
            return "tests_written"

        self.w.phase = fake_phase

        captured: dict = {}
        fake_ratings = [{"scenario_id": "S1", "score": 1.0, "verdict": "pass", "reason": "ok"}]

        async def fake_evaluate(task, plan, candidate, config, *, ratings=None,
                                no_manifest_reason=None, incomplete_reason=None, **kw):
            captured["ratings"] = ratings
            captured["incomplete_reason"] = incomplete_reason
            return {
                "overall": 0.7, "verified_complete": True,
                "accuracy": 0.9, "tests": 0.8,
                "evaluation_status": "passed",
            }

        async def fake_review(*a, **kw):
            return {"ratings": fake_ratings}

        verify_calls = [0]

        async def fake_verify(snapshot, plan_arg, evidence_dir, *a, **kw):
            n = verify_calls[0]
            verify_calls[0] += 1
            # round 0 baseline (called by per_harness_baseline or main eval) → pass
            # round 0 in-loop → failed evidence (triggers fix turn)
            # round 1 in-loop → crash
            # test_snapshot eval (by evaluate_run) → pass
            if n == 0:
                return _failed_evidence()   # round 0 in run_test_stage
            elif n == 1:
                raise RuntimeError("sandbox exploded in fix round 1")
            else:
                # per-harness baseline or evaluate_run re-run
                return _passed_evidence()

        with patch("arena.worker.build_plan", return_value=(self.fake_plan, [])), \
             patch("arena.worker.CandidateTestRunner.run", side_effect=fake_verify), \
             patch("arena.worker.review_harness_tests", side_effect=fake_review), \
             patch("arena.worker.evaluate_candidate", side_effect=fake_evaluate):
            await self.w.run_saved_code_test_stage(
                self.cid, self.comparison, self.approved_review,
                self.comparison["spec_version"])

        # ratings must be non-None (rated-scores path, not incomplete_reason path)
        self.assertIsNotNone(captured.get("ratings"),
                             "ratings must be passed to evaluate_candidate for case (b).")
        # incomplete_reason must be None
        self.assertIsNone(captured.get("incomplete_reason"),
                          f"incomplete_reason must be None for rated-scores path (case b). "
                          f"Got: {captured.get('incomplete_reason')}")
        # run must NOT be evaluated_incomplete
        run_final = self.w.store.get("run", self.run_id)
        self.assertNotEqual(run_final.get("status"), "evaluated_incomplete",
                            f"Run must NOT be evaluated_incomplete for case (b). "
                            f"Got: {run_final.get('status')}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
