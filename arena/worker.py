# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio
import base64
import difflib
import fcntl
import hashlib
import json
import os
import shutil
import time
import traceback
from pathlib import Path

from .bridge import atomic_json
from .config import Config
from .evaluation import (EVALUATOR_VERSION, CandidateTestRunner, Plan, default_plan, evaluate_candidate,
                         evaluation_status, prepare_plan, repository_context)
from .harness_tests import (MANIFEST_NAME, bounded_log_tail, build_plan, create_lock,
                             enforce_lock, format_fix_feedback, format_repair_feedback, load_manifest)
from .isolation import execution_runtime, command, copy_tree, immutable, initialize_checkout, make_writable, patch, tree_hash
from .model_match import model_mismatch
from .prompts import TEMPLATE_VERSION, effective_prompt, follow_ups, for_harness
from .service import Service
from .artifact_storage import StorageError, remove_tree
from .artifact_readiness import RECOVERABLE
from .store import COMPARISON_PHASE_PRIORITY, TERMINAL, durations, now, uid
from .execution_evidence import native_execution_evidence
from .test_review import (TestProposal, plan_digest, propose_tests, requirements_digest,
                          review_alignment_issues, review_harness_tests, update_review)

# Phase names that belong to the test stage.  They share phase_status="testing".
_TEST_PHASES = frozenset({"test", "repair", "fix"})

# Directories excluded from workspace traversal (mirrors harness_tests._SKIP_DIRS).
_WORKSPACE_SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", ".tox"})


def _compute_implementation_diff(
    impl_snapshot: Path,
    test_snapshot: Path,
    locked_paths: set,
) -> str | None:
    """Compute a unified diff of non-locked implementation files between two snapshots.

    W6 (§9.12): the diff is passed to review_harness_tests() which caps it at 256 KiB
    and wraps it in an untrusted-data block.  Returns None when snapshots are equal or
    when neither directory exists.
    """
    if not impl_snapshot.is_dir() or not test_snapshot.is_dir():
        return None
    all_rel: set[str] = set()
    for root_dir in (impl_snapshot, test_snapshot):
        try:
            for p in root_dir.rglob("*"):
                if not p.is_file() or p.is_symlink():
                    continue
                try:
                    rel = p.relative_to(root_dir)
                except ValueError:
                    continue
                if any(part in _WORKSPACE_SKIP_DIRS for part in rel.parts):
                    continue
                rel_str = str(rel)
                if rel_str not in locked_paths:
                    all_rel.add(rel_str)
        except OSError:
            pass
    if not all_rel:
        return None
    diffs: list[str] = []
    for rel_str in sorted(all_rel):
        a_path = impl_snapshot / rel_str
        b_path = test_snapshot / rel_str
        try:
            a_lines = a_path.read_text(errors="replace").splitlines(keepends=True) if a_path.exists() else []
            b_lines = b_path.read_text(errors="replace").splitlines(keepends=True) if b_path.exists() else []
        except OSError:
            continue
        if a_lines == b_lines:
            continue
        for chunk in difflib.unified_diff(a_lines, b_lines,
                                          fromfile=f"a/{rel_str}", tofile=f"b/{rel_str}"):
            diffs.append(chunk)
    return "".join(diffs) if diffs else None


def _format_approved_spec(approved_review: dict) -> str:
    """Return full approved test-spec text for appending to repair prompts (W5)."""
    proposal = (approved_review or {}).get("proposal") or {}
    test_cases = proposal.get("test_cases") or []
    if not test_cases:
        return ""
    lines = ["", "APPROVED TEST SPECIFICATION:"]
    for tc in test_cases:
        lines.append(
            f"  Scenario {tc.get('id', '?')}: "
            f"{tc.get('title', tc.get('description', ''))}"
        )
        for step in (tc.get("steps") or []):
            lines.append(f"    Step: {step}")
        if tc.get("expected_result"):
            lines.append(f"    Expected: {tc['expected_result']}")
        if tc.get("test_data"):
            lines.append(f"    Test data: {tc['test_data']}")
    assumptions = proposal.get("assumptions") or []
    if assumptions:
        lines.append("  Assumptions:")
        for a in assumptions:
            lines.append(f"    - {a}")
    return "\n".join(lines)


def _scored_failing(results: dict, plan) -> list:
    """Return only scored checks that failed or errored (W4)."""
    if plan is None:
        return [c for c in results.get("checks", []) if c.get("status") != "passed"]
    scored_ids = {c.id for c in getattr(plan, "checks", []) if getattr(c, "scored", True)}
    if not scored_ids:
        return []
    return [c for c in results.get("checks", [])
            if c.get("id") in scored_ids and c.get("status") != "passed"]


def _save_lock_to_disk(root: Path, lock: dict) -> None:
    """Persist lock bytes to disk so enforce_lock can restore files after a restart (R2-2).

    Encodes file bytes as base64 so the result is valid JSON.  Stored at
    `root/artifacts/test-stage-lock.json`.
    """
    disk: dict = {
        "files": {
            p: {
                "sha256": info["sha256"],
                "bytes_b64": base64.b64encode(info["bytes"]).decode(),
            }
            for p, info in lock.get("files", {}).items()
        },
        "auto_locked": lock.get("auto_locked", []),
        "warnings": lock.get("warnings", []),
        "config_snapshots": lock.get("config_snapshots", {}),
        "special_file_baseline": lock.get("special_file_baseline", []),
    }
    lock_path = root / "artifacts" / "test-stage-lock.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(lock_path, disk)


def _load_lock_from_disk(root: Path) -> "dict | None":
    """Reload lock bytes from disk on resume (R2-2).  Returns None if the file is absent
    or unreadable.
    """
    lock_path = root / "artifacts" / "test-stage-lock.json"
    if not lock_path.exists():
        return None
    try:
        data = json.loads(lock_path.read_text())
    except Exception:
        return None
    files: dict = {}
    for p, info in data.get("files", {}).items():
        raw = base64.b64decode(info.get("bytes_b64", ""))
        files[p] = {"sha256": info["sha256"], "bytes": raw}
    return {
        "files": files,
        "auto_locked": data.get("auto_locked", []),
        "warnings": data.get("warnings", []),
        "config_snapshots": data.get("config_snapshots", {}),
        "special_file_baseline": data.get("special_file_baseline", []),
    }


async def finish_file_operation(function, *args):
    """Drain filesystem writes on cancellation; do not leave a copying thread behind."""
    job = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        await job
        raise


