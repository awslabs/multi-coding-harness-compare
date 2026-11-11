# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise the real scheduler with controlled native/model boundaries."""
import asyncio
from collections import Counter
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from arena.config import Config
from arena.evaluation import Plan, Check, Criterion
from arena.isolation import tree_hash
from arena.store import now
from arena.test_review import TestProposal, TestScenario, digest
from arena.worker import Worker


def proposal():
    return TestProposal(summary="Reviewed CLI", test_cases=[
        TestScenario(id="hello", title="CLI greeting", steps=["Invoke the CLI"],
                     expected_result="Expected greeting", test_data="Local inputs")
    ]).model_dump()


class ObservedWorker(Worker):
    def __init__(self, config):
        super().__init__(config)
        self.dispatched = []
        self.executing = 0
        self.peak = 0

    async def compare(self, id):
        self.dispatched.append(id)
        self.executing += 1
        self.peak = max(self.peak, self.executing)
        try:
            await super().compare(id)
        finally:
            self.executing -= 1


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(data_dir=str(Path(self.temp.name) / "data"), max_parallel_comparisons=1,
                          artifact_s3_uri=None)
        self.w = ObservedWorker(self.cfg)
        self.s = self.w.store
        self.server = None
        self.calls = Counter()
        self.gates = {}
        self.w.runtime.cleanup_owned = AsyncMock()

        async def phase(rid, phase, c, plan):
            self.calls[c["id"], phase] += 1
            self.s.transition(rid, "clarifying" if phase == "clarify" else "developing",
                              spec_version=c["spec_version"])
            if phase == "clarify":
                self.s.update("run", rid, clarified=True)
                return "ready"
            if c["id"] in self.gates:
                await self.gates[c["id"]].wait()
            root = self.w.run_root(self.s.get("run", rid)) / "workspace"
            root.mkdir(parents=True, exist_ok=True)
            (root / "main.py").write_text("print('Hello')\n")
            self.s.transition(rid, "implemented")
            return "implemented"

        async def draft(task, baseline, config, review):
            self.calls[task["id"], "proposal"] += 1
            return proposal(), {"source": "controlled"}

        async def prepare(task, baseline, config):
            self.calls[task["id"], "generation"] += 1
            return Plan(
                criteria=[Criterion(id="hello", requirement="Greeting", check_ids=["cli"])],
                checks=[Check(id="cli", group="acceptance", command="python main.py")],
                review_id=task["approved_test_review"]["id"], review_coverage={"hello": ["cli"]},
                excluded_groups={"regression": "None", "project": "None", "candidate": "None"},
            ), {"source": "controlled"}

        async def checks(*args, **kwargs):
            return {"checks": [{"id": "cli", "group": "acceptance", "status": "passed",
                                "exit_code": 0, "log": "Hello", "counts": {}}], "environment": {}}

        self.w.phase = phase
        self.w.score_candidate = AsyncMock(return_value={"overall": 100, "verified_complete": True})
        for target, function in (("arena.worker.propose_tests", draft),
                                 ("arena.worker.prepare_plan", prepare),
                                 ("arena.worker.CandidateTestRunner.run", checks)):
            patcher = patch(target, side_effect=function)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        if self.server:
            self.server.cancel()
            await asyncio.gather(self.server, return_exceptions=True)

    async def wait(self, condition):
        for _ in range(160):
            if condition():
                return
            if self.server and self.server.done():
                self.server.result()
            await asyncio.sleep(.05)
        self.fail("Timed out waiting for scheduler state")

    def waiting_comparison(self, status="generation_failed"):
        created = self.w.service.create("Build a CLI")
        c = self.w.freeze_requirements(created["id"])
        for run in self.s.detail(c["id"])["runs"]:
            snapshot = self.w.run_root(run) / "artifacts/saved/snapshot"
            snapshot.mkdir(parents=True)
            (snapshot / "main.py").write_text("print('Hello')\n")
            patch_file = snapshot.parent / "implementation.patch"
            patch_file.write_text("new main.py\n")
            self.s.transition(run["id"], "waiting_test_review", clarified=True,
                              artifacts={"snapshot": str(snapshot), "patch": str(patch_file),
                                         "sha256": tree_hash(snapshot)})
        review = self.s.get("comparison", c["id"])["test_review"]
        review.update(status=status, proposal=proposal(), answers={}, approved_at=now(),
                      approval_sha256=digest({"proposal": proposal(), "answers": {}}))
        self.s.update("comparison", c["id"], status="waiting_test_review", test_review=review)
        return self.s.detail(c["id"])

    def start(self):
        self.server = asyncio.create_task(self.w.serve())

    async def test_failed_review_does_not_block_new_comparison_after_restart(self):
        old = self.waiting_comparison()
        new = self.w.service.create("A new task")
        self.gates[new["id"]] = asyncio.Event()
        self.start()
        await self.wait(lambda: self.calls[new["id"], "develop"] == 3)
        self.assertEqual(self.calls[old["id"], "develop"], 0)
        self.assertEqual(self.calls[old["id"], "generation"], 0)
        self.assertNotIn(old["id"], self.w.active)
        self.assertEqual(self.s.get("comparison", old["id"])["test_review"], old["test_review"])
        self.assertEqual(self.w.peak, 1)

    async def test_newly_idle_review_releases_slot_and_approval_waits_for_capacity(self):
        first = self.w.service.create("First task")
        second = self.w.service.create("Second task")
        self.gates[second["id"]] = asyncio.Event()
        self.start()
        await self.wait(lambda: self.calls[second["id"], "develop"] == 3)
        before = self.s.detail(first["id"])
        self.assertEqual(before["status"], "waiting_test_review")
        self.assertNotIn(first["id"], self.w.active)
        self.w.service.review_tests(first["id"], before["test_review"]["id"], answers={})
        await asyncio.sleep(.7)
        self.assertEqual(self.calls[first["id"], "generation"], 0)
        self.gates[second["id"]].set()
        await self.wait(lambda: self.s.get("comparison", first["id"])["status"] == "complete")
        after = self.s.detail(first["id"])
        self.assertEqual(self.calls[first["id"], "develop"], 3)
        self.assertEqual(self.calls[first["id"], "generation"], 1)
        self.assertEqual(self.w.peak, 1)
        for old in before["runs"]:
            run = next(r for r in after["runs"] if r["id"] == old["id"])
            self.assertEqual(run["artifacts"]["sha256"], old["artifacts"]["sha256"])

    async def test_feedback_wakes_parked_review_without_recoding(self):
        old = self.waiting_comparison("awaiting_review")
        self.start()
        await asyncio.sleep(.6)
        self.w.service.review_tests(old["id"], old["test_review"]["id"], feedback="Clarify expected output")
        await self.wait(lambda: self.calls[old["id"], "proposal"] == 1)
        await self.wait(lambda: old["id"] not in self.w.active)
        current = self.s.detail(old["id"])
        self.assertEqual(current["test_review"]["status"], "awaiting_review")
        self.assertEqual(self.calls[old["id"], "develop"], 0)
        self.assertEqual(current["status"], "waiting_test_review")

    async def test_cancelled_parked_review_finishes_without_coding(self):
        old = self.waiting_comparison()
        self.start()
        await asyncio.sleep(.6)
        self.w.service.cancel(old["id"])
        await self.wait(lambda: all(r["status"] == "cancelled" for r in self.s.detail(old["id"])["runs"]))
        self.assertEqual(self.calls[old["id"], "develop"], 0)
        self.assertEqual(self.calls[old["id"], "generation"], 0)

    async def test_saved_code_review_returns_slot_after_proposal(self):
        old = self.waiting_comparison()
        review = old["test_review"]
        review.update(mode="saved_code", status="queued", proposal=None)
        for run in old["runs"]:
            self.s.transition(run["id"], "verified_complete",
                              evaluation={"overall": 100, "verified_complete": True})
        self.s.update("comparison", old["id"], status="test_review_queued", test_review=review)
        new = self.w.service.create("New task after saved-code feedback")
        self.gates[new["id"]] = asyncio.Event()
        self.start()
        await self.wait(lambda: self.calls[new["id"], "develop"] == 3)
        self.assertEqual(self.calls[old["id"], "proposal"], 1)
        self.assertEqual(self.s.get("comparison", old["id"])["status"], "waiting_test_review")
        self.assertTrue(all(r["evaluation"]["overall"] == 100 for r in self.s.detail(old["id"])["runs"]))

    def test_only_saved_idle_runs_can_release_capacity(self):
        c = self.waiting_comparison()
        for review_status in ("awaiting_review", "proposal_failed", "generation_failed"):
            candidate = copy.deepcopy(c)
            candidate["test_review"]["status"] = review_status
            self.assertTrue(self.w.waiting_only_for_test_review(candidate))
        for status in ("developing", "setup", "planning", "evaluating", "waiting_user"):
            candidate = copy.deepcopy(c)
            candidate["runs"][0]["status"] = status
            self.assertFalse(self.w.waiting_only_for_test_review(candidate))
        for change in ("missing_snapshot", "stale_spec", "cancelled"):
            candidate = copy.deepcopy(c)
            if change == "missing_snapshot":
                candidate["runs"][0]["artifacts"] = {}
            elif change == "stale_spec":
                candidate["runs"][0]["spec_version"] = 0
            else:
                candidate["runs"][0]["cancel_requested"] = True
            self.assertFalse(self.w.waiting_only_for_test_review(candidate))
        c["test_review"]["status"] = "approved"
        self.assertTrue(self.w.ready_to_dispatch(c))
        self.assertFalse(self.w.waiting_only_for_test_review(c))


if __name__ == "__main__":
    unittest.main(verbosity=2)
