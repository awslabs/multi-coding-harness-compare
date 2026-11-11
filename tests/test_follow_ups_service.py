# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Contract items 1-3: follow-up record, comparison JSON, delivery to all harnesses."""
import tempfile
import unittest
from pathlib import Path

from arena.config import Config
from arena.service import Service
from arena.store import TERMINAL, now


def make_service():
    tmp = tempfile.mkdtemp(prefix="mhc-fu-test-")
    return Service(Config(data_dir=tmp, artifact_s3_uri=None)), tmp


def _runs(svc, cid):
    return svc.store.detail(cid)["runs"]


def _run_statuses(svc, cid):
    return {r["harness"]: r["status"] for r in _runs(svc, cid)}


def _transition_all(svc, cid, status):
    for r in _runs(svc, cid):
        svc.store.transition(r["id"], status)


class FollowUpRecordTests(unittest.TestCase):
    """Contract item 1: follow-up record shape and comparison.follow_ups list."""

    def setUp(self):
        self.svc, _ = make_service()
        self.c = self.svc.create("Build a CLI greeting tool")
        self.cid = self.c["id"]

    def _revise_from_terminal(self, text="Add a --verbose flag"):
        _transition_all(self.svc, self.cid, "failed")
        self.svc.store.update("comparison", self.cid, status="incomplete", ended_at=now())
        return self.svc.revise(self.cid, text)

    def test_follow_up_kind_in_answers(self):
        result = self._revise_from_terminal("Add a --verbose flag")
        follow_up_answers = [a for a in result["answers"]
                             if a.get("kind") == "follow_up"]
        self.assertEqual(len(follow_up_answers), 1)
        self.assertEqual(follow_up_answers[0]["answer"], "Add a --verbose flag")

    def test_follow_ups_list_in_comparison_json(self):
        result = self._revise_from_terminal("Add a --verbose flag")
        report = self.svc.report(self.cid)
        self.assertEqual(len(report["follow_ups"]), 1)
        fu = report["follow_ups"][0]
        self.assertEqual(fu["text"], "Add a --verbose flag")
        self.assertIn("id", fu)
        self.assertIn("created_at", fu)

    def test_multiple_follow_ups_in_submission_order(self):
        _transition_all(self.svc, self.cid, "failed")
        self.svc.store.update("comparison", self.cid, status="incomplete", ended_at=now())
        self.svc.revise(self.cid, "First follow-up")
        _transition_all(self.svc, self.cid, "failed")
        self.svc.store.update("comparison", self.cid, status="incomplete", ended_at=now())
        self.svc.revise(self.cid, "Second follow-up")
        report = self.svc.report(self.cid)
        self.assertEqual(len(report["follow_ups"]), 2)
        self.assertEqual(report["follow_ups"][0]["text"], "First follow-up")
        self.assertEqual(report["follow_ups"][1]["text"], "Second follow-up")

    def test_legacy_answers_without_kind_included_in_follow_ups(self):
        # Answers with question == "User specification revision" but no kind field
        # (older records) must also appear in follow_ups.
        _transition_all(self.svc, self.cid, "failed")
        self.svc.store.update("comparison", self.cid, status="incomplete", ended_at=now())
        self.svc.revise(self.cid, "Legacy follow-up")
        c = self.svc.store.get("comparison", self.cid)
        # Strip the kind field to simulate a legacy record
        for a in c["answers"]:
            a.pop("kind", None)
        self.svc.store.put("comparison", c)
        report = self.svc.report(self.cid)
        self.assertEqual(len(report["follow_ups"]), 1)
        self.assertEqual(report["follow_ups"][0]["text"], "Legacy follow-up")

    def test_compatibility_question_text_preserved(self):
        _transition_all(self.svc, self.cid, "failed")
        self.svc.store.update("comparison", self.cid, status="incomplete", ended_at=now())
        self.svc.revise(self.cid, "Compat test")
        c = self.svc.store.get("comparison", self.cid)
        follow_up_entries = [a for a in c["answers"] if a.get("kind") == "follow_up"]
        self.assertTrue(all(a["question"] == "User specification revision"
                            for a in follow_up_entries))

    def test_initial_comparison_has_empty_follow_ups(self):
        report = self.svc.report(self.cid)
        self.assertEqual(report["follow_ups"], [])