class Worker:
    def __init__(self, config: Config):
        self.config = config
        self.service = Service(config)
        self.store = self.service.store
        self.runtime = execution_runtime(config)
        self.active = {}
        self.storage = self.service.storage
        self.instance_id = uid()

    def recovery_progress(self, **fields):
        try:
            previous = self.store.get("system", "artifact-recovery")
        except KeyError:
            previous = {}
        self.store.put("system", {**previous, **fields, "id": "artifact-recovery",
                                 "worker_instance": self.instance_id, "updated_at": now()})

    def finish_state(self, id, status, **fields):
        """Do not expose a final comparison state before durable artifact storage."""
        if self.storage.enabled:
            return self.store.update("comparison", id, status="archiving",
                                     artifact_final_status=status, **fields)
        return self.store.update("comparison", id, status=status, **fields)

    def finish_comparison(self, id, observed):
        """Finalize under the same write lock used to accept a follow-up."""
        unfinished = False
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            comparison = json.loads(db.execute(
                "SELECT data FROM objects WHERE kind='comparison' AND id=?", (id,)).fetchone()[0])
            runs = [json.loads(row[0]) for row in db.execute(
                "SELECT data FROM objects WHERE kind='run' AND json_extract(data,'$.comparison_id')=?",
                (id,)).fetchall()]
            pending = (comparison["answers"] != observed["answers"] or any(
                not run.get("cancel_requested") and (run["status"] == "queued"
                    or self.has_pending_revision(run, comparison))
                for run in runs))
            if pending and not comparison["cancel_requested"]:
                status = "queued"
                comparison["ended_at"] = None
            else:
                for run in runs:
                    if run["status"] in TERMINAL:
                        continue
                    cancelled = comparison["cancel_requested"] or run["cancel_requested"]
                    self.store.transition_in_transaction(db, run["id"], "cancelled" if cancelled else "failed",
                        error=None if cancelled else
                        "Run ended without a terminal native or evaluation result; explicit Resume is required.")
                    unfinished = True
                status = "complete" if observed["comparison_complete"] and not unfinished else "incomplete"
                comparison["ended_at"] = now()
            comparison["status"] = "archiving" if self.storage.enabled else status
            if self.storage.enabled:
                comparison["artifact_final_status"] = status
            db.execute("UPDATE objects SET data=? WHERE kind='comparison' AND id=?",
                       (json.dumps(comparison), id))

    def archive_comparison(self, id):
        """Called only after native processes and filesystem writers have stopped."""
        c = self.store.get("comparison", id)
        target = c.get("artifact_final_status") or c["status"]
        if target in ("archiving", "storage_failed", "archive_queued", "restoring"):
            target = "interrupted"
        self.store.update("comparison", id, status="archiving", artifact_final_status=target)
        try:
            with self.storage.lock(id):
                root = self.storage.root(id)
                state = self.storage.state(id)
                if root.exists() and state["status"] not in ("evicting", "restoring", "restore_failed"):
                    self.archive_scratch(root)
                    self.archive_closed_logs()
                    self.storage.save_checkpoint(id)
                    atomic_json(root / "comparison-report.json", self.service.report(id, archival=True))
                result = self.storage.archive(id)
            self.store.update("comparison", id, status=target, artifact_final_status=None,
                              artifact_error=None)
            self.store.event(id, "artifacts_archived", {"uri": result["uri"],
                "bytes": result["archive"]["bytes"], "local_present": False})
        except Exception as exc:
            self.store.update("comparison", id, status="storage_failed", artifact_error=str(exc))
            self.store.event(id, "artifact_storage_failed", {"error": str(exc)})
            raise

    def archive_scratch(self, root):
        # Also find scratch from older runs that predate temporary-path.json.
        owners = {p.parent for p in root.rglob("temporary-path.json") if not p.is_symlink()}
        owners.update(p.parent for p in root.rglob("home") if p.is_dir() and not p.is_symlink())
        for owner in sorted(owners):
            path = Path("/private/tmp") / ("arena-" + hashlib.sha256(
                str(owner.resolve()).encode()).hexdigest()[:24])
            id = "extra-" + hashlib.sha256(str(path).encode()).hexdigest()[:32]
            if path.exists() or self.storage.state(id)["status"] in ("restoring", "restore_failed", "evicting"):
                if path.is_symlink() or (path.exists() and path.stat().st_uid != os.getuid()):
                    raise StorageError("Invalid temporary directory ownership: " + str(path))
                result = self.storage.archive_path(path)
                atomic_json(owner / "temporary-path.json",
                            {"path": str(path), "root": str(owner.resolve()), "archive_id": result["id"]})

    def restore_scratch(self, root):
        for record in root.rglob("temporary-path.json"):
            if record.is_symlink():
                continue
            data = json.loads(record.read_text())
            id = data.get("archive_id")
            if not id:
                continue
            if id != "extra-" + hashlib.sha256(data["path"].encode()).hexdigest()[:32]:
                raise StorageError("Invalid scratch archive reference")
            path = Path("/private/tmp") / ("arena-" + hashlib.sha256(
                str(record.parent.resolve()).encode()).hexdigest()[:24])
            if path.is_symlink() or (path.exists() and path.stat().st_uid != os.getuid()):
                raise StorageError("Invalid temporary directory ownership: " + str(path))
            self.storage.restore_scratch(id, path)

    def archive_closed_logs(self):
        logs = self.config.root / "logs"
        if logs.exists():
            for path in sorted(logs.glob("*.log")):
                self.storage.archive_path(path)

    def recover_extra_artifacts(self):
        """Explicit migration paths and orphan comparison roots are never executed."""
        # A partial deletion may remove the output marker. The durable transfer
        # journal, rather than rediscovery by filename, owns unfinished cleanup.
        for state in self.store.list("artifact_archive"):
            if (state.get("category") != "artifacts" or not state.get("source_path")
                    or state["status"] not in ("uploading", "upload_failed", "evicting", "restoring", "restore_failed")):
                continue
            try:
                with self.storage.lock(state["id"], blocking=False):
                    self.storage.archive(state["id"], Path(state["source_path"]))
            except BlockingIOError:
                continue
        known = {c["id"] for c in self.store.list("comparison")}
        comparisons = self.config.root / "comparisons"
        if comparisons.exists():
            for root in comparisons.iterdir():
                if root.name.startswith(".restore-"):
                    id = root.name.removeprefix(".restore-")
                    if id in known:
                        continue
                elif root.name in known:
                    continue
                # Creation holds this lease until the database cohort is committed.
                try:
                    with self.storage.lock(root.name + ".execution", blocking=False):
                        if any(c["id"] == root.name for c in self.store.list("comparison")):
                            continue
                        self.archive_scratch(root)
                        self.storage.archive_path(root)
                except BlockingIOError:
                    continue
        protected = [Path(__file__).parent.resolve(), Path(__file__).parent.parent / "mchc",
                     Path(__file__).parent.parent / "tests", self.storage.control,
                     self.config.root / "credential-templates", self.config.root / "model-discovery"]
        protected += [Path(__file__).parent.parent / name for name in (
            ".venv", ".git", ".agents", ".codex", "README.md", "pyproject.toml",
            "requirements.lock", "config.toml", "config.example.toml",
            "examples/demo-project", "examples/demo-task.json")]
        protected += [Path(h.home_template).expanduser().resolve() for h in self.config.harnesses.values()
                      if h.home_template]
        # A harness that falls back to its CLI's own configuration directory reads the
        # operator's real one, so that directory needs the same protection a configured
        # template has: a coding run must never be able to rewrite it.
        from .host_setup import default_home_source
        for name, harness in self.config.harnesses.items():
            source = None if harness.home_template else default_home_source(name)[0]
            if source and source.exists():
                protected.append(source.resolve())
        protected += [self.config.root / name for name in (
            "arena.sqlite3", "arena.sqlite3-wal", "arena.sqlite3-shm", "ui-token",
            "worker.lock", "launcher.lock", "services.json", "native-ports.json",
            "native-processes", "worker.log", "api.log", "worker.log.lock", "api.log.lock")]
        for value in self.config.artifact_paths:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = self.config.root.parent / path
            path = path.absolute()
            # Refuse overlap with code, active storage, or runtime/authentication.
            resolved = path.resolve()
            if (resolved == self.config.root or resolved in self.config.root.parents
                    or any(resolved == p or resolved.is_relative_to(p) or p.is_relative_to(resolved)
                           for p in protected)
                    or resolved.is_relative_to(comparisons)):
                raise StorageError("artifact_paths overlaps application runtime or source: " + str(path))
            if path.is_symlink():
                raise StorageError("Artifact migration path must not be a symlink: " + str(path))
            self.storage.archive_path(path)
        # Closed rotated logs are immutable. Active log streams are runtime state.
        self.archive_closed_logs()

    def recover_artifacts(self, excluded=()):
        """Before new work, finish uploads left behind by stopped comparisons."""
        if not self.storage.enabled:
            return
        # Serialize the sweep across simultaneously dispatched comparisons.
        with self.storage.lock("maintenance"):
            self.recovery_progress(status="running", phase="comparisons", completed=0, total=0,
                                   current_comparison=None, error=None, started_at=now(), finished_at=None)
            try:
                self._recover_artifacts(excluded)
            except BaseException as exc:
                self.recovery_progress(status="failed", error=str(exc) or type(exc).__name__)
                raise
            else:
                self.recovery_progress(status="ready", phase="complete",
                                       current_comparison=None, finished_at=now(), error=None)

    def _recover_artifacts(self, excluded):
        comparisons = [c for c in self.store.list("comparison")
                       if c["id"] not in excluded and c["status"] in RECOVERABLE]
        self.recovery_progress(total=len(comparisons))
        for index, c in enumerate(comparisons):
            id = c["id"]
            self.recovery_progress(completed=index, current_comparison=id)
            state = self.storage.state(id)
            if not self.storage.root(id).exists() and state["status"] == "archived":
                # A crash can occur after eviction but before the final status.
                if c.get("artifact_final_status"):
                    self.store.update("comparison", id, status=c["artifact_final_status"],
                                      artifact_final_status=None, artifact_error=None)
                staging = self.storage.root(id).parent / (".restore-" + id)
                if staging.exists():
                    self.storage._verify_remote(state)
                    remove_tree(staging)
                continue
            try:
                with self.storage.lock(id + ".execution", blocking=False):
                    # A queued API action may have arrived while taking the lease.
                    if self.store.get("comparison", id)["status"] != c["status"]:
                        continue
                    self.archive_comparison(id)
            except BlockingIOError:
                continue
        self.recovery_progress(completed=len(comparisons), current_comparison=None, phase="leftovers")
        self.recover_extra_artifacts()

    async def stored_operation(self, id, operation, archive_only=False):
        """A comparison lease covers restore, execution, upload and eviction."""
        if not self.storage.enabled:
            return await operation(id)
        lease = self.storage.lock(id + ".execution", blocking=False)
        acquired = False
        try:
            # Nonblocking flock has no slow IO and no cancellation window between
            # taking ownership and recording that it must be released.
            lease.__enter__()
            acquired = True
            # Do not execute a new job while old artifacts could not be secured.
            await finish_file_operation(self.recover_artifacts, (*self.active, id))
            if archive_only:
                await finish_file_operation(self.archive_comparison, id)
                return
            c = self.store.get("comparison", id)
            if (self.storage.root(id).exists()
                    and self.storage.state(id)["status"] in ("upload_failed", "evicting", "restoring", "restore_failed")):
                await finish_file_operation(self.archive_comparison, id)
            self.store.update("comparison", id, status="restoring")
            def restore():
                with self.storage.lock(id):
                    self.storage.restore(id)
                    self.restore_scratch(self.storage.root(id))
            await finish_file_operation(restore)
            self.store.update("comparison", id, status="preparing")
            try:
                await operation(id)
            except asyncio.CancelledError:
                # Shutdown drains model, native, and copying jobs first. The
                # next worker completes the durable upload before starting work.
                current = self.store.get("comparison", id)
                self.finish_state(id, current.get("artifact_final_status") or
                                  ("waiting_test_review" if self.waiting_only_for_test_review(current) else "interrupted"))
                raise
            except Exception as exc:
                self.finish_state(id, "incomplete", error=str(exc))
                self.store.event(id, "operation_failed", {"error": str(exc)})
            finally:
                # operation() has drained its own writers before this boundary.
                await finish_file_operation(self.archive_comparison, id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            c = self.store.get("comparison", id)
            if c["status"] != "storage_failed":
                self.store.update("comparison", id, status="storage_failed",
                    artifact_final_status=c.get("artifact_final_status") or "interrupted",
                    artifact_error=str(exc))
            self.store.event(id, "artifact_storage_failed", {"error": str(exc)})
        finally:
            if acquired:
                lease.__exit__(None, None, None)

    def run_root(self, run):
        return self.config.root / "comparisons" / run["comparison_id"] / "runs" / run["id"]

    def requested_version(self, run, comparison):
        return run.get("requested_spec_version", comparison["spec_version"])

    def has_pending_revision(self, run, comparison):
        if run["status"] not in TERMINAL:
            return False
        attempted = run.get("attempted_requested_spec_version", run["spec_version"])
        if attempted != self.requested_version(run, comparison):
            return True
        # Answers can be appended before the shared version is frozen. Preserve
        # those late requests even if an older turn fails before its next poll.
        attempted_answers = run.get("attempted_answer_ids")
        return attempted_answers is not None and any(
            answer["question_id"] not in attempted_answers
            for answer in self.run_specification(comparison, run).get("answers", []))

    def record_requested_attempt(self, run_id, spec):
        # Record preparation failures too, without relabeling a previous snapshot
        # as this version or repeatedly retrying a broken target automatically.
        self.store.update("run", run_id, attempted_requested_spec_version=spec["spec_version"],
                          attempted_answer_ids=[a["question_id"] for a in spec.get("answers", [])])

    def run_specification(self, comparison, run):
        """An OFF recipient keeps its previous version and only sees addressed answers."""
        version = self.requested_version(run, comparison)
        frozen = next((s for s in comparison.get("specifications", [])
                       if s["version"] == version), None)
        spec = comparison
        if frozen and version != comparison["spec_version"]:
            spec = {**comparison, "prompt": frozen["task"], "answers": frozen["answers"],
                    "spec_version": version, "plan": frozen.get("plan"),
                    "development_plan": frozen.get("development_plan")}
        addressed = for_harness(spec, run["harness"])
        addressed["coding_criteria_filtered"] = (spec.get("coding_criteria_filtered", False)
                                                 or spec.get("answers", []) != addressed["answers"])
        return addressed

    def retain_previous_implementation(self, run_id, version):
        note = (f"Implementation saved for specification v{version}. A follow-up sent to other harnesses "
                "changed the shared evaluation; this harness was not sent that follow-up.")
        self.store.transition(run_id, "evaluated_incomplete", evaluation=None,
                              note=note, evaluation_scope_note=note)

    def cancelled(self, run):
        return self.store.get("run", run["id"])["cancel_requested"] or self.store.get(
            "comparison", run["comparison_id"])["cancel_requested"]

    def comparison_cancelled(self, id):
        c = self.store.detail(id)
        return c["cancel_requested"] or all(r["cancel_requested"] or r["status"] in TERMINAL for r in c["runs"])

    def waiting_only_for_test_review(self, comparison):
        """True only after executable work has stopped and code is saved."""
        review = comparison.get("test_review")
        if (comparison.get("cancel_requested") or not review
                or review["status"] not in ("awaiting_review", "proposal_failed", "generation_failed")):
            return False
        detail = comparison if "runs" in comparison else self.store.detail(comparison["id"])
        pending = [run for run in detail["runs"] if run["status"] not in TERMINAL]
        if not pending:
            return review.get("mode") == "saved_code"
        return all(run["status"] == "waiting_test_review"
                   and not run.get("cancel_requested")
                   and run["spec_version"] == comparison["spec_version"]
                   and run.get("artifacts", {}).get("snapshot") for run in pending)

    def ready_to_dispatch(self, comparison):
        if self.storage.enabled and comparison["status"] == "archive_queued":
            return True
        if comparison["status"] not in (
                "queued", "evaluation_queued", "verification_queued",
                "test_review_queued", "waiting_test_review"):
            return False
        if comparison["status"] in ("test_review_queued", "waiting_test_review"):
            return not self.waiting_only_for_test_review(comparison)
        return True

    async def score_candidate(self, run, comparison, plan, candidate, *,
                              ratings=None, no_manifest_reason=None, incomplete_reason=None):
        def judge_progress(event):
            self.store.event(comparison["id"], "diagnostic",
                             {**event, "stage": "judge"}, run["id"])

        job = asyncio.create_task(evaluate_candidate(
            comparison, plan, candidate, self.config.judge_for_comparison(comparison),
            ratings=ratings, no_manifest_reason=no_manifest_reason,
            incomplete_reason=incomplete_reason,
            judge_progress=judge_progress))
        try:
            while not job.done():
                if self.cancelled(run):
                    return None
                await asyncio.wait({job}, timeout=.2)
            return None if self.cancelled(run) else await job
        finally:
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def recovery(self):
        # The exclusive worker lock is held before inspecting or recovering runs.
        # Never silently relaunch a coding turn after loss of worker ownership.
        await self.runtime.cleanup_owned()
        try:
            heartbeat = self.store.get("system", "worker")["heartbeat"]
        except KeyError:
            heartbeat = 0
        for r in self.store.list("run"):
            comparison = self.store.get("comparison", r["comparison_id"])
            if (comparison.get("test_review") and r.get("artifacts", {}).get("snapshot")
                    and r["status"] in ("waiting_test_review", "planning", "testing", "reviewing_tests")):
                # DESIGN 8/9.3: runs in testing/reviewing_tests are recovered like
                # interrupted development: stop orphaned processes, close the open attempt
                # with correct active-seconds accounting, then move to waiting_test_review
                # so the test stage re-runs (or finishes quickly if the budget is spent).
                if r["status"] in ("testing", "reviewing_tests"):
                    await self.runtime.stop("arena-" + r["id"])
                    recovered_at = now()
                    last_observed = min(recovered_at, max(heartbeat, r["updated_at"],
                        r["intervals"][-1]["start"] if r["intervals"] else r["submitted_at"]))
                    attempts = r["attempts"]
                    if attempts and attempts[-1].get("ended_at") is None:
                        attempt = attempts[-1]
                        observed = sum(max(0, min(i["end"] or last_observed, last_observed)
                            - max(i["start"], attempt["started_at"])) for i in r["intervals"]
                            if i["status"] in ("clarifying", "developing", "testing"))
                        attempt.update(ended_at=recovered_at, outcome="interrupted",
                            active_seconds=observed,
                            unobserved_active_seconds=(recovered_at - last_observed
                                if r["status"] == "testing" else 0),
                            timing_note="Worker-offline processing was not measured; "
                                "test-stage budget may have been partially consumed.")
                    self.store.transition(r["id"], "waiting_test_review",
                        attempts=attempts,
                        note="Test stage interrupted by worker restart; "
                             "saved code retained, test stage will re-run.")
                else:
                    if r["status"] not in ("waiting_test_review",):
                        self.store.transition(r["id"], "waiting_test_review",
                                              note="Saved code retained; continuing test review after restart.")
                continue
            if r["status"] not in TERMINAL and r["status"] != "queued":
                await self.runtime.stop("arena-" + r["id"])
                recovered_at = now()
                last_observed = min(recovered_at, max(heartbeat, r["updated_at"],
                    r["intervals"][-1]["start"] if r["intervals"] else r["submitted_at"]))
                self.store.transition(r["id"], "unobserved", at=last_observed)
                attempts = r["attempts"]
                if attempts and attempts[-1].get("ended_at") is None:
                    attempt = attempts[-1]
                    observed = sum(max(0, min(i["end"] or last_observed, last_observed)
                        - max(i["start"], attempt["started_at"])) for i in r["intervals"]
                        if i["status"] in ("clarifying", "developing", "testing"))
                    attempt.update(ended_at=recovered_at, outcome="interrupted", active_seconds=observed,
                        unobserved_active_seconds=(recovered_at-last_observed
                            if r["status"] in ("clarifying", "developing", "testing") else 0),
                        timing_note="Worker-offline processing was not measured; possible native execution is conservatively reserved against the active budget.")
                self.store.transition(r["id"], "interrupted",
                    attempts=attempts,
                    error="Worker restarted. Native session and workspace retained; explicit Resume is required.")
                self.store.update("comparison", r["comparison_id"], status="interrupted")
                root = self.run_root(r)
                baseline = root.parent.parent / "baseline"
                if (root / "workspace").exists() and baseline.exists():
                    try:
                        workspace_sha = tree_hash(root / "workspace")
                        if r.get("artifacts", {}).get("sha256") == workspace_sha:
                            # An interrupted re-evaluation must keep its reusable
                            # case and previously recorded independent evidence.
                            await finish_file_operation(self.service.publish_run_output, r["id"])
                            continue
                        if workspace_sha != tree_hash(baseline):
                            artifact = root / "artifacts" / ("recovered-" + uid()[:8])
                            await finish_file_operation(copy_tree, root / "workspace", artifact / "snapshot")
                            immutable(artifact / "snapshot")
                            patch(baseline, artifact / "snapshot", artifact / "implementation.patch")
                            history = r.get("artifact_history", [])
                            if r.get("artifacts"):
                                history.append({"spec_version": r["spec_version"], "archived_at": now(),
                                                "artifacts": r["artifacts"], "evaluation": r.get("evaluation"),
                                                "evidence": r.get("evidence")})
                            self.store.update("run", r["id"], artifacts={
                                "snapshot": str(artifact / "snapshot"), "patch": str(artifact / "implementation.patch"),
                                "sha256": tree_hash(artifact / "snapshot"), "partial": True,
                                "note": "Recovered after worker interruption; not independently verified."},
                                evaluation=None, evidence=None, artifact_history=history)
                            await finish_file_operation(self.service.publish_run_output, r["id"])
                    except Exception as exc:
                        self.store.event(r["comparison_id"], "artifact_error", {"message": str(exc)}, r["id"])
        for c in self.store.list("comparison"):
            if self.storage.enabled and c["status"] in ("archiving", "storage_failed", "archive_queued"):
                continue
            review = c.get("test_review")
            if review and review.get("status") == "drafting":
                update_review(self.store, c["id"], review["id"], status="queued")
            waiting = any(r["status"] == "waiting_test_review" for r in self.store.detail(c["id"])["runs"])
            if review and (waiting or (review.get("mode") == "saved_code" and
                          c["status"] in ("reviewing_tests", "waiting_test_review", "preparing"))):
                self.store.update("comparison", c["id"], status="waiting_test_review"
                                  if self.waiting_only_for_test_review(self.store.detail(c["id"]))
                                  else "test_review_queued")
                continue
            if c["status"] not in ("queued", "evaluation_queued", "verification_queued", "test_review_queued",
                                   "complete", "incomplete", "interrupted"):
                self.store.update("comparison", c["id"], status="interrupted",
                    error="Worker restarted during orchestration. Explicit Resume is required.")

    async def serve(self):
        lock = open(self.config.root / "worker.lock", "a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A persistent worker already owns this data directory")
        self.store.put("system", {"id": "worker", "pid": os.getpid(), "heartbeat": now(),
                                 "instance_id": self.instance_id, "test_coverage_version": 1,
                                 "harness_selection_version": 1,
                                 "evaluator_version": EVALUATOR_VERSION})
        if self.storage.enabled:
            self.recovery_progress(status="running", phase="stopping_interrupted_jobs",
                                   completed=0, total=0, current_comparison=None,
                                   error=None, started_at=now(), finished_at=None)
        try:
            await self.recovery()
        except BaseException as exc:
            if self.storage.enabled:
                self.recovery_progress(status="failed", error=str(exc) or type(exc).__name__)
            raise
        # Recovery runs off the event loop so heartbeats and API status stay live.
        maintenance = (asyncio.create_task(finish_file_operation(self.recover_artifacts))
                       if self.storage.enabled else None)
        try:
            while True:
                self.store.put("system", {"id": "worker", "pid": os.getpid(), "heartbeat": now(),
                    "instance_id": self.instance_id, "test_coverage_version": 1,
                    "harness_selection_version": 1,
                    "evaluator_version": EVALUATOR_VERSION,
                    "comparison_capacity": self.config.max_parallel_comparisons,
                    "harness_capacity": 3 * self.config.max_parallel_comparisons})
                if maintenance is not None:
                    if not maintenance.done():
                        await asyncio.sleep(.5)
                        continue
                    try:
                        maintenance.result()
                    except Exception:
                        traceback.print_exc()
                    maintenance = None
                if self.storage.enabled and self.store.get("system", "artifact-recovery")["status"] == "queued":
                    maintenance = asyncio.create_task(finish_file_operation(
                        self.recover_artifacts, tuple(self.active)))
                    continue
                for id, task in list(self.active.items()):
                    if task.done():
                        try:
                            task.result()
                        except Exception:
                            traceback.print_exc()
                        self.active.pop(id)
                for c in sorted(self.store.list("comparison"), key=lambda c: c["created_at"]):
                    if len(self.active) >= self.config.max_parallel_comparisons:
                        break
                    if self.ready_to_dispatch(c) and c["id"] not in self.active:
                        self.store.update("comparison", c["id"], status="preparing")
                        operation = (self.review_saved_code if c["status"] in ("test_review_queued", "waiting_test_review")
                                     and c.get("test_review", {}).get("mode") == "saved_code" else
                                     self.reverify if c["status"] == "verification_queued" else
                                     self.reevaluate if c["status"] == "evaluation_queued" else self.compare)
                        self.active[c["id"]] = asyncio.create_task(self.stored_operation(
                            c["id"], operation, archive_only=c["status"] == "archive_queued"))
                await asyncio.sleep(.5)
        finally:
            if maintenance is not None:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
            for task in self.active.values():
                task.cancel()
            await asyncio.gather(*self.active.values(), return_exceptions=True)
            lock.close()

    async def receive(self, process, run):
        async def stdout():
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    message = {"type": "diagnostic", "data": {"text": line.decode(errors="replace")}}
                kind, data = message["type"], message["data"]
                self.store.event(run["comparison_id"], kind, data, run["id"])
                current = self.store.get("run", run["id"])
                if kind == "session":
                    self.store.update("run", run["id"], session_id=data["session_id"],
                                      model=data.get("model") or current.get("model"))
                    atomic_json(self.run_root(run) / "bridge" / "session.json",
                                {"session_id": data["session_id"]})
                    r, m = run.get("requested_model"), data.get("model")
                    if model_mismatch(r, m):
                        # The first substitution is the run's record; each distinct model reported
                        # later (a resumed session reports once per turn) gets one event.
                        first = current.get("model_mismatch") or {
                            "requested": r, "reported": m, "detected_at": time.time()}
                        seen = first.get("seen", [first["reported"]] if current.get("model_mismatch") else [])
                        if m not in seen:
                            self.store.update("run", run["id"], model_mismatch={**first, "seen": [*seen, m]})
                            harness = run["harness"].capitalize()
                            self.store.event(run["comparison_id"], "model_mismatch", {
                                "requested": r, "reported": m,
                                "message": (f"{harness} ran {m} instead of the requested {r}. "
                                            "Its own settings or a managed policy chose the model.")},
                                run["id"])
                elif kind == "usage":
                    # The primary native thread is known. Do not add delegated
                    # cumulative events which might already be included there.
                    if data["source"].startswith("codex") and data["scope"] != current.get("session_id"):
                        data = {**data, "mode": "unavailable", "coverage": "unavailable",
                                "exclusions": ["Child-session usage retained raw; aggregation overlap is unknown."]}
                    self.store.update("run", run["id"], usage_records=current["usage_records"] + [data])
                elif kind == "capabilities":
                    self.store.update("run", run["id"], capabilities=current["capabilities"] + [data])
                elif kind == "error":
                    self.store.update("run", run["id"], error=data["message"],
                                      error_terminal=data.get("terminal"))
                elif kind == "turn_complete":
                    self.store.update("run", run["id"], turn_completed=True)

        async def stderr():
            while line := await process.stderr.readline():
                self.store.event(run["comparison_id"], "diagnostic",
                                 {"text": line.decode(errors="replace")}, run["id"])
        await asyncio.gather(stdout(), stderr())

    async def mailbox(self, run, turn_key):
        root = self.run_root(run) / "bridge"
        for file in (root / "requests").glob("*.json"):
            request = json.loads(file.read_text())
            if request["turn_key"] == turn_key:
                if request.get("type") == "ready":
                    atomic_json(root / "ready.json", request)
                    self.store.update("run", run["id"], clarified=True,
                                      readiness={"turn_key": turn_key, "assumptions": request.get("assumptions"),
                                                 "spec_version": run["spec_version"]})
                else:
                    self.service.register_question(self.store.get("run", run["id"]), request)
        questions = self.store.detail(run["comparison_id"])["questions"]
        pending = []
        for question in questions:
            for mapping in question["mappings"]:
                if mapping["run_id"] == run["id"] and mapping["turn_key"] == turn_key:
                    if question["answer"] is not None:
                        atomic_json(root / "answers" / (mapping["mailbox_id"] + ".json"),
                                    {"answer": question["answer"]})
                    elif not question.get("withdrawn"):
                        pending.append(question)
        return questions, pending

    async def phase(self, run_id: str, phase: str, spec: dict, plan: Plan,
                    readiness_reminder: bool = False,
                    active_budget: float | None = None,
                    prior_active_seconds: float = 0.0,
                    prompt_override: str | None = None):
        is_test_phase = phase in _TEST_PHASES
        run = self.store.get("run", run_id)
        original_answers = spec.get("answers", [])
        spec = self.run_specification(spec, run)
        # A shared/generated rubric can paraphrase excluded instructions. Coding
        # gets deterministic original-task criteria when history differs by recipient.
        if not is_test_phase and (spec.get("coding_criteria_filtered") or original_answers != spec.get("answers", [])):
            plan = default_plan(spec, self.config.root / "comparisons" / spec["id"] / "baseline")
        if self.cancelled(run):
            self.store.transition(run_id, "cancelled")
            return "cancelled"
        root = self.run_root(run)
        root.mkdir(parents=True, exist_ok=True)
        baseline = root.parent.parent / "baseline"
        container = "arena-" + run_id
        process, read_task = None, None
        paused = False
        phase_status = ("clarifying" if phase == "clarify"
                        else "testing" if is_test_phase
                        else "developing")
        turn_key = uid()
        self.store.transition(run_id, "setup", error=None, error_details=None,
                              error_terminal=None, turn_completed=False)
        delivered = set(run.get("follow_ups_delivered", []))
        pending_follow_ups = ([] if phase in ("clarify", *_TEST_PHASES) else
            [fu for fu in follow_ups(spec) if fu["id"] not in delivered])
        # A follow-up outside this set arriving mid-turn restarts the turn, even when the
        # specification was not frozen and so its version did not change.
        sent = delivered | {fu["id"] for fu in pending_follow_ups}
        if prompt_override is not None:
            prompt = prompt_override
        elif is_test_phase:
            prompt = effective_prompt(spec, "write_tests" if phase == "test" else phase, None,
                                      False, None,
                                      approved_review=spec.get("approved_test_review"))
        else:
            prompt = effective_prompt(spec, phase, plan.model_dump() if plan else None,
                                      readiness_reminder, pending_follow_ups or None)
        attempt = {"id": turn_key, "phase": phase, "spec_version": spec["spec_version"],
                   "started_at": now(), "template_version": TEMPLATE_VERSION, "prompt": prompt,
                   "runtime": None, "readiness_reminder": readiness_reminder,
                   "follow_ups": [fu["id"] for fu in pending_follow_ups]}
        (root / "prompts").mkdir(exist_ok=True)
        atomic_json(root / "prompts" / (turn_key + ".json"), attempt)
        current = self.store.get("run", run_id)
        self.store.update("run", run_id, spec_version=spec["spec_version"],
                          attempts=current["attempts"] + [attempt])
        self.store.event(spec["id"], "dispatched", {"attempt_id": turn_key, "phase": phase,
                                                  "spec_version": spec["spec_version"]}, run_id)
        activity = "preparing the native workspace"
        try:
            if tree_hash(baseline) != spec["baseline_sha256"]:
                raise RuntimeError("Baseline hash mismatch; refusing to execute")
            if not (root / "workspace").exists():
                await finish_file_operation(copy_tree, baseline, root / "workspace")
                make_writable(root / "workspace")
            await initialize_checkout(root / "workspace")
            runtime = await self.runtime.start(run, root, phase)
            attempt["runtime"] = runtime
            atomic_json(root / "prompts" / (turn_key + ".json"), attempt)
            spec_data: dict = {
                "task": spec["prompt"], "shared_answers": spec.get("answers", []),
                "specification_version": spec.get("spec_version", 1),
                "criteria": plan.model_dump()["criteria"] if plan else [],
            }
            if is_test_phase:
                approved_review = spec.get("approved_test_review") or {}
                proposal = approved_review.get("proposal") or {}
                spec_data["test_specification"] = {
                    "summary": proposal.get("summary", ""),
                    "assumptions": proposal.get("assumptions", []),
                    "limitations": proposal.get("limitations", []),
                    "scenarios": proposal.get("test_cases", []),
                    "approved_scenario_ids": [c["id"] for c in proposal.get("test_cases", [])],
                }
            atomic_json(root / "bridge" / "specification.json", spec_data)
            atomic_json(root / "bridge" / "turn.json", {
                "turn_key": turn_key, "phase": phase, "prompt": prompt,
                "harness": run["harness"], "session_id": run.get("session_id"),
                "workspace": runtime.get("workspace", "/workspace"),
                "config": self.config.harness_for_run(run).model_dump(exclude={"home_template"}),
                "max_turns": self.config.max_turns, "setup_seconds": self.config.setup_seconds})
            current = self.store.get("run", run_id)
            current["attempts"][-1] = attempt
            self.store.update("run", run_id, runtime=runtime, spec_version=spec["spec_version"],
                              attempts=current["attempts"])
            self.store.transition(run_id, phase_status)
            process = await self.runtime.launch(container)
            read_task = asyncio.create_task(self.receive(process, run))
            active = 0.
            previous = time.monotonic()
            while process.returncode is None:
                await asyncio.sleep(.25)
                activity = "monitoring the native process"
                await self.runtime.supervise(container)
                tick = time.monotonic()
                if not paused:
                    active += tick - previous
                previous = tick
                if self.cancelled(run):
                    self.store.transition(run_id, "cancelled")
                    return "cancelled"
                current_spec = self.store.get("comparison", spec["id"])
                current_run = self.store.get("run", run_id)
                if phase != "clarify" and (self.requested_version(current_run, current_spec) != spec["spec_version"] or any(
                        fu["id"] not in sent for fu in follow_ups(for_harness(current_spec, run["harness"])))):
                    self.store.transition(run_id, "ready", note="Shared requirements changed; resuming this session against the new specification.")
                    return "revised"
                activity = "processing native clarification requests"
                _, wait = await self.mailbox(self.store.get("run", run_id), turn_key)
                if wait and not paused:
                    activity = "pausing the native process for clarification"
                    code, _, err = await self.runtime.pause(container)
                    if code:
                        raise RuntimeError("Could not pause native work for user input: " + err)
                    paused = True
                    status = "waiting_approval" if all(q["kind"] == "approval" for q in wait) else "waiting_user"
                    self.store.transition(run_id, status)
                elif not wait and paused:
                    activity = "resuming the native process after clarification"
                    await self.runtime.unpause(container)
                    paused = False
                    self.store.transition(run_id, phase_status)
                if is_test_phase:
                    # Test-stage budget tracks only test-turn active time.
                    # active_budget is the REMAINING budget (budget - prior_used); compare
                    # only against active seconds accrued THIS turn (L4).
                    _test_budget = active_budget if active_budget is not None else self.config.test_active_seconds
                    if active >= _test_budget:
                        charged = self.store.get("run", run_id)["attempts"]
                        if charged and charged[-1]["id"] == turn_key:
                            charged[-1].update(ended_at=now(), outcome="test_budget_exhausted",
                                               active_seconds=active)
                        self.store.transition(run_id, "testing", attempts=charged)
                        return "test_budget_exhausted"
                else:
                    previous_active = sum(a.get("active_seconds", 0) + a.get("unobserved_active_seconds", 0)
                                          for a in run["attempts"])
                    if active + previous_active >= self.config.active_seconds:
                        # Charge this turn in the same atomic write as the terminal
                        # status. The `finally` bookkeeping below is lost if the worker
                        # is killed first, and a resume that cannot see these seconds
                        # silently grants the whole budget again.
                        charged = self.store.get("run", run_id)["attempts"]
                        if charged and charged[-1]["id"] == turn_key:
                            charged[-1].update(ended_at=now(), outcome="limit_exhausted",
                                               active_seconds=active)
                        reserved = sum(a.get("unobserved_active_seconds", 0) for a in run["attempts"])
                        self.store.transition(run_id, "limit_exhausted", attempts=charged, error=(
                            f"Cumulative active-time budget exhausted: {active + previous_active:.0f}s used of the "
                            f"configured active_seconds={self.config.active_seconds}s "
                            f"({active:.0f}s this turn, {previous_active:.0f}s in earlier turns"
                            + (f", including {reserved:.0f}s reserved for unobserved execution after an interruption"
                               if reserved else "")
                            + "). Raise active_seconds in configuration, restart the worker, then resume."))
                        return "limit_exhausted"
                if read_task.done() and read_task.exception():
                    raise read_task.exception()
            await read_task
            _, pending = await self.mailbox(self.store.get("run", run_id), turn_key)
            current = self.store.get("run", run_id)
            if process.returncode != 0 or not current.get("turn_completed"):
                failure = RuntimeError(current.get("error")
                                       or f"Native process exited {process.returncode} without a completed turn")
                if current.get("error"):
                    failure.terminal = current.get("error_terminal")
                raise failure
            if phase == "clarify":
                if pending:
                    raise RuntimeError("Clarification turn ended with unanswered questions; readiness is unverified.")
                ready_file = root / "bridge" / "ready.json"
                ready = json.loads(ready_file.read_text()) if ready_file.exists() else {}
                if ready.get("turn_key") != turn_key:
                    if not readiness_reminder:
                        # Finishing the turn without the bookkeeping call is a
                        # recoverable omission, not a broken harness. Record it
                        # and let the caller re-prompt this session once.
                        self.store.event(spec["id"], "readiness_reminder_required",
                                         {"attempt_id": turn_key}, run_id)
                        return "readiness_unverified"
                    raise RuntimeError("Harness did not call the required clarification ready tool. Readiness is unverified.")
                self.store.transition(run_id, "ready", clarified=True, assumptions=ready.get("assumptions"))
            elif is_test_phase:
                # Test-stage turn completed; stay in "testing" until the orchestrator
                # validates and verifies the output.
                self.store.transition(run_id, "testing",
                                      note="Test turn completed; independent verification pending.")
            else:
                # Delivered means the harness finished a turn that carried the follow-up; a
                # turn cut short (revised, failed) leaves it pending for the next one.
                new_ids = [fu["id"] for fu in pending_follow_ups
                           if fu["id"] not in current.get("follow_ups_delivered", [])]
                self.store.transition(run_id, "implemented",
                                      note="Native turn finished; independent verification has not completed.",
                                      **({"follow_ups_delivered": current.get("follow_ups_delivered", []) + new_ids}
                                         if new_ids else {}))
                if new_ids:
                    self.store.event(spec["id"], "follow_up_delivered",
                                     {"follow_up_ids": new_ids, "attempt": turn_key}, run_id)
            return ("ready" if phase == "clarify"
                    else "tests_written" if is_test_phase
                    else "implemented")
        except asyncio.CancelledError:
            self.store.transition(run_id, "interrupted", error="Worker stopped; explicit Resume required")
            raise
        except Exception as exc:
            message = str(exc) or f"{type(exc).__name__} while {activity}"
            # A raiser that knows the cause classifies it; wording is only a
            # fallback, and it cannot tell a dropped socket from a missing login.
            classified = getattr(exc, "terminal", None)
            blocked = classified if classified is not None else any(
                word in message.lower() for word in
                ("auth", "login", "credential", "unavailable", "not found", "no such file",
                 "permission denied", "image", "ready tool", "api key"))
            self.store.transition(run_id, "blocked" if blocked else "failed", error=message,
                                  error_details={"exception": type(exc).__name__, "activity": activity,
                                                 "traceback": traceback.format_exc()})
            return "blocked" if blocked else "failed"
        finally:
            if paused:
                await self.runtime.unpause(container)
            if process and process.returncode is None:
                atomic_json(root / "bridge" / "cancel.json", {"turn_key": turn_key})
                try:
                    await asyncio.wait_for(process.wait(), 7)
                except TimeoutError:
                    pass
            cleanup_warning = await self.runtime.stop(container)
            if process and process.returncode is None:
                await process.wait()
            if read_task:
                await asyncio.gather(read_task, return_exceptions=True)
            current = self.store.get("run", run_id)
            if isinstance(cleanup_warning, str):
                # Surface the secrets-deletion failure on the run, not only in the log:
                # the error field shows it now, and the error event keeps it in the
                # run's activity after a later transition clears the error field.
                self.store.event(run["comparison_id"], "error",
                                 {"message": cleanup_warning,
                                  "message_type": "secrets_cleanup_failed"}, run_id)
                existing = current.get("error")
                current = self.store.update("run", run_id, error=(
                    (existing + "\n" if existing else "") + cleanup_warning))
            if current["status"] in TERMINAL:
                self.store.update("run", run_id, ended_at=now())
            attempts = current["attempts"]
            if attempts and attempts[-1]["id"] == turn_key:
                attempts[-1].update(ended_at=now(), outcome=current["status"],
                                   active_seconds=locals().get("active", 0.))
                self.store.update("run", run_id, attempts=attempts)
            if current["status"] in TERMINAL and (root / "workspace").exists():
                try:
                    if tree_hash(root / "workspace") != spec["baseline_sha256"]:
                        partial = root / "artifacts" / ("partial-" + turn_key[:8])
                        await finish_file_operation(copy_tree, root / "workspace", partial / "snapshot")
                        immutable(partial / "snapshot")
                        patch(baseline, partial / "snapshot", partial / "implementation.patch")
                        self.store.update("run", run_id, artifacts={
                            "snapshot": str(partial / "snapshot"),
                            "patch": str(partial / "implementation.patch"),
                            "sha256": tree_hash(partial / "snapshot"),
                            "partial": True, "note": "Interrupted or failed implementation; not independently verified."})
                        await finish_file_operation(self.service.publish_run_output, run_id)
                except Exception as exc:
                    self.store.event(run["comparison_id"], "artifact_error", {"message": str(exc)}, run_id)

    def freeze_requirements(self, id):
        """Commit the shared requirements before coding, without waiting for the planner."""
        created = False
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            c = json.loads(db.execute("SELECT data FROM objects WHERE kind='comparison' AND id=?",
                                     (id,)).fetchone()[0])
            frozen = next((s for s in c["specifications"] if s["version"] == c["spec_version"]), None)
            if frozen is None:
                frozen = {"version": c["spec_version"], "task": c["prompt"], "answers": c["answers"],
                          "plan": None, "baseline_sha256": c["baseline_sha256"],
                          "frozen_at": now(), "planning": {"status": "pending"}}
                c["specifications"].append(frozen)
                created = True
            if "development_plan" not in frozen:
                # Freeze shared coding criteria before asynchronous evaluation
                # planning. Each phase filters these when recipient histories differ.
                baseline = self.config.root / "comparisons" / id / "baseline"
                frozen["development_plan"] = (frozen.get("plan") or c.get("validation_plan")
                                               or default_plan(c, baseline).model_dump())
            c["frozen"] = True
            c["plan"] = frozen["plan"]
            db.execute("UPDATE objects SET data=? WHERE kind='comparison' AND id=?", (json.dumps(c), id))
        if created:
            self.store.event(id, "specification_frozen", frozen)
            atomic_json(self.config.root / "comparisons" / id / "specifications"
                        / str(frozen["version"]) / "specification.json", frozen)
        return {**c, "prompt": frozen["task"], "answers": frozen["answers"],
                "development_plan": frozen["development_plan"]}

    async def review_tests(self, id, baseline, task_spec, obsolete):
        """Draft intent, then wait for a human; no executable tests exist here."""
        while not obsolete():
            current = self.store.get("comparison", id)
            review = current["test_review"]
            if review.get("mode") == "saved_code":
                desired = "reviewing_tests" if review["status"] in ("queued", "drafting", "approved") else "waiting_test_review"
                if current["status"] != desired:
                    self.store.update("comparison", id, status=desired)
            if review["requirements_sha256"] != requirements_digest(current):
                raise ValueError("Test review does not match the current requirements")
            if review["status"] == "approved":
                return {**{key: review[key] for key in ("id", "revision", "spec_version", "proposal",
                        "answers", "approved_at", "approval_sha256", "requirements_sha256")},
                        "test_coverage": review.get("test_coverage", "comprehensive"),
                        "supplied_plan_sha256": review.get("supplied_plan_sha256")}
            if review["status"] == "queued":
                update_review(self.store, id, review["id"], status="drafting", error=None)
                job = asyncio.create_task(propose_tests(task_spec, baseline, self.config.planning_for_comparison(task_spec), review))
                try:
                    while not job.done():
                        if obsolete() or self.store.get("comparison", id)["test_review"]["id"] != review["id"]:
                            break
                        await asyncio.wait({job}, timeout=.3)
                    if job.done() and not obsolete():
                        proposal, metadata = await job
                        proposal = TestProposal.model_validate(proposal).model_dump()
                        update_review(self.store, id, review["id"], status="awaiting_review",
                                      proposal=proposal, planning=metadata, proposed_at=now(),
                                      supplied_plan_sha256=metadata.get("plan_sha256")
                                          if metadata.get("source") == "supplied plan" else None)
                except Exception as exc:
                    update_review(self.store, id, review["id"], status="proposal_failed",
                                  error="Test proposal failed: " + (str(exc) or type(exc).__name__))
                finally:
                    if not job.done():
                        job.cancel()
                        update_review(self.store, id, review["id"], status="queued")
                    await asyncio.gather(job, return_exceptions=True)
            else:
                if (id in self.active and review.get("mode") == "saved_code"
                        and self.waiting_only_for_test_review(current)):
                    # Return the scheduler's slot while keeping the review and
                    # saved evaluation evidence. Approval/feedback makes it runnable.
                    self.store.update("comparison", id, status="waiting_test_review")
                    return None
                await asyncio.sleep(.3)
        return None

    async def approved_plan(self, id, baseline, task_spec, obsolete):
        """Feedback supersedes in-flight generation; failure never bypasses review.

        Returns a dict with key '_new_flow':
          True  → {"_new_flow": True, "review": review_dict}
          False → {"_new_flow": False, "plan": Plan, "metadata": dict, "review": review_dict}
        Returns None when the caller should stop (cancelled/obsolete).
        """
        while not obsolete():
            review = await self.review_tests(id, baseline, task_spec, obsolete)
            if review is None:
                return None
            latest_review = self.store.get("comparison", id)["test_review"]
            if latest_review["id"] != review["id"] or latest_review["status"] != "approved":
                continue
            # New-flow comparisons have no plan_sha256 (harnesses write their own tests).
            # Legacy comparisons already have plan_sha256 from an earlier generation run.
            is_new_flow = not latest_review.get("plan_sha256")
            if is_new_flow:
                return {"_new_flow": True, "review": review}
            # Legacy path: planning model writes shared executable tests.
            update_review(self.store, id, review["id"], generation_status="generating", error=None)
            job = asyncio.create_task(prepare_plan(
                {**task_spec, "approved_test_review": review,
                 "planning_retry": {
                     "requested_at": latest_review.get("generation_retry_requested_at"),
                     "attempts": latest_review.get("generation_attempts", []),
                 },
                 "planning_artifact_dir": str(baseline.parent / "test-reviews" / review["id"] / uid())},
                baseline, self.config.planning_for_comparison(task_spec)))
            try:
                superseded = False
                while not job.done():
                    latest = self.store.get("comparison", id)["test_review"]
                    if obsolete() or latest["id"] != review["id"] or latest["status"] != "approved":
                        superseded = True
                        break
                    await asyncio.wait({job}, timeout=.3)
                if superseded:
                    continue
                plan, metadata = await job
                issues = review_alignment_issues(plan, review)
                if issues:
                    raise ValueError("; ".join(issues))
                plan.review_id = review["id"]
                return {"_new_flow": False, "plan": plan, "metadata": metadata, "review": review}
            except Exception as exc:
                update_review(self.store, id, review["id"], status="generation_failed",
                              generation_status="failed",
                              generation_attempts=getattr(exc, "planning_attempts", []),
                              error="Approved tests could not be developed: " + (str(exc) or type(exc).__name__))
            finally:
                if not job.done():
                    job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        return None

    async def review_saved_code(self, id):
        """Test-only feedback reuses saved candidates and preserves prior evidence."""
        c = self.store.get("comparison", id)
        version = c["spec_version"]
        baseline = self.config.root / "comparisons" / id / "baseline"
        def obsolete():
            latest = self.store.get("comparison", id)
            return latest["cancel_requested"] or latest["spec_version"] != version
        self.store.update("comparison", id, status="waiting_test_review")
        try:
            while not obsolete():
                prepared = await self.approved_plan(id, baseline, c, obsolete)
                if prepared is None:
                    break
                if prepared["_new_flow"]:
                    # New flow: each harness re-runs write_tests against its saved snapshot.
                    review = prepared["review"]
                    with self.store.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        latest = json.loads(db.execute(
                            "SELECT data FROM objects WHERE kind='comparison' AND id=?",
                            (id,)).fetchone()[0])
                        if latest["test_review"]["id"] != review["id"] or latest["test_review"]["status"] != "approved":
                            continue
                        latest["test_review"].update(generation_status="complete")
                        latest["status"] = "reviewing_tests"
                        db.execute("UPDATE objects SET data=? WHERE kind='comparison' AND id=?",
                                   (json.dumps(latest), id))
                    task_spec = {**c, "approved_test_review": review}
                    await self.run_saved_code_test_stage(id, task_spec, review, version)
                    return
                plan, metadata, review = prepared["plan"], prepared["metadata"], prepared["review"]
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    latest = json.loads(db.execute("SELECT data FROM objects WHERE kind='comparison' AND id=?",
                                                   (id,)).fetchone()[0])
                    if latest["test_review"]["id"] != review["id"] or latest["test_review"]["status"] != "approved":
                        continue
                    latest["test_review"].update(plan_sha256=plan_digest(plan),
                                                generation_status="complete", generation=metadata)
                    latest["status"] = "reviewing_tests"
                    db.execute("UPDATE objects SET data=? WHERE kind='comparison' AND id=?", (json.dumps(latest), id))
                self.service.reverify(id, validation_plan=plan.model_dump(),
                    reason=f"Human-approved test review revision {review['revision']} ({review['id']}); saved code unchanged.")
                await self.reverify(id)
                return
            if self.store.get("comparison", id)["cancel_requested"]:
                self.store.update("comparison", id, status="interrupted",
                                  error="Test review cancelled. Previous code and evaluations are retained.")
        except asyncio.CancelledError:
            self.store.update("comparison", id, status="waiting_test_review")
            raise
        except Exception as exc:
            self.store.update("comparison", id, status="incomplete", error=str(exc))

    async def freeze(self, id, baseline, spec=None):
        """Prepare one version's independent rubric and baseline alongside coding."""
        c = spec or self.freeze_requirements(id)
        version = c["spec_version"]
        def obsolete():
            latest = self.store.get("comparison", id)
            return latest["cancel_requested"] or latest["spec_version"] != version

        if obsolete():
            return None
        frozen = next(s for s in c["specifications"] if s["version"] == version)
        while not frozen.get("plan"):
            review = None
            if c.get("test_review"):
                prepared = await self.approved_plan(id, baseline, c, obsolete)
                if prepared is None:
                    return None
                if prepared["_new_flow"]:
                    # New flow: no shared Plan is generated.  Record approval and return
                    # (None, None, task_spec_with_review) so each harness can run its own
                    # write_tests phase after getting the approved specification.
                    review = prepared["review"]
                    if obsolete():
                        return None
                    with self.store.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        latest = json.loads(db.execute(
                            "SELECT data FROM objects WHERE kind='comparison' AND id=?",
                            (id,)).fetchone()[0])
                        if latest["test_review"]["id"] != review["id"] or latest["test_review"]["status"] != "approved":
                            continue  # re-enter while loop
                        latest["test_review"].update(generation_status="complete")
                        updated_frozen = {**frozen, "test_review": review, "planned_at": now()}
                        latest["specifications"] = [updated_frozen if s["version"] == version else s
                                                    for s in latest["specifications"]]
                        db.execute("UPDATE objects SET data=? WHERE kind='comparison' AND id=?",
                                   (json.dumps(latest), id))
                    self.store.event(id, "test_review_approved_new_flow",
                                     {"spec_version": version, "review_id": review["id"]})
                    return None if obsolete() else (None, None, {
                        **c, "plan": None, "approved_test_review": review})
                plan, planning, review = prepared["plan"], prepared["metadata"], prepared["review"]
            else:
                job = asyncio.create_task(prepare_plan(c, baseline, self.config.planning_for_comparison(c)))
                try:
                    while not job.done():
                        if obsolete():
                            return None
                        await asyncio.wait({job}, timeout=.2)
                    plan, planning = await job
                except Exception as exc:
                    reason = str(exc) or type(exc).__name__
                    plan, planning = default_plan(c, baseline, reason=reason), {
                        "error": reason, "usage": None,
                        "attempts": getattr(exc, "planning_attempts", [])}
                finally:
                    if not job.done():
                        job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
            if obsolete():
                return None
            updated = {**frozen, "plan": plan.model_dump(), "planning": planning, "planned_at": now()}
            if review:
                updated["test_review"] = review
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                latest = json.loads(db.execute("SELECT data FROM objects WHERE kind='comparison' AND id=?",
                                               (id,)).fetchone()[0])
                if review:
                    if latest["test_review"]["id"] != review["id"] or latest["test_review"]["status"] != "approved":
                        continue
                    latest["test_review"].update(plan_sha256=plan_digest(plan),
                                                generation_status="complete", generation=planning)
                frozen = updated
                latest["specifications"] = [frozen if s["version"] == version else s
                                            for s in latest["specifications"]]
                if latest["spec_version"] == version:
                    latest["plan"] = frozen["plan"]
                db.execute("UPDATE objects SET data=? WHERE kind='comparison' AND id=?", (json.dumps(latest), id))
            self.store.event(id, "evaluation_plan_ready", {"spec_version": version, "planning": planning})
        plan = Plan.model_validate(frozen["plan"])
        version_dir = baseline.parent / "specifications" / str(version)
        atomic_json(version_dir / "specification.json", frozen)
        if obsolete():
            return None
        result_file = version_dir / "baseline-results.json"
        if result_file.exists():
            baseline_results = json.loads(result_file.read_text())
        else:
            baseline_results = await CandidateTestRunner(self.config).run(
                baseline, plan, version_dir / "baseline-evidence",
                cancellation=obsolete, baseline_snapshot=baseline)
            if obsolete():
                return None
            atomic_json(result_file, baseline_results)
        return None if obsolete() else (plan, baseline_results, {
            **c, "plan": frozen["plan"], "approved_test_review": frozen.get("test_review")})

    async def snapshot_run(self, run_id, c):
        """Publish completed code even when its independent planner is still running."""
        run = self.store.get("run", run_id)
        root = self.run_root(run)
        artifacts = root / "artifacts" / f"v{c['spec_version']}-{uid()[:8]}"
        artifacts.mkdir(parents=True)
        snapshot = artifacts / "snapshot"
        await finish_file_operation(copy_tree, root / "workspace", snapshot)
        immutable(snapshot)
        patch(root.parent.parent / "baseline", snapshot, artifacts / "implementation.patch")
        self.store.update("run", run_id, artifacts={
            "patch": str(artifacts / "implementation.patch"), "snapshot": str(snapshot),
            "evidence": str(artifacts / "evidence"), "sha256": tree_hash(snapshot)})
        await finish_file_operation(self.service.publish_run_output, run_id)

    async def test_snapshot_run(self, run_id, c):
        """Store the post-test-stage workspace as test_snapshot, preserving the development snapshot.

        DESIGN 9.1: run.artifacts.snapshot is the implementation snapshot (never overwritten);
        run.artifacts.test_snapshot is the final tested snapshot (written here).
        """
        run = self.store.get("run", run_id)
        root = self.run_root(run)
        impl_artifacts = run.get("artifacts", {})
        artifacts = root / "artifacts" / f"v{c['spec_version']}-test-{uid()[:8]}"
        artifacts.mkdir(parents=True)
        test_snap = artifacts / "snapshot"
        await finish_file_operation(copy_tree, root / "workspace", test_snap)
        immutable(test_snap)
        baseline = root.parent.parent / "baseline"
        patch(baseline, test_snap, artifacts / "test.patch")
        test_sha = tree_hash(test_snap)
        # Preserve all existing artifact fields; add test_snapshot entries.
        self.store.update("run", run_id, artifacts={
            **impl_artifacts,
            "test_snapshot": str(test_snap),
            "test_snapshot_sha256": test_sha,
            "test_patch": str(artifacts / "test.patch"),
        })
        await finish_file_operation(self.service.publish_run_output, run_id)

    async def evaluate_run(self, run_id, c, plan, baseline_results, *,
                           ratings=None, no_manifest_reason=None, incomplete_reason=None):
        run = self.store.get("run", run_id)
        root = self.run_root(run)
        # When no harness plan was produced (manifest missing/invalid) fall back to a
        # default (no checks) plan so relevance can still be computed from the diff.
        effective_plan = plan if plan is not None else default_plan(c, root.parent.parent / "baseline")
        if self.cancelled(run):
            self.store.transition(run_id, "cancelled")
            return
        self.store.transition(run_id, "evaluating", evaluation_scope_note=None)
        try:
            if not run.get("artifacts", {}).get("snapshot"):
                await self.snapshot_run(run_id, c)
                run = self.store.get("run", run_id)
            art = run["artifacts"]
            # DESIGN 9.1: for new-flow runs (plan was written by the harness, test_snapshot
            # exists), evaluate against the final tested snapshot; for legacy / no-manifest
            # paths fall back to the development snapshot.
            if plan is not None and art.get("test_snapshot") and art.get("test_snapshot_sha256"):
                snapshot = Path(art["test_snapshot"])
                snapshot_sha = art["test_snapshot_sha256"]
                patch_path = Path(art["test_patch"]) if art.get("test_patch") else None
                artifacts = snapshot.parent
            else:
                snapshot = Path(art["snapshot"])
                snapshot_sha = art["sha256"]
                patch_path = Path(art["patch"]) if art.get("patch") else None
                artifacts = snapshot.parent
            if tree_hash(snapshot) != snapshot_sha:
                raise RuntimeError("Immutable snapshot changed before evaluation")
            evidence = await CandidateTestRunner(self.config).run(snapshot, effective_plan, artifacts / "evidence",
                    baseline_results, cancellation=lambda: self.cancelled(run),
                    baseline_snapshot=root.parent.parent / "baseline",
                    verification_context=native_execution_evidence(self.store, run))
            if self.cancelled(run):
                self.store.transition(run_id, "cancelled")
                return
            diff_text = patch_path.read_text(errors="replace") if patch_path and patch_path.exists() else ""
            candidate = {"snapshot_sha256": snapshot_sha,
                         "diff": diff_text,
                         "files": repository_context(snapshot),
                         "repository_context": repository_context(root.parent.parent / "baseline"),
                         "evidence": evidence}
            atomic_json(artifacts / "case.json", {
                "original_task": c["prompt"], "shared_answers": c["answers"],
                "specification_version": c["spec_version"], "plan": effective_plan.model_dump(),
                "test_review": c.get("approved_test_review"),
                "baseline_sha256": c["baseline_sha256"], "candidate": candidate,
                "native_usage": self.store.get("run", run_id)["usage_records"],
                "execution_timestamps": self.store.get("run", run_id)["intervals"],
                # Store ratings for reevaluate() to pass through without re-running the review.
                "ratings": ratings})
            result = await self.score_candidate(run, c, effective_plan, candidate,
                                                ratings=ratings, no_manifest_reason=no_manifest_reason,
                                                incomplete_reason=incomplete_reason)
            if result is None:
                self.store.transition(run_id, "cancelled")
                return
            result["test_review_id"] = effective_plan.review_id
            # For new-flow comparisons (approved_test_review set, no shared plan_sha256),
            # the per-harness plan has review_id = approved_review["id"].  When the
            # manifest was never accepted, effective_plan is the default (review_id=None);
            # still bind the result to the approved review so comparison_complete works.
            if result["test_review_id"] is None and c.get("approved_test_review"):
                result["test_review_id"] = (c["approved_test_review"] or {}).get("id")
            if tree_hash(snapshot) != snapshot_sha:
                raise RuntimeError("Immutable snapshot changed during evaluation")
            atomic_json(artifacts / "evaluation.json", result)
            # Preserve any existing artifact fields (e.g. test_snapshot) while adding
            # the case/evaluation/evidence paths from this evaluation run.
            current_art = self.store.get("run", run_id).get("artifacts", {})
            self.store.transition(run_id, evaluation_status(result), evaluation=result, evidence=evidence,
                note="Independent verification finished.",
                artifacts={
                    **current_art,
                    "patch": str(Path(art.get("patch") or art.get("test_patch", ""))),
                    "snapshot": str(Path(art["snapshot"])),
                    "case": str(artifacts / "case.json"),
                    "evaluation": str(artifacts / "evaluation.json"),
                    "evidence": str(artifacts / "evidence"),
                    "sha256": art["sha256"],
                })
        except Exception as exc:
            self.store.transition(run_id, "evaluated_incomplete", error="Independent evaluation failed: " + str(exc))
            traceback.print_exc()

    async def run_test_stage(self, run_id, task_spec, baseline, version_dir):
        """Write-tests loop: write → validate → lock → verify → fix × fix_rounds.

        Returns (plan_or_none, stop_reason_dict, test_info_dict).
        plan_or_none   — per-harness Plan when manifest is valid, else None.
        stop_reason    — {code, message[, detail]} per DESIGN §10.
        test_info      — {manifest, verified_results, lock_violations, lock_file_paths,
                          evaluated_round} for review_harness_tests and callers.
        """
        fix_rounds = self.config.test_fix_rounds
        budget = self.config.test_active_seconds
        run = self.store.get("run", run_id)
        root = self.run_root(run)
        workspace = root / "workspace"
        id_ = task_spec["id"]
        approved_review = task_spec.get("approved_test_review") or {}
        spec_version = task_spec.get("spec_version", 1)

        lock: dict | None = None
        lock_file_paths: set = set()
        all_violations: list = []
        verified_results: dict = {}
        plan = None
        manifest_data = None
        evaluated_round: int | None = None
        last_verified_round: int | None = None  # set after each successful verification

        def _active():
            r = self.store.get("run", run_id)
            return sum(a.get("active_seconds", 0) for a in r["attempts"]
                       if a.get("phase") in _TEST_PHASES)

        def _remaining():
            return max(0.0, budget - _active())

        # ── W2: Stage entry persisted in run["test_stages"] ────────────────────
        stage_entry: dict = {
            "spec_version": spec_version,
            "status": "testing",
            "budget_seconds": budget,
            "used_seconds": 0,
            "rounds": [],
            "lock": None,
            "lock_violations": [],
            "evaluated_round": None,
            "stop_reason": None,
            "review": None,
        }

        def _persist_stage(status: str | None = None) -> None:
            if status is not None:
                stage_entry["status"] = status
            stage_entry["used_seconds"] = _active()
            stage_entry["lock_violations"] = list(all_violations)
            run_now = self.store.get("run", run_id)
            stages = list(run_now.get("test_stages") or [])
            idx = next((i for i, e in enumerate(stages)
                        if e.get("spec_version") == spec_version), None)
            if idx is None:
                stages.append(dict(stage_entry))
            else:
                stages[idx] = dict(stage_entry)
            self.store.update("run", run_id, test_stages=stages)

        def _make_stop(code: str, message: str, detail: str | None = None) -> dict:
            s: dict = {"code": code, "message": message}
            if detail is not None:
                s["detail"] = detail
            stage_entry["stop_reason"] = s
            return s

        def _test_info() -> dict:
            return {
                "manifest": manifest_data,
                "verified_results": verified_results,
                "lock_violations": all_violations,
                "lock_file_paths": lock_file_paths,
                "evaluated_round": evaluated_round,
            }

        # ── L3: Detect resume from a persisted completed write turn ────────────
        # Read the existing entry BEFORE the initial _persist_stage() call so
        # that overwriting it does not destroy the resume signal.
        _pre_existing_stages = self.store.get("run", run_id).get("test_stages") or []
        _existing_entry = next(
            (e for e in _pre_existing_stages if e.get("spec_version") == spec_version), None)
        skip_write = (
            _existing_entry is not None
            and _existing_entry.get("lock") is not None
            and any(r.get("kind") == "write" and r.get("outcome") == "tests_written"
                    for r in _existing_entry.get("rounds", []))
        )
        if skip_write:
            # Restore the pre-existing rounds and lock into stage_entry so
            # _persist_stage() does not lose them when it writes the entry.
            stage_entry["rounds"] = list(_existing_entry.get("rounds", []))
            stage_entry["lock"] = _existing_entry.get("lock")

        # R2-2: On resume with an existing lock, reload the full lock (including bytes)
        # from disk so enforce_lock() can restore files.  If the file is absent, record
        # the problem; we must not silently skip restoration.
        _lock_load_error: "str | None" = None
        if skip_write:
            _loaded_lock = _load_lock_from_disk(root)
            if _loaded_lock is not None:
                lock = _loaded_lock
                lock_file_paths = set(lock.get("files", {}).keys())
            else:
                _lock_load_error = (
                    "Lock bytes file not found after restart; "
                    "test-file restoration is unavailable for this resumed stage."
                )

        _persist_stage()

        # R2-1: If the budget is already spent on entry (resume scenario), do not dispatch
        # another turn.  With a lock, re-verify once and report budget_exhausted (rated).
        # Without a lock, report budget_exhausted_no_manifest (accuracy/tests 0).
        if _remaining() <= 0:
            if skip_write and lock is not None:
                # Lock exists from a previous session.  Re-verify the workspace once to
                # get fresh results.
                manifest_data, _merr = load_manifest(workspace, approved_review=approved_review)
                _plan_resume, _perr = (
                    build_plan(workspace, manifest_data, approved_review)
                    if manifest_data and not _merr else (None, [])
                )
                if _plan_resume is not None:
                    plan = _plan_resume
                    _prior_rounds = stage_entry.get("rounds", [])
                    _prior_round_nums = [
                        r.get("number", 0) for r in _prior_rounds
                        if r.get("verification") is not None
                        or r.get("outcome") in ("tests_written", "all_passed", "rounds_exhausted")
                    ]
                    evaluated_round = max(_prior_round_nums) if _prior_round_nums else 0
                    stage_entry["evaluated_round"] = evaluated_round
                    _ev_dir = (version_dir / "test-evidence" / run_id[:8]
                               / ("resume-verify-" + uid()[:6]))
                    _ev_dir.mkdir(parents=True, exist_ok=True)
                    _snap = _ev_dir / "snapshot"
                    await finish_file_operation(copy_tree, workspace, _snap)
                    immutable(_snap)
                    try:
                        verified_results = await CandidateTestRunner(self.config).run(
                            _snap, plan, _ev_dir / "evidence",
                            cancellation=lambda: self.cancelled(self.store.get("run", run_id)),
                            baseline_snapshot=baseline,
                        )
                    except Exception as _exc:
                        stop = _make_stop(
                            "infrastructure_error",
                            f"An infrastructure error occurred during resume verification: {_exc}. "
                            "The evaluation is incomplete; this is not the harness's fault.")
                        _persist_stage("stopped")
                        return plan, stop, _test_info()
                    _failing_resume = _scored_failing(verified_results, plan)
                    _fail_ids_resume = [ch.get("id", "?") for ch in _failing_resume[:5]]
                    _eval_label_resume = f"round {evaluated_round}"
                    stop = _make_stop(
                        "budget_exhausted",
                        f"The {budget}-second test budget was already spent on resume; "
                        f"{len(_failing_resume)} scored check(s) still failing. "
                        f"Scored the code from {_eval_label_resume}.",
                        detail=", ".join(_fail_ids_resume))
                    _persist_stage("complete")
                    return plan, stop, _test_info()
            # No lock or no valid manifest: budget_exhausted_no_manifest
            stop = _make_stop(
                "budget_exhausted_no_manifest",
                f"The {budget}-second test budget was already spent on resume before an "
                "acceptable test manifest existed. Accuracy and Tests are 0.")
            _persist_stage("stopped")
            return None, stop, _test_info()

        # ── Fix loop ───────────────────────────────────────────────────────────
        for round_num in range(fix_rounds + 1):

            # ── Write turn (first iteration only, unless resuming) ──────────
            if round_num == 0 and not skip_write:
                # R2-1: guard — never dispatch a turn when budget is already 0.
                if _remaining() <= 0:
                    stop = _make_stop(
                        "budget_exhausted_no_manifest",
                        f"The {budget}-second test budget ran out before the write turn "
                        "could start. Accuracy and Tests are 0.")
                    _persist_stage("stopped")
                    return None, stop, _test_info()
                outcome = await self.phase(run_id, "test", task_spec, None,
                                           active_budget=_remaining(),
                                           prior_active_seconds=_active())
                write_entry: dict = {
                    "number": 0, "kind": "write",
                    "active_seconds": _active(), "outcome": outcome,
                    "validation_errors": [], "verification": None, "lock_violations": [],
                }
                stage_entry["rounds"].append(write_entry)

                if outcome == "revised":  # L1
                    new_ver = spec_version + 1
                    stop = _make_stop("spec_revised",
                        f"The specification was revised during the test stage; this test stage "
                        f"was abandoned and a new one runs against revision {new_ver}.")
                    _persist_stage("stopped")
                    return None, stop, {}

                if outcome != "tests_written":
                    cause = str(outcome)
                    if outcome in ("infrastructure_error", "blocked"):  # W7
                        stop = _make_stop("infrastructure_error",
                            f"{cause} stopped the test stage before any test manifest was "
                            "accepted. The evaluation is incomplete; this is not the harness's fault.")
                    elif outcome == "test_budget_exhausted":
                        stop = _make_stop("budget_exhausted_no_manifest",
                            f"The {budget}-second test budget ran out during the write turn "
                            "before an acceptable test manifest existed. Accuracy and Tests are 0.")
                    elif outcome == "cancelled":
                        stop = _make_stop("cancelled",
                            "The comparison was cancelled during the write turn; "
                            "this run was not scored.")
                    else:
                        stop = _make_stop("harness_failed",
                            f"The write turn ended without a result: {cause}. "
                            "Accuracy and Tests are 0.")
                    _persist_stage("stopped")
                    return None, stop, _test_info()
                _persist_stage()

            # ── Validate manifest ───────────────────────────────────────────
            manifest_data, errors = load_manifest(workspace, approved_review=approved_review)
            if manifest_data is None or errors:
                msgs = errors if errors else [
                    f"{MANIFEST_NAME!r} was not found in the workspace root."]
                if round_num >= fix_rounds:
                    code = "manifest_missing" if manifest_data is None else "manifest_invalid"
                    if code == "manifest_missing":
                        msg = (f"The harness did not write the test manifest ({MANIFEST_NAME}). "
                               "Accuracy and Tests are 0.")
                    else:
                        msg = (f"The test manifest still failed validation after every repair "
                               f"turn: {'; '.join(msgs)}. Accuracy and Tests are 0.")
                    stop = _make_stop(code, msg)
                    _persist_stage("stopped")
                    return None, stop, _test_info()

                # Repair turn — W5: include full approved spec
                # R2-1: guard before dispatching repair
                if _remaining() <= 0:
                    stop = _make_stop(
                        "budget_exhausted_no_manifest",
                        f"The {budget}-second test budget ran out during repair before an "
                        "acceptable test manifest existed. Accuracy and Tests are 0.")
                    _persist_stage("stopped")
                    return None, stop, _test_info()
                spec_text = _format_approved_spec(approved_review)
                base_fb = format_repair_feedback(msgs)
                feedback = base_fb + spec_text if spec_text else base_fb
                outcome = await self.phase(run_id, "repair", task_spec, None,
                                           active_budget=_remaining(),
                                           prior_active_seconds=_active(),
                                           prompt_override=feedback)
                repair_entry: dict = {
                    "number": round_num, "kind": "repair",
                    "active_seconds": _active(), "outcome": outcome,
                    "validation_errors": msgs, "verification": None, "lock_violations": [],
                }
                stage_entry["rounds"].append(repair_entry)

                if outcome == "revised":  # L1
                    new_ver = spec_version + 1
                    stop = _make_stop("spec_revised",
                        f"The specification was revised during the test stage; this test stage "
                        f"was abandoned and a new one runs against revision {new_ver}.")
                    _persist_stage("stopped")
                    return None, stop, {}
                if outcome != "tests_written":
                    cause = str(outcome)
                    if outcome in ("infrastructure_error", "blocked"):
                        stop = _make_stop("infrastructure_error",
                            f"{cause} stopped the test stage before any test manifest was "
                            "accepted. The evaluation is incomplete; this is not the harness's fault.")
                    elif outcome == "test_budget_exhausted":
                        stop = _make_stop("budget_exhausted_no_manifest",
                            f"The {budget}-second test budget ran out during the repair turn "
                            "before an acceptable test manifest existed. Accuracy and Tests are 0.")
                    elif outcome == "cancelled":
                        stop = _make_stop("cancelled",
                            f"The comparison was cancelled during repair turn {round_num + 1}; "
                            "this run was not scored.")
                    else:
                        stop = _make_stop("harness_failed",
                            f"The repair turn ended without a result: {cause}. "
                            "Accuracy and Tests are 0.")
                    _persist_stage("stopped")
                    return None, stop, _test_info()
                _persist_stage()
                continue

            # ── Build plan ──────────────────────────────────────────────────
            plan, issues = build_plan(workspace, manifest_data, approved_review)
            if plan is None:
                if round_num >= fix_rounds:
                    msg = (f"The test manifest still failed validation after every repair turn: "
                           f"{'; '.join(issues)}. Accuracy and Tests are 0.")
                    stop = _make_stop("manifest_invalid", msg)
                    _persist_stage("stopped")
                    return None, stop, _test_info()

                # R2-1: guard before dispatching second repair
                if _remaining() <= 0:
                    stop = _make_stop(
                        "budget_exhausted_no_manifest",
                        f"The {budget}-second test budget ran out during repair before an "
                        "acceptable test manifest existed. Accuracy and Tests are 0.")
                    _persist_stage("stopped")
                    return None, stop, _test_info()
                spec_text = _format_approved_spec(approved_review)
                base_fb = format_repair_feedback(issues)
                feedback = base_fb + spec_text if spec_text else base_fb
                outcome = await self.phase(run_id, "repair", task_spec, None,
                                           active_budget=_remaining(),
                                           prior_active_seconds=_active(),
                                           prompt_override=feedback)
                repair_entry = {
                    "number": round_num, "kind": "repair",
                    "active_seconds": _active(), "outcome": outcome,
                    "validation_errors": issues, "verification": None, "lock_violations": [],
                }
                stage_entry["rounds"].append(repair_entry)

                if outcome == "revised":  # L1
                    new_ver = spec_version + 1
                    stop = _make_stop("spec_revised",
                        f"The specification was revised during the test stage; this test stage "
                        f"was abandoned and a new one runs against revision {new_ver}.")
                    _persist_stage("stopped")
                    return None, stop, {}
                if outcome != "tests_written":
                    cause = str(outcome)
                    if outcome in ("infrastructure_error", "blocked"):
                        stop = _make_stop("infrastructure_error",
                            f"{cause} stopped the test stage before any test manifest was "
                            "accepted. The evaluation is incomplete; this is not the harness's fault.")
                    elif outcome == "test_budget_exhausted":
                        stop = _make_stop("budget_exhausted_no_manifest",
                            f"The {budget}-second test budget ran out during the repair turn "
                            "before an acceptable test manifest existed. Accuracy and Tests are 0.")
                    elif outcome == "cancelled":
                        stop = _make_stop("cancelled",
                            f"The comparison was cancelled during repair turn {round_num + 1}; "
                            "this run was not scored.")
                    else:
                        stop = _make_stop("harness_failed",
                            f"The repair turn ended without a result: {cause}. "
                            "Accuracy and Tests are 0.")
                    _persist_stage("stopped")
                    return None, stop, _test_info()
                _persist_stage()
                continue

            # ── Set / enforce lock — W3: violations never trigger repair ───
            round_violations: list = []
            if lock is None:
                lock = create_lock(workspace, manifest_data)
                # R2-2: persist lock bytes to disk so they survive a worker restart.
                _save_lock_to_disk(root, lock)
                lock_file_paths = set(lock.get("files", {}).keys())
                lock_for_store = {
                    "files": {p: info["sha256"] for p, info in lock.get("files", {}).items()},
                    "auto_locked": lock.get("auto_locked", []),
                }
                stage_entry["lock"] = lock_for_store
                self.store.update("run", run_id, test_lock=list(lock_file_paths))
                _persist_stage()
            else:
                # R2-2: if lock was not reloaded from disk, skip restoration and record
                # the problem so the caller knows files could not be verified.
                if _lock_load_error:
                    all_violations.append({
                        "round": round_num, "path": "(all locked files)",
                        "kind": "infrastructure_error",
                        "detail": _lock_load_error,
                    })
                else:
                    round_violations = enforce_lock(workspace, lock, round_num)
                    all_violations.extend(round_violations)

            # ── Independent sandbox verification ───────────────────────────
            evidence_dir = (version_dir / "test-evidence" / run_id[:8]
                            / (str(round_num) + "-" + uid()[:6]))
            evidence_dir.mkdir(parents=True, exist_ok=True)
            temp_snap = evidence_dir / "snapshot"
            await finish_file_operation(copy_tree, workspace, temp_snap)
            immutable(temp_snap)
            try:
                verified_results = await CandidateTestRunner(self.config).run(
                    temp_snap, plan, evidence_dir / "evidence",
                    cancellation=lambda: self.cancelled(self.store.get("run", run_id)),
                    baseline_snapshot=baseline,
                )
            except Exception as exc:
                self.store.event(id_, "test_verification_error",
                                 {"round": round_num, "error": str(exc)}, run_id)
                # §14 / W7: distinguish whether a prior round was verified.
                # (a) No prior completed round (last_verified_round is None): return None so
                #     the caller routes to evaluated_incomplete via incomplete_reason.
                # (b) A prior completed round exists: use it with rated scores.
                if last_verified_round is None:
                    stop = _make_stop("infrastructure_error",
                        f"{exc} stopped the test stage before any round was verified. "
                        "The evaluation is incomplete; this is not the harness's fault.")
                    _persist_stage("stopped")
                    return None, stop, _test_info()
                else:
                    evaluated_round = last_verified_round
                    stage_entry["evaluated_round"] = evaluated_round
                    stop = _make_stop("infrastructure_error",
                        f"An infrastructure error stopped verification in fix round "
                        f"{round_num}: {exc}. "
                        f"Scored the code from round {last_verified_round}; "
                        "this is not the harness's fault.")
                    _persist_stage("stopped")
                    return plan, stop, _test_info()

            # Verification succeeded; record it as the last verified round.
            last_verified_round = round_num

            checks_now = verified_results.get("checks", [])
            verify_entry: dict = {
                "number": round_num,
                "kind": "write" if round_num == 0 else "fix",
                "active_seconds": _active(),
                "outcome": "verified",
                "validation_errors": [],
                "verification": {
                    "passed": sum(1 for c in checks_now if c.get("status") == "passed"),
                    "failed": sum(1 for c in checks_now if c.get("status") == "failed"),
                    "errors": sum(1 for c in checks_now
                                  if c.get("status") not in ("passed", "failed")),
                    "failing_checks": [c.get("id", "?") for c in checks_now
                                       if c.get("status") != "passed"],
                },
                "lock_violations": round_violations,
            }

            # W4: only scored checks determine failing
            failing = _scored_failing(verified_results, plan)

            if not failing:
                evaluated_round = round_num
                stage_entry["evaluated_round"] = evaluated_round
                passed_count = sum(1 for c in checks_now if c.get("status") == "passed")
                stop = _make_stop("all_passed",
                    f"All {passed_count} scored checks passed in round {round_num}.")
                self.store.event(id_, "test_stage_all_passed",
                                 {"round": round_num, "total": len(checks_now)}, run_id)
                verify_entry["outcome"] = "all_passed"
                stage_entry["rounds"].append(verify_entry)
                _persist_stage("complete")
                return plan, stop, _test_info()

            if round_num >= fix_rounds:
                evaluated_round = round_num
                stage_entry["evaluated_round"] = evaluated_round
                failing_ids = [c.get("id", "?") for c in failing[:5]]
                self.store.event(id_, "test_stage_rounds_exhausted",
                                 {"round": round_num, "failing": len(failing)}, run_id)
                stop = _make_stop("rounds_exhausted",
                    f"All {fix_rounds} fix rounds were used; {len(failing)} scored check(s) "
                    f"still failed: {', '.join(failing_ids)}. "
                    f"Scored the code from round {round_num}.",
                    detail=", ".join(failing_ids))
                verify_entry["outcome"] = "rounds_exhausted"
                stage_entry["rounds"].append(verify_entry)
                _persist_stage("complete")
                return plan, stop, _test_info()

            # ── Fix turn — W3: include violation notice if any ─────────────
            check_to_scenario: dict = {}
            review_coverage = getattr(plan, "review_coverage", None) or {}
            approved_scenarios = {tc["id"]: tc.get("title", tc.get("description", ""))
                                  for tc in (approved_review.get("proposal") or {}).get("test_cases", [])}
            for scenario_id, check_ids in review_coverage.items():
                for cid in (check_ids or []):
                    check_to_scenario[cid] = (scenario_id, approved_scenarios.get(scenario_id, ""))
            remaining_secs = max(0, int(budget - _active()))
            failing_checks = []
            for c_item in failing:
                cid = c_item.get("id", "")
                s_id, s_title = check_to_scenario.get(cid, ("?", ""))
                failing_checks.append({
                    "id": cid, "scenario_id": s_id, "scenario_title": s_title,
                    "command": c_item.get("command"), "exit_code": c_item.get("exit_code"),
                    "counts": c_item.get("counts"),
                    "log_tail": bounded_log_tail(c_item.get("log") or ""),
                })
            fix_feedback = format_fix_feedback(
                failing_checks, list(lock_file_paths),
                round_num + 1, fix_rounds, remaining_secs)
            # W3: prepend violation notice when locked files were modified
            if round_violations:
                violation_notice = " ".join(
                    f"Locked test file '{v['path']}' was {v['kind']} and has been restored."
                    for v in round_violations
                )
                fix_feedback = (violation_notice
                                + " Edit the implementation, not the tests.\n\n"
                                + fix_feedback)

            stage_entry["rounds"].append(verify_entry)
            _persist_stage()

            # R2-1: guard before fix turn — if budget is 0 at this point, evaluate now.
            if _remaining() <= 0:
                evaluated_round = round_num
                stage_entry["evaluated_round"] = evaluated_round
                _fail_ids_guard = [ch.get("id", "?") for ch in failing[:5]]
                _eval_label_guard = f"round {round_num}"
                stop = _make_stop(
                    "budget_exhausted",
                    f"The {budget}-second test budget ran out before fix round "
                    f"{round_num + 1} of {fix_rounds} could start; "
                    f"{len(failing)} scored check(s) still failing. "
                    f"Scored the code from {_eval_label_guard}.",
                    detail=", ".join(_fail_ids_guard))
                _persist_stage("complete")
                return plan, stop, _test_info()

            outcome = await self.phase(run_id, "fix", task_spec, None,
                                       active_budget=_remaining(),
                                       prior_active_seconds=_active(),
                                       prompt_override=fix_feedback)
            fix_entry: dict = {
                "number": round_num, "kind": "fix",
                "active_seconds": _active(), "outcome": outcome,
                "validation_errors": [], "verification": None,
                "lock_violations": round_violations,
            }
            stage_entry["rounds"].append(fix_entry)

            if outcome == "revised":  # L1
                new_ver = spec_version + 1
                stop = _make_stop("spec_revised",
                    f"The specification was revised during the test stage; this test stage "
                    f"was abandoned and a new one runs against revision {new_ver}.")
                _persist_stage("stopped")
                return None, stop, {}

            if outcome != "tests_written":
                cause = str(outcome)
                evaluated_round = round_num
                stage_entry["evaluated_round"] = evaluated_round
                if outcome in ("infrastructure_error", "blocked"):
                    # R2-3 / DESIGN §14: after the lock, infrastructure error uses the last
                    # completed round with rated scores.  Never say "incomplete" next to
                    # rated scores.
                    _eval_label_infra = f"round {round_num}"
                    stop = _make_stop("infrastructure_error",
                        f"An infrastructure error ended fix round {round_num + 1}: {cause}. "
                        f"Scored the code from {_eval_label_infra}; "
                        "this is not the harness's fault.")
                    _persist_stage("complete")  # §14: rated evaluation
                elif outcome == "test_budget_exhausted":
                    failing_ids = [c.get("id", "?") for c in failing[:5]]
                    stop = _make_stop("budget_exhausted",
                        f"The {budget}-second test budget ran out during fix round "
                        f"{round_num + 1} of {fix_rounds}; {len(failing)} scenario(s) were "
                        f"still failing: {', '.join(failing_ids)}. "
                        f"Scored the code from round {round_num}.",
                        detail=", ".join(failing_ids))
                    _persist_stage("complete")
                elif outcome == "cancelled":
                    stop = _make_stop("cancelled",
                        f"The comparison was cancelled during fix round "
                        f"{round_num + 1} of {fix_rounds}; this run was not scored.")
                    _persist_stage("stopped")
                else:
                    stop = _make_stop("harness_failed",
                        f"Fix round {round_num + 1} ended without a result: {cause}; "
                        f"{len(failing)} scored check(s) still failed. "
                        f"Scored the code from round {round_num}.")
                    _persist_stage("complete")
                return plan, stop, _test_info()
            _persist_stage()

        # Fallback (should not be reached)
        stop = _make_stop("rounds_exhausted", f"All {fix_rounds} fix round(s) exhausted.")
        _persist_stage("complete")
        return plan, stop, _test_info()

    async def run_saved_code_test_stage(self, id, task_spec, review, version):
        """Test stage for the saved-code path: re-run test-writing against each harness's snapshot.

        Called from review_saved_code() when a new test review is approved in new-flow mode
        and each harness already has a saved implementation snapshot.
        """
        c = self.store.detail(id)
        baseline = self.config.root / "comparisons" / id / "baseline"
        version_dir = baseline.parent / "specifications" / str(version)
        full_task_spec = {**task_spec, "approved_test_review": review}

        eligible = [r for r in c["runs"]
                    if r.get("artifacts", {}).get("snapshot")
                    and r.get("spec_version") == version]

        async def one_saved(run):
            run_id = run["id"]
            snapshot_path = Path(run["artifacts"]["snapshot"])
            root = self.run_root(run)
            workspace = root / "workspace"

            # Restore workspace from snapshot so it has the implementation but no stale test files.
            make_writable(workspace)
            if workspace.exists():
                await asyncio.to_thread(shutil.rmtree, workspace)
            await finish_file_operation(copy_tree, snapshot_path, workspace)
            make_writable(workspace)

            # Archive current artifacts into history before resetting (mirrors reverify() behaviour).
            history = run.get("artifact_history", [])
            if run.get("artifacts"):
                history = history + [{"spec_version": run["spec_version"], "archived_at": now(),
                                       "artifacts": run["artifacts"], "evaluation": run.get("evaluation"),
                                       "evidence": run.get("evidence"),
                                       "reason": "saved-code test-review re-run"}]

            # Reset run to allow test-stage phases.
            self.store.transition(run_id, "queued", ended_at=None, error=None, error_details=None,
                                  error_terminal=None, evaluation=None, evidence=None,
                                  artifact_history=history)

            # Run test stage.
            harness_plan, stop_reason, test_info = await self.run_test_stage(
                run_id, full_task_spec, baseline, version_dir)
            self.store.update("run", run_id, test_stop_reason=stop_reason)

            if harness_plan is None:
                # W7: infrastructure errors → incomplete_reason
                if stop_reason.get("code") == "infrastructure_error":
                    await self.evaluate_run(run_id, full_task_spec, None, None,
                                            incomplete_reason=stop_reason.get("message", ""))
                else:
                    no_manifest_msg = stop_reason.get("detail") or stop_reason.get("message", "")
                    await self.evaluate_run(run_id, full_task_spec, None, None,
                                            no_manifest_reason=no_manifest_msg)
                return

            # Store the final tested snapshot (DESIGN 9.1: preserves development snapshot).
            await self.test_snapshot_run(run_id, full_task_spec)

            # W6: compute implementation diff for the test agent review
            run_now = self.store.get("run", run_id)
            impl_snap = Path(run_now["artifacts"]["snapshot"])
            test_snap_str = run_now.get("artifacts", {}).get("test_snapshot")
            impl_diff = None
            if test_snap_str:
                impl_diff = _compute_implementation_diff(
                    impl_snap, Path(test_snap_str),
                    set(test_info.get("lock_file_paths") or []))

            # Test agent review.
            self.store.transition(run_id, "reviewing_tests",
                                  note="Tests written; running test agent review.")
            ratings = None
            incomplete_reason = None
            try:
                manifest_d = test_info.get("manifest") or {}
                snap_now = Path(run_now["artifacts"]["snapshot"])
                test_file_contents = {}
                for rel in (manifest_d.get("test_files") or []):
                    try:
                        test_file_contents[rel] = (snap_now / rel).read_text(
                            encoding="utf-8", errors="replace")
                    except OSError:
                        pass
                review_result = await review_harness_tests(
                    full_task_spec, review, manifest_d,
                    test_file_contents, test_info.get("verified_results") or {},
                    self.config.planning_for_comparison(full_task_spec),
                    lock_violations=test_info.get("lock_violations"),
                    stop_reason=stop_reason,
                    implementation_diff=impl_diff,  # W6
                )
                # W1+W2: store review in test_stages entry
                _rn = self.store.get("run", run_id)
                _stages = list(_rn.get("test_stages") or [])
                _sv = full_task_spec.get("spec_version", 1)
                _idx = next((i for i, e in enumerate(_stages)
                             if e.get("spec_version") == _sv), None)
                if _idx is not None:
                    # I1: add status field so the UI can read stageEntry.review.status
                    _review_with_status = {
                        **review_result,
                        "status": "failed" if review_result.get("failure") else "complete",
                    }
                    _stages[_idx]["review"] = _review_with_status
                    self.store.update("run", run_id, test_stages=_stages)
                # W1: review failure → incomplete_reason (not ratings=None)
                if review_result.get("failure"):
                    self.store.event(id, "test_review_agent_failure",
                                     {"failure": review_result["failure"]}, run_id)
                    incomplete_reason = review_result["failure"].get(
                        "message", "Test agent review failed.")
                else:
                    ratings = review_result.get("ratings")
            except Exception as exc:
                self.store.event(id, "test_review_agent_error", {"error": str(exc)}, run_id)
                traceback.print_exc()

            # Per-harness baseline.
            per_harness_baseline = None
            try:
                per_harness_baseline = await CandidateTestRunner(self.config).run(
                    baseline, harness_plan,
                    version_dir / "per-harness-baseline" / run_id[:8],
                    cancellation=lambda: self.cancelled(self.store.get("run", run_id)),
                    baseline_snapshot=baseline)
            except Exception as exc:
                self.store.event(id, "per_harness_baseline_error", {"error": str(exc)}, run_id)

            await self.evaluate_run(run_id, full_task_spec, harness_plan, per_harness_baseline,
                                    ratings=ratings, incomplete_reason=incomplete_reason)

        jobs = [asyncio.create_task(one_saved(r)) for r in eligible]
        try:
            await asyncio.gather(*jobs)
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

        report = self.service.report(id)
        self.finish_state(id, "complete" if report["comparison_complete"] else "incomplete", ended_at=now())
        atomic_json(self.config.root / "comparisons" / id / "comparison-report.json",
                    self.service.report(id))

    async def compare(self, id):
        root = self.config.root / "comparisons" / id
        baseline = root / "baseline"
        jobs, plans = {}, {}

        async def one(run_id):
            stage = "Initialization"
            try:
                run = self.store.get("run", run_id)
                c = self.run_specification(self.store.get("comparison", id), run)
                self.record_requested_attempt(run_id, c)
                initial = Plan.model_validate(c["plan"]) if c.get("plan") else default_plan(c, baseline)
                resume_review = run["status"] == "waiting_test_review" and run.get("artifacts", {}).get("snapshot")
                if not run.get("clarified") and not resume_review:
                    stage = "Clarification"
                    outcome = await self.phase(run_id, "clarify", c, initial)
                    if outcome == "readiness_unverified":
                        # Resume the same session with an explicit reminder. A
                        # second omission is a genuine protocol failure.
                        outcome = await self.phase(run_id, "clarify", c, initial,
                                                   readiness_reminder=True)
                    if outcome != "ready":
                        return
                while not self.cancelled(run):
                    # Freezing requirements is immediate. The shared planner
                    # only receives this immutable version and baseline, never
                    # candidate workspaces or results.
                    shared_spec = self.freeze_requirements(id)
                    current = self.store.get("run", run_id)
                    c = self.run_specification(shared_spec, current)
                    self.record_requested_attempt(run_id, c)
                    version = c["spec_version"]
                    if version not in plans:
                        plans[version] = asyncio.create_task(self.freeze(
                            id, baseline, shared_spec if version == shared_spec["spec_version"] else c))
                    plan_job = plans[version]
                    development_plan = (Plan.model_validate(c["development_plan"])
                                        if c.get("development_plan") else default_plan(c, baseline))
                    if c["answers"] != shared_spec["answers"]:
                        development_plan = default_plan(c, baseline)
                    current = self.store.get("run", run_id)
                    reuse_snapshot = (current["status"] == "waiting_test_review"
                                      and current["spec_version"] == version
                                      and current.get("artifacts", {}).get("snapshot"))
                    if current.get("artifacts") and not reuse_snapshot:
                        self.service.queue_run(current)
                        current = self.store.get("run", run_id)
                    checkpoint = current.get("resume_implementation_attempt")
                    last_attempt = current["attempts"][-1] if current["attempts"] else {}
                    if checkpoint:
                        self.store.update("run", run_id, resume_implementation_attempt=None)
                    if reuse_snapshot:
                        outcome = "implemented"
                    elif (checkpoint and last_attempt.get("id") == checkpoint
                            and last_attempt.get("outcome") == "implemented"
                            and last_attempt.get("spec_version") == version):
                        self.store.transition(run_id, "implemented", error=None, error_details=None,
                            note="Continuing artifact collection and evaluation from the completed native turn.")
                        self.store.event(id, "implementation_reused",
                            {"attempt_id": checkpoint, "spec_version": version}, run_id)
                        outcome = "implemented"
                    else:
                        stage = "Development"
                        outcome = await self.phase(run_id, "develop", c, development_plan)
                    if outcome == "revised":
                        continue
                    if outcome != "implemented":
                        return
                    stage = "Artifact collection"
                    if not reuse_snapshot:
                        await self.snapshot_run(run_id, c)
                    stage = "Evaluation preparation"
                    latest = self.store.get("comparison", id)
                    if c["answers"] != shared_spec["answers"]:
                        self.retain_previous_implementation(run_id, version)
                        return
                    if latest["spec_version"] != version and self.requested_version(
                            self.store.get("run", run_id), latest) == version:
                        self.retain_previous_implementation(run_id, version)
                        return
                    self.store.transition(run_id, "planning",
                        note="Implementation finished. Preparing independent evaluation.")
                    while not plan_job.done():
                        if self.cancelled(run):
                            self.store.transition(run_id, "cancelled")
                            return
                        if self.store.get("comparison", id)["spec_version"] != version:
                            break
                        review = self.store.get("comparison", id).get("test_review")
                        desired = ("waiting_test_review" if review and review["status"] != "approved"
                                   else "planning")
                        if self.store.get("run", run_id)["status"] != desired:
                            self.store.transition(run_id, desired,
                                note="Implementation saved. Review and approve the shared tests to continue."
                                     if desired == "waiting_test_review" else "Developing approved tests and preparing evaluation.")
                        # A run's cancellation must not cancel another run's
                        # shared planning job.
                        await asyncio.wait({plan_job}, timeout=.2)
                    if self.store.get("comparison", id)["spec_version"] != version:
                        latest = self.store.get("comparison", id)
                        if self.requested_version(self.store.get("run", run_id), latest) == version:
                            self.retain_previous_implementation(run_id, version)
                            return
                        continue
                    prepared = await asyncio.shield(plan_job)
                    if prepared is None:
                        continue
                    plan, baseline_results, task_spec = prepared
                    if plan is None:
                        # New flow: each harness writes its own tests after implement phase.
                        stage = "Test stage"
                        version_dir = baseline.parent / "specifications" / str(version)
                        version_dir.mkdir(parents=True, exist_ok=True)
                        harness_plan, stop_reason, test_info = await self.run_test_stage(
                            run_id, task_spec, baseline, version_dir)
                        self.store.update("run", run_id, test_stop_reason=stop_reason)
                        # L1: spec was revised during test stage — re-loop for new version
                        if stop_reason.get("code") == "spec_revised":
                            continue
                        if harness_plan is None:
                            stage = "Evaluation"
                            # W7: infrastructure errors → incomplete_reason (not no_manifest_reason)
                            if stop_reason.get("code") == "infrastructure_error":
                                await self.evaluate_run(
                                    run_id, task_spec, None, None,
                                    incomplete_reason=stop_reason.get("message", ""))
                            else:
                                no_manifest_msg = (stop_reason.get("detail") or
                                                   stop_reason.get("message", "Test stage failed."))
                                await self.evaluate_run(run_id, task_spec, None, None,
                                                        no_manifest_reason=no_manifest_msg)
                        else:
                            # Store the final tested snapshot separately (DESIGN 9.1):
                            # run.artifacts.snapshot stays as the development snapshot;
                            # run.artifacts.test_snapshot is the post-test-stage code.
                            await self.test_snapshot_run(run_id, task_spec)
                            # W6: compute implementation diff for the test agent review
                            run_now = self.store.get("run", run_id)
                            impl_snap = Path(run_now["artifacts"]["snapshot"])
                            test_snap_str = run_now.get("artifacts", {}).get("test_snapshot")
                            impl_diff = None
                            if test_snap_str:
                                impl_diff = _compute_implementation_diff(
                                    impl_snap, Path(test_snap_str),
                                    set(test_info.get("lock_file_paths") or []))
                            # Test agent review (non-blocking: failures don't stop evaluation).
                            stage = "Test agent review"
                            self.store.transition(run_id, "reviewing_tests",
                                                  note="Tests written; running test agent review.")
                            ratings = None
                            incomplete_reason = None
                            try:
                                manifest_d = test_info.get("manifest") or {}
                                snap_now = Path(run_now["artifacts"]["snapshot"])
                                test_file_contents = {}
                                for rel in (manifest_d.get("test_files") or []):
                                    try:
                                        test_file_contents[rel] = (snap_now / rel).read_text(
                                            encoding="utf-8", errors="replace")
                                    except OSError:
                                        pass
                                approved_tr = task_spec.get("approved_test_review") or {}
                                review_result = await review_harness_tests(
                                    task_spec, approved_tr, manifest_d,
                                    test_file_contents, test_info.get("verified_results") or {},
                                    self.config.planning_for_comparison(task_spec),
                                    lock_violations=test_info.get("lock_violations"),
                                    stop_reason=stop_reason,
                                    implementation_diff=impl_diff,  # W6
                                )
                                # W1+W2: store review in test_stages entry
                                _rn = self.store.get("run", run_id)
                                _stages = list(_rn.get("test_stages") or [])
                                _sv = task_spec.get("spec_version", 1)
                                _idx = next((i for i, e in enumerate(_stages)
                                             if e.get("spec_version") == _sv), None)
                                if _idx is not None:
                                    # I1: add status field so the UI can read stageEntry.review.status
                                    _review_with_status = {
                                        **review_result,
                                        "status": "failed" if review_result.get("failure") else "complete",
                                    }
                                    _stages[_idx]["review"] = _review_with_status
                                    self.store.update("run", run_id, test_stages=_stages)
                                # W1: review failure → incomplete_reason (not ratings=None)
                                if review_result.get("failure"):
                                    self.store.event(id, "test_review_agent_failure",
                                                     {"failure": review_result["failure"]}, run_id)
                                    incomplete_reason = review_result["failure"].get(
                                        "message", "Test agent review failed.")
                                else:
                                    ratings = review_result.get("ratings")
                            except Exception as exc:
                                self.store.event(id, "test_review_agent_error",
                                                 {"error": str(exc)}, run_id)
                                traceback.print_exc()
                            # Per-harness baseline (non-blocking).
                            per_harness_baseline = None
                            try:
                                per_harness_baseline = await CandidateTestRunner(self.config).run(
                                    baseline, harness_plan,
                                    version_dir / "per-harness-baseline" / run_id[:8],
                                    cancellation=lambda: self.cancelled(self.store.get("run", run_id)),
                                    baseline_snapshot=baseline)
                            except Exception as exc:
                                self.store.event(id, "per_harness_baseline_error",
                                                 {"error": str(exc)}, run_id)
                            stage = "Evaluation"
                            await self.evaluate_run(run_id, task_spec, harness_plan,
                                                    per_harness_baseline, ratings=ratings,
                                                    incomplete_reason=incomplete_reason)
                    else:
                        stage = "Evaluation"
                        await self.evaluate_run(run_id, task_spec, plan, baseline_results)
                    if self.requested_version(self.store.get("run", run_id),
                                              self.store.get("comparison", id)) == version:
                        return
                self.store.transition(run_id, "cancelled")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.transition(run_id, "failed",
                    error=stage + " failed: " + (str(exc) or type(exc).__name__),
                    error_details={"stage": stage, "traceback": traceback.format_exc()})

        try:
            c = self.store.detail(id)
            self.store.update("comparison", id, status="clarifying")
            # Finalization can hand a late requirement answer to the next pass.
            # Bring recipients with an unattempted revision forward before making jobs.
            for run in c["runs"]:
                if self.has_pending_revision(run, c) and not self.cancelled(run):
                    self.service.queue_run(run)
            c = self.store.detail(id)
            participants = [r for r in c["runs"] if r["status"] in ("queued", "waiting_test_review")]
            # Readiness is a durable tool result, independent of subprocess cleanup.
            for run in participants:
                ready_path = self.run_root(run) / "bridge" / "ready.json"
                if not run.get("clarified") and ready_path.exists():
                    ready = json.loads(ready_path.read_text())
                    valid = any(a["id"] == ready.get("turn_key") and a["phase"] == "clarify"
                                and a["spec_version"] == c["spec_version"] for a in run["attempts"])
                    if valid and not any(q["answer"] is None and not q.get("withdrawn")
                        and any(m["run_id"] == run["id"] for m in q["mappings"]) for q in c["questions"]):
                        run["clarified"] = True
                        self.store.update("run", run["id"], clarified=True,
                                          readiness={**ready, "recovered": True})
            jobs = {r["id"]: asyncio.create_task(one(r["id"])) for r in participants}
            while jobs:
                await asyncio.wait(jobs.values(), timeout=.2, return_when=asyncio.FIRST_COMPLETED)
                for run_id, job in list(jobs.items()):
                    if job.done():
                        await job
                        del jobs[run_id]
                latest = self.store.detail(id)
                # A late answer can arrive after a fast harness has already
                # been evaluated. Preserve that result and automatically bring
                # its existing session forward to the shared revision.
                for run in latest["runs"]:
                    if run["id"] not in jobs and not self.cancelled(run):
                        if self.has_pending_revision(run, latest):
                            self.service.queue_run(run)
                            jobs[run["id"]] = asyncio.create_task(one(run["id"]))
                        elif run["status"] == "queued":
                            jobs[run["id"]] = asyncio.create_task(one(run["id"]))
                statuses = {r["status"] for r in self.store.detail(id)["runs"] if r["status"] not in TERMINAL}
                status = next((s for s in COMPARISON_PHASE_PRIORITY if s in statuses), "preparing")
                if jobs and latest["status"] != status:
                    self.store.update("comparison", id, status=status)
                if id in self.active and self.waiting_only_for_test_review(self.store.detail(id)):
                    # All native processes and snapshot writes have finished.
                    # The finally block cancels only idle orchestration waiters;
                    # no approval, saved code or evaluation result is discarded.
                    self.store.update("comparison", id, status="waiting_test_review")
                    self.store.event(id, "test_review_wait_released_capacity", {
                        "review_id": self.store.get("comparison", id)["test_review"]["id"]})
                    return
            self.finish_comparison(id, self.service.report(id))
        except asyncio.CancelledError:
            pending = [r for r in self.store.detail(id)["runs"] if r["status"] not in TERMINAL]
            self.store.update("comparison", id, status="waiting_test_review"
                              if pending and all(r["status"] == "waiting_test_review" for r in pending)
                              else "interrupted")
            raise
        except Exception as exc:
            self.finish_state(id, "incomplete", error=str(exc), ended_at=now())
            for r in self.store.detail(id)["runs"]:
                if r["status"] not in TERMINAL:
                    self.store.transition(r["id"], "failed", error=str(exc))
            traceback.print_exc()
        finally:
            for job in [*jobs.values(), *plans.values()]:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs.values(), *plans.values(), return_exceptions=True)
            atomic_json(root / "comparison-report.json", self.service.report(id))

    async def reevaluate(self, id):
        c = self.store.detail(id)
        version = c.get("evaluation_spec_version", c["spec_version"])
        frozen = next((s for s in c["specifications"] if s["version"] == version), None)
        if frozen is None:
            raise ValueError("No frozen specification exists for this evaluation")
        task_spec = {**c, "prompt": frozen["task"], "answers": frozen["answers"], "spec_version": version}
        self.store.update("comparison", id, status="evaluating")

        async def one(run):
            historical = run["spec_version"] != version or not run.get("artifacts", {}).get("case")
            snapshots = run.get("artifact_history", [])
            archived = next((h for h in reversed(snapshots)
                if h["spec_version"] == version and h["artifacts"].get("case")), None) if historical else None
            previous = archived.get("evaluation") if archived else run.get("evaluation")
            if self.cancelled(run):
                if not historical:
                    self.store.transition(run["id"], "cancelled")
                return
            if not historical:
                self.store.transition(run["id"], "evaluating", ended_at=None)
            try:
                artifacts = (archived["artifacts"] if historical else run["artifacts"]).copy()
                case = json.loads(Path(artifacts["case"]).read_text())
                # DESIGN 9.1: for new-flow runs (test_snapshot present), verify against
                # the test snapshot; legacy/no-manifest runs use the development snapshot.
                has_test_snap = (artifacts.get("test_snapshot")
                                 and artifacts.get("test_snapshot_sha256"))
                eval_snap_path = Path(artifacts["test_snapshot"] if has_test_snap else artifacts["snapshot"])
                eval_sha = (artifacts["test_snapshot_sha256"] if has_test_snap
                            else artifacts.get("sha256", case["candidate"]["snapshot_sha256"]))
                if tree_hash(eval_snap_path) != case["candidate"]["snapshot_sha256"]:
                    if not has_test_snap or tree_hash(eval_snap_path) != eval_sha:
                        raise RuntimeError("Stored snapshot hash changed; evaluation refused")
                # Restore stored ratings for new-flow comparisons.
                stored_ratings = case.get("ratings")
                result = await self.score_candidate(run, task_spec, Plan.model_validate(case["plan"]),
                                                    case["candidate"], ratings=stored_ratings)
                if result is None:
                    if not historical:
                        self.store.transition(run["id"], "cancelled")
                    return
                result["test_review_id"] = case["plan"].get("review_id")
                if result["test_review_id"] is None and case.get("test_review"):
                    result["test_review_id"] = (case["test_review"] or {}).get("id")
                path = Path(artifacts["case"]).parent / ("reevaluation-" + uid()[:8] + ".json")
                atomic_json(path, result)
                artifacts["evaluation"] = str(path)
                history = run.get("evaluation_history", [])
                if previous:
                    history.append({"replaced_at": now(), "spec_version": version, "evaluation": previous})
                if historical:
                    archived.update(evaluation=result, artifacts=artifacts, reevaluated_at=now())
                    self.store.update("run", run["id"], artifact_history=snapshots, evaluation_history=history)
                    self.store.event(id, "historical_evaluation", {"spec_version": version,
                        "overall": result["overall"], "evaluation": str(path)}, run["id"])
                else:
                    self.store.transition(run["id"], evaluation_status(result),
                        evaluation=result, evaluation_history=history, artifacts=artifacts, error=None,
                        note="Independent verification finished.")
            except Exception as exc:
                if historical:
                    self.store.event(id, "historical_evaluation_error",
                        {"spec_version": version, "error": str(exc)}, run["id"])
                else:
                    self.store.transition(run["id"], "evaluated_incomplete", evaluation=None,
                                          error="Stored-artifact evaluation failed: " + str(exc))

        jobs = [asyncio.create_task(one(r)) for r in c["runs"]
                if r["id"] in c.get("evaluation_run_ids", [run["id"] for run in c["runs"]])
                and ((r.get("artifacts", {}).get("case") and r["spec_version"] == version) or any(
                    h["spec_version"] == version and h["artifacts"].get("case") for h in r.get("artifact_history", [])))]
        try:
            await asyncio.gather(*jobs)
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
        report = self.service.report(id)
        self.finish_state(id, "complete" if report["comparison_complete"] else "incomplete", ended_at=now())
        atomic_json(self.config.root / "comparisons" / id / "comparison-report.json", self.service.report(id))

    async def reverify(self, id):
        """New evidence and verdicts for the same code; original plans/results are retained."""
        c = self.store.detail(id)
        request = c["verification_request"]
        version = request["spec_version"]
        frozen = next(s for s in c["specifications"] if s["version"] == version)
        task_spec = {**c, "prompt": frozen["task"], "answers": frozen["answers"], "spec_version": version}
        # For new-flow comparisons, request["plan"] is None; each run has its own plan in
        # request["candidates"][run_id]["plan"].  For legacy comparisons, there is a shared plan.
        is_new_flow_reverify = request.get("plan") is None
        shared_plan = None if is_new_flow_reverify else Plan.model_validate(request["plan"])
        root = self.config.root / "comparisons" / id
        verification = root / "verifications" / request["id"]
        atomic_json(verification / "request.json", request)
        self.store.update("comparison", id, status="evaluating")
        runs = [r for r in c["runs"] if r["id"] in request["candidates"]]
        for run in runs:
            history = run.get("artifact_history", []) + [{
                "spec_version": version, "archived_at": now(), "artifacts": run["artifacts"],
                "evaluation": run.get("evaluation"), "evidence": run.get("evidence"),
                "reason": request["reason"], "superseded_by_verification": request["id"]}]
            self.store.transition(run["id"], "evaluating", ended_at=None, error=None,
                                  evaluation=None, evidence=None, artifact_history=history,
                                  note="Re-running independent checks on the saved code.")
        try:
            baseline = root / "baseline"
            if tree_hash(baseline) != frozen["baseline_sha256"]:
                raise ValueError("Stored baseline changed; verification refused")
            if is_new_flow_reverify:
                # No shared baseline run; each run uses its own plan.
                shared_baseline_results = None
            else:
                shared_baseline_results = await CandidateTestRunner(self.config).run(
                    baseline, shared_plan, verification / "baseline-evidence",
                    baseline_snapshot=baseline,
                    cancellation=lambda: self.comparison_cancelled(id))

            async def one(run):
                artifacts = request["candidates"][run["id"]].copy()
                candidate_dir = verification / run["id"]
                try:
                    # Determine which snapshot to use and the per-run plan.
                    if artifacts.get("test_snapshot") and artifacts.get("test_snapshot_sha256"):
                        snapshot = Path(artifacts["test_snapshot"])
                        snapshot_hash = artifacts["test_snapshot_sha256"]
                    else:
                        snapshot = Path(artifacts["snapshot"])
                        snapshot_hash = artifacts["sha256"]
                    if tree_hash(snapshot) != snapshot_hash:
                        raise ValueError("Stored candidate changed; verification refused")
                    if is_new_flow_reverify:
                        # Per-run plan stored in the candidates dict by service.reverify().
                        run_plan = Plan.model_validate(artifacts["plan"])
                        # Per-harness baseline for regression labels.
                        baseline_results = None
                        try:
                            baseline_results = await CandidateTestRunner(self.config).run(
                                baseline, run_plan,
                                candidate_dir / "baseline-evidence",
                                baseline_snapshot=baseline,
                                cancellation=lambda: self.comparison_cancelled(id))
                        except Exception as exc:
                            self.store.event(id, "per_harness_baseline_error",
                                             {"error": str(exc)}, run["id"])
                        stored_ratings = artifacts.get("plan") and json.loads(
                            Path(artifacts["case"]).read_text()).get("ratings")
                    else:
                        run_plan = shared_plan
                        baseline_results = shared_baseline_results
                        stored_ratings = None
                    evidence = await CandidateTestRunner(self.config).run(
                        snapshot, run_plan, candidate_dir / "evidence", baseline_results,
                        baseline_snapshot=baseline, cancellation=lambda: self.cancelled(run),
                        verification_context=native_execution_evidence(self.store, run))
                    if self.cancelled(run):
                        self.store.transition(run["id"], "cancelled")
                        return
                    original_case = json.loads(Path(artifacts["case"]).read_text())
                    candidate = {**original_case["candidate"], "evidence": evidence}
                    case = {**original_case, "plan": run_plan.model_dump(), "candidate": candidate,
                            "verification_revision": request["id"], "verification_reason": request["reason"],
                            "previous_case": artifacts["case"],
                            "test_review": request.get("test_review", original_case.get("test_review")),
                            "ratings": stored_ratings}
                    atomic_json(candidate_dir / "case.json", case)
                    artifacts.update(case=str(candidate_dir / "case.json"),
                                     evidence=str(candidate_dir / "evidence"))
                    artifacts.pop("evaluation", None)
                    self.store.update("run", run["id"], artifacts=artifacts, evidence=evidence)
                    result = await self.score_candidate(run, task_spec, run_plan, candidate,
                                                        ratings=stored_ratings)
                    if result is None:
                        self.store.transition(run["id"], "cancelled")
                        return
                    if tree_hash(snapshot) != snapshot_hash:
                        raise ValueError("Stored candidate changed during verification")
                    result.update(verification_revision=request["id"], verification_reason=request["reason"],
                                  test_review_id=run_plan.review_id)
                    if result.get("test_review_id") is None and case.get("test_review"):
                        result["test_review_id"] = (case["test_review"] or {}).get("id")
                    atomic_json(candidate_dir / "evaluation.json", result)
                    artifacts["evaluation"] = str(candidate_dir / "evaluation.json")
                    self.store.transition(run["id"], evaluation_status(result), artifacts=artifacts,
                                          evaluation=result, evidence=evidence,
                                          note="Independent verification finished.", error=None)
                except Exception as exc:
                    self.store.transition(run["id"], "evaluated_incomplete",
                                          error="Independent re-verification failed: " + str(exc))

            jobs = [asyncio.create_task(one(run)) for run in runs]
            try:
                await asyncio.gather(*jobs)
            finally:
                for job in jobs:
                    if not job.done():
                        job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
        except asyncio.CancelledError:
            request["status"] = "interrupted"
            for run in runs:
                if self.store.get("run", run["id"])["status"] not in TERMINAL:
                    self.store.transition(run["id"], "interrupted",
                                          error="Verification interrupted; saved code is retained.")
            raise
        except Exception as exc:
            for run in runs:
                self.store.transition(run["id"], "evaluated_incomplete",
                                      error="Independent re-verification failed: " + str(exc))
        finally:
            if request["status"] != "interrupted":
                request["status"] = "finished"
            request["ended_at"] = now()
            report = self.service.report(id)
            self.finish_state(id,
                              ("interrupted" if request["status"] == "interrupted" else
                               "complete" if report["comparison_complete"] else "incomplete"),
                              ended_at=now(), verification_request=request,
                              verification_history=c.get("verification_history", []) + [request])
            atomic_json(verification / "request.json", request)
            atomic_json(root / "comparison-report.json", self.service.report(id))
