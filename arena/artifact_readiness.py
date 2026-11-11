# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Local readiness snapshots; polling never downloads or modifies S3 artifacts."""
import json
from contextlib import contextmanager, ExitStack
from functools import wraps

from .artifact_storage import StorageError
from .store import COMPARISON_PHASE_PRIORITY, now

RECOVERABLE = frozenset(("complete", "incomplete", "interrupted", "waiting_test_review",
                        "archiving", "storage_failed", "archive_queued"))
TRANSFERRING = frozenset(("uploading", "evicting", "restoring"))
WORKING = (frozenset(COMPARISON_PHASE_PRIORITY) - {"waiting_test_review"}) | frozenset((
    "queued", "preparing", "reviewing_tests", "evaluation_queued", "verification_queued", "test_review_queued"))


def working_comparison(comparison):
    review = comparison.get("test_review") or {}
    return comparison.get("status") in WORKING or (
        comparison.get("status") == "waiting_test_review"
        and review.get("status") in ("queued", "drafting", "approved")
        and not review.get("plan_sha256"))


class ArtifactsNotReady(StorageError):
    def __init__(self, readiness):
        self.readiness = readiness
        super().__init__(readiness["message"])


def readiness(storage, comparison_id=None, db=None):
    if not storage.enabled:
        return {"enabled": False, "ready": True, "status": "disabled",
                "message": "S3 artifact storage is disabled."}
    if db is None:
        with storage.store.connect() as connection:
            return readiness(storage, comparison_id, connection)
    objects = {}
    for kind, id, data in db.execute(
            "SELECT kind,id,data FROM objects WHERE kind IN ('comparison','artifact_archive','system')"):
        objects[kind, id] = json.loads(data)
    recovery = objects.get(("system", "artifact-recovery"), {})
    worker = objects.get(("system", "worker"), {})
    online = bool(worker and now() - worker.get("heartbeat", 0) < 5)
    same_worker = bool(worker.get("instance_id") and
                       worker["instance_id"] == recovery.get("worker_instance"))
    comparisons = [c for (kind, _), c in objects.items() if kind == "comparison"
                   and c["status"] in RECOVERABLE and not working_comparison(c)]
    pending = [c for c in comparisons if
               objects.get(("artifact_archive", c["id"]), {}).get("status") != "archived"
               or storage.root(c["id"]).exists()]
    archives = [a for (kind, _), a in objects.items() if kind == "artifact_archive"]
    transfers = [a for a in archives if a["status"] in TRANSFERRING]
    failures = [a for a in archives if a["status"] in ("upload_failed", "restore_failed")
                or (a["status"] == "evicting" and a.get("error"))]
    current = max(transfers, key=lambda a: a.get("updated_at", 0), default={})
    selected = objects.get(("artifact_archive", comparison_id), {}) if comparison_id else {}
    selected_working = working_comparison(objects.get(("comparison", comparison_id), {}))
    selected_ready = comparison_id is None or (
        selected.get("status") == "archived" and not storage.root(comparison_id).exists())
    status, message = "ready", "S3 artifacts are ready. You can retry."
    error = recovery.get("error") or next((a.get("error") for a in failures if a.get("error")), None)
    if not online:
        status, message = "offline", "Worker offline. Start the worker to finish checking S3 artifacts."
    elif not worker.get("instance_id"):
        status, message = "restart_required", "The running worker predates S3 readiness reporting. Let its current transfer finish, then restart the worker."
    elif not same_worker:
        status, message = "pending", "Waiting for this worker to check S3 artifacts."
    elif recovery.get("status") == "failed":
        status, message = "failed", "S3 recovery failed. Retry S3 recovery before retrying the comparison."
    elif recovery.get("status") != "ready":
        status, message = recovery.get("status", "pending"), "Checking and saving artifacts from prior runs."
    elif failures:
        status, message = "failed", "S3 recovery failed. Retry S3 recovery before retrying the comparison."
    elif transfers or pending:
        status, message = "pending", "Waiting for artifact uploads, verification and temporary-file cleanup."
    elif selected_working:
        status, message = "in_use", (
            "S3 recovery checks are complete. Temporary files are in use by this comparison; "
            "they will be saved to S3 and removed after the active work stops.")
    elif not selected_ready:
        status, message = "pending", "Waiting for artifact uploads, verification and temporary-file cleanup."
    return {
        "enabled": True, "ready": status == "ready", "status": status, "message": message,
        "worker_online": online, "error": error,
        "can_retry_recovery": online and same_worker and status in ("failed", "pending")
            and recovery.get("status") in ("ready", "failed")
            and not any(not a.get("error") for a in transfers),
        "comparisons_ready": len(comparisons) - len(pending), "comparisons_total": len(comparisons),
        "checks_completed": recovery.get("completed", 0), "checks_total": recovery.get("total", 0),
        "phase": recovery.get("phase", "waiting"), "current_comparison": recovery.get("current_comparison"),
        "transfer": ({key: current.get(key) for key in
                      ("id", "status", "transfer_phase", "source_path", "uploaded_bytes", "uploaded_parts", "updated_at")}
                     if current else None),
        "updated_at": recovery.get("updated_at"), "finished_at": recovery.get("finished_at"),
        "selected_status": selected.get("status", "local") if comparison_id else None,
    }


@contextmanager
def retry_gate(storage, comparison_id, db=None):
    if not storage.enabled:
        yield
        return
    with ExitStack() as locks:
        try:
            # Do not wait behind a large upload or race a new recovery sweep.
            locks.enter_context(storage.lock("maintenance", blocking=False, shared=True))
            locks.enter_context(storage.lock(comparison_id + ".execution", blocking=False))
        except BlockingIOError:
            state = readiness(storage, comparison_id, db)
            state.update(ready=False, status="busy", message="Artifact recovery or comparison work is still running.")
            raise ArtifactsNotReady(state) from None
        state = readiness(storage, comparison_id, db)
        if not state["ready"]:
            raise ArtifactsNotReady(state)
        yield


def requires_ready_artifacts(method=None, *, run=False):
    def decorate(function):
        @wraps(function)
        def guarded(service, id, *args, **kwargs):
            comparison_id = service.store.get("run", id)["comparison_id"] if run else id
            service.ensure_comparison(comparison_id)
            with retry_gate(service.storage, comparison_id):
                return function(service, id, *args, **kwargs)
        return guarded
    return decorate(method) if method else decorate
