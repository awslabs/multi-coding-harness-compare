# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio
import fcntl
import hashlib
import json
import os
import time
import traceback
from pathlib import Path

from .bridge import atomic_json
from .config import Config
from .evaluation import (EVALUATOR_VERSION, CandidateTestRunner, Plan, default_plan, evaluate_candidate,
                         evaluation_status, prepare_plan, repository_context)
from .isolation import execution_runtime, command, copy_tree, immutable, initialize_checkout, make_writable, patch, tree_hash
from .model_match import model_mismatch
from .prompts import TEMPLATE_VERSION, effective_prompt, follow_ups, for_harness
from .service import Service
from .artifact_storage import StorageError, remove_tree
from .artifact_readiness import RECOVERABLE
from .store import COMPARISON_PHASE_PRIORITY, TERMINAL, durations, now, uid
from .execution_evidence import native_execution_evidence
from .test_review import (TestProposal, plan_digest, propose_tests, requirements_digest,
                          review_alignment_issues, update_review)


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
        protected = [Path(__file__).parent.resolve(), Path(__file__).parent.parent / "codecouncil",
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

    async def score_candidate(self, run, comparison, plan, candidate):
        def judge_progress(event):
            self.store.event(comparison["id"], "diagnostic",
                             {**event, "stage": "judge"}, run["id"])

        job = asyncio.create_task(evaluate_candidate(
            comparison, plan, candidate, self.config.judge_for_comparison(comparison),
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
                    and r["status"] in ("waiting_test_review", "planning")):
                if r["status"] != "waiting_test_review":
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
                        if i["status"] in ("clarifying", "developing"))
                    attempt.update(ended_at=recovered_at, outcome="interrupted", active_seconds=observed,
                        unobserved_active_seconds=(recovered_at-last_observed
                            if r["status"] in ("clarifying", "developing") else 0),
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
                    readiness_reminder: bool = False):
        run = self.store.get("run", run_id)
        original_answers = spec.get("answers", [])
        spec = self.run_specification(spec, run)
        # A shared/generated rubric can paraphrase excluded instructions. Coding
        # gets deterministic original-task criteria when history differs by recipient.
        if spec.get("coding_criteria_filtered") or original_answers != spec.get("answers", []):
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
        phase_status = "clarifying" if phase == "clarify" else "developing"
        turn_key = uid()
        self.store.transition(run_id, "setup", error=None, error_details=None,
                              error_terminal=None, turn_completed=False)
        delivered = set(run.get("follow_ups_delivered", []))
        pending_follow_ups = ([] if phase == "clarify" else
            [fu for fu in follow_ups(spec) if fu["id"] not in delivered])
        # A follow-up outside this set arriving mid-turn restarts the turn, even when the
        # specification was not frozen and so its version did not change.
        sent = delivered | {fu["id"] for fu in pending_follow_ups}
        prompt = effective_prompt(spec, phase, plan.model_dump(), readiness_reminder,
                                  pending_follow_ups or None)
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
            atomic_json(root / "bridge" / "specification.json", {
                "task": spec["prompt"], "shared_answers": spec["answers"],
                "specification_version": spec["spec_version"], "criteria": plan.model_dump()["criteria"]})
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
            return "ready" if phase == "clarify" else "implemented"
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
        """Feedback supersedes in-flight generation; failure never bypasses review."""
        while not obsolete():
            review = await self.review_tests(id, baseline, task_spec, obsolete)
            if review is None:
                return None
            latest_review = self.store.get("comparison", id)["test_review"]
            if latest_review["id"] != review["id"] or latest_review["status"] != "approved":
                continue
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
                return plan, metadata, review
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
                plan, metadata, review = prepared
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
                plan, planning, review = prepared
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

    async def evaluate_run(self, run_id, c, plan, baseline_results):
        run = self.store.get("run", run_id)
        root = self.run_root(run)
        if self.cancelled(run):
            self.store.transition(run_id, "cancelled")
            return
        self.store.transition(run_id, "evaluating", evaluation_scope_note=None)
        try:
            if not run.get("artifacts", {}).get("snapshot"):
                await self.snapshot_run(run_id, c)
                run = self.store.get("run", run_id)
            snapshot = Path(run["artifacts"]["snapshot"])
            artifacts = snapshot.parent
            snapshot_sha = run["artifacts"]["sha256"]
            if tree_hash(snapshot) != snapshot_sha:
                raise RuntimeError("Immutable snapshot changed before evaluation")
            evidence = await CandidateTestRunner(self.config).run(snapshot, plan, artifacts / "evidence",
                    baseline_results, cancellation=lambda: self.cancelled(run),
                    baseline_snapshot=root.parent.parent / "baseline",
                    verification_context=native_execution_evidence(self.store, run))
            if self.cancelled(run):
                self.store.transition(run_id, "cancelled")
                return
            candidate = {"snapshot_sha256": snapshot_sha,
                         "diff": (artifacts / "implementation.patch").read_text(errors="replace"),
                         "files": repository_context(snapshot),
                         "repository_context": repository_context(root.parent.parent / "baseline"),
                         "evidence": evidence}
            atomic_json(artifacts / "case.json", {
                "original_task": c["prompt"], "shared_answers": c["answers"],
                "specification_version": c["spec_version"], "plan": plan.model_dump(),
                "test_review": c.get("approved_test_review"),
                "baseline_sha256": c["baseline_sha256"], "candidate": candidate,
                "native_usage": self.store.get("run", run_id)["usage_records"],
                "execution_timestamps": self.store.get("run", run_id)["intervals"]})
            result = await self.score_candidate(run, c, plan, candidate)
            if result is None:
                self.store.transition(run_id, "cancelled")
                return
            result["test_review_id"] = plan.review_id
            if tree_hash(snapshot) != snapshot_sha:
                raise RuntimeError("Immutable snapshot changed during evaluation")
            atomic_json(artifacts / "evaluation.json", result)
            self.store.transition(run_id, evaluation_status(result), evaluation=result, evidence=evidence,
                note="Independent verification finished.",
                artifacts={"patch": str(artifacts / "implementation.patch"), "snapshot": str(snapshot),
                           "case": str(artifacts / "case.json"), "evaluation": str(artifacts / "evaluation.json"),
                           "evidence": str(artifacts / "evidence"), "sha256": snapshot_sha})
        except Exception as exc:
            self.store.transition(run_id, "evaluated_incomplete", error="Independent evaluation failed: " + str(exc))
            traceback.print_exc()

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
                if tree_hash(Path(artifacts["snapshot"])) != case["candidate"]["snapshot_sha256"]:
                    raise RuntimeError("Stored snapshot hash changed; evaluation refused")
                result = await self.score_candidate(run, task_spec, Plan.model_validate(case["plan"]), case["candidate"])
                if result is None:
                    if not historical:
                        self.store.transition(run["id"], "cancelled")
                    return
                result["test_review_id"] = case["plan"].get("review_id")
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
        plan = Plan.model_validate(request["plan"])
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
            baseline_results = await CandidateTestRunner(self.config).run(
                baseline, plan, verification / "baseline-evidence", baseline_snapshot=baseline,
                cancellation=lambda: self.comparison_cancelled(id))

            async def one(run):
                artifacts = request["candidates"][run["id"]].copy()
                candidate_dir = verification / run["id"]
                try:
                    snapshot = Path(artifacts["snapshot"])
                    if tree_hash(snapshot) != artifacts["sha256"]:
                        raise ValueError("Stored candidate changed; verification refused")
                    evidence = await CandidateTestRunner(self.config).run(
                        snapshot, plan, candidate_dir / "evidence", baseline_results,
                        baseline_snapshot=baseline, cancellation=lambda: self.cancelled(run),
                        verification_context=native_execution_evidence(self.store, run))
                    if self.cancelled(run):
                        self.store.transition(run["id"], "cancelled")
                        return
                    original_case = json.loads(Path(artifacts["case"]).read_text())
                    candidate = {**original_case["candidate"], "evidence": evidence}
                    case = {**original_case, "plan": plan.model_dump(), "candidate": candidate,
                            "verification_revision": request["id"], "verification_reason": request["reason"],
                            "previous_case": artifacts["case"],
                            "test_review": request.get("test_review", original_case.get("test_review"))}
                    atomic_json(candidate_dir / "case.json", case)
                    artifacts.update(case=str(candidate_dir / "case.json"),
                                     evidence=str(candidate_dir / "evidence"))
                    artifacts.pop("evaluation", None)
                    self.store.update("run", run["id"], artifacts=artifacts, evidence=evidence)
                    result = await self.score_candidate(run, task_spec, plan, candidate)
                    if result is None:
                        self.store.transition(run["id"], "cancelled")
                        return
                    if tree_hash(snapshot) != artifacts["sha256"]:
                        raise ValueError("Stored candidate changed during verification")
                    result.update(verification_revision=request["id"], verification_reason=request["reason"],
                                  test_review_id=plan.review_id)
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