class RevivalTests(unittest.TestCase):
    """Contract item 3: delivery to all harnesses — run revival semantics."""

    def setUp(self):
        self.svc, _ = make_service()
        self.c = self.svc.create("Build something")
        self.cid = self.c["id"]
        self.run_ids = {r["harness"]: r["id"] for r in self.c["runs"]}

    def _set_run_status(self, harness, status):
        self.svc.store.transition(self.run_ids[harness], status)

    def test_cancelled_run_is_revived_on_follow_up(self):
        self._set_run_status("claude", "cancelled")
        self._set_run_status("codex", "cancelled")
        self._set_run_status("kiro", "cancelled")
        self.svc.store.update("comparison", self.cid, status="incomplete",
                              cancel_requested=True, ended_at=now())
        self.svc.revise(self.cid, "Try again with a different approach")
        statuses = _run_statuses(self.svc, self.cid)
        self.assertTrue(all(s == "queued" for s in statuses.values()),
                        statuses)

    def test_failed_run_is_revived(self):
        for h in ("claude", "codex", "kiro"):
            self._set_run_status(h, "failed")
        self.svc.store.update("comparison", self.cid, status="incomplete", ended_at=now())
        self.svc.revise(self.cid, "Follow-up")
        statuses = _run_statuses(self.svc, self.cid)
        self.assertTrue(all(s == "queued" for s in statuses.values()), statuses)

    def test_verified_complete_run_is_revived(self):
        for h in ("claude", "codex", "kiro"):
            self.svc.store.transition(self.run_ids[h], "verified_complete",
                                      evaluation={"overall": 100, "verified_complete": True})
        self.svc.store.update("comparison", self.cid, status="complete", ended_at=now())
        self.svc.revise(self.cid, "Extend the feature")
        statuses = _run_statuses(self.svc, self.cid)
        self.assertTrue(all(s == "queued" for s in statuses.values()), statuses)

    def test_cancel_requested_run_is_revived(self):
        for h in ("claude", "codex", "kiro"):
            self.svc.store.transition(self.run_ids[h], "cancelled")
            self.svc.store.update("run", self.run_ids[h], cancel_requested=True)
        self.svc.store.update("comparison", self.cid, status="incomplete",
                              cancel_requested=True, ended_at=now())
        self.svc.revise(self.cid, "Revive all")
        statuses = _run_statuses(self.svc, self.cid)
        self.assertTrue(all(s == "queued" for s in statuses.values()), statuses)

    def test_all_cancelled_comparison_revived_to_queued(self):
        for h in ("claude", "codex", "kiro"):
            self._set_run_status(h, "cancelled")
        self.svc.store.update("comparison", self.cid, status="incomplete",
                              cancel_requested=True, ended_at=now())
        result = self.svc.revise(self.cid, "Restart everything")
        self.assertEqual(result["status"], "queued")
        self.assertFalse(result.get("cancel_requested"))
        statuses = _run_statuses(self.svc, self.cid)
        self.assertTrue(all(s == "queued" for s in statuses.values()), statuses)

    def test_active_comparison_executing_runs_untouched(self):
        # Some runs terminal, some actively developing
        self._set_run_status("claude", "failed")
        self.svc.store.transition(self.run_ids["codex"], "developing")
        self.svc.store.transition(self.run_ids["kiro"], "developing")
        self.svc.store.update("comparison", self.cid, status="developing",
                              frozen=True)
        self.svc.revise(self.cid, "Add tests")
        statuses = _run_statuses(self.svc, self.cid)
        # Failed run revived, developing runs untouched
        self.assertEqual(statuses["claude"], "queued")
        self.assertIn(statuses["codex"], ("developing",))
        self.assertIn(statuses["kiro"], ("developing",))

    def test_active_comparison_status_unchanged(self):
        self.svc.store.transition(self.run_ids["claude"], "developing")
        self.svc.store.transition(self.run_ids["codex"], "developing")
        self.svc.store.transition(self.run_ids["kiro"], "developing")
        self.svc.store.update("comparison", self.cid, status="developing", frozen=True)
        self.svc.revise(self.cid, "Follow-up while active")
        c = self.svc.store.get("comparison", self.cid)
        self.assertEqual(c["status"], "developing")

    def test_active_comparison_cancel_requested_cleared(self):
        self.svc.store.transition(self.run_ids["claude"], "developing")
        self.svc.store.transition(self.run_ids["codex"], "developing")
        self.svc.store.transition(self.run_ids["kiro"], "developing")
        self.svc.store.update("comparison", self.cid, status="developing",
                              frozen=True, cancel_requested=True)
        self.svc.revise(self.cid, "Uncancel via follow-up")
        c = self.svc.store.get("comparison", self.cid)
        self.assertFalse(c.get("cancel_requested"))

    def test_waiting_test_review_comparison_requeued(self):
        for h in ("claude", "codex", "kiro"):
            self.svc.store.transition(self.run_ids[h], "waiting_test_review")
        self.svc.store.update("comparison", self.cid, status="waiting_test_review")
        result = self.svc.revise(self.cid, "Follow-up during test review wait")
        self.assertEqual(result["status"], "queued")

    def test_waiting_test_review_with_failed_run_revives_it(self):
        self.svc.store.transition(self.run_ids["claude"], "waiting_test_review")
        self.svc.store.transition(self.run_ids["codex"], "waiting_test_review")
        self.svc.store.transition(self.run_ids["kiro"], "failed")
        self.svc.store.update("comparison", self.cid, status="waiting_test_review")
        self.svc.revise(self.cid, "Revive the failed run too")
        statuses = _run_statuses(self.svc, self.cid)
        self.assertEqual(statuses["kiro"], "queued")
        # waiting_test_review runs are not re-queued here; the worker re-develops them on its next
        # dispatch, since reusing a snapshot needs an unchanged spec version and a stored snapshot
        self.assertEqual(statuses["claude"], "waiting_test_review")

    def test_comparison_cancel_requested_cleared_on_released_comparison(self):
        for h in ("claude", "codex", "kiro"):
            self._set_run_status(h, "failed")
        self.svc.store.update("comparison", self.cid, status="incomplete",
                              cancel_requested=True, ended_at=now())
        self.svc.revise(self.cid, "Retry with new instructions")
        c = self.svc.store.get("comparison", self.cid)
        self.assertFalse(c.get("cancel_requested"))


