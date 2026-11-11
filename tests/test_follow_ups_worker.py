# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Follow-up delivery: prompt block, worker pending computation, and compare() dispatch."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from arena.config import Config
from arena.evaluation import Plan, default_plan
from arena.prompts import effective_prompt
from arena.worker import Worker


# ── Helpers shared across test classes ──────────────────────────────────────

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


def _turn_complete():
    return {"type": "turn_complete", "data": {}}


class TestFollowUpsHelper(unittest.TestCase):
    def test_reads_marked_and_legacy_follow_ups_from_the_raw_answers(self):
        from arena.prompts import follow_ups
        task = {"answers": [
            {"question_id": "q1", "question": "Which file name?", "answer": "app.py"},
            {"question_id": "q2", "question": "User specification revision", "answer": "Legacy text"},
            {"question_id": "q3", "question": "User specification revision", "answer": "New", "kind": "follow_up"}]}
        self.assertEqual(follow_ups(task), [{"id": "q2", "text": "Legacy text"}, {"id": "q3", "text": "New"}])
        self.assertEqual(follow_ups({}), [])


# ── Contract item 5, prompt half ─────────────────────────────────────────────

class TestEffectivePromptFollowUps(unittest.TestCase):

    def _spec(self, answers=None):
        return {"prompt": "Build a CLI", "answers": answers or [], "spec_version": 1}

    def test_clarify_phase_never_shows_follow_up_block(self):
        follow_ups = [{"id": "fu1", "text": "Add color output"}]
        prompt = effective_prompt(self._spec(), "clarify", follow_ups=follow_ups)
        self.assertNotIn("FOLLOW-UP INSTRUCTIONS", prompt)

    def test_develop_phase_none_follow_ups_no_block(self):
        prompt = effective_prompt(self._spec(), "develop")
        self.assertNotIn("FOLLOW-UP INSTRUCTIONS", prompt)

    def test_develop_phase_empty_follow_ups_no_block(self):
        prompt = effective_prompt(self._spec(), "develop", follow_ups=[])
        self.assertNotIn("FOLLOW-UP INSTRUCTIONS", prompt)

    def test_develop_phase_single_follow_up_inserted_after_intro(self):
        follow_ups = [{"id": "fu1", "text": "Use purple colour"}]
        prompt = effective_prompt(self._spec(), "develop", follow_ups=follow_ups)
        self.assertIn("FOLLOW-UP INSTRUCTIONS FROM THE USER", prompt)
        self.assertIn("1. Use purple colour", prompt)

    def test_multiple_follow_ups_numbered_oldest_first(self):
        follow_ups = [
            {"id": "fu1", "text": "First instruction"},
            {"id": "fu2", "text": "Second instruction"},
        ]
        prompt = effective_prompt(self._spec(), "develop", follow_ups=follow_ups)
        idx1 = prompt.index("1. First instruction")
        idx2 = prompt.index("2. Second instruction")
        self.assertLess(idx1, idx2)

    def test_follow_up_block_placed_before_spec_json(self):
        follow_ups = [{"id": "fu1", "text": "extra"}]
        prompt = effective_prompt(self._spec(), "develop", follow_ups=follow_ups)
        self.assertLess(
            prompt.index("FOLLOW-UP INSTRUCTIONS"),
            prompt.index('"original_task"'))

    def test_shared_answers_unchanged_by_follow_up(self):
        answers = [{"question_id": "q1", "question": "User specification revision",
                    "answer": "Use Python", "kind": "follow_up"}]
        spec = self._spec(answers=answers)
        follow_ups = [{"id": "fu1", "text": "Also add tests"}]
        prompt = effective_prompt(spec, "develop", follow_ups=follow_ups)
        # spec JSON is the last \n\n-delimited block before MANDATORY
        idx_start = prompt.index("\n\n{")
        idx_end = prompt.index("\n\nBEGIN MANDATORY", idx_start)
        spec_obj = json.loads(prompt[idx_start:idx_end])
        self.assertEqual(spec_obj["shared_answers"], answers)

    def test_later_follow_up_wins_wording_preserved_in_block(self):
        # The prompt includes the text verbatim; ordering advice is in the header.
        follow_ups = [{"id": "fu1", "text": "do A"},
                      {"id": "fu2", "text": "do B instead"}]
        prompt = effective_prompt(self._spec(), "develop", follow_ups=follow_ups)
        self.assertIn("later ones win where they conflict", prompt)
        self.assertIn("do A", prompt)
        self.assertIn("do B instead", prompt)


# ── Contract item 5, worker half: pending computation + delivery recording ───

