# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Real storage/queue boundaries with controlled S3 responses; no provider calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from arena.api import create_app
from arena.artifact_readiness import ArtifactsNotReady, readiness, retry_gate
from arena.artifact_storage import MultipartWriter, remove_tree
from arena.auth import access_token
from arena.config import Config
from arena.store import now
from arena.test_review import TestProposal, TestScenario, digest
from arena.worker import Worker
from test_artifact_storage import FakeS3


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                          artifact_s3_uri="s3://test-bucket/output/codecouncil/")
        self.w = Worker(self.cfg)
        self.w.storage._client = self.s3 = FakeS3()
        self.s = self.w.store
        self.heartbeat()
        created = self.w.service.create("Build a CLI")
        self.id = created["id"]
        self.w.freeze_requirements(self.id)
        self.root = self.w.storage.root(self.id)
        for run in created["runs"]:
            snapshot = self.w.run_root(run) / "artifacts/saved/snapshot"
            snapshot.mkdir(parents=True)
            (snapshot / "main.py").write_text("print('hello')\n")
            self.s.transition(run["id"], "waiting_test_review",
                              artifacts={"snapshot": str(snapshot)})
        self.review = self.s.get("comparison", self.id)["test_review"]
        proposal = TestProposal(summary="CLI", test_cases=[TestScenario(
            id="cli", title="CLI", steps=["Run CLI"], expected_result="Hello", test_data="Local fixture")]).model_dump()
        self.review.update(status="generation_failed", proposal=proposal, answers={}, approved_at=now(),
                           approval_sha256=digest({"proposal": proposal, "answers": {}}))
        self.s.update("comparison", self.id, status="waiting_test_review", test_review=self.review)

    def heartbeat(self):
        self.s.put("system", {"id": "worker", "pid": os.getpid(), "heartbeat": now(),
                              "instance_id": self.w.instance_id})

    def status(self):
        return readiness(self.w.storage, self.id)

    def approve(self):
        return self.w.service.review_tests(self.id, self.review["id"], answers={})

    def recover(self):
        self.heartbeat()
        self.w.recover_artifacts()

    def test_retry_waits_for_all_three_upload_verification_and_cleanup_boundaries(self):
        observations = []
        original = self.s3.check
        def check(name, args):
            original(name, args)
            if name in ("complete", "head", "put"):
                state = self.status()
                observations.append(state)
                self.assertFalse(state["ready"])
                with self.assertRaises(ArtifactsNotReady):
                    self.approve()
        def cleanup(path):
            if path == self.root:
                self.assertEqual(self.w.storage.state(self.id)["status"], "evicting")
                self.assertFalse(self.status()["ready"])
                with self.assertRaises(ArtifactsNotReady):
                    self.approve()
            remove_tree(path)
        self.s3.check = check
        with patch("arena.artifact_storage.remove_tree", side_effect=cleanup):
            self.recover()
        self.assertTrue(observations)
        self.assertTrue(any(s["transfer"]["uploaded_bytes"] > 0 for s in observations))
        state = self.status()
        self.assertTrue(state["ready"])
        self.assertEqual((state["comparisons_ready"], state["comparisons_total"]), (1, 1))
        self.assertEqual(state["phase"], "complete")
        self.assertFalse(self.root.exists())
        self.approve()
        after = self.s.get("comparison", self.id)["test_review"]
        self.assertEqual(after["approved_at"], self.review["approved_at"])
        self.assertEqual(after["approval_sha256"], self.review["approval_sha256"])
        self.assertEqual(after["generation_status"], "queued")

    def test_leftover_failure_blocks_ready_comparison_and_storage_retry_preserves_approval(self):
        extra = Path(self.temp.name) / "leftover.txt"
        extra.write_text("evidence")
        self.cfg.artifact_paths = [str(extra)]
        original = self.s3.check
        def check(name, args):
            if "/artifacts/" in args.get("Key", "") and name == "part":
                raise OSError("Controlled leftover upload failure")
            original(name, args)
        self.s3.check = check
        with self.assertRaisesRegex(OSError, "leftover"):
            self.recover()
        state = self.status()
        self.assertEqual(state["comparisons_ready"], 1)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["phase"], "leftovers")
        self.assertIn("leftover", state["error"])
        self.assertTrue(state["can_retry_recovery"])
        with self.assertRaises(ArtifactsNotReady):
            self.approve()
        before = self.s.detail(self.id)
        queued = self.w.service.retry_artifact_recovery()
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(self.s.detail(self.id), before)
        self.s3.check = original
        self.recover()
        self.assertTrue(self.status()["ready"])
        self.assertFalse(extra.exists())
        self.assertEqual(self.s.get("comparison", self.id)["test_review"], self.review)

    def test_cleanup_failure_never_enables_retry(self):
        def cleanup(path):
            if path == self.root:
                raise OSError("Controlled cleanup failure")
            remove_tree(path)
        with patch("arena.artifact_storage.remove_tree", side_effect=cleanup):
            with self.assertRaisesRegex(OSError, "cleanup"):
                self.recover()
        self.assertFalse(self.status()["ready"])
        self.assertTrue(self.root.exists())
        self.assertEqual(self.w.storage.state(self.id)["status"], "evicting")
        self.w.service.retry_artifact_recovery()
        self.recover()
        self.assertTrue(self.status()["ready"])
        self.assertFalse(self.root.exists())

    def test_offline_restarted_or_legacy_worker_cannot_reuse_ready_status(self):
        self.recover()
        self.assertTrue(self.status()["ready"])
        worker = self.s.get("system", "worker")
        for changes, expected in (({"heartbeat": now()-20}, "offline"),
                                  ({"instance_id": "new-worker"}, "pending"),
                                  ({"instance_id": None}, "restart_required")):
            self.s.put("system", {**worker, **changes})
            self.assertEqual(self.status()["status"], expected)
            with self.assertRaises(ArtifactsNotReady):
                self.approve()
        self.s.put("system", worker)

    def test_stale_browser_race_with_maintenance_or_execution_lock_is_rejected(self):
        self.recover()
        for lock_id in ("maintenance", self.id + ".execution"):
            with self.w.storage.lock(lock_id):
                with self.assertRaises(ArtifactsNotReady) as result:
                    self.approve()
                self.assertEqual(result.exception.readiness["status"], "busy")
        self.assertEqual(self.s.get("comparison", self.id)["test_review"], self.review)

    def test_new_leftover_comparison_invalidates_previously_ready_snapshot(self):
        self.recover()
        prior = self.w.service.create("Another stopped comparison")
        self.s.update("comparison", prior["id"], status="incomplete")
        self.assertFalse(self.status()["ready"])
        self.assertEqual(self.status()["comparisons_total"], 2)
        with self.assertRaises(ArtifactsNotReady):
            self.approve()
        self.assertTrue(self.status()["can_retry_recovery"])
        self.w.service.retry_artifact_recovery()
        self.recover()
        self.assertTrue(self.status()["ready"])

    def test_readiness_and_api_polling_do_not_touch_s3(self):
        self.recover()
        with patch("arena.api.load_config", return_value=self.cfg):
            app = create_app()
        calls = len(self.s3.requests)
        headers = {"Authorization": "Bearer " + access_token(self.cfg.root)}
        with TestClient(app, base_url="http://127.0.0.1", headers=headers) as client:
            self.assertTrue(client.get("/api/system").json()["artifact_readiness"]["ready"])
            self.assertTrue(client.get(f"/api/comparisons/{self.id}").json()["artifact_readiness"]["ready"])
            self.w.recovery_progress(status="running", phase="leftovers")
            before = self.s.get("comparison", self.id)
            result = client.post(f"/api/comparisons/{self.id}/test-review/approve",
                                 json={"review_id": self.review["id"], "answers": {}})
            self.assertEqual(result.status_code, 409)
            self.assertFalse(result.json()["artifact_readiness"]["ready"])
            self.assertEqual(self.s.get("comparison", self.id), before)
        self.assertEqual(len(self.s3.requests), calls)

    def test_all_saved_code_retry_routes_are_guarded_before_queue_mutations(self):
        self.s.update("comparison", self.id, status="incomplete")
        run = self.s.detail(self.id)["runs"][0]
        self.s.transition(run["id"], "failed")
        before = self.s.detail(self.id)
        for action in (
            lambda: self.w.service.retry(run["id"]),
            lambda: self.w.service.retry_comparison(self.id),
            lambda: self.w.service.reevaluate(self.id),
            lambda: self.w.service.reverify(self.id),
            lambda: self.w.service.start_test_review(self.id),
        ):
            with self.assertRaises(ArtifactsNotReady):
                action()
            self.assertEqual(self.s.detail(self.id), before)

    def test_initial_review_approval_can_continue_during_active_coding(self):
        self.review["status"] = "awaiting_review"
        self.s.update("comparison", self.id, test_review=self.review)
        self.assertFalse(self.status()["ready"])
        self.approve()
        self.assertEqual(self.s.get("comparison", self.id)["test_review"]["status"], "approved")

    def test_active_comparison_work_is_not_a_pending_s3_upload(self):
        self.w.recovery_progress(status="ready", phase="complete", finished_at=now())
        for comparison_status, review_status in (
            ("queued", "queued"), ("preparing", "queued"), ("setup", "queued"),
            ("clarifying", "queued"), ("developing", "drafting"),
            ("waiting_user", "drafting"), ("waiting_approval", "drafting"),
            ("planning", "approved"), ("evaluating", "approved"),
            ("reviewing_tests", "approved"), ("evaluation_queued", "approved"),
            ("verification_queued", "approved"), ("test_review_queued", "queued"),
            ("waiting_test_review", "queued"),
            ("waiting_test_review", "drafting"), ("waiting_test_review", "approved"),
        ):
            with self.subTest(comparison=comparison_status, review=review_status):
                self.s.update("comparison", self.id, status=comparison_status,
                              test_review={**self.review, "status": review_status})
                state = self.status()
                self.assertEqual(state["status"], "in_use")
                self.assertFalse(state["ready"])
                self.assertFalse(state["can_retry_recovery"])
                self.assertEqual(state["comparisons_total"], 0)
                self.assertTrue(readiness(self.w.storage)["ready"])
                self.assertTrue(self.root.exists())
                with self.assertRaises(ArtifactsNotReady):
                    with retry_gate(self.w.storage, self.id):
                        self.fail("Active comparison must not be retried")
        self.assertEqual(self.s3.requests, [])
        # Once the proposal is available, the stopped work really does need
        # archiving. Readiness must wait for upload, verification and eviction.
        self.s.update("comparison", self.id, status="waiting_test_review",
                      test_review={**self.review, "status": "awaiting_review"})
        self.assertEqual(self.status()["status"], "pending")
        self.assertEqual(self.status()["comparisons_total"], 1)
        self.recover()
        self.assertTrue(self.status()["ready"])
        self.assertFalse(self.root.exists())

    def test_active_proposal_does_not_hide_other_recovery_work_or_worker_failure(self):
        self.w.recovery_progress(status="ready", phase="complete", finished_at=now())
        self.s.update("comparison", self.id,
                      test_review={**self.review, "status": "drafting"})
        prior = self.w.service.create("Stopped comparison")
        self.s.update("comparison", prior["id"], status="incomplete")
        self.assertEqual(self.status()["status"], "pending")
        self.assertEqual(self.status()["comparisons_total"], 1)
        self.w.storage._save("extra-test", category="artifacts", status="upload_failed",
                             error="Controlled storage failure")
        self.assertEqual(self.status()["status"], "failed")
        self.assertIn("Controlled storage failure", self.status()["error"])
        self.s.update("system", "worker", heartbeat=now()-20)
        self.assertEqual(self.status()["status"], "offline")

    def test_local_only_mode_does_not_require_s3_or_worker_readiness(self):
        self.w.storage.enabled = False
        self.assertTrue(self.status()["ready"])
        self.approve()


class MultipartProgressTests(unittest.TestCase):
    def test_progress_only_counts_checksum_confirmed_parts(self):
        s3, progress = FakeS3(), []
        writer = MultipartWriter(s3, "bucket", "key", lambda _: None,
                                 lambda size, parts: progress.append((size, parts)))
        writer.part_size = 4
        writer.write(b"1234")
        self.assertEqual(progress, [(4, 1)])
        original = s3.upload_part
        def bad_checksum(**args):
            return {**original(**args), "ChecksumSHA256": "wrong"}
        s3.upload_part = bad_checksum
        with self.assertRaisesRegex(Exception, "checksum"):
            writer.write(b"5678")
        self.assertEqual(progress, [(4, 1)])


if __name__ == "__main__":
    unittest.main()
