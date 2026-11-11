# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Run the existing scheduler scenarios with real S3 archive/hydration boundaries."""
import test_queue_review as queue
from test_artifact_storage import FakeS3
from arena.artifact_storage import ArtifactStorage
from arena.artifact_readiness import readiness


class ArchivedQueueTests(queue.QueueTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cfg.artifact_s3_uri = "s3://test-bucket/output/codecouncil/"
        self.w.storage = ArtifactStorage(self.cfg, self.s, FakeS3())
        self.w.service.storage = self.w.storage

    async def test_explicit_storage_recovery_request_runs_without_retrying_comparison(self):
        old = self.waiting_comparison()
        self.w.storage.client.fail = "part"
        self.start()
        await self.wait(lambda: self.s.get("system", "artifact-recovery")["status"] == "failed"
                        if any(x["id"] == "artifact-recovery" for x in self.s.list("system")) else False)
        self.assertFalse(readiness(self.w.storage, old["id"])["ready"])
        self.w.storage.client.fail = None
        self.w.service.retry_artifact_recovery()
        await self.wait(lambda: readiness(self.w.storage, old["id"])["ready"])
        self.assertEqual(self.s.get("comparison", old["id"])["test_review"], old["test_review"])
        self.assertEqual(self.calls[old["id"], "develop"], 0)
        self.assertEqual(self.calls[old["id"], "generation"], 0)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        for c in self.s.list("comparison"):
            if c["status"] in ("complete", "incomplete", "waiting_test_review", "interrupted"):
                # The synchronous capacity predicate test doesn't start a worker.
                if self.server:
                    self.assertFalse(self.w.storage.root(c["id"]).exists(), c["id"])