class RefusalTests(unittest.TestCase):
    """Contract item 2: explicit refusals."""

    def setUp(self):
        self.svc, _ = make_service()
        self.c = self.svc.create("Build a service")
        self.cid = self.c["id"]

    def test_refuses_during_artifact_transfer(self):
        for status in ("archiving", "archive_queued", "restoring", "storage_failed"):
            with self.subTest(status=status):
                self.svc.store.update("comparison", self.cid, status=status)
                with self.assertRaises(ValueError):
                    self.svc.revise(self.cid, "Follow-up during transfer")

    def test_refuses_during_evaluation_queued(self):
        self.svc.store.update("comparison", self.cid, status="evaluation_queued")
        with self.assertRaises(ValueError) as ctx:
            self.svc.revise(self.cid, "Follow-up during re-score")
        self.assertIn("re-scoring", str(ctx.exception))

    def test_refuses_during_verification_queued(self):
        self.svc.store.update("comparison", self.cid, status="verification_queued")
        with self.assertRaises(ValueError) as ctx:
            self.svc.revise(self.cid, "Follow-up during re-verify")
        self.assertIn("re-scoring", str(ctx.exception))

    def test_refuses_empty_text(self):
        with self.assertRaises(ValueError):
            self.svc.revise(self.cid, "   ")