class TestPhaseFollowUpDelivery(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w = Worker(Config(data_dir=str(Path(self.temp.name) / "data")))
        self.c = self.w.service.create("Test task")
        self.cid = self.c["id"]
        self.comparison = self.w.freeze_requirements(self.cid)
        self.runs = self.w.store.detail(self.cid)["runs"]
        self.plan = Plan.model_validate(self.comparison["development_plan"])
        # workspace must exist so phase() skips the copy_tree step
        for run in self.runs:
            (self.w.run_root(run) / "workspace").mkdir(parents=True, exist_ok=True)

    def _setup_runtime(self):
        """Replace runtime methods with no-op fakes that set up the bridge dir."""
        async def fake_start(run, root, phase):
            (root / "bridge" / "requests").mkdir(parents=True, exist_ok=True)
            (root / "bridge" / "answers").mkdir(parents=True, exist_ok=True)
            return {}

        self.w.runtime.start = fake_start
        self.w.runtime.launch = AsyncMock(
            return_value=_FakeProcess([_turn_complete()]))
        self.w.runtime.stop = AsyncMock()
        self.w.runtime.supervise = AsyncMock()

    def _record_follow_ups(self, follow_ups):
        """Store follow-ups the way Service.revise() does: as shared answers on the raw record."""
        answers = self.w.store.get("comparison", self.cid)["answers"] + [
            {"question_id": fu["id"], "question": "User specification revision", "answer": fu["text"],
             "origins": [], "kind": "follow_up"} for fu in follow_ups]
        self.w.store.update("comparison", self.cid, answers=answers)

    async def _run_develop(self, run, follow_ups=None, process=None):
        if follow_ups is not None:
            self._record_follow_ups(follow_ups)
        spec = {**self.comparison, **self.w.store.get("comparison", self.cid)}
        self._setup_runtime()
        if process is not None:
            self.w.runtime.launch = AsyncMock(return_value=process)
        with patch("arena.worker.initialize_checkout", AsyncMock()):
            return await self.w.phase(run["id"], "develop", spec, self.plan)

    async def test_follow_up_ids_in_attempt_record(self):
        run = self.runs[0]
        fus = [{"id": "fu-1", "text": "Add colours", "created_at": 1.0}]
        result = await self._run_develop(run, fus)
        self.assertEqual(result, "implemented")
        last = self.w.store.get("run", run["id"])["attempts"][-1]
        self.assertEqual(last["follow_ups"], ["fu-1"])

    async def test_follow_ups_delivered_once_the_turn_completes(self):
        run = self.runs[0]
        fus = [{"id": "fu-1", "text": "Do X", "created_at": 1.0},
               {"id": "fu-2", "text": "Do Y", "created_at": 2.0}]
        await self._run_develop(run, fus)
        delivered = self.w.store.get("run", run["id"]).get("follow_ups_delivered", [])
        self.assertCountEqual(delivered, ["fu-1", "fu-2"])

    async def test_follow_up_delivered_event_emitted(self):
        run = self.runs[0]
        fus = [{"id": "fu-1", "text": "Do X", "created_at": 1.0}]
        await self._run_develop(run, fus)
        events = [e for e in self.w.store.events(self.cid)
                  if e["type"] == "follow_up_delivered"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["follow_up_ids"], ["fu-1"])
        self.assertEqual(events[0]["run_id"], run["id"])

    async def test_already_delivered_ids_excluded_from_pending(self):
        run = self.runs[0]
        # Pre-mark fu-1 as delivered
        self.w.store.update("run", run["id"], follow_ups_delivered=["fu-1"])
        fus = [{"id": "fu-1", "text": "Old", "created_at": 1.0},
               {"id": "fu-2", "text": "New", "created_at": 2.0}]
        await self._run_develop(run, fus)
        delivered = self.w.store.get("run", run["id"]).get("follow_ups_delivered", [])
        # fu-1 already there; fu-2 newly added
        self.assertIn("fu-1", delivered)
        self.assertIn("fu-2", delivered)
        last = self.w.store.get("run", run["id"])["attempts"][-1]
        self.assertEqual(last["follow_ups"], ["fu-2"])
        events = [e for e in self.w.store.events(self.cid)
                  if e["type"] == "follow_up_delivered"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["follow_up_ids"], ["fu-2"])

    async def test_the_prompt_the_harness_receives_carries_the_follow_up(self):
        run = self.runs[0]
        await self._run_develop(run, [{"id": "fu-1", "text": "Print the date too"}])
        prompt = self.w.store.get("run", run["id"])["attempts"][-1]["prompt"]
        self.assertIn("FOLLOW-UP INSTRUCTIONS FROM THE USER", prompt)
        self.assertIn("Print the date too", prompt)
        turn = json.loads((self.w.run_root(run) / "bridge" / "turn.json").read_text())
        self.assertIn("Print the date too", turn["prompt"])

    async def test_a_turn_that_does_not_complete_leaves_the_follow_up_pending(self):
        run = self.runs[0]
        result = await self._run_develop(run, [{"id": "fu-1", "text": "Do X"}], process=_FakeProcess([]))
        self.assertEqual(result, "failed")
        current = self.w.store.get("run", run["id"])
        self.assertEqual(current["attempts"][-1]["follow_ups"], ["fu-1"])
        self.assertFalse(current.get("follow_ups_delivered"))
        self.assertFalse([e for e in self.w.store.events(self.cid) if e["type"] == "follow_up_delivered"])

    async def test_a_new_follow_up_restarts_a_running_turn_without_a_version_change(self):
        run = self.runs[0]

        class Running(_FakeProcess):
            returncode = None

            async def wait(self):
                self.returncode = 0
                return 0

        version = self.w.store.get("comparison", self.cid)["spec_version"]

        async def supervise(container):
            # Arrives mid-turn while the specification is unfrozen: no version bump.
            if not any(a.get("kind") == "follow_up" for a in self.w.store.get("comparison", self.cid)["answers"]):
                self._record_follow_ups([{"id": "fu-late", "text": "Late instruction"}])

        spec = {**self.comparison, **self.w.store.get("comparison", self.cid)}
        self._setup_runtime()
        self.w.runtime.launch = AsyncMock(return_value=Running())
        self.w.runtime.supervise = supervise
        with patch("arena.worker.initialize_checkout", AsyncMock()):
            result = await self.w.phase(run["id"], "develop", spec, self.plan)
        self.assertEqual(result, "revised")
        self.assertEqual(self.w.store.get("comparison", self.cid)["spec_version"], version)
        self.assertNotIn("fu-late", self.w.store.get("run", run["id"]).get("follow_ups_delivered") or [])

    async def test_no_event_when_no_pending_follow_ups(self):
        run = self.runs[0]
        # No follow_ups on comparison
        await self._run_develop(run, follow_ups=None)
        events = [e for e in self.w.store.events(self.cid)
                  if e["type"] == "follow_up_delivered"]
        self.assertEqual(events, [])
        delivered = self.w.store.get("run", run["id"]).get("follow_ups_delivered")
        self.assertFalse(delivered)

    async def test_no_delivery_for_clarify_phase(self):
        """Clarify phase must not touch follow_ups_delivered even when fus present."""
        run = self.runs[0]
        fus = [{"id": "fu-1", "text": "Do X", "created_at": 1.0}]
        self._record_follow_ups(fus)
        spec = {**self.comparison, **self.w.store.get("comparison", self.cid)}

        # A clarify-capable fake process: writes ready.json via a coroutine injected
        # into runtime.start, then emits turn_complete.
        async def fake_start_with_ready(run_rec, root, phase):
            (root / "bridge" / "requests").mkdir(parents=True, exist_ok=True)
            (root / "bridge" / "answers").mkdir(parents=True, exist_ok=True)
            return {}

        # We need ready.json to exist with the correct turn_key; we'll intercept
        # the store update that records attempts to capture turn_key, then write it.
        turn_keys_seen = []
        original_update = self.w.store.update

        def tracking_update(kind, id, **fields):
            if kind == "run" and "attempts" in fields:
                for a in fields["attempts"]:
                    if a.get("phase") == "clarify" and a["id"] not in turn_keys_seen:
                        turn_keys_seen.append(a["id"])
                        root = self.w.run_root(self.w.store.get("run", id))
                        from arena.bridge import atomic_json as aj
                        aj(root / "bridge" / "ready.json",
                           {"turn_key": a["id"], "assumptions": "test"})
            return original_update(kind, id, **fields)

        self.w.runtime.start = fake_start_with_ready
        self.w.runtime.launch = AsyncMock(
            return_value=_FakeProcess([_turn_complete()]))
        self.w.runtime.stop = AsyncMock()
        self.w.runtime.supervise = AsyncMock()

        with patch("arena.worker.initialize_checkout", AsyncMock()), \
             patch.object(self.w.store, "update", side_effect=tracking_update):
            result = await self.w.phase(run["id"], "clarify", spec, self.plan)

        # clarify must not record delivery even when follow_ups present
        events = [e for e in self.w.store.events(self.cid)
                  if e["type"] == "follow_up_delivered"]
        self.assertEqual(events, [])
        delivered = self.w.store.get("run", run["id"]).get("follow_ups_delivered")
        self.assertFalse(delivered)

    async def test_attempt_follow_ups_empty_when_no_comparison_follow_ups(self):
        run = self.runs[0]
        result = await self._run_develop(run, follow_ups=[])
        self.assertEqual(result, "implemented")
        last = self.w.store.get("run", run["id"])["attempts"][-1]
        self.assertEqual(last.get("follow_ups", []), [])

    async def test_delivery_recorded_once_not_duplicated(self):
        """Running phase twice does not double-record the same id."""
        run = self.runs[0]
        self._record_follow_ups([{"id": "fu-1", "text": "Do X"}])
        spec_v1 = {**self.comparison, **self.w.store.get("comparison", self.cid)}

        self._setup_runtime()
        with patch("arena.worker.initialize_checkout", AsyncMock()):
            await self.w.phase(run["id"], "develop", spec_v1, self.plan)

        # Second turn (same follow_up already delivered)
        self.w.runtime.launch = AsyncMock(
            return_value=_FakeProcess([_turn_complete()]))
        self.w.store.transition(run["id"], "queued")
        with patch("arena.worker.initialize_checkout", AsyncMock()):
            await self.w.phase(run["id"], "develop", spec_v1, self.plan)

        delivered = self.w.store.get("run", run["id"]).get("follow_ups_delivered", [])
        self.assertEqual(delivered.count("fu-1"), 1)
        # No second delivery event
        events = [e for e in self.w.store.events(self.cid)
                  if e["type"] == "follow_up_delivered"]
        self.assertEqual(len(events), 1)


# ── Contract item 4: compare() late-arrival block picks up "queued" runs ─────

class TestCompareQueuedLateArrival(unittest.IsolatedAsyncioTestCase):
    """A run re-queued by a follow-up while compare() is active gets picked up."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w = Worker(Config(data_dir=str(Path(self.temp.name) / "data")))
        self.c = self.w.service.create("Test late arrival")
        self.cid = self.c["id"]
        # freeze_requirements populates specifications[]; one() calls it every round
        self.w.freeze_requirements(self.cid)
        self.runs = self.w.store.detail(self.cid)["runs"]

    async def test_queued_run_revived_during_loop_is_dispatched(self):
        ab_gate = asyncio.Event()
        evaluate_calls = {}
        dispatch_log = []

        async def fake_phase(run_id, phase, spec, plan, readiness_reminder=False):
            dispatch_log.append((run_id, phase))
            if phase == "clarify":
                self.w.store.update("run", run_id, clarified=True)
                return "ready"
            # develop: A and B wait until run C's first evaluation is done
            if run_id in (self.runs[0]["id"], self.runs[1]["id"]):
                await ab_gate.wait()
            return "implemented"

        async def fake_snapshot(run_id, c):
            root = self.w.run_root(self.w.store.get("run", run_id))
            snap = root / "artifacts" / "snap"
            snap.mkdir(parents=True, exist_ok=True)
            self.w.store.update("run", run_id, artifacts={
                "snapshot": str(snap), "sha256": "x", "patch": "x"})

        async def fake_evaluate(run_id, c, plan, baseline_results):
            evaluate_calls[run_id] = evaluate_calls.get(run_id, 0) + 1
            run = self.w.store.get("run", run_id)
            if run_id == self.runs[2]["id"] and evaluate_calls[run_id] == 1:
                # First eval of C: re-queue it (simulates follow-up revival)
                self.w.service.queue_run(run)
                ab_gate.set()  # allow A and B to proceed
            else:
                self.w.store.transition(run_id, "evaluated_incomplete")

        async def fake_freeze(id, baseline, spec=None):
            c = spec or self.w.freeze_requirements(id)
            plan = Plan.model_validate(c["development_plan"])
            return (plan, {}, {**c, "plan": plan.model_dump(),
                               "approved_test_review": None})

        with patch.object(self.w, "phase", side_effect=fake_phase), \
             patch.object(self.w, "snapshot_run", side_effect=fake_snapshot), \
             patch.object(self.w, "evaluate_run", side_effect=fake_evaluate), \
             patch.object(self.w, "freeze", side_effect=fake_freeze):
            await self.w.compare(self.cid)

        # run C should have been dispatched to develop twice
        c_develop_calls = [d for d in dispatch_log
                           if d[0] == self.runs[2]["id"] and d[1] == "develop"]
        self.assertGreaterEqual(len(c_develop_calls), 2,
            "run C's develop phase should be dispatched at least twice "
            "(initial + re-dispatch after re-queuing)")
        # evaluate_run for C should have been called twice
        self.assertGreaterEqual(evaluate_calls.get(self.runs[2]["id"], 0), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
