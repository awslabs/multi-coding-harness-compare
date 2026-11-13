# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for model_mismatch() and the worker's mismatch-detection / planner-config paths."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch, call

from arena.model_match import model_mismatch
from arena.config import Config
from arena.worker import Worker


# ── model_mismatch() truth table ────────────────────────────────────────────

class TestModelMismatch(unittest.TestCase):

    def test_both_none_returns_false(self):
        self.assertFalse(model_mismatch(None, None))

    def test_requested_none_returns_false(self):
        self.assertFalse(model_mismatch(None, "us.anthropic.claude-sonnet-4-6"))

    def test_reported_none_returns_false(self):
        self.assertFalse(model_mismatch("us.anthropic.claude-sonnet-4-6", None))

    def test_empty_string_returns_false(self):
        self.assertFalse(model_mismatch("", "us.anthropic.claude-sonnet-4-6"))
        self.assertFalse(model_mismatch("us.anthropic.claude-sonnet-4-6", ""))

    def test_equal_ids_return_false(self):
        m = "us.anthropic.claude-sonnet-4-6"
        self.assertFalse(model_mismatch(m, m))

    def test_1m_suffix_stripped_on_requested(self):
        # [1m] is a terminal colour-code suffix some CLIs append to model names.
        self.assertFalse(model_mismatch(
            "us.anthropic.claude-sonnet-4-6[1m]",
            "us.anthropic.claude-sonnet-4-6"))

    def test_1m_suffix_stripped_on_reported(self):
        self.assertFalse(model_mismatch(
            "us.anthropic.claude-sonnet-4-6",
            "us.anthropic.claude-sonnet-4-6[1m]"))

    def test_1m_stripped_on_both(self):
        self.assertFalse(model_mismatch(
            "us.anthropic.claude-sonnet-4-6[1m]",
            "us.anthropic.claude-sonnet-4-6[1m]"))

    def test_alias_without_dot_returns_false(self):
        for alias in ("sonnet", "opus", "default", "auto"):
            with self.subTest(alias=alias):
                self.assertFalse(model_mismatch(alias, "us.anthropic.claude-opus-5"))

    def test_different_full_ids_return_true(self):
        self.assertTrue(model_mismatch(
            "us.anthropic.claude-sonnet-4-6",
            "global.anthropic.claude-opus-5"))

    def test_different_region_prefix_returns_true(self):
        # us. vs global. is a real substitution that must be flagged.
        self.assertTrue(model_mismatch(
            "us.anthropic.claude-sonnet-4-6",
            "global.anthropic.claude-sonnet-4-6"))

    def test_different_model_version_returns_true(self):
        self.assertTrue(model_mismatch(
            "us.anthropic.claude-sonnet-4-5",
            "us.anthropic.claude-sonnet-4-6"))

    def test_1m_still_mismatches_different_model(self):
        self.assertTrue(model_mismatch(
            "us.anthropic.claude-sonnet-4-6[1m]",
            "global.anthropic.claude-opus-5[1m]"))


# ── Worker.receive() mismatch-detection ─────────────────────────────────────

class _FakeStream:
    """Async readline() that drains a list of pre-encoded lines, then returns b""."""
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeProcess:
    def __init__(self, stdout_lines=(), stderr_lines=()):
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)


def _enc(obj):
    return json.dumps(obj).encode() + b"\n"


def _session(session_id, model):
    return _enc({"type": "session", "data": {"session_id": session_id, "model": model}})


