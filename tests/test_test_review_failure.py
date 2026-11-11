# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""S1 + S2: test-review attempt endpoint and failure block in comparison JSON."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from arena.api import create_app
from arena.artifact_storage import ArtifactStorage, StorageError
from arena.auth import access_token
from arena.config import Config
from arena.evaluation import PLANNING_STALL_SECONDS
from arena.service import Service
from arena.store import Store


def make_service(s3_uri=None):
    tmp = tempfile.mkdtemp(prefix="mhc-trfail-")
    cfg = Config(data_dir=tmp, artifact_s3_uri=s3_uri)
    return Service(cfg), tmp


def _set_review(svc, cid, **fields):
    with svc.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT data FROM objects WHERE kind='comparison' AND id=?", (cid,)).fetchone()
        c = json.loads(row[0])
        c["test_review"].update(fields)
        db.execute("UPDATE objects SET data=? WHERE kind='comparison' AND id=?", (json.dumps(c), cid))


def _write_attempt_artifact(root: Path, cid: str, review_id: str, uid: str, payload: dict) -> str:
    d = root / "comparisons" / cid / "test-reviews" / review_id / uid
    d.mkdir(parents=True, exist_ok=True)
    p = d / "attempt-1.json"
    p.write_text(json.dumps(payload))
    return str(p)