class QueueContinuationTests(unittest.TestCase):
    """queue_continuation keeps its original semantics: cancelled runs stay cancelled."""

    def setUp(self):
        self.svc, _ = make_service()
        self.c = self.svc.create("Build a CLI")
        self.cid = self.c["id"]

    def _set_all_terminal(self, status):
        for r in _runs(self.svc, self.cid):
            self.svc.store.transition(r["id"], status)
        self.svc.store.update("comparison", self.cid, status="incomplete", ended_at=now())

    def test_cancelled_runs_are_skipped(self):
        self._set_all_terminal("cancelled")
        self.svc.queue_continuation(self.cid)
        statuses = _run_statuses(self.svc, self.cid)
        self.assertTrue(all(s == "cancelled" for s in statuses.values()), statuses)

    def test_failed_runs_are_queued_and_the_comparison_too(self):
        self._set_all_terminal("failed")
        self.svc.queue_continuation(self.cid)
        statuses = _run_statuses(self.svc, self.cid)
        self.assertTrue(all(s == "queued" for s in statuses.values()), statuses)
        self.assertEqual(self.svc.store.get("comparison", self.cid)["status"], "queued")


class PendingCancelTests(unittest.TestCase):
    def test_a_follow_up_supersedes_a_cancel_that_has_not_landed(self):
        svc, _ = make_service()
        c = svc.create("Build a CLI")
        runs = _runs(svc, c["id"])
        for r in runs:
            svc.store.transition(r["id"], "developing")
        svc.store.update("comparison", c["id"], status="developing")
        svc.store.update("run", runs[0]["id"], cancel_requested=True)   # Cancel clicked, not yet acted on
        svc.revise(c["id"], "Use a main() function")
        current = svc.store.get("run", runs[0]["id"])
        self.assertFalse(current["cancel_requested"])
        self.assertEqual(current["status"], "developing")


class SpecVersionTests(unittest.TestCase):
    """spec_version/test_review behaviour unchanged."""

    def setUp(self):
        self.svc, _ = make_service()
        self.c = self.svc.create("Build something")
        self.cid = self.c["id"]

    def test_spec_version_bumped_when_frozen(self):
        # Freeze the comparison (worker sets this before coding)
        self.svc.store.update("comparison", self.cid, frozen=True)
        self.svc.store.transition(
            self.svc.store.detail(self.cid)["runs"][0]["id"], "developing")
        self.svc.store.update("comparison", self.cid, status="developing")
        before = self.svc.store.get("comparison", self.cid)["spec_version"]
        self.svc.revise(self.cid, "Add a flag")
        after = self.svc.store.get("comparison", self.cid)["spec_version"]
        self.assertEqual(after, before + 1)

    def test_spec_version_not_bumped_when_not_frozen(self):
        # Not frozen yet (pre-coding); spec_version stays
        self._set_terminal()
        before = self.svc.store.get("comparison", self.cid)["spec_version"]
        self.svc.revise(self.cid, "A revision")
        after = self.svc.store.get("comparison", self.cid)["spec_version"]
        self.assertEqual(after, before)

    def _set_terminal(self):
        for r in _runs(self.svc, self.cid):
            self.svc.store.transition(r["id"], "failed")
        self.svc.store.update("comparison", self.cid, status="incomplete", ended_at=now())

    def test_test_review_reset_on_revise(self):
        from arena.test_review import new_review
        c = self.svc.store.get("comparison", self.cid)
        # Inject a test_review into the comparison
        c["test_review"] = new_review(c)
        c["test_review"]["status"] = "awaiting_review"
        self.svc.store.put("comparison", c)
        self._set_terminal()
        self.svc.revise(self.cid, "Add new requirement")
        c_after = self.svc.store.get("comparison", self.cid)
        # Test review should be reset (new id and pending status)
        self.assertNotEqual(c_after["test_review"]["id"], c["test_review"]["id"])