class TestReceiveModelMismatch(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w = Worker(Config(data_dir=str(Path(self.temp.name) / "data")))
        c = self.w.service.create("Test task")
        self.cid = c["id"]
        self.runs = self.w.store.detail(self.cid)["runs"]
        # run_root must exist for the bridge/session.json write.
        for run in self.runs:
            root = self.w.run_root(run)
            (root / "bridge").mkdir(parents=True, exist_ok=True)

    def _run_with_model(self, requested_model):
        """Return the first run after seeding requested_model."""
        run = self.runs[0]
        self.w.store.update("run", run["id"], requested_model=requested_model)
        return self.w.store.get("run", run["id"])

    async def test_matching_model_emits_no_event_and_no_field(self):
        model = "us.anthropic.claude-sonnet-4-6"
        run = self._run_with_model(model)
        process = _FakeProcess([_session("s1", model)])
        await self.w.receive(process, run)
        updated = self.w.store.get("run", run["id"])
        self.assertIsNone(updated.get("model_mismatch"))
        events = [e for e in self.w.store.events(self.cid) if e["type"] == "model_mismatch"]
        self.assertEqual(events, [])

    async def test_mismatch_stored_once_and_event_emitted(self):
        run = self._run_with_model("us.anthropic.claude-sonnet-4-6")
        wrong = "global.anthropic.claude-opus-5"
        process = _FakeProcess([_session("s1", wrong)])
        await self.w.receive(process, run)
        updated = self.w.store.get("run", run["id"])
        mm = updated.get("model_mismatch")
        self.assertIsNotNone(mm)
        self.assertEqual(mm["requested"], "us.anthropic.claude-sonnet-4-6")
        self.assertEqual(mm["reported"], wrong)
        self.assertAlmostEqual(mm["detected_at"], time.time(), delta=5)
        events = [e for e in self.w.store.events(self.cid) if e["type"] == "model_mismatch"]
        self.assertEqual(len(events), 1)
        self.assertIn(wrong, events[0]["data"]["message"])
        # Harness name capitalized in the message (e.g. "Claude", "Codex", "Kiro").
        self.assertIn(self.runs[0]["harness"].capitalize(), events[0]["data"]["message"])

    async def test_same_wrong_model_twice_emits_only_one_event(self):
        run = self._run_with_model("us.anthropic.claude-sonnet-4-6")
        wrong = "global.anthropic.claude-opus-5"
        process = _FakeProcess([_session("s1", wrong), _session("s2", wrong)])
        await self.w.receive(process, run)
        events = [e for e in self.w.store.events(self.cid) if e["type"] == "model_mismatch"]
        self.assertEqual(len(events), 1)
        updated = self.w.store.get("run", run["id"])
        # stored once
        self.assertEqual(updated["model_mismatch"]["reported"], wrong)

    async def test_two_distinct_wrong_models_emit_two_events(self):
        run = self._run_with_model("us.anthropic.claude-sonnet-4-6")
        wrong1 = "global.anthropic.claude-opus-5"
        wrong2 = "global.anthropic.claude-sonnet-4-5"
        # A resumed session reports once per turn: repeats of either model add no events.
        process = _FakeProcess([_session("s1", wrong1), _session("s2", wrong2),
                                _session("s3", wrong2), _session("s4", wrong1)])
        await self.w.receive(process, run)
        events = [e for e in self.w.store.events(self.cid) if e["type"] == "model_mismatch"]
        self.assertEqual(len(events), 2)
        reported = {e["data"]["reported"] for e in events}
        self.assertEqual(reported, {wrong1, wrong2})
        record = self.w.store.get("run", run["id"])["model_mismatch"]
        self.assertEqual((record["reported"], record["seen"]), (wrong1, [wrong1, wrong2]))

    async def test_mismatch_does_not_change_run_status(self):
        run = self._run_with_model("us.anthropic.claude-sonnet-4-6")
        status_before = self.w.store.get("run", run["id"])["status"]
        process = _FakeProcess([_session("s1", "global.anthropic.claude-opus-5")])
        await self.w.receive(process, run)
        self.assertEqual(self.w.store.get("run", run["id"])["status"], status_before)

    async def test_alias_requested_model_emits_no_mismatch_event(self):
        run = self._run_with_model("sonnet")
        process = _FakeProcess([_session("s1", "us.anthropic.claude-sonnet-4-6")])
        await self.w.receive(process, run)
        events = [e for e in self.w.store.events(self.cid) if e["type"] == "model_mismatch"]
        self.assertEqual(events, [])

    async def test_no_requested_model_emits_no_mismatch_event(self):
        run = self.runs[1]  # No requested_model set
        process = _FakeProcess([_session("s1", "global.anthropic.claude-opus-5")])
        await self.w.receive(process, run)
        events = [e for e in self.w.store.events(self.cid) if e["type"] == "model_mismatch"]
        self.assertEqual(events, [])


# ── Planner call sites use planning_for_comparison ──────────────────────────

def _fake_planning_for_comparison(sentinel):
    """Return a method that records calls and returns sentinel."""
    calls = []

    def pfc(self_config, comparison):
        calls.append(comparison)
        return sentinel

    return pfc, calls


class TestPlannerCallSites(unittest.IsolatedAsyncioTestCase):
    """Verify each of the three planner call sites passes planning_for_comparison()'s result.

    Config.planning_for_comparison is implemented by agent C; these tests monkeypatch it
    to verify the wiring in worker.py rather than Config's own logic.
    """

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w = Worker(Config(data_dir=str(Path(self.temp.name) / "data")))
        c = self.w.service.create("Test task")
        self.cid = c["id"]
        # freeze_requirements populates specifications[], which freeze() expects.
        self.comparison = self.w.freeze_requirements(self.cid)
        self.baseline = self.w.config.root / "comparisons" / self.cid / "baseline"
        self.baseline.mkdir(parents=True, exist_ok=True)

    def _sentinel_config(self):
        return self.w.config.model_copy(update={"planning_model": "sentinel"})

    async def test_freeze_passes_planning_for_comparison_to_prepare_plan(self):
        """freeze()'s direct prepare_plan call passes planning_for_comparison(c), not self.config.

        The direct call site (else-branch in freeze) is only reached when the comparison
        has no test_review. Pass a stripped spec to exercise that path.
        """
        sentinel = self._sentinel_config()
        pfc, calls = _fake_planning_for_comparison(sentinel)
        configs_seen = []

        async def fake_prepare(task_spec, baseline, config):
            configs_seen.append(config)
            # Cancel so freeze() exits at the 'if obsolete()' check after the except block.
            self.w.store.update("comparison", self.cid, cancel_requested=True)
            raise RuntimeError("stop after capture")

        # Remove test_review so freeze() takes the else-branch with direct prepare_plan.
        spec_without_review = {k: v for k, v in self.comparison.items() if k != "test_review"}

        with patch.object(Config, "planning_for_comparison", pfc, create=True), \
             patch("arena.worker.prepare_plan", fake_prepare):
            await self.w.freeze(self.cid, self.baseline, spec_without_review)

        self.assertTrue(calls, "planning_for_comparison was never called")
        self.assertTrue(configs_seen, "prepare_plan was never called")
        self.assertIs(configs_seen[0], sentinel,
                      "prepare_plan received self.config, not planning_for_comparison result")

    async def test_review_tests_passes_planning_for_comparison_to_propose_tests(self):
        """review_tests() must pass self.config.planning_for_comparison(task_spec) to propose_tests."""
        sentinel = self._sentinel_config()
        pfc, pfc_calls = _fake_planning_for_comparison(sentinel)
        configs_seen = []

        async def fake_propose(task_spec, baseline, config, review):
            configs_seen.append(config)
            raise RuntimeError("stop after capture")  # bail out of propose_tests

        # Create a minimal task_spec with a queued test_review.
        from arena.test_review import requirements_digest
        task_spec = {**self.comparison}
        rev_id = "rev-test-1"
        task_spec["test_review"] = {
            "id": rev_id, "status": "queued", "revision": 1,
            "requirements_sha256": requirements_digest(task_spec),
        }
        self.w.store.update("comparison", self.cid, test_review=task_spec["test_review"])

        obsolete_after = []

        def obsolete():
            # Allow one iteration, then stop.
            obsolete_after.append(1)
            return len(obsolete_after) > 2

        with patch.object(Config, "planning_for_comparison", pfc, create=True), \
             patch("arena.worker.propose_tests", fake_propose):
            await self.w.review_tests(self.cid, self.baseline, task_spec, obsolete)

        self.assertTrue(pfc_calls, "planning_for_comparison was never called")
        self.assertTrue(configs_seen, "propose_tests was never called")
        self.assertIs(configs_seen[0], sentinel,
                      "propose_tests received self.config, not planning_for_comparison result")

    async def test_approved_plan_passes_planning_for_comparison_to_prepare_plan(self):
        """approved_plan() generates executable tests with the comparison's planning model."""
        sentinel = self._sentinel_config()
        pfc, pfc_calls = _fake_planning_for_comparison(sentinel)
        configs_seen = []

        async def fake_prepare(task_spec, baseline, config):
            configs_seen.append(config)
            raise RuntimeError("stop after capture")

        from arena.test_review import requirements_digest
        task_spec = {**self.comparison}
        task_spec["test_review"] = {
            "id": "rev-approved-1", "status": "approved", "revision": 1, "spec_version": 1,
            "proposal": {}, "answers": {}, "approved_at": 0.0, "approval_sha256": "x",
            "requirements_sha256": requirements_digest(task_spec),
            # plan_sha256 must be set to force the legacy (prepare_plan) path; without it
            # approved_plan() uses the new harness-written-tests flow and skips prepare_plan.
            "plan_sha256": "legacy-forced",
        }
        self.w.store.update("comparison", self.cid, test_review=task_spec["test_review"])
        checks = []

        def obsolete():
            checks.append(1)
            return bool(configs_seen) or len(checks) > 20

        with patch.object(Config, "planning_for_comparison", pfc, create=True), \
             patch("arena.worker.prepare_plan", fake_prepare):
            await self.w.approved_plan(self.cid, self.baseline, task_spec, obsolete)

        self.assertTrue(pfc_calls, "planning_for_comparison was never called")
        self.assertTrue(configs_seen, "prepare_plan was never called")
        self.assertIs(configs_seen[0], sentinel,
                      "prepare_plan received self.config, not planning_for_comparison result")


if __name__ == "__main__":
    unittest.main(verbosity=2)
