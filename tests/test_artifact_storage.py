# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Storage boundary tests with real tar/checksum/filesystem/SQLite behavior."""
import asyncio
import base64
import hashlib
import io
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from arena.artifact_storage import ArtifactStorage, MultipartWriter, StorageError, digest64, remove_tree
from arena.config import Config
from arena.service import Service
from arena.store import Store
from arena.worker import Worker
from arena.evaluation import Plan, Check, Criterion
from arena.isolation import tree_hash


class FakeS3:
    def __init__(self):
        self.objects, self.uploads, self.checksums = {}, {}, {}
        self.fail = None
        self.requests = []
        self.serial = 0

    def check(self, name, args):
        self.requests.append((name, args.get("Key")))
        if self.fail == name:
            raise OSError("Injected " + name + " failure")

    def create_multipart_upload(self, **args):
        self.check("create", args)
        self.serial += 1
        upload = str(self.serial)
        self.uploads[upload] = {}
        return {"UploadId": upload}

    def upload_part(self, **args):
        self.check("part", args)
        assert digest64(args["Body"]) == args["ChecksumSHA256"]
        self.uploads[args["UploadId"]][args["PartNumber"]] = args["Body"]
        return {"ETag": str(args["PartNumber"]), "ChecksumSHA256": args["ChecksumSHA256"]}

    def complete_multipart_upload(self, **args):
        self.check("complete", args)
        parts = self.uploads.pop(args["UploadId"])
        values = [parts[n] for n in sorted(parts)]
        self.objects[args["Bucket"], args["Key"]] = b"".join(values)
        self.checksums[args["Bucket"], args["Key"]] = (
            digest64(b"".join(hashlib.sha256(p).digest() for p in values)) + "-" + str(len(values)))
        return {}

    def head_object(self, **args):
        self.check("head", args)
        key = args["Bucket"], args["Key"]
        return {"ContentLength": len(self.objects[key]), "ChecksumSHA256": self.checksums[key]}

    def put_object(self, **args):
        self.check("put", args)
        assert digest64(args["Body"]) == args["ChecksumSHA256"]
        key = args["Bucket"], args["Key"]
        self.objects[key], self.checksums[key] = args["Body"], args["ChecksumSHA256"]
        return {"ChecksumSHA256": args["ChecksumSHA256"]}

    def get_object(self, **args):
        self.check("get", args)
        value = self.objects[args["Bucket"], args["Key"]]
        if args.get("Range"):
            start, end = map(int, args["Range"][6:].split("-"))
            value = value[start:end + 1]
        return {"Body": io.BytesIO(value)}

    def abort_multipart_upload(self, **args):
        self.check("abort", args)
        if args["UploadId"] not in self.uploads:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchUpload"}}, "AbortMultipartUpload")
        self.uploads.pop(args["UploadId"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                          artifact_s3_uri="s3://test-bucket/output/mchc/")
        self.store = Store(self.cfg.root)
        self.s3 = FakeS3()
        self.storage = ArtifactStorage(self.cfg, self.store, self.s3)
        self.root = self.storage.root("comparison")
        self.root.mkdir(parents=True)
        self.write("baseline/main.py", "print('baseline')\n")
        self.write("runs/r/artifacts/v1/snapshot/main.py", "print('candidate')\n")
        self.write("runs/r/artifacts/v1/implementation.patch", "+print('candidate')\n")
        self.write("runs/r/artifacts/v1/case.json", '{"revision": 1}')

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def archive(self):
        with self.storage.lock("comparison"):
            return self.storage.archive("comparison")

    def test_round_trip_permissions_long_unicode_names_and_history(self):
        name = "runs/r/artifacts/v1/snapshot/" + "long_" * 40 + "é.py"
        path = self.write(name, "v1")
        path.chmod(0o444)
        self.write("runs/r/home/history.json", '{"session":"existing"}')
        result = self.archive()
        self.assertFalse(self.root.exists())
        self.assertEqual(result["status"], "archived")
        self.assertEqual(self.storage.read_file("comparison", path), b"v1")
        self.storage.restore("comparison")
        self.assertEqual(path.read_text(), "v1")
        self.assertEqual(path.stat().st_mode & 0o777, 0o444)
        self.assertIn("existing", (self.root / "runs/r/home/history.json").read_text())

    def test_multipart_is_verified_and_no_tar_is_written_locally(self):
        self.write("large.txt", "x" * 50000)
        with patch.object(MultipartWriter, "part_size", 8192):
            result = self.archive()
        self.assertGreater(int(result["archive"]["checksum"].split("-")[-1]), 1)
        self.assertFalse(list(self.cfg.root.rglob("*.tar")))
        self.storage.restore("comparison")
        self.assertEqual((self.root / "large.txt").stat().st_size, 50000)

    def test_remote_file_listing_and_patch_do_not_restore_tree(self):
        snapshot = self.root / "runs/r/artifacts/v1/snapshot"
        self.archive()
        self.assertEqual(self.storage.files("comparison", snapshot), ["main.py"])
        self.assertIn(b"candidate", self.storage.read_file(
            "comparison", self.root / "runs/r/artifacts/v1/implementation.patch"))
        self.assertFalse(self.root.exists())

    def test_all_upload_boundary_failures_keep_original_files(self):
        for boundary in ("create", "part", "complete", "head", "put"):
            with self.subTest(boundary=boundary):
                self.s3.fail = boundary
                with self.assertRaises(OSError):
                    self.archive()
                self.assertTrue((self.root / "baseline/main.py").exists())
                self.assertNotEqual(self.storage.state("comparison")["status"], "archived")
                self.s3.fail = None
        self.archive()
        self.assertFalse(self.root.exists())

    def test_bad_s3_checksum_prevents_deletion(self):
        original = self.s3.head_object
        with patch.object(self.s3, "head_object",
                          side_effect=lambda **kw: {**original(**kw), "ChecksumSHA256": "bad"}):
            with self.assertRaisesRegex(StorageError, "verification"):
                self.archive()
        self.assertTrue(self.root.exists())

    def test_corrupt_manifest_or_data_refuses_restore(self):
        state = self.archive()
        key = state["bucket"], state["manifest_key"]
        saved = self.s3.objects[key]
        self.s3.objects[key] = b"{}"
        with self.assertRaisesRegex(StorageError, "manifest checksum"):
            self.storage.restore("comparison")
        self.assertFalse(self.root.exists())
        self.s3.objects[key] = saved
        data_key = state["bucket"], state["archive"]["key"]
        value = bytearray(self.s3.objects[data_key])
        entry = next(e for e in self.storage.manifest("comparison")["entries"] if e["type"] == "file")
        value[entry["offset"]] ^= 1
        self.s3.objects[data_key] = bytes(value)
        with self.assertRaisesRegex(StorageError, "checksum"):
            self.storage.restore("comparison")
        self.assertFalse(self.root.exists())
        self.assertFalse((self.root.parent / ".restore-comparison").exists())

    def test_cleanup_crash_reuses_full_committed_archive(self):
        real_remove = remove_tree
        def partial_delete(root):
            (root / "baseline/main.py").unlink()
            raise OSError("crash during cleanup")
        with patch("arena.artifact_storage.remove_tree", side_effect=partial_delete):
            with self.assertRaisesRegex(OSError, "cleanup"):
                self.archive()
        state = self.storage.state("comparison")
        self.assertEqual(state["status"], "evicting")
        self.archive()
        self.assertEqual(self.storage.state("comparison")["manifest_key"], state["manifest_key"])
        self.storage.restore("comparison")
        self.assertEqual((self.root / "baseline/main.py").read_text(), "print('baseline')\n")

    def test_mutation_during_upload_is_not_evicted(self):
        original = self.s3.complete_multipart_upload
        def mutate(**args):
            self.write("new-file.txt", "concurrent write")
            return original(**args)
        with patch.object(self.s3, "complete_multipart_upload", side_effect=mutate):
            with self.assertRaisesRegex(StorageError, "changed during upload"):
                self.archive()
        self.assertTrue((self.root / "new-file.txt").exists())

    def test_runtime_credentials_and_external_links_never_uploaded(self):
        private = self.write("runs/r/home/.codex/auth.json", "secret-value")
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("external-secret")
        link = self.root / "external"
        link.symlink_to(outside)
        self.archive()
        self.assertFalse(any(b"secret-value" in v or b"external-secret" in v for v in self.s3.objects.values()))
        self.storage.restore("comparison")
        self.assertEqual(private.read_text(), "secret-value")
        self.assertEqual(link.read_text(), "external-secret")
        self.assertEqual(outside.read_text(), "external-secret")
        with self.assertRaises(StorageError):
            self.storage.read_file("comparison", link)

    def test_internal_symlink_round_trip_and_traversal_refused(self):
        (self.root / "linked.py").symlink_to("baseline/main.py")
        self.archive()
        self.storage.restore("comparison")
        self.assertEqual(os.readlink(self.root / "linked.py"), "baseline/main.py")
        with self.assertRaises(StorageError):
            self.storage.read_file("comparison", self.root / "../arena.sqlite3")

    def test_manifest_path_escape_refused_even_with_updated_index_digest(self):
        state = self.archive()
        key = state["bucket"], state["manifest_key"]
        manifest = json.loads(self.s3.objects[key])
        # Runtime entries are restored after tar members; validate them too.
        manifest["entries"].append({"name": "../outside", "type": "runtime", "mode": 0o600})
        raw = json.dumps(manifest).encode()
        self.s3.objects[key] = raw
        self.storage._save("comparison", manifest_sha256=hashlib.sha256(raw).hexdigest())
        with self.assertRaisesRegex(StorageError, "Unsafe"):
            self.storage.restore("comparison")
        self.assertFalse((self.cfg.root / "outside").exists())

    def test_new_generation_preserves_original_and_restores_latest(self):
        first = self.archive()
        self.storage.restore("comparison")
        self.write("runs/r/artifacts/v2/case.json", '{"revision":2}')
        second = self.archive()
        self.assertNotEqual(first["manifest_key"], second["manifest_key"])
        self.assertIn((first["bucket"], first["archive"]["key"]), self.s3.objects)
        self.storage.restore("comparison")
        self.assertTrue((self.root / "runs/r/artifacts/v1/case.json").exists())
        self.assertTrue((self.root / "runs/r/artifacts/v2/case.json").exists())

    def test_default_destination_and_invalid_empty_uri(self):
        # No bucket is assumed: ARTIFACT_S3_URI comes from deploy.properties.
        self.assertIsNone(Config().artifact_s3_uri)
        with self.assertRaises(ValueError):
            Config(artifact_s3_uri="")

    def test_index_failure_retains_every_local_file(self):
        original = self.s3.put_object
        def fail_index(**args):
            if args["Key"].endswith("/latest.json"):
                raise OSError("index unavailable")
            return original(**args)
        with patch.object(self.s3, "put_object", side_effect=fail_index):
            with self.assertRaisesRegex(OSError, "index unavailable"):
                self.archive()
        self.assertTrue((self.root / "baseline/main.py").exists())
        self.archive()
        self.assertFalse(self.root.exists())

    def test_interrupted_restore_never_archives_partial_download(self):
        state = self.archive()
        staging = self.root.parent / ".restore-comparison"
        staging.mkdir()
        (staging / "partial").write_text("not complete")
        self.storage._save("comparison", status="restoring")
        self.archive()
        self.assertFalse(staging.exists())
        self.assertEqual(self.storage.state("comparison")["manifest_key"], state["manifest_key"])
        self.storage.restore("comparison")
        self.assertTrue((self.root / "baseline/main.py").exists())

    def test_kiro_sessions_are_remote_and_authentication_stays_local(self):
        database = self.root / "runs/r/home/.local/share/kiro-cli/data.sqlite3"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as db:
            db.executescript("""
                CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE conversations_v2 (conversation_id TEXT PRIMARY KEY, value TEXT);
                INSERT INTO auth_kv VALUES ('token', 'unique-secret-token');
                INSERT INTO conversations_v2 VALUES ('saved-session', 'unique-conversation-text');
            """)
        self.archive()
        self.assertFalse(any(b"unique-secret-token" in v for v in self.s3.objects.values()))
        self.assertTrue(any(b"unique-conversation-text" in v for v in self.s3.objects.values()))
        vault = self.storage.control / "runtime-vault/comparison" / database.relative_to(self.root)
        self.assertNotIn(b"unique-conversation-text", vault.read_bytes())
        self.storage.restore("comparison")
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT value FROM auth_kv").fetchone()[0], "unique-secret-token")
            self.assertEqual(db.execute("SELECT conversation_id FROM conversations_v2").fetchone()[0], "saved-session")
        self.archive()
        remove_tree(self.storage.control / "runtime-vault")
        self.storage.restore("comparison")
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_kv").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT conversation_id FROM conversations_v2").fetchone()[0], "saved-session")

    def test_external_file_and_directory_eviction_retry(self):
        for name, directory in (("old-report.json", False), ("old-output", True)):
            path = Path(self.temp.name) / name
            if directory:
                path.mkdir()
                (path / "result.txt").write_text("preserve")
            else:
                path.write_text("preserve")
            self.s3.fail = "part"
            with self.assertRaises(OSError):
                self.storage.archive_path(path)
            self.assertTrue(path.exists())
            self.s3.fail = None
            state = self.storage.archive_path(path)
            self.assertFalse(path.exists())
            self.assertIn("/artifacts/extra-", state["archive"]["key"])
            self.assertEqual(self.storage.archive_path(path)["manifest_key"], state["manifest_key"])


class WorkerStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(data_dir=str(Path(self.temp.name) / "data"),
                          artifact_s3_uri="s3://test-bucket/output/mchc")
        self.worker = Worker(self.cfg)
        self.worker.storage._client = FakeS3()
        self.service = self.worker.service
        self.c = self.service.create("Build a CLI")
        self.id = self.c["id"]
        self.root = self.worker.storage.root(self.id)
        self.worker.active[self.id] = None

    def ready_for_retry(self, worker=None):
        # These fixtures call worker operations directly instead of serve().
        # Establish the same completed recovery and fresh worker identity.
        from arena.store import now
        worker = worker or self.worker
        worker.store.put("system", {"id": "worker", "pid": os.getpid(), "heartbeat": now(),
                                    "instance_id": worker.instance_id})
        worker.recover_artifacts()

    async def test_completion_requires_upload_and_eviction(self):
        async def operation(id):
            (self.root / "output.txt").write_text("completed")
            self.worker.finish_state(id, "complete")
            self.assertEqual(self.worker.store.get("comparison", id)["status"], "archiving")
            self.assertTrue(self.root.exists())
        await self.worker.stored_operation(self.id, operation)
        self.assertEqual(self.worker.store.get("comparison", self.id)["status"], "complete")
        self.assertFalse(self.root.exists())

    async def test_failed_upload_cannot_be_reported_complete_retry_never_reruns_code(self):
        self.worker.storage.client.fail = "part"
        operation = AsyncMock(side_effect=lambda id: self.worker.finish_state(id, "complete"))
        await self.worker.stored_operation(self.id, operation)
        self.assertEqual(self.worker.store.get("comparison", self.id)["status"], "storage_failed")
        self.assertTrue(self.root.exists())
        self.worker.storage.client.fail = None
        self.service.retry_archive(self.id)
        await self.worker.stored_operation(self.id, operation, archive_only=True)
        self.assertEqual(operation.await_count, 1)
        self.assertEqual(self.worker.store.get("comparison", self.id)["status"], "complete")
        self.assertFalse(self.root.exists())

    async def test_resume_restores_workspace_baseline_and_session(self):
        run = self.c["runs"][0]
        root = self.worker.run_root(run)
        (root / "workspace").mkdir(parents=True)
        (root / "workspace/main.py").write_text("existing implementation")
        self.worker.store.update("run", run["id"], session_id="same-session")
        self.worker.finish_state(self.id, "interrupted")
        self.worker.archive_comparison(self.id)
        self.ready_for_retry()
        self.service.retry_comparison(self.id)
        async def resumed(id):
            self.assertEqual((root / "workspace/main.py").read_text(), "existing implementation")
            self.assertTrue((self.root / "baseline").exists())
            self.assertEqual(self.worker.store.get("run", run["id"])["session_id"], "same-session")
            self.worker.finish_state(id, "incomplete")
        await self.worker.stored_operation(self.id, resumed)
        self.assertEqual(self.worker.store.get("comparison", self.id)["status"], "incomplete")
        self.assertFalse(self.root.exists())

    async def test_prior_leftovers_uploaded_before_next_operation(self):
        old = self.service.create("Prior comparison")
        self.worker.store.update("comparison", old["id"], status="incomplete")
        oldroot = self.worker.storage.root(old["id"])
        (oldroot / "leftover.txt").write_text("recover me")
        async def operation(id):
            self.assertFalse(oldroot.exists())
            self.assertEqual(self.worker.storage.state(old["id"])["status"], "archived")
            self.worker.finish_state(id, "complete")
        await self.worker.stored_operation(self.id, operation)
        self.assertEqual(self.worker.store.get("comparison", self.id)["status"], "complete")

    async def test_storage_failure_on_prior_run_prevents_new_execution(self):
        old = self.service.create("Prior comparison")
        self.worker.store.update("comparison", old["id"], status="incomplete")
        self.worker.storage.client.fail = "part"
        operation = AsyncMock()
        await self.worker.stored_operation(self.id, operation)
        operation.assert_not_awaited()
        self.assertTrue(self.worker.storage.root(old["id"]).exists())
        self.assertTrue(self.root.exists())

    async def test_operation_failure_is_archived_and_not_misreported_as_storage_failure(self):
        async def operation(id):
            (self.root / "partial.txt").write_text("partial work")
            raise RuntimeError("coding failed")
        await self.worker.stored_operation(self.id, operation)
        result = self.worker.store.get("comparison", self.id)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["error"], "coding failed")
        self.assertFalse(self.root.exists())
        self.assertEqual(self.worker.storage.read_file(self.id, self.root / "partial.txt"), b"partial work")

    async def test_metadata_can_be_imported_from_s3_with_no_local_comparison_files(self):
        async def operation(id):
            (self.root / "output.txt").write_text("saved")
            self.worker.finish_state(id, "incomplete")
        await self.worker.stored_operation(self.id, operation)
        # Recreate both the database and artifact storage in a different root.
        config = self.cfg.model_copy(update={"data_dir": str(Path(self.temp.name) / "fresh")})
        fresh = Worker(config)
        fresh.storage._client = self.worker.storage.client
        imported = fresh.service.report(self.id)
        self.assertEqual(imported["prompt"], self.c["prompt"])
        self.assertEqual(len(imported["runs"]), 3)
        self.assertFalse(fresh.storage.root(self.id).exists())
        self.assertTrue(fresh.store.events(self.id))
        self.ready_for_retry(fresh)
        fresh.service.retry_comparison(self.id)
        async def resumed(id):
            self.assertEqual((fresh.storage.root(id) / "output.txt").read_text(), "saved")
            # Deliverables live outside the comparison root, so they survive it.
            self.assertEqual(fresh.store.detail(id)["project_directory"],
                             str(config.root.parent / "projects" / id))
            fresh.finish_state(id, "incomplete")
        await fresh.stored_operation(self.id, resumed)
        self.assertFalse(fresh.storage.root(self.id).exists())
        self.assertEqual(fresh.store.get("comparison", self.id)["status"], "incomplete")

    async def test_unrelated_artifacts_and_orphan_creation_are_recovered_before_execution(self):
        extra = Path(self.temp.name) / "deliveries"
        extra.mkdir()
        (extra / "report").write_text("historic")
        self.cfg.artifact_paths = ["deliveries"]
        orphan = self.root.parent / "never-queued"
        orphan.mkdir()
        (orphan / "partial").write_text("creation interrupted")
        async def operation(id):
            self.assertFalse(extra.exists())
            self.assertFalse(orphan.exists())
            self.worker.finish_state(id, "complete")
        await self.worker.stored_operation(self.id, operation)
        self.assertEqual(self.worker.store.get("comparison", self.id)["status"], "complete")
        self.assertTrue(any(b"creation interrupted" in v for v in self.worker.storage.client.objects.values()))

    async def test_each_harness_publishes_its_code_into_the_selected_project_folder(self):
        from arena.isolation import tree_hash
        project = Path(self.temp.name) / "prj"
        project.mkdir()
        self.worker.store.update("comparison", self.id, project_directory=str(project))
        for run in self.c["runs"]:
            snapshot = self.root / "runs" / run["id"] / "artifacts" / "snapshot"
            snapshot.mkdir(parents=True)
            (snapshot / "weather_agent.py").write_text(f"# {run['harness']} implementation\n")
            self.worker.store.update("run", run["id"], artifacts={
                "snapshot": str(snapshot), "sha256": tree_hash(snapshot)})
            self.service.publish_run_output(run["id"])
        # Enabling S3 must not divert deliverables away from the chosen folder.
        for run in self.c["runs"]:
            harness = run["harness"]
            published = project / harness / "weather_agent.py"
            self.assertTrue(published.exists(), harness)
            self.assertEqual(published.read_text(), f"# {harness} implementation\n")
            self.assertEqual(self.worker.store.get("run", run["id"])["project_output"]["status"],
                             "published")
        async def operation(id):
            self.worker.finish_state(id, "complete")
        await self.worker.stored_operation(self.id, operation)
        self.assertFalse(self.root.exists())
        for run in self.c["runs"]:
            self.assertTrue((project / run["harness"] / "weather_agent.py").exists(),
                            run["harness"])

    async def test_scratch_is_archived_while_project_deliverables_stay_in_the_project(self):
        from arena.native_sandbox import private_tmp
        from arena.project_outputs import prepare_outputs
        project = Path(self.temp.name) / "source"
        project.mkdir()
        (project / "original.py").write_text("original")
        prepare_outputs(project, self.c, self.c["runs"])
        self.worker.store.update("comparison", self.id, project_directory=str(project))
        scratch = private_tmp(self.root / "runs" / self.c["runs"][0]["id"])
        self.addCleanup(lambda: remove_tree(scratch))
        (scratch / "results.txt").write_text("scratch evidence")
        async def operation(id):
            self.worker.finish_state(id, "complete")
        await self.worker.stored_operation(self.id, operation)
        self.assertFalse(scratch.exists())
        # The chosen project folder is the deliverable location: archiving the
        # comparison must not empty it. The run snapshot is the archived copy.
        for harness in ("claude", "codex", "kiro"):
            self.assertTrue((project / harness).exists(), harness)
        self.assertEqual((project / "original.py").read_text(), "original")
        self.assertFalse(self.root.exists())
        self.assertTrue(any(b"scratch evidence" in v for v in self.worker.storage.client.objects.values()))
        self.ready_for_retry()
        self.service.retry_comparison(self.id)
        async def resumed(id):
            self.assertEqual((scratch / "results.txt").read_text(), "scratch evidence")
            self.worker.finish_state(id, "complete")
        await self.worker.stored_operation(self.id, resumed)
        self.assertFalse(scratch.exists())
        self.assertFalse(self.root.exists())

    async def test_rotated_logs_are_archived_before_completion(self):
        from arena.service_logs import ArtifactLog
        log = ArtifactLog(self.cfg.root, "worker", max_bytes=1)
        async def operation(id):
            log.write("first segment\n")
            log.write("current segment\n")
            self.worker.finish_state(id, "complete")
        await self.worker.stored_operation(self.id, operation)
        self.assertEqual((self.cfg.root / "worker.log").read_text(), "current segment\n")
        self.assertFalse(list((self.cfg.root / "logs").glob("*.log")))
        self.assertTrue(any(b"first segment" in v for v in self.worker.storage.client.objects.values()))

    async def test_migration_refuses_application_source_and_runtime(self):
        for path in (Path(__file__).parent.parent / "arena", self.worker.store.path,
                     self.cfg.root / "ui-token", self.cfg.root):
            with self.subTest(path=path):
                self.cfg.artifact_paths = [str(path)]
                with self.assertRaisesRegex(StorageError, "runtime or source"):
                    self.worker.recover_extra_artifacts()
                self.assertTrue(self.worker.store.path.exists())

    async def test_cancellation_drains_writers_before_archival(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def operation(id):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                (self.root / "last-write.txt").write_text("writer drained")
                stopped.set()
        job = asyncio.create_task(self.worker.stored_operation(self.id, operation))
        await started.wait()
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.root.exists())
        self.assertEqual(self.worker.storage.read_file(self.id, self.root / "last-write.txt"), b"writer drained")

    def saved_candidates(self):
        frozen = self.worker.freeze_requirements(self.id)
        plan = Plan(criteria=[Criterion(id="cli", requirement="CLI works", check_ids=["cli"])],
                    checks=[Check(id="cli", group="acceptance", command="python main.py")],
                    excluded_groups={"project": "No build", "candidate": "No extra suite", "regression": "Empty baseline"})
        self.worker.store.update("comparison", self.id, test_review=None, plan=plan.model_dump())
        for run in self.c["runs"]:
            directory = self.worker.run_root(run) / "artifacts/v1"
            snapshot = directory / "snapshot"
            snapshot.mkdir(parents=True)
            (snapshot / "main.py").write_text("print('hello')")
            (directory / "implementation.patch").write_text("+print('hello')")
            sha = tree_hash(snapshot)
            (directory / "case.json").write_text(json.dumps({
                "plan": plan.model_dump(), "candidate": {"snapshot_sha256": sha, "evidence": {}}}))
            self.worker.store.transition(run["id"], "verified_complete",
                artifacts={"snapshot": str(snapshot), "sha256": sha,
                           "case": str(directory / "case.json"), "patch": str(directory / "implementation.patch")},
                evaluation={"overall": 100, "verified_complete": True})
        self.worker.finish_state(self.id, "complete")
        self.worker.archive_comparison(self.id)

    async def test_real_evaluation_and_verification_reruns_use_only_s3_saved_candidates(self):
        self.saved_candidates()
        self.assertFalse(self.root.exists())
        first_key = self.worker.storage.state(self.id)["manifest_key"]
        self.worker.phase = AsyncMock(side_effect=AssertionError("Coding must not run"))
        self.worker.score_candidate = AsyncMock(return_value={"overall": 100, "verified_complete": True})
        self.ready_for_retry()
        self.service.reevaluate(self.id)
        await self.worker.stored_operation(self.id, self.worker.reevaluate)
        self.assertEqual(self.worker.store.get("comparison", self.id)["status"], "complete")
        self.assertFalse(self.root.exists())
        self.assertEqual(self.worker.score_candidate.await_count, 3)
        self.assertNotEqual(self.worker.storage.state(self.id)["manifest_key"], first_key)
        self.ready_for_retry()
        self.service.reverify(self.id)
        with patch("arena.worker.CandidateTestRunner.run", new=AsyncMock(return_value={"checks": []})) as checks:
            await self.worker.stored_operation(self.id, self.worker.reverify)
        self.assertEqual(checks.await_count, 4)
        self.assertEqual(self.worker.score_candidate.await_count, 6)
        self.worker.phase.assert_not_awaited()
        self.assertEqual(self.worker.store.get("comparison", self.id)["status"], "complete")
        self.assertFalse(self.root.exists())
        for run in self.worker.store.detail(self.id)["runs"]:
            self.assertTrue(run["artifact_history"])
            self.assertIn(b"verification_revision", self.worker.storage.read_file(self.id, Path(run["artifacts"]["case"])))

    async def test_file_patch_and_report_endpoints_work_after_eviction(self):
        from fastapi.testclient import TestClient
        from arena.api import create_app
        from arena.auth import access_token
        self.saved_candidates()
        with patch("arena.api.load_config", return_value=self.cfg):
            app = create_app()
        app.state.service.storage._client = self.worker.storage.client
        client = TestClient(app, base_url="http://127.0.0.1",
                            headers={"Authorization": "Bearer " + access_token(self.cfg.root)})
        run = self.c["runs"][0]
        self.assertEqual(client.get(f"/api/runs/{run['id']}/files").json()["files"], ["main.py"])
        self.assertEqual(client.get(f"/api/runs/{run['id']}/file", params={"path": "main.py"}).json()["content"],
                         "print('hello')")
        self.assertEqual(client.get(f"/api/runs/{run['id']}/patch").text, "+print('hello')")
        report = client.get(f"/api/comparisons/{self.id}/report").json()
        self.assertEqual(report["artifact_storage"]["status"], "archived")
        self.assertTrue(report["runs"][0]["artifact_storage"]["archive_uri"].startswith("s3://test-bucket/"))
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
