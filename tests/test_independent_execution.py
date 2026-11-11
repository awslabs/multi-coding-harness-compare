# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise orchestration with controlled native/planner delays; no model calls."""
import asyncio
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

from arena.config import Config
from arena.evaluation import default_plan
from arena.worker import Worker


class IndependentExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.worker = Worker(Config(data_dir=str(Path(self.directory.name) / "data"), artifact_s3_uri=None))
        self.comparison = self.worker.service.create("Create a small local command line tool.")
        self.id = self.comparison["id"]
        self.worker.store.update("comparison", self.id, test_review=None)
        self.runs = {r["harness"]: r["id"] for r in self.comparison["runs"]}
        self.gates = {}
        self.reached = defaultdict(asyncio.Event)
        self.calls = Counter()
        self.coding_criteria = {}
        self.planner_gates = {}
        self.planners = Counter()
        self.readiness_omissions = set()

        async def phase(run_id, phase, spec, plan, readiness_reminder=False):
            harness = self.worker.store.get("run", run_id)["harness"]
            key = (harness, phase, spec["spec_version"])
            self.calls[key] += 1
            if phase == "develop":
                self.coding_criteria[(harness, spec["spec_version"])] = plan.model_dump()["criteria"]
            self.worker.store.transition(run_id, "clarifying" if phase == "clarify" else "developing",
                                         spec_version=spec["spec_version"], ended_at=None)
            self.reached[key].set()
            if harness == "claude":
                self.worker.store.transition(run_id, "failed", error="Simulated launcher failure")
                return "failed"
            if key in self.gates:
                await self.gates[key].wait()
            if self.worker.cancelled(self.worker.store.get("run", run_id)):
                self.worker.store.transition(run_id, "cancelled")
                return "cancelled"
            if phase == "clarify":
                if harness in self.readiness_omissions and not readiness_reminder:
                    return "readiness_unverified"
                self.worker.store.transition(run_id, "ready", clarified=True,
                                             readiness_reminder=readiness_reminder)
                return "ready"
            if self.worker.store.get("comparison", self.id)["spec_version"] != spec["spec_version"]:
                self.worker.store.transition(run_id, "ready")
                return "revised"
            workspace = self.worker.run_root(self.worker.store.get("run", run_id)) / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "main.py").write_text(f"print({spec['spec_version']})\n")
            self.worker.store.transition(run_id, "implemented")
            return "implemented"

        async def evaluate(run_id, spec, plan, baseline):
            harness = self.worker.store.get("run", run_id)["harness"]
            self.calls[(harness, "evaluate", spec["spec_version"])] += 1
            self.worker.store.transition(run_id, "verified_complete",
                                         evaluation={"overall": 100, "spec_version": spec["spec_version"]})
            self.reached[(harness, "evaluate", spec["spec_version"])].set()

        async def prepare(spec, baseline, config):
            version = spec["spec_version"]
            self.assertEqual(spec["answers"], self.worker.store.get("comparison", self.id)["answers"])
            self.assertEqual(list(baseline.iterdir()), [])
            self.planners[version] += 1
            self.reached[("planner", version)].set()
            if version in self.planner_gates:
                await self.planner_gates[version].wait()
            plan = default_plan(spec, baseline)
            plan.criteria[0].requirement = "A detailed evaluation criterion created after coding started."
            return plan, {"source": "controlled independent planner"}

        async def checks(*args, **kwargs):
            return {"checks": [], "environment": {}}

        self.worker.phase = phase
        self.worker.evaluate_run = evaluate
        self.prepare_patch = patch("arena.worker.prepare_plan", side_effect=prepare)
        self.checks_patch = patch("arena.worker.CandidateTestRunner.run", side_effect=checks)
        self.prepare_patch.start()
        self.checks_patch.start()
        self.addCleanup(self.prepare_patch.stop)
        self.addCleanup(self.checks_patch.stop)
        self.task = None

    async def asyncTearDown(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def start(self):
        self.task = asyncio.create_task(self.worker.compare(self.id))

    async def reached_event(self, key):
        await asyncio.wait_for(self.reached[key].wait(), 15)

    async def status(self, harness, status):
        async with asyncio.timeout(15):
            while self.worker.store.get("run", self.runs[harness])["status"] != status:
                await asyncio.sleep(.02)

    async def finish(self):
        await asyncio.wait_for(self.task, 15)

    async def test_failure_slow_clarification_and_planning_do_not_block_coding(self):
        self.gates[("kiro", "clarify", 1)] = asyncio.Event()
        self.planner_gates[1] = asyncio.Event()
        self.start()
        await self.reached_event(("codex", "develop", 1))
        await self.status("codex", "planning")
        codex = self.worker.store.get("run", self.runs["codex"])
        self.assertTrue(Path(codex["artifacts"]["snapshot"]).exists())
        self.assertEqual(codex["project_output"]["status"], "published")
        self.assertFalse(self.reached[("codex", "evaluate", 1)].is_set())
        self.planner_gates[1].set()
        await self.reached_event(("codex", "evaluate", 1))
        self.assertFalse(self.reached[("kiro", "develop", 1)].is_set())
        self.gates[("kiro", "clarify", 1)].set()
        await self.finish()
        self.assertEqual(self.planners, {1: 1})
        self.assertEqual(self.calls[("kiro", "evaluate", 1)], 1)
        self.assertEqual(self.coding_criteria[("codex", 1)], self.coding_criteria[("kiro", 1)])
        self.assertEqual(self.worker.store.get("run", self.runs["claude"])["status"], "failed")

    async def test_fast_candidate_evaluated_while_other_candidate_is_developing(self):
        self.gates[("kiro", "develop", 1)] = asyncio.Event()
        self.start()
        await self.reached_event(("codex", "evaluate", 1))
        self.assertEqual(self.worker.store.get("run", self.runs["kiro"])["status"], "developing")
        self.gates[("kiro", "develop", 1)].set()
        await self.finish()

    async def test_harness_that_skips_the_ready_tool_is_reminded_and_still_competes(self):
        self.readiness_omissions = {"kiro"}
        self.start()
        await self.finish()
        # The omission costs one extra clarification turn, not the whole run.
        self.assertEqual(self.calls[("kiro", "clarify", 1)], 2)
        self.assertEqual(self.calls[("codex", "clarify", 1)], 1)
        kiro = self.worker.store.get("run", self.runs["kiro"])
        self.assertTrue(kiro["clarified"])
        self.assertTrue(kiro["readiness_reminder"])
        self.assertEqual(self.calls[("kiro", "evaluate", 1)], 1)

    async def test_answer_restarts_completed_harness_and_preserves_old_results(self):
        self.gates[("kiro", "clarify", 1)] = asyncio.Event()
        kiro = self.worker.store.get("run", self.runs["kiro"])
        question = self.worker.service.register_question(kiro, {
            "id": "q", "turn_key": "turn", "kind": "requirement",
            "question": "Which output format?", "choices": ["JSON", "text"]})
        self.start()
        await self.reached_event(("codex", "evaluate", 1))
        # Even an unanswered question from Kiro did not hold Codex.
        self.worker.service.answer(question["id"], "JSON")
        self.gates[("kiro", "clarify", 1)].set()
        await self.finish()
        self.assertEqual(self.planners, {1: 1, 2: 1})
        for harness in ("kiro", "codex"):
            self.assertEqual(self.calls[(harness, "evaluate", 2)], 1)
            self.assertEqual(self.calls[(harness, "clarify", 1)], 1)
        codex = self.worker.store.get("run", self.runs["codex"])
        previous = codex["artifact_history"][0]
        self.assertEqual(previous["spec_version"], 1)
        self.assertEqual(previous["evaluation"]["overall"], 100)
        self.assertEqual((Path(previous["artifacts"]["snapshot"]) / "main.py").read_text(), "print(1)\n")

    async def test_cancelling_one_harness_keeps_shared_plan_and_other_harness_running(self):
        self.planner_gates[1] = asyncio.Event()
        self.start()
        await self.status("codex", "planning")
        await self.status("kiro", "planning")
        self.worker.service.cancel(self.id, self.runs["codex"])
        await self.status("codex", "cancelled")
        self.planner_gates[1].set()
        await self.finish()
        self.assertEqual(self.calls[("kiro", "evaluate", 1)], 1)
        self.assertEqual(self.calls[("codex", "evaluate", 1)], 0)
        self.assertEqual(self.planners, {1: 1})

    async def test_revision_replaces_pending_plan_and_resumes_both_sessions(self):
        self.planner_gates[1] = asyncio.Event()
        self.start()
        await self.status("codex", "planning")
        await self.status("kiro", "planning")
        self.worker.service.revise(self.id, "Write JSON output.")
        await self.finish()
        self.assertEqual(self.planners, {1: 1, 2: 1})
        for harness in ("kiro", "codex"):
            self.assertEqual(self.calls[(harness, "evaluate", 1)], 0)
            self.assertEqual(self.calls[(harness, "evaluate", 2)], 1)
            self.assertEqual(self.calls[(harness, "clarify", 1)], 1)
        c = self.worker.store.get("comparison", self.id)
        self.assertEqual([s["version"] for s in c["specifications"]], [1, 2])
        self.assertEqual(c["specifications"][1]["answers"][0]["answer"], "Write JSON output.")

    async def test_worker_shutdown_cancels_pending_planner_and_native_jobs(self):
        self.gates[("kiro", "clarify", 1)] = asyncio.Event()
        self.planner_gates[1] = asyncio.Event()
        self.start()
        await self.reached_event(("planner", 1))
        self.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.task
        self.assertEqual(self.worker.store.get("comparison", self.id)["status"], "interrupted")
        self.assertEqual(self.calls[("codex", "evaluate", 1)], 0)

    async def test_real_development_loop_does_not_pause_for_another_harness_question(self):
        # Use the actual phase loop, replacing only the native process transport.
        # This catches reintroduction of a global question barrier inside phase().
        class Process:
            returncode = None

            async def wait(self):
                self.returncode = 0
                return 0

        process = Process()
        run_id = self.runs["codex"]
        run = self.worker.store.get("run", run_id)
        root = self.worker.run_root(run)

        async def start(*args):
            (root / "bridge" / "requests").mkdir(parents=True)
            return {"workspace": str(root / "workspace")}

        async def receive(*args):
            await asyncio.sleep(.35)
            self.worker.store.update("run", run_id, turn_completed=True)
            process.returncode = 0

        self.worker.receive = receive
        self.worker.runtime.start = AsyncMock(side_effect=start)
        self.worker.runtime.launch = AsyncMock(return_value=process)
        self.worker.runtime.supervise = AsyncMock()
        self.worker.runtime.pause = AsyncMock(return_value=(0, "", ""))
        self.worker.runtime.unpause = AsyncMock()
        self.worker.runtime.stop = AsyncMock()
        self.worker.service.register_question(
            self.worker.store.get("run", self.runs["kiro"]),
            {"id": "q-other", "turn_key": "other-turn", "kind": "requirement",
             "question": "Which output format?", "choices": ["JSON", "text"]})
        spec = self.worker.freeze_requirements(self.id)
        baseline = root.parent.parent / "baseline"
        with patch("arena.worker.initialize_checkout", new=AsyncMock()):
            result = await Worker.phase(self.worker, run_id, "develop", spec, default_plan(spec, baseline))
        self.assertEqual(result, "implemented")
        self.worker.runtime.pause.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