class FailureBlockTests(unittest.TestCase):
    """S2: comparison JSON test_review.failure field."""

    def setUp(self):
        self.svc, _ = make_service()
        self.c = self.svc.create("Build a greeting CLI.")
        self.cid = self.c["id"]

    def _report(self):
        return self.svc.report(self.cid)

    def test_no_failure_when_status_is_queued(self):
        report = self._report()
        self.assertNotIn("failure", report["test_review"])

    def test_generation_failed_adds_failure_block(self):
        _set_review(self.svc, self.cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Approved tests could not be developed: something went wrong")
        report = self._report()
        f = report["test_review"].get("failure")
        self.assertIsNotNone(f)
        self.assertEqual(f["stage"], "test development")
        self.assertEqual(f["error"], "Approved tests could not be developed: something went wrong")
        self.assertIsNone(f["attempt"])
        self.assertIsNone(f["attempt_url"])

    def test_proposal_failed_adds_failure_block_with_stage(self):
        _set_review(self.svc, self.cid,
                    status="proposal_failed",
                    error="Test proposal failed: timeout")
        report = self._report()
        f = report["test_review"]["failure"]
        self.assertEqual(f["stage"], "test proposal")

    def test_cause_read_timeout_mentions_stall_seconds_model_effort(self):
        _set_review(self.svc, self.cid,
                    status="generation_failed",
                    generation_status="failed",
                    error=("Approved tests could not be developed: Acceptance planning failed: "
                           "AWSHTTPSConnectionPool(host='bedrock-runtime.us-east-1.amazonaws.com', "
                           "port=443): Read timed out."),
                    generation_attempts=[{
                        "model": "us.anthropic.claude-opus-5",
                        "effort": "max",
                        "max_tokens": 100000,
                        "started_at": 1792501395.64,
                        "ended_at": 1792501697.09,
                        "artifact": None,
                    }])
        report = self._report()
        f = report["test_review"]["failure"]
        cause = f["cause"]
        self.assertIn(str(PLANNING_STALL_SECONDS), cause)
        self.assertIn("us.anthropic.claude-opus-5", cause)
        self.assertIn("max", cause)

    def test_cause_throttling(self):
        _set_review(self.svc, self.cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Bedrock ThrottlingException: Too Many Requests")
        f = self._report()["test_review"]["failure"]
        self.assertIn("throttl", f["cause"].lower())

    def test_cause_access_denied(self):
        _set_review(self.svc, self.cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="AccessDeniedException: User is not authorized")
        f = self._report()["test_review"]["failure"]
        self.assertIn("access denied", f["cause"].lower())

    def test_cause_max_tokens(self):
        _set_review(self.svc, self.cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Acceptance planning reached the configured output limit")
        f = self._report()["test_review"]["failure"]
        self.assertIn("output token limit", f["cause"].lower())

    def test_cause_unknown_returns_first_line(self):
        _set_review(self.svc, self.cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Some unexpected error\nwith more detail on the next line")
        f = self._report()["test_review"]["failure"]
        self.assertEqual(f["cause"], "Some unexpected error")

    def test_attempt_summary_present_when_attempts_recorded(self):
        _set_review(self.svc, self.cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Approved tests could not be developed: Acceptance planning failed: Read timed out.",
                    generation_attempts=[{
                        "model": "us.anthropic.claude-opus-5",
                        "effort": "max",
                        "max_tokens": 100000,
                        "started_at": 1792501395.64,
                        "ended_at": 1792501697.09,
                        "artifact": None,
                    }])
        f = self._report()["test_review"]["failure"]
        a = f["attempt"]
        self.assertEqual(a["index"], 1)
        self.assertEqual(a["model"], "us.anthropic.claude-opus-5")
        self.assertEqual(a["effort"], "max")
        self.assertEqual(a["max_tokens"], 100000)
        self.assertAlmostEqual(a["duration_seconds"], 301.5, delta=0.5)

    def test_attempt_url_present_when_artifact_recorded(self):
        svc, tmp = make_service()
        c = svc.create("Build a greeting CLI.")
        cid = c["id"]
        review_id = c["test_review"]["id"]
        artifact_path = str(Path(tmp) / "comparisons" / cid / "test-reviews" / review_id / "u1" / "attempt-1.json")
        _set_review(svc, cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Read timed out.",
                    generation_attempts=[{
                        "model": "us.anthropic.claude-opus-5",
                        "effort": "max",
                        "max_tokens": 100000,
                        "started_at": 1.0,
                        "ended_at": 301.0,
                        "artifact": artifact_path,
                    }])
        f = svc.report(cid)["test_review"]["failure"]
        self.assertEqual(f["attempt_url"], f"/api/comparisons/{cid}/test-review/attempts/1")

    def test_no_failure_for_approved_status(self):
        _set_review(self.svc, self.cid, status="approved")
        self.assertNotIn("failure", self._report()["test_review"])


class AttemptEndpointTests(unittest.TestCase):
    """S1: GET /api/comparisons/{id}/test-review/attempts/{index}."""

    def _client(self, svc):
        cfg = svc.config
        with patch("arena.api.load_config", return_value=cfg):
            app = create_app()
        app.state.service = svc
        token = access_token(cfg.root)
        return TestClient(app, base_url="http://127.0.0.1",
                          headers={"Authorization": f"Bearer {token}"}), token

    def test_locally_present_record(self):
        svc, tmp = make_service()
        c = svc.create("Build a greeting CLI.")
        cid = c["id"]
        review_id = c["test_review"]["id"]
        payload = {"attempt": {"index": 1}, "generated": {"checks": []},
                   "review_id": review_id, "model": "claude-opus", "effort": "max"}
        artifact_path = _write_attempt_artifact(Path(tmp), cid, review_id, "u1", payload)
        _set_review(svc, cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Read timed out.",
                    generation_attempts=[{
                        "model": "us.anthropic.claude-opus-5",
                        "effort": "max",
                        "max_tokens": 100000,
                        "started_at": 1.0,
                        "ended_at": 301.0,
                        "artifact": artifact_path,
                    }])
        client, _ = self._client(svc)
        resp = client.get(f"/api/comparisons/{cid}/test-review/attempts/1")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["comparison_id"], cid)
        self.assertEqual(data["review_id"], review_id)
        self.assertEqual(data["index"], 1)
        self.assertEqual(data["record"]["review_id"], review_id)

    def test_bad_index_gives_404(self):
        svc, tmp = make_service()
        c = svc.create("Build a greeting CLI.")
        cid = c["id"]
        review_id = c["test_review"]["id"]
        artifact_path = _write_attempt_artifact(Path(tmp), cid, review_id, "u1", {"ok": True})
        _set_review(svc, cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Read timed out.",
                    generation_attempts=[{"model": "m", "effort": "e", "max_tokens": 1,
                                          "started_at": 1.0, "ended_at": 2.0,
                                          "artifact": artifact_path}])
        client, _ = self._client(svc)
        self.assertEqual(client.get(f"/api/comparisons/{cid}/test-review/attempts/2").status_code, 404)
        self.assertEqual(client.get(f"/api/comparisons/{cid}/test-review/attempts/0").status_code, 404)

    def test_no_attempts_gives_404(self):
        svc, tmp = make_service()
        c = svc.create("Build a greeting CLI.")
        cid = c["id"]
        _set_review(svc, cid, status="generation_failed", generation_status="failed",
                    error="boom", generation_attempts=[])
        client, _ = self._client(svc)
        self.assertEqual(client.get(f"/api/comparisons/{cid}/test-review/attempts/1").status_code, 404)

    def test_attempt_without_artifact_gives_404(self):
        svc, tmp = make_service()
        c = svc.create("Build a greeting CLI.")
        cid = c["id"]
        _set_review(svc, cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Read timed out.",
                    generation_attempts=[{"model": "m", "effort": "e", "max_tokens": 1,
                                          "started_at": 1.0, "ended_at": 2.0,
                                          "artifact": None}])
        client, _ = self._client(svc)
        resp = client.get(f"/api/comparisons/{cid}/test-review/attempts/1")
        self.assertEqual(resp.status_code, 404)

    def test_rerooting_works_with_different_data_directory(self):
        """Reroots a path recorded under a different data_dir onto the current root."""
        svc, tmp = make_service()
        c = svc.create("Build a greeting CLI.")
        cid = c["id"]
        review_id = c["test_review"]["id"]
        payload = {"review_id": review_id, "rerooted": True}
        # Write artifact at the current root path
        artifact_path = _write_attempt_artifact(Path(tmp), cid, review_id, "u2", payload)
        # Simulate a recorded path that was written to a DIFFERENT data dir but same structure.
        recorded_path = str(Path("/old/data/dir") / "comparisons" / cid / "test-reviews" / review_id / "u2" / "attempt-1.json")
        _set_review(svc, cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Timed out.",
                    generation_attempts=[{"model": "m", "effort": "e", "max_tokens": 1,
                                          "started_at": 1.0, "ended_at": 2.0,
                                          "artifact": recorded_path}])
        client, _ = self._client(svc)
        # Should re-root to the actual storage root and find the file.
        resp = client.get(f"/api/comparisons/{cid}/test-review/attempts/1")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["record"]["rerooted"])

    def test_escaping_path_gives_404(self):
        svc, tmp = make_service()
        c = svc.create("Build a greeting CLI.")
        cid = c["id"]
        # Craft an attempt whose artifact path escapes via ".."
        bad_path = str(Path(tmp) / "comparisons" / cid / ".." / ".." / "etc" / "passwd")
        _set_review(svc, cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Timed out.",
                    generation_attempts=[{"model": "m", "effort": "e", "max_tokens": 1,
                                          "started_at": 1.0, "ended_at": 2.0,
                                          "artifact": bad_path}])
        client, _ = self._client(svc)
        self.assertEqual(client.get(f"/api/comparisons/{cid}/test-review/attempts/1").status_code, 404)

    def test_archived_record_read_through_storage(self):
        """read_file is invoked for the attempt artifact (file present locally → success)."""
        svc, tmp = make_service(s3_uri="s3://test-bucket/prefix/")
        c = svc.create("Build a greeting CLI.")
        cid = c["id"]
        review_id = c["test_review"]["id"]
        payload = {"review_id": review_id, "from_s3": True}
        artifact_path = _write_attempt_artifact(Path(tmp), cid, review_id, "u3", payload)
        _set_review(svc, cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Timed out.",
                    generation_attempts=[{"model": "m", "effort": "e", "max_tokens": 1,
                                          "started_at": 1.0, "ended_at": 2.0,
                                          "artifact": artifact_path}])
        cfg = svc.config
        with patch("arena.api.load_config", return_value=cfg):
            app = create_app()
        token = access_token(cfg.root)
        # Patch read_file on the app's own service to track calls.
        calls = []
        original_read = app.state.service.storage.read_file
        def fake_read(id, path):
            calls.append((id, str(path)))
            return original_read(id, path)
        app.state.service.storage.read_file = fake_read
        with TestClient(app, base_url="http://127.0.0.1",
                        headers={"Authorization": f"Bearer {token}"}) as client:
            resp = client.get(f"/api/comparisons/{cid}/test-review/attempts/1")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(calls, "read_file was not called")
        self.assertTrue(resp.json()["record"]["from_s3"])

    def test_storage_error_gives_404_archived_unavailable(self):
        svc, tmp = make_service()
        c = svc.create("Build a greeting CLI.")
        cid = c["id"]
        review_id = c["test_review"]["id"]
        artifact_path = str(Path(tmp) / "comparisons" / cid / "test-reviews" / review_id / "u4" / "attempt-1.json")
        _set_review(svc, cid,
                    status="generation_failed",
                    generation_status="failed",
                    error="Timed out.",
                    generation_attempts=[{"model": "m", "effort": "e", "max_tokens": 1,
                                          "started_at": 1.0, "ended_at": 2.0,
                                          "artifact": artifact_path}])
        def bad_read(id, path):
            raise StorageError("S3 archive not available")
        svc.storage.read_file = bad_read
        client, _ = self._client(svc)
        resp = client.get(f"/api/comparisons/{cid}/test-review/attempts/1")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("archived record unavailable", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
