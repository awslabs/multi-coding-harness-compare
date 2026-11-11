# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A follow-up accepted during completion stays queued for its selected recipient."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena.config import Config
from arena.evaluation import Plan
from arena.store import TERMINAL
from arena.worker import Worker


class HarnessSelectionFinalizationTests(unittest.IsolatedAsyncioTestCase):
    def test_follow_up_revives_cancelled_recipient_before_finalization(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = Worker(Config(data_dir=str(Path(temp) / "data"), artifact_s3_uri=None))
            comparison = worker.service.create("Build a CLI", harnesses=["claude", "codex"])
            comparison_id = comparison["id"]
            worker.freeze_requirements(comparison_id)
            for run in comparison["runs"]:
                worker.store.transition(run["id"], "cancelled", cancel_requested=True)
            worker.store.update("comparison", comparison_id, status="developing", cancel_requested=True)
            original_queue = worker.service.queue_run
            finalized = False

            def queue_then_finalize(run):
                nonlocal finalized
                original_queue(run)
                finalized = True
                worker.finish_comparison(comparison_id, worker.service.report(comparison_id))

            with patch.object(worker.service, "queue_run", side_effect=queue_then_finalize):
                worker.service.revise(comparison_id, "Add JSON output", harnesses=["codex"])
            if not finalized:
                # Atomic revival no longer exposes a queue_run/answer gap.
                worker.finish_comparison(comparison_id, worker.service.report(comparison_id))
            result = worker.service.report(comparison_id)

            runs = {run["harness"]: run for run in result["runs"]}
            self.assertEqual(result["status"], "queued")
            self.assertFalse(result["cancel_requested"])
            self.assertEqual(runs["codex"]["status"], "queued")
            self.assertFalse(runs["codex"]["cancel_requested"])
            self.assertEqual(runs["claude"]["status"], "cancelled")
            self.assertTrue(runs["claude"]["cancel_requested"])

    def test_follow_up_revives_failed_recipient_before_archival_finalization(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = Worker(Config(data_dir=str(Path(temp) / "data"), artifact_s3_uri=None))
            comparison = worker.service.create("Build a CLI", harnesses=["claude", "codex"])
            comparison_id = comparison["id"]
            worker.freeze_requirements(comparison_id)
            for run in comparison["runs"]:
                worker.store.transition(run["id"], "failed")
            worker.store.update("comparison", comparison_id, status="developing")
            original_event = worker.store.event
            finalized = False

            def finalize_after_answer(comparison_id, kind, data, run=None):
                nonlocal finalized
                original_event(comparison_id, kind, data, run)
                if kind == "answer":
                    finalized = True
                    observed = worker.service.report(comparison_id)
                    # Only the final-state marker is exercised. This test does
                    # not call archive, S3, or any other remote operation.
                    with patch.object(worker.storage, "enabled", True):
                        worker.finish_comparison(comparison_id, observed)

            with patch.object(worker.store, "event", side_effect=finalize_after_answer):
                result = worker.service.revise(comparison_id, "Add JSON output", harnesses=["codex"])

            self.assertTrue(finalized)
            self.assertEqual(result["artifact_final_status"], "queued")
            runs = {run["harness"]: run for run in result["runs"]}
            self.assertEqual(runs["codex"]["status"], "queued")
            self.assertEqual(runs["claude"]["status"], "failed")

    async def test_follow_up_during_final_report_is_dispatched_on_next_worker_pass(self):
        await self._check_late_message(follow_up=True)

    async def test_requirement_answer_during_final_report_is_dispatched_on_next_worker_pass(self):
        await self._check_late_message(follow_up=False)

    async def test_follow_up_is_dispatched_after_the_previous_native_turn_fails(self):
        await self._check_previous_native_failure(fail_requested=False)

    async def test_failure_of_requested_follow_up_is_not_automatically_retried(self):
        await self._check_previous_native_failure(fail_requested=True)

    async def _check_previous_native_failure(self, *, fail_requested):
        with tempfile.TemporaryDirectory() as temp:
            worker = Worker(Config(data_dir=str(Path(temp) / "data"), artifact_s3_uri=None))
            comparison = worker.service.create("Build a CLI", harnesses=["codex"])
            comparison_id = comparison["id"]
            run_id = comparison["runs"][0]["id"]
            worker.store.update("run", run_id, clarified=True)
            dispatched = []

            async def phase(run_id, kind, spec, plan, **kwargs):
                dispatched.append(spec["spec_version"])
                worker.store.transition(run_id, "developing", spec_version=spec["spec_version"])
                if spec["spec_version"] == 1:
                    worker.service.revise(comparison_id, "Add JSON output", harnesses=["codex"])
                    # The old native process can exit before the next revision
                    # poll. Its failure must not drop a newer accepted prompt.
                    worker.store.transition(run_id, "failed", error="Old native turn failed")
                    return "failed"
                if fail_requested:
                    worker.store.transition(run_id, "failed", error="Requested native turn failed")
                    return "failed"
                worker.store.transition(run_id, "implemented")
                return "implemented"

            async def snapshot(run_id, spec):
                worker.store.update("run", run_id, artifacts={})

            async def freeze(comparison_id, baseline, spec=None):
                return Plan.model_validate(spec["development_plan"]), {}, spec

            async def evaluate(run_id, spec, plan, baseline_results):
                worker.store.transition(run_id, "evaluated_incomplete")

            with patch.object(worker, "phase", side_effect=phase), \
                    patch.object(worker, "snapshot_run", side_effect=snapshot), \
                    patch.object(worker, "freeze", side_effect=freeze), \
                    patch.object(worker, "evaluate_run", side_effect=evaluate):
                await worker.compare(comparison_id)

            self.assertEqual(dispatched, [1, 2])
            run = worker.store.get("run", run_id)
            self.assertEqual(run["spec_version"], 2)
            self.assertEqual(run["status"], "failed" if fail_requested else "evaluated_incomplete")

    async def _check_late_message(self, *, follow_up):
        with tempfile.TemporaryDirectory() as temp:
            worker = Worker(Config(data_dir=str(Path(temp) / "data"), artifact_s3_uri=None))
            comparison = worker.service.create("Build a CLI", harnesses=["claude", "codex"])
            comparison_id = comparison["id"]
            harnesses = {run["id"]: run["harness"] for run in comparison["runs"]}
            dispatched = []
            for run in comparison["runs"]:
                worker.store.update("run", run["id"], clarified=True)
            question = None
            if not follow_up:
                question = worker.service.register_question(comparison["runs"][0], {
                    "id": "late-native-question", "turn_key": "old-turn", "kind": "requirement",
                    "question": "What other output is required?", "choices": [], "schema": None})

            async def phase(run_id, kind, spec, plan, **kwargs):
                dispatched.append((harnesses[run_id], spec["spec_version"],
                                   [answer["answer"] for answer in spec["answers"]]))
                worker.store.update("run", run_id, spec_version=spec["spec_version"])
                worker.store.transition(run_id, "implemented")
                return "implemented"

            async def snapshot(run_id, spec):
                worker.store.update("run", run_id, artifacts={})

            async def freeze(comparison_id, baseline, spec=None):
                return Plan.model_validate(spec["development_plan"]), {}, spec

            async def evaluate(run_id, spec, plan, baseline_results):
                worker.store.transition(run_id, "evaluated_incomplete")

            original_report = worker.service.report
            injected = False

            def report_with_late_follow_up(comparison_id):
                nonlocal injected
                runs = worker.store.detail(comparison_id)["runs"]
                if not injected and all(run["status"] in TERMINAL for run in runs):
                    injected = True
                    # The API is a separate process. It can accept a follow-up
                    # after the worker's last dispatch poll and before completion.
                    if follow_up:
                        worker.service.revise(comparison_id, "Add JSON output",
                                              harnesses=["codex"])
                    else:
                        worker.service.answer(question["id"], "Add JSON output")
                return original_report(comparison_id)

            with patch.object(worker, "phase", side_effect=phase), \
                    patch.object(worker, "snapshot_run", side_effect=snapshot), \
                    patch.object(worker, "freeze", side_effect=freeze), \
                    patch.object(worker, "evaluate_run", side_effect=evaluate), \
                    patch.object(worker.service, "report", side_effect=report_with_late_follow_up):
                await worker.compare(comparison_id)
                self.assertTrue(injected)
                pending = worker.store.detail(comparison_id)
                self.assertEqual(pending["status"], "queued")
                runs = {run["harness"]: run for run in pending["runs"]}
                self.assertEqual(runs["codex"]["requested_spec_version"], 2)
                if follow_up:
                    self.assertEqual(runs["codex"]["status"], "queued")
                    self.assertEqual(runs["claude"]["status"], "evaluated_incomplete")
                    self.assertEqual(runs["claude"]["requested_spec_version"], 1)
                else:
                    self.assertEqual(runs["claude"]["requested_spec_version"], 2)

                await worker.compare(comparison_id)

            self.assertEqual([entry for entry in dispatched if entry[0] == "claude"],
                             [("claude", 1, [])] if follow_up else
                             [("claude", 1, []), ("claude", 2, ["Add JSON output"])])
            self.assertEqual([entry for entry in dispatched if entry[0] == "codex"],
                             [("codex", 1, []), ("codex", 2, ["Add JSON output"])])
