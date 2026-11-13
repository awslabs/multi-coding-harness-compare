# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import re
from contextlib import ExitStack
from pathlib import Path

from .config import EFFORTS, Config, highest_effort
from .artifact_storage import ArtifactStorage, StorageError
from .artifact_readiness import readiness, retry_gate, requires_ready_artifacts, ArtifactsNotReady
from .evaluation import PLANNING_STALL_SECONDS
from .evaluation import Plan, plan_quality_issues
from .isolation import copy_tree, immutable, tree_hash
from .judge_models import catalogue_fingerprint
from .prompts import TEMPLATE_VERSION, follow_ups, for_harness
from .project_outputs import PROJECT_METADATA, output_marker, output_status, prepare_outputs, publish_output
from .store import HARNESS_NAMES, TERMINAL, Store, durations, now, uid
from .telemetry import usage_summary
from .test_review import (DEFAULT_TEST_COVERAGE, TestCoverage, TestProposal, digest, new_review,
                         plan_digest, proposal_content_issues, requirements_digest,
                         validate_test_coverage)


_HARNESS_PROPERTY = {"claude": "CLAUDE_MODEL", "codex": "CODEX_MODEL", "kiro": "KIRO_MODEL"}


def selected_harnesses(value, available=HARNESS_NAMES):
    """Validate before creating any records; omission preserves legacy callers."""
    if value is None:
        value = list(available)
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("Select at least one coding harness.")
    if any(not isinstance(h, str) or h not in available for h in value):
        raise ValueError("Select only coding harnesses participating in this comparison.")
    if len(set(value)) != len(value):
        raise ValueError("Coding harness selections must not contain duplicates.")
    return [h for h in HARNESS_NAMES if h in value]


def harness_catalogue_fingerprint(config) -> str:
    """Fingerprint matching ModelCatalogue.fingerprint — config identity for stored model lists."""
    return hashlib.sha256(json.dumps({
        "runtime": config.runtime,
        "harnesses": {h: config.harnesses[h].model_dump() for h in HARNESS_NAMES}
    }, sort_keys=True).encode()).hexdigest()


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.root)
        self.storage = ArtifactStorage(config, self.store)

    def ensure_comparison(self, id):
        try:
            return self.store.get("comparison", id)
        except KeyError:
            if not self.storage.enabled:
                raise
            return self.storage.import_comparison(id)

    def judge_selection(self, model_id: str | None = None, comparison: dict | None = None):
        if model_id is None and comparison and comparison.get("judge"):
            return comparison["judge"].copy()
        resolved = model_id or self.config.judge_model
        entry = None
        if resolved is not None:
            try:
                catalog = self.store.get("system", "judge-model-catalogue")
            except KeyError:
                catalog = {}
            fp = catalogue_fingerprint(self.config)
            if catalog.get("fingerprint") != fp:
                source = "the configured default JUDGE_MODEL" if model_id is None else "your selection"
                raise ValueError(f"The judge model catalog is stale or missing; "
                                 f"refresh the judge catalog, then select {source} again.")
            entry = next((m for m in catalog.get("models", []) if m["id"] == resolved), None)
            if entry is None:
                source = "the configured default JUDGE_MODEL" if model_id is None else "your selection"
                raise ValueError(f"Judge model {resolved!r} (from {source}) is not in the catalog; "
                                 "refresh the judge catalog and select again.")
            if not entry["selectable"]:
                raise ValueError(entry["reason"])
        return {"model": resolved, "profile": self.config.judge_profile,
                "region": self.config.judge_region,
                "streaming": entry["streaming"] if entry else self.config.judge_streaming}

    def planning_selection(self, model_id: str | None, judge: dict | None) -> dict | None:
        """Resolve and validate the planning model; return {"model": m, "effort": ..., "effort_basis": ...} or None."""
        resolved = model_id or self.config.planning_model or (judge or {}).get("model")
        if resolved is None:
            return None
        source = ("your selection" if model_id else "the configured default PLANNING_MODEL"
                  if self.config.planning_model else "the judge model, used while PLANNING_MODEL is unset")
        try:
            catalog = self.store.get("system", "judge-model-catalogue")
        except KeyError:
            catalog = {}
        fp = catalogue_fingerprint(self.config)
        if catalog.get("fingerprint") != fp:
            raise ValueError(f"The judge model catalog is stale or missing; "
                             f"refresh the judge catalog, then select {source} again.")
        entry = next((m for m in catalog.get("models", []) if m["id"] == resolved), None)
        if entry is None:
            raise ValueError(f"Planning model {resolved!r} (from {source}) is not in the judge catalog; "
                             "refresh the catalog and select again.")
        if not entry["selectable"]:
            raise ValueError(entry["reason"])
        if self.config.planning_effort and "anthropic.claude" not in resolved:
            raise ValueError(
                f"planning_effort requires a Claude planning model (one whose ID contains "
                f"'anthropic.claude'); {resolved!r} is not a Claude model.")
        configured = self.config.planning_effort
        if configured is None:
            return {"model": resolved, "effort": None, "effort_basis": None}
        levels = self._claude_effort_levels(resolved)
        if configured == "highest":
            if levels is None:
                return {"model": resolved, "effort": "max", "effort_basis": "highest (model levels unknown)"}
            top = highest_effort(levels)
            if top is None:
                raise ValueError(
                    f"{resolved} supports no known effort level ({', '.join(levels) or 'none'}); "
                    "choose a planning model that does, or unset planning_effort")
            return {"model": resolved, "effort": top, "effort_basis": "highest the model supports"}
        if levels is not None and configured not in levels:
            raise ValueError(
                f"{resolved} does not support effort {configured!r}; "
                f"supported: {', '.join(levels) or 'none'}")
        return {"model": resolved, "effort": configured, "effort_basis": "planning_effort in config.toml"}

    def _claude_effort_levels(self, model_id: str) -> list | None:
        """Return effort levels from the harness catalogue claude entry, or None if unknown."""
        try:
            cat = self.store.get("system", "model-catalogue")
        except KeyError:
            return None
        models = cat.get("harnesses", {}).get("claude", {}).get("models") or []

        def _strip(mid):
            mid = mid.removesuffix("[1m]")
            for pfx in ("us.", "eu.", "apac.", "global.", "jp.", "au."):
                if mid.startswith(pfx):
                    return mid[len(pfx):]
            return mid

        stripped = _strip(model_id)
        # Exact match first, then stripped; use first entry where efforts is a list
        candidates = ([e for e in models if e.get("id") == model_id]
                      + [e for e in models if e.get("id") != model_id
                         and _strip(e.get("id", "")) == stripped])
        for e in candidates:
            if isinstance(e.get("efforts"), list):
                return e["efforts"]
        return None

    def repository_path(self, value: str) -> Path:
        try:
            source = Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("Project folder does not exist or cannot be accessed") from exc
        if not source.is_dir() or not any(source.is_relative_to(Path(p).expanduser().resolve())
                                          for p in self.config.repository_roots):
            raise ValueError("Project folder must be within a configured repository root")
        if source.is_relative_to(self.config.root):
            raise ValueError("multi-coding-harness-compare's saved artifacts cannot be selected as a project")
        if any(p.name == PROJECT_METADATA or output_marker(p) for p in (source, *source.parents)):
            raise ValueError("Choose the original project folder, above multi-coding-harness-compare's harness output folders")
        return source

    def directories(self, path: str | None = None):
        roots = []
        for value in self.config.repository_roots:
            try:
                root = self.repository_path(value)
                if str(root) not in roots:
                    roots.append(str(root))
            except ValueError:
                continue
        result = {"roots": roots, "path": None, "parent": None, "directories": []}
        if path is None:
            return result
        source = self.repository_path(path)
        parent = str(source.parent) if source.parent != source and any(
            source.parent.is_relative_to(Path(r)) for r in roots) else None
        try:
            children = []
            for child in source.iterdir():
                if child.name in {".git", ".venv", "node_modules", "__pycache__"}:
                    continue
                try:
                    if child.is_dir():
                        resolved = self.repository_path(str(child))
                        children.append({"name": child.name, "path": str(resolved)})
                except (ValueError, OSError):
                    continue
        except OSError as exc:
            raise ValueError("This project folder cannot be read") from exc
        return {**result, "path": str(source), "parent": parent,
                "directories": sorted(children, key=lambda c: (c["name"].casefold(), c["name"]))}

    def create(self, prompt: str, repository: str | None = None,
               validation_plan: dict | None = None, models: dict | None = None,
               judge_model: str | None = None, test_coverage: TestCoverage = DEFAULT_TEST_COVERAGE,
               efforts: dict | None = None, planning_model: str | None = None,
               configuration_choices: dict | None = None, harnesses: list[str] | None = None):
        if not prompt.strip():
            raise ValueError("A development task is required")
        harnesses = selected_harnesses(harnesses)
        test_coverage = validate_test_coverage(test_coverage)
        models = models or {}
        if set(models) - set(HARNESS_NAMES):
            raise ValueError("Unknown coding harness in model selections")
        try:
            catalogue_data = self.store.get("system", "model-catalogue")
        except KeyError:
            catalogue_data = {}
        current_fp = harness_catalogue_fingerprint(self.config)
        fp_matched = catalogue_data.get("fingerprint") == current_fp
        catalogue = catalogue_data.get("harnesses", {})
        resolved_models = {}
        for harness in harnesses:
            chosen = models.get(harness)
            resolved = chosen if chosen is not None else self.config.harnesses[harness].model
            if resolved is not None:
                if not fp_matched:
                    detail = (f" (from the configured default {_HARNESS_PROPERTY[harness]})"
                              if chosen is None else "")
                    raise ValueError(
                        f"The model catalog for {harness} is stale or missing{detail}; "
                        "refresh the model list and select again.")
                model_ids = {m["id"] for m in catalogue.get(harness, {}).get("models", [])}
                if resolved not in model_ids:
                    if chosen is None:
                        raise ValueError(
                            f"Model {resolved!r} from the configured default "
                            f"{_HARNESS_PROPERTY[harness]} is not in the {harness} model list; "
                            "refresh the model list and select again.")
                    raise ValueError(
                        f"Unsupported model {resolved!r} for {harness}; "
                        "refresh the model list and select again.")
            resolved_models[harness] = resolved
        efforts = efforts or {}
        if set(efforts) - set(HARNESS_NAMES) or set(efforts.values()) - {*EFFORTS, None}:
            raise ValueError("Unknown coding harness or effort level in effort selections")
        resolved_efforts = {}
        for harness in harnesses:
            entry = next((m for m in catalogue.get(harness, {}).get("models", [])
                          if m["id"] == resolved_models[harness]), {})
            chosen, configured = efforts.get(harness), self.config.harnesses[harness].effort
            supported = entry.get("efforts")
            if chosen and supported is not None and chosen not in supported:
                raise ValueError(f"{resolved_models[harness]} does not support effort {chosen} for {harness}; "
                                 f"choose one of: {', '.join(supported) or 'none'}")
            # The configured default only applies where the model takes it; otherwise the
            # model's own default, which is what the UI shows for an empty selection.
            if not chosen and configured and supported is not None and configured not in supported:
                configured = None
            resolved_efforts[harness] = chosen or configured
        if validation_plan is not None:
            validation_plan = Plan.model_validate(validation_plan).model_dump()
        judge = self.judge_selection(judge_model)
        planning = self.planning_selection(planning_model, judge)
        # Validate and resolve configuration choices per harness.
        _VALID_CONFIG_CHOICES = frozenset({"default", "overrides", "none"})
        configuration_choices = configuration_choices or {}
        if set(configuration_choices) - set(HARNESS_NAMES):
            raise ValueError("Unknown coding harness in configuration_choices")
        if set(configuration_choices.values()) - _VALID_CONFIG_CHOICES:
            raise ValueError("configuration_choices values must be 'default', 'overrides', or 'none'")
        resolved_choices = {h: configuration_choices.get(h, self.config.default_configuration_choice)
                            for h in harnesses}
        id = uid()
        with self.storage.lock("maintenance"), self.storage.lock(id + ".execution"):
            return self._create_comparison(id, prompt, repository, validation_plan, resolved_models, judge,
                                           test_coverage, resolved_efforts, planning, resolved_choices, harnesses)

    def _create_comparison(self, id, prompt, repository, validation_plan, resolved_models, judge,
                           test_coverage, resolved_efforts, planning, resolved_choices=None, harnesses=None):
        harnesses = selected_harnesses(harnesses)
        at = now()
        root = self.config.root / "comparisons" / id
        baseline = root / "baseline"
        if repository:
            source = self.repository_path(repository)
            repository = str(source)
            copy_tree(source, baseline, excluded_roots=(self.config.root,), exclude_outputs=True)
        else:
            baseline.mkdir(parents=True)
            # Outside the comparison root, so archiving the run does not take
            # the deliverables with it.
            source = self.config.root.parent / "projects" / id
            source.mkdir(parents=True)
        immutable(baseline)
        data = {
            "id": id, "prompt": prompt, "repository": repository or "empty project",
            "project_directory": str(source),
            "baseline_sha256": tree_hash(baseline), "status": "queued", "created_at": at,
            "answers": [], "spec_version": 1, "frozen": False, "cancel_requested": False,
            "template_version": TEMPLATE_VERSION, "validation_plan": validation_plan,
            "models": resolved_models, "efforts": resolved_efforts, "judge": judge, "harnesses": harnesses,
            "planning": planning, "test_coverage": test_coverage,
            "specifications": [], "limits": {
                k: getattr(self.config, k) for k in ("active_seconds", "setup_seconds", "check_seconds",
                                                   "max_turns", "memory", "pids",
                                                   "test_active_seconds", "test_fix_rounds")},
        }
        data["test_review"] = new_review(data)
        runs = []
        for harness in harnesses:
            run = {"id": uid(), "comparison_id": id, "harness": harness, "status": "queued",
                "requested_model": resolved_models[harness], "requested_effort": resolved_efforts[harness],
                "session_id": None, "submitted_at": at, "updated_at": at, "spec_version": 1,
                "requested_spec_version": 1,
                "cancel_requested": False, "intervals": [{"status": "queued", "start": at, "end": None}],
                "attempts": [], "usage_records": [], "capabilities": [], "artifacts": {}, "evaluation": None,
                # Per-run configuration choice applied by prepare_home.
                # Stored explicitly so the choice is visible and immutable even
                # if the operator later changes HARNESS_CONFIG_DEFAULT_CHOICE.
                "configuration_choice": (resolved_choices or {}).get(harness, self.config.default_configuration_choice)}
            runs.append(run)
        output_root = root / "project-outputs" if self.storage.enabled else source
        output_root.mkdir(parents=True, exist_ok=True)
        outputs = prepare_outputs(output_root, data, runs)
        for run in runs:
            run["project_output"] = outputs[run["id"]]
        # Publish the runnable comparison and selected jobs in one transaction.
        # A different worker process must never observe a partially created cohort.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany("INSERT INTO objects(kind,id,data) VALUES(?,?,?)",
                [("comparison", id, json.dumps(data))]
                + [("run", run["id"], json.dumps(run)) for run in runs])
        self.store.event(id, "submitted", {"prompt": prompt, "harnesses": harnesses,
                                           "models": resolved_models, "efforts": resolved_efforts,
                                           "judge": judge, "planning": planning,
                                           "test_coverage": test_coverage,
                                           "baseline_sha256": data["baseline_sha256"]})
        return self.store.detail(id)

    def publish_run_output(self, run_id: str):
        run = self.store.get("run", run_id)
        if not run.get("artifacts", {}).get("snapshot"):
            return
        comparison = self.store.get("comparison", run["comparison_id"])
        output = run.get("project_output", {})
        try:
            # Deliverables belong in the folder the user chose, whether or not
            # artifacts are mirrored to S3. The run's snapshot is the archived
            # copy, so keeping these local costs no durability.
            project = comparison.get("project_directory") or comparison.get("repository")
            if not project or project == "empty project":
                project = self.config.root.parent / "projects" / comparison["id"]
                Path(project).mkdir(parents=True, exist_ok=True)
            project = Path(project).expanduser().resolve(strict=True)
            if not comparison.get("project_directory"):
                self.store.update("comparison", comparison["id"], project_directory=str(project))
            output = publish_output(project, comparison, run)
        except Exception as exc:
            output = {**output, "status": "error", "error": str(exc)}
        self.store.update("run", run_id, project_output=output)
        self.store.event(comparison["id"], "project_output", output, run_id)
        return output

    def register_question(self, run: dict, request: dict):
        store = self.store
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            comparison = json.loads(db.execute(
                "SELECT data FROM objects WHERE kind='comparison' AND id=?",
                (run["comparison_id"],)).fetchone()[0])
            frozen = next((s for s in comparison.get("specifications", [])
                           if s["version"] == run["spec_version"]), None)
            question_spec = {**comparison, "answers": frozen["answers"]} if frozen else comparison
            participants = comparison.get("harnesses", list(HARNESS_NAMES))
            addressed_answers = for_harness(question_spec, run["harness"])["answers"]
            recipients = [h for h in participants
                          if for_harness(question_spec, h)["answers"] == addressed_answers]
            rows = db.execute("SELECT data FROM objects WHERE kind='question'").fetchall()
            fingerprint = re.sub(r"\s+", " ", request["question"].strip().casefold())
            match = None
            for row in rows:
                q = json.loads(row[0])
                if any(m["mailbox_id"] == request["id"] for m in q["mappings"]):
                    return q
                if (q["comparison_id"] == run["comparison_id"] and q["kind"] == "requirement"
                    and q.get("spec_version") == run["spec_version"]
                    and request["kind"] == "requirement" and q["fingerprint"] == fingerprint
                    and q.get("harnesses", participants) == recipients
                    and not q.get("withdrawn") and q["choices"] == request["choices"]
                    and q.get("schema") == request.get("schema")):
                    match = q
            q = match or {"id": uid(), "comparison_id": run["comparison_id"], "kind": request["kind"],
                "spec_version": run["spec_version"], "harnesses": recipients,
                "question": request["question"], "choices": request["choices"], "schema": request.get("schema"),
                "fingerprint": fingerprint, "created_at": now(), "answer": None, "mappings": []}
            q["mappings"].append({"run_id": run["id"], "harness": run["harness"],
                "session_id": request.get("session_id") or run.get("session_id"), "turn_key": request["turn_key"],
                "mailbox_id": request["id"], "native_id": request.get("native_id")})
            db.execute("INSERT OR REPLACE INTO objects VALUES('question',?,?)", (q["id"], json.dumps(q)))
        store.event(run["comparison_id"], "question", q, run["id"])
        return q

    def answer(self, question_id: str, answer, validation_plan: Plan | None = None):
        if answer is None or (isinstance(answer, str) and not answer.strip()):
            raise ValueError("An explicit answer is required")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM objects WHERE kind='question' AND id=?", (question_id,)).fetchone()
            if row is None:
                raise KeyError(question_id)
            q = json.loads(row[0])
            c = json.loads(db.execute("SELECT data FROM objects WHERE kind='comparison' AND id=?",
                                     (q["comparison_id"],)).fetchone()[0])
            if c["status"] in ("archiving", "storage_failed", "archive_queued", "restoring"):
                raise ValueError("Artifact transfer must finish before changing comparison answers.")
            if q.get("withdrawn"):
                raise ValueError("This question was withdrawn when its requesting runs were cancelled")
            if q["answer"] is not None:
                if q["answer"] == answer:
                    return q
                raise ValueError("This question is already answered; submit a specification revision instead")
            if q.get("schema"):
                import jsonschema
                value = json.loads(answer) if isinstance(answer, str) else answer
                try:
                    jsonschema.validate(value, q["schema"])
                except jsonschema.ValidationError as exc:
                    raise ValueError("Answer does not match the requested schema: " + exc.message) from exc
                answer = value
            q.update(answer=answer, answered_at=now())
            db.execute("UPDATE objects SET data=? WHERE kind='question' AND id=?", (json.dumps(q), question_id))
            c = json.loads(db.execute("SELECT data FROM objects WHERE kind='comparison' AND id=?",
                                     (q["comparison_id"],)).fetchone()[0])
            reviewing_saved_code = (c.get("test_review") or {}).get("mode") == "saved_code"
            if q["kind"] == "requirement":
                follow_up = q.get("question") == "User specification revision"
                entry = {"question_id": q["id"], "question": q["question"], "answer": answer,
                         "origins": [m["harness"] for m in q["mappings"]]}
                if follow_up:
                    entry["kind"] = "follow_up"
                if "harnesses" in q:
                    entry["harnesses"] = q["harnesses"]
                c["answers"].append(entry)
                if c["frozen"]:
                    c["spec_version"] += 1
                    c["frozen"] = False
                if validation_plan is not None:
                    c["validation_plan"] = validation_plan.model_dump()
                    c["plan_spec_version"] = c["spec_version"]
                if c.get("test_review"):
                    c["test_review"] = new_review(c, c["test_review"],
                        "Shared requirements changed. Reconsider the scenarios and request fresh approval.")
                if follow_up:
                    # Accept the recipients, their continuation, and cancellation
                    # overrides together so a finishing worker cannot lose this turn.
                    c.update(cancel_requested=False, ended_at=None)
                    if c["status"] not in self._WORKER_ACTIVE:
                        c["status"] = "queued"
                for row in db.execute(
                        "SELECT data FROM objects WHERE kind='run' AND json_extract(data,'$.comparison_id')=?",
                        (c["id"],)).fetchall():
                    run = json.loads(row[0])
                    # Legacy runs need an explicit previous target before the first
                    # selective revision; absence otherwise means the latest version.
                    run.setdefault("requested_spec_version", run["spec_version"])
                    selected = run["harness"] in q.get("harnesses", HARNESS_NAMES)
                    if selected:
                        run["requested_spec_version"] = c["spec_version"]
                        if follow_up:
                            run["cancel_requested"] = False
                    db.execute("UPDATE objects SET data=? WHERE kind='run' AND id=?",
                               (json.dumps(run), run["id"]))
                    if follow_up and selected and run["status"] in TERMINAL:
                        self.store.transition_in_transaction(db, run["id"], "queued",
                                                             **self.queued_run_fields(run))
                db.execute("UPDATE objects SET data=? WHERE kind='comparison' AND id=?", (json.dumps(c), c["id"]))
        self.store.event(q["comparison_id"], "answer", {"question_id": q["id"], "answer": answer})
        # If a native process ended while its question was outstanding, the
        # explicit answer authorizes continuation of this same comparison.
        if q["kind"] == "requirement" and not follow_up \
                and (c["status"] in ("complete", "incomplete", "interrupted") or reviewing_saved_code) \
                and not c["cancel_requested"]:
            self.queue_continuation(c["id"], q.get("harnesses"))
        return q

    def review_tests(self, comparison_id: str, review_id: str, *, feedback=None, answers=None,
                     test_coverage: TestCoverage | None = None):
        """Approval is explicit, version-bound and independent of coding approvals."""
        if test_coverage is not None:
            validate_test_coverage(test_coverage)
            if feedback is None:
                raise ValueError("Changing test coverage requires a new proposal and approval.")
        with ExitStack() as guards, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM objects WHERE kind='comparison' AND id=?",
                             (comparison_id,)).fetchone()
            if not row:
                raise KeyError(comparison_id)
            c = json.loads(row[0])
            review = c.get("test_review")
            if review and (review["status"] in ("generation_failed", "proposal_failed")
                           or (feedback is not None and c["status"] in ("complete", "incomplete", "interrupted"))):
                guards.enter_context(retry_gate(self.storage, comparison_id, db))
            if c["status"] in ("archiving", "storage_failed", "archive_queued", "restoring"):
                raise ValueError("Artifact transfer must finish before changing the test review.")
            if not review or review["id"] != review_id or review["requirements_sha256"] != requirements_digest(c):
                raise ValueError("This test proposal has changed. Refresh and review the latest version.")
            if c["cancel_requested"]:
                raise ValueError("This comparison is cancelled.")
            finalized = bool(review.get("plan_sha256"))
            if finalized and (feedback is None or c["status"] not in ("complete", "incomplete", "interrupted")):
                raise ValueError("Wait for the current evaluation to finish before requesting changes to finalized tests.")
            if feedback is not None:
                if not feedback.strip():
                    raise ValueError("Describe the changes you want to the proposed tests.")
                if review["status"] not in ("awaiting_review", "proposal_failed", "approved", "generation_failed"):
                    raise ValueError("Wait for the proposed tests to finish updating.")
                if test_coverage is not None:
                    c["test_coverage"] = test_coverage
                c["test_review"] = new_review(c, review, feedback.strip())
                if finalized or review.get("mode") == "saved_code":
                    c["test_review"]["mode"] = "saved_code"
                    c["status"] = "test_review_queued" if finalized else c["status"]
                action = "test_changes_requested"
            else:
                if review["status"] not in ("awaiting_review", "generation_failed", "approved"):
                    raise ValueError("A complete test proposal must be available before approval.")
                proposal = TestProposal.model_validate(review["proposal"])
                # Scenarios mirrored from a supplied validation plan are the
                # user's own, named by their check IDs; only a model's own draft
                # can be filler here.
                filler = [] if review.get("supplied_plan_sha256") else proposal_content_issues(proposal)
                if filler:
                    # Approving filler cannot produce executable tests: no
                    # generated check can bind to a scenario that says nothing.
                    raise ValueError("These proposed tests describe no real scenarios, so no "
                                     "executable tests can be developed from them. Use Request "
                                     "changes to get a new proposal. " + "; ".join(filler))
                answers = answers or {}
                expected = {q.id for q in proposal.questions}
                if set(answers) != expected or any(not answer.strip() for answer in answers.values()):
                    raise ValueError("Answer every test-review question before approving the tests.")
                if review["status"] == "approved":
                    if answers != review["answers"]:
                        raise ValueError("Request a test revision to change answers after approval.")
                    return c
                approval_hash = digest({"proposal": review["proposal"], "answers": answers})
                if (review["status"] == "generation_failed" and review.get("approved_at")
                        and review.get("approval_sha256") == approval_hash):
                    # Retry the already-authorized generation without inventing
                    # a new approval or losing the original human timestamp.
                    review.update(status="approved", generation_status="queued", error=None,
                                  generation_retry_requested_at=now())
                    action = "test_generation_retry_requested"
                else:
                    review.update(status="approved", answers=answers, approved_at=now(), error=None,
                                  approval_sha256=approval_hash)
                    action = "tests_approved"
            db.execute("UPDATE objects SET data=? WHERE kind='comparison' AND id=?",
                       (json.dumps(c), comparison_id))
        self.store.event(comparison_id, action, {
            "review_id": c["test_review"]["id"], "revision": c["test_review"]["revision"],
            "approval_sha256": c["test_review"].get("approval_sha256"),
            "answers": c["test_review"].get("answers") if action == "tests_approved" else None,
            "test_coverage": c["test_review"].get("test_coverage", "comprehensive"),
            "feedback": feedback})
        return self.store.detail(comparison_id)

    @requires_ready_artifacts
    def start_test_review(self, comparison_id: str):
        self.ensure_comparison(comparison_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM objects WHERE kind='comparison' AND id=?", (comparison_id,)).fetchone()
            if not row:
                raise KeyError(comparison_id)
            c = json.loads(row[0])
            if c["status"] not in ("complete", "incomplete", "interrupted"):
                raise ValueError("Wait for the current comparison to finish before reviewing saved-code tests.")
            candidates = [json.loads(r[0]) for r in db.execute(
                "SELECT data FROM objects WHERE kind='run' AND json_extract(data,'$.comparison_id')=?",
                (comparison_id,)).fetchall()]
            if not any(r["spec_version"] == c["spec_version"] and r.get("artifacts", {}).get("case") for r in candidates):
                raise ValueError("There are no evaluated saved implementations for this specification.")
            if c.get("test_review") and not c["cancel_requested"]:
                raise ValueError("Use Request changes on the existing test review.")
            c["test_review"] = new_review(c, c.get("test_review"),
                "Review the test scenarios for the saved implementations before developing new tests.")
            c["test_review"]["mode"] = "saved_code"
            c.update(status="test_review_queued", cancel_requested=False)
            db.execute("UPDATE objects SET data=? WHERE kind='comparison' AND id=?", (json.dumps(c), comparison_id))
        self.store.event(comparison_id, "test_review_requested", {"review_id": c["test_review"]["id"]})
        return self.store.detail(comparison_id)

    def queue_continuation(self, comparison_id: str, harnesses=None):
        for run in self.store.detail(comparison_id)["runs"]:
            if (harnesses is None or run["harness"] in harnesses) \
                    and run["status"] != "cancelled" and not run["cancel_requested"]:
                self.queue_run(run)
        self.store.update("comparison", comparison_id, status="queued", cancel_requested=False, ended_at=None)

    def queue_run(self, run: dict):
        self.store.transition(run["id"], "queued", **self.queued_run_fields(run))

    @staticmethod
    def queued_run_fields(run: dict):
        last_attempt = run.get("attempts", [])[-1] if run.get("attempts") else {}
        resume_implementation = (last_attempt.get("id") if
            run["status"] in ("failed", "interrupted")
            and last_attempt.get("phase") == "develop"
            and last_attempt.get("outcome") == "implemented"
            and last_attempt.get("spec_version") == run["spec_version"]
            and not run.get("artifacts", {}).get("case") else None)
        history = list(run.get("artifact_history", []))
        if run.get("artifacts"):
            history.append({"spec_version": run["spec_version"], "archived_at": now(),
                            "artifacts": run["artifacts"], "evaluation": run.get("evaluation"),
                            "evidence": run.get("evidence")})
        return {"cancel_requested": False, "ended_at": None, "evaluation": None,
                "evaluation_scope_note": None, "evidence": None, "artifacts": {},
                "artifact_history": history, "resume_implementation_attempt": resume_implementation}

    # Comparison statuses where the worker loop is actively running a task; spec_version
    # change is enough — do not reassign the comparison to the dispatch queue.
    # W8: includes "testing" and "reviewing_tests" so a spec revision during the test
    # stage is handled via the existing in-flight revision path, not a re-dispatch.
    _WORKER_ACTIVE = frozenset({"preparing", "clarifying", "developing", "evaluating", "planning",
                                 "waiting_user", "waiting_approval", "testing", "reviewing_tests",
                                 "test_review_queued"})
    # Comparison statuses set by reevaluate/reverify — no coding turns run; follow-ups must wait.
    _RESCORE = frozenset({"evaluation_queued", "verification_queued"})

    def revise(self, comparison_id: str, text: str, validation_plan: dict | None = None,
               harnesses: list[str] | None = None):
        if not text.strip():
            raise ValueError("Revision text is required")
        plan = Plan.model_validate(validation_plan) if validation_plan is not None else None
        c = self.store.detail(comparison_id)
        harnesses = selected_harnesses(harnesses, [r["harness"] for r in c["runs"]])
        if c["status"] in ("archiving", "storage_failed", "archive_queued", "restoring"):
            raise ValueError("Artifact transfer must finish before revising the comparison.")
        if c["status"] in self._RESCORE:
            raise ValueError("Wait for re-scoring to finish, then send the follow-up.")
        q = {"id": uid(), "comparison_id": comparison_id, "kind": "requirement",
             "question": "User specification revision", "choices": [], "schema": None,
             "fingerprint": uid(), "answer": None, "mappings": [], "created_at": now(),
             "harnesses": harnesses}
        self.store.put("question", q)
        self.answer(q["id"], text, plan)
        return self.store.detail(comparison_id)

    def cancel(self, comparison_id: str, run_id: str | None = None):
        if run_id:
            r = self.store.get("run", run_id)
            if r["comparison_id"] != comparison_id:
                raise ValueError("Run belongs to another comparison")
            self.store.update("run", run_id, cancel_requested=True)
        else:
            self.store.update("comparison", comparison_id, cancel_requested=True)
            for r in self.store.detail(comparison_id)["runs"]:
                self.store.update("run", r["id"], cancel_requested=True)
        for q in self.store.detail(comparison_id)["questions"]:
            if q["answer"] is None and q["mappings"] and all(
                self.store.get("run", m["run_id"])["cancel_requested"] for m in q["mappings"]):
                self.store.update("question", q["id"], withdrawn=True)
        self.store.event(comparison_id, "cancel_requested", {"run_id": run_id})

    @requires_ready_artifacts(run=True)
    def retry(self, run_id: str):
        r = self.store.get("run", run_id)
        c = self.store.get("comparison", r["comparison_id"])
        if r["status"] not in TERMINAL or c["status"] not in ("complete", "incomplete", "interrupted"):
            raise ValueError("Retry is available after the current comparison reaches a terminal state")
        self.queue_run(r)
        self.store.update("comparison", c["id"], status="queued", cancel_requested=False)
        return self.store.get("run", run_id)

    @requires_ready_artifacts
    def retry_comparison(self, comparison_id: str):
        self.ensure_comparison(comparison_id)
        c = self.store.detail(comparison_id)
        if c["status"] not in ("complete", "incomplete", "interrupted"):
            raise ValueError("Wait for the current comparison to stop before resuming")
        for r in c["runs"]:
            if r["status"] in TERMINAL and r["status"] != "verified_complete":
                self.queue_run(r)
        self.store.update("comparison", comparison_id, status="queued", cancel_requested=False)
        self.store.event(comparison_id, "resume_requested", {"scope": "incomplete runs"})
        return self.store.detail(comparison_id)

    @requires_ready_artifacts
    def reevaluate(self, comparison_id: str, spec_version: int | None = None,
                   judge_model: str | None = None, run_id: str | None = None):
        self.ensure_comparison(comparison_id)
        c = self.store.detail(comparison_id)
        version = c["spec_version"] if spec_version is None else spec_version
        if c["status"] not in ("complete", "incomplete", "interrupted"):
            raise ValueError("Wait for this comparison to stop before re-evaluating")
        if run_id is not None and not any(r["id"] == run_id for r in c["runs"]):
            raise ValueError("Run belongs to another comparison")
        def available(run):
            return (run_id is None or run["id"] == run_id) and ((run["spec_version"] == version and run.get("artifacts", {}).get("case"))
                or any(h["spec_version"] == version and h["artifacts"].get("case")
                       for h in run.get("artifact_history", [])))
        if not any(available(r) for r in c["runs"]):
            raise ValueError("No stored candidate exists for that specification")
        judge = self.judge_selection(judge_model, c)
        for run in c["runs"]:
            if available(run):
                self.store.update("run", run["id"], cancel_requested=False)
        self.store.update("comparison", comparison_id, status="evaluation_queued", cancel_requested=False,
                          evaluation_spec_version=version, judge=judge,
                          evaluation_run_ids=[r["id"] for r in c["runs"] if available(r)])
        self.store.event(comparison_id, "evaluation_queued",
                         {"source": "stored immutable candidates only", "spec_version": version,
                          "run_id": run_id, "judge": judge})
        return self.store.detail(comparison_id)

    @requires_ready_artifacts
    def reverify(self, comparison_id: str, validation_plan: dict | None = None,
                 reason: str | None = None, judge_model: str | None = None):
        self.ensure_comparison(comparison_id)
        c = self.store.detail(comparison_id)
        if c["status"] not in ("complete", "incomplete", "interrupted", "reviewing_tests"):
            raise ValueError("Wait for the current comparison to finish before re-running verification")
        candidates = [r for r in c["runs"] if r.get("artifacts", {}).get("case")
                      and r["spec_version"] == c["spec_version"]]
        if not candidates:
            raise ValueError("No saved implementations exist for the current specification")
        if validation_plan is not None and not (reason or "").strip():
            raise ValueError("A corrected validation plan requires a reason for the audit history")
        previous = c.get("verification_plan", {})
        # Detect new-flow comparisons: test_review is set but plan_sha256 is None,
        # meaning each harness wrote its own tests and there is no shared plan.
        is_new_flow = (c.get("test_review") and c["test_review"].get("plan_sha256") is None
                       and not validation_plan)
        if is_new_flow:
            # New-flow reverify uses each run's stored per-harness plan from case.json.
            # A caller-supplied validation_plan is not supported for new-flow; direct
            # them to review_saved_code instead.
            candidates_dict = {}
            for r in candidates:
                art = r["artifacts"].copy()
                case_data = json.loads(Path(art["case"]).read_text())
                art["plan"] = case_data["plan"]  # per-harness plan from original evaluation
                candidates_dict[r["id"]] = art
            # Validate each per-run plan.
            for r_id, art in candidates_dict.items():
                try:
                    per_plan = Plan.model_validate(art["plan"])
                    q = plan_quality_issues(per_plan)
                    if q:
                        raise ValueError(f"Run {r_id}: " + "; ".join(q))
                except Exception as exc:
                    raise ValueError(f"Cannot re-verify: per-run plan invalid for run {r_id}: {exc}") from exc
            judge = self.judge_selection(judge_model, c)
            revision = uid()
            request = {"id": revision, "spec_version": c["spec_version"], "plan": None,
                       "reason": reason or "Re-run independent checks on the same saved per-harness plans",
                       "created_at": now(), "status": "queued", "judge": judge,
                       "candidates": candidates_dict}
        else:
            source_plan = validation_plan if validation_plan is not None else (
                previous["plan"] if previous.get("spec_version") == c["spec_version"] else c.get("plan"))
            plan = Plan.model_validate(source_plan)
            if (c.get("test_review") and c["test_review"].get("plan_sha256") is not None
                    and c["test_review"].get("plan_sha256") != plan_digest(plan)):
                raise ValueError("Changed tests need a new approved review. Use Request changes in Review tests.")
            issues = plan_quality_issues(plan)
            if issues:
                raise ValueError("The saved evaluation plan needs correction before re-running checks: "
                                 + "; ".join(issues) + ". Supply a corrected plan with multi-coding-harness-compare reverify --plan.")
            judge = self.judge_selection(judge_model, c)
            revision = uid()
            request = {"id": revision, "spec_version": c["spec_version"], "plan": plan.model_dump(),
                       "reason": reason or "Re-run independent checks on the same saved implementations",
                       "created_at": now(), "status": "queued", "judge": judge,
                       "candidates": {r["id"]: r["artifacts"] for r in candidates}}
        if c.get("test_review"):
            request["test_review"] = {key: c["test_review"].get(key) for key in
                ("id", "revision", "proposal", "answers", "approved_at", "approval_sha256", "plan_sha256")}
            request["test_review"]["test_coverage"] = c["test_review"].get("test_coverage", "comprehensive")
        for run in candidates:
            self.store.update("run", run["id"], cancel_requested=False)
        self.store.update("comparison", comparison_id, status="verification_queued",
                          cancel_requested=False, verification_request=request, judge=judge,
                          verification_plan={"spec_version": c["spec_version"], "plan": plan.model_dump(),
                                             "revision": revision, "reason": request["reason"]})
        self.store.event(comparison_id, "verification_queued", {
            "revision": revision, "reason": request["reason"], "spec_version": c["spec_version"], "judge": judge,
            "source": "saved immutable candidates; coding harnesses will not run"})
        return self.store.detail(comparison_id)

    @staticmethod
    def _test_review_failure(comparison_id: str, review: dict) -> dict | None:
        """Build the 'failure' block for a generation_failed or proposal_failed review."""
        status = review.get("status")
        if status not in ("generation_failed", "proposal_failed"):
            return None
        stage = "test development" if status == "generation_failed" else "test proposal"
        error = review.get("error") or ""
        attempts = review.get("generation_attempts") or []
        latest = attempts[-1] if attempts else None

        attempt_summary = None
        attempt_index = len(attempts) if attempts else None
        if latest:
            started, ended = latest.get("started_at"), latest.get("ended_at")
            duration = round(ended - started, 1) if started and ended else None
            attempt_summary = {
                "index": attempt_index,
                "model": latest.get("model"),
                "effort": latest.get("effort"),
                "started_at": started,
                "ended_at": ended,
                "duration_seconds": duration,
                "max_tokens": latest.get("max_tokens"),
            }

        attempt_url = (f"/api/comparisons/{comparison_id}/test-review/attempts/{attempt_index}"
                       if latest and latest.get("artifact") else None)

        # Map the raw error to a plain-language cause.
        err_low = error.lower()
        model = (latest or {}).get("model") or "the model"
        effort = (latest or {}).get("effort") or "default"
        if "read timed out" in err_low or "readtimeouterror" in err_low:
            cause = (f"Bedrock sent no data for {PLANNING_STALL_SECONDS} s "
                     f"(the planner's stall limit) while {model} was working at effort {effort}; "
                     "the model likely needed longer than the limit to start responding.")
        elif "throttling" in err_low or "too many requests" in err_low:
            cause = "Bedrock throttled the request; try again in a few minutes."
        elif "accessdenied" in err_low or "unauthorized" in err_low or "forbidden" in err_low:
            cause = "Access denied — check that the IAM role or Bedrock model access policy allows this model."
        elif "output limit" in err_low or "maxtokens" in err_low or "max_tokens" in err_low:
            cause = "The model's response hit the configured output token limit."
        elif "validationexception" in err_low or "invalid model" in err_low:
            cause = "The planning model ID was rejected; verify the model is available in the configured region."
        elif "connection" in err_low or "name or service not known" in err_low:
            cause = "Network error connecting to Bedrock; check connectivity and retry."
        else:
            cause = error.split("\n")[0] if error else "Unknown error"

        return {"stage": stage, "error": error, "cause": cause,
                "attempt": attempt_summary, "attempt_url": attempt_url}

    def read_test_review_attempt(self, comparison_id: str, index: int) -> dict:
        """Return the attempt record for a test-review generation attempt (1-based)."""
        c = self.ensure_comparison(comparison_id)
        review = c.get("test_review") or {}
        attempts = review.get("generation_attempts") or []
        if not attempts:
            raise KeyError("attempt not found")
        if index < 1 or index > len(attempts):
            raise KeyError(f"attempt {index} not found; {len(attempts)} attempt(s) recorded")
        attempt = attempts[index - 1]
        if not attempt.get("artifact"):
            raise KeyError("attempt record not saved")
        # Re-root: find "comparisons/<id>/" in the recorded absolute path.
        artifact_path = Path(attempt["artifact"])
        parts = artifact_path.parts
        rel_parts = None
        for i, part in enumerate(parts[:-1]):
            if part == "comparisons" and parts[i + 1] == comparison_id:
                rel_parts = parts[i + 2:]
                break
        if not rel_parts:
            raise KeyError("attempt record path does not reference this comparison")
        rel = Path(*rel_parts)
        if rel.is_absolute() or ".." in rel.parts:
            raise KeyError("attempt record path escapes the comparison root")
        rerooted = self.storage.root(comparison_id) / rel
        raw = self.storage.read_file(comparison_id, rerooted)
        return {"comparison_id": comparison_id, "review_id": review.get("id"),
                "index": index, "attempt": attempt, "record": json.loads(raw)}

    def report(self, comparison_id: str, *, archival=False):
        self.ensure_comparison(comparison_id)
        c = self.store.detail(comparison_id)
        if archival:
            c["status"] = c.get("artifact_final_status") or c["status"]
            c["artifact_final_status"] = None
        if self.storage.enabled:
            if not archival:
                c["artifact_readiness"] = readiness(self.storage, comparison_id)
            c["artifact_storage"] = (
                {"status": "archived", "index_uri":
                 f"s3://{self.storage.bucket}/{self.storage.prefix}/comparisons/{comparison_id}/latest.json"}
                if archival else self.storage.state(comparison_id))
        for r in c["runs"]:
            r["duration"] = durations(r)
            r["usage"] = usage_summary(r["usage_records"], r["harness"])
            if r.get("project_output"):
                r["project_output"] = output_status(r["project_output"])
            if self.storage.enabled:
                state = c["artifact_storage"]
                r["artifact_storage"] = {
                    "status": state["status"], "uri": state.get("uri"),
                    "archive_uri": (f"s3://{state['bucket']}/{state['archive']['key']}"
                                    if state.get("archive") else None),
                    "members": {key: str(Path(value).relative_to(self.storage.root(comparison_id)))
                                for key, value in r.get("artifacts", {}).items()
                                if isinstance(value, str) and Path(value).is_absolute()
                                and Path(value).is_relative_to(self.storage.root(comparison_id))},
                }
                if state["status"] == "archived" and r.get("project_output"):
                    r["project_output"] = {**r["project_output"], "status": "archived",
                                           "uri": r["artifact_storage"]["archive_uri"],
                                           "note": "Code is stored in S3. Inspect files or download the patch."}
        questions_by_id = {q["id"]: q for q in c.get("questions", [])}
        c["follow_ups"] = [{**fu, "created_at": questions_by_id.get(fu["id"], {}).get("created_at", 0)}
                           for fu in follow_ups(c)]
        c["comparison_complete"] = bool(c["runs"]) and all(
            r.get("evaluation") and r["evaluation"]["overall"] is not None
            and r["spec_version"] == c["spec_version"] for r in c["runs"])
        if c.get("test_review"):
            failure = self._test_review_failure(comparison_id, c["test_review"])
            if failure:
                c["test_review"] = {**c["test_review"], "failure": failure}
            c["comparison_complete"] = c["comparison_complete"] and all(
                (r.get("evaluation") or {}).get("test_review_id") == c["test_review"]["id"]
                for r in c["runs"])
        if self.storage.enabled and c["status"] in ("archiving", "storage_failed", "archive_queued", "restoring"):
            c["comparison_complete"] = False
        c["material_differences"] = [{
            "harness": r["harness"], "model": r.get("model"), "requested_model": r.get("requested_model"),
            "requested_effort": r.get("requested_effort"),
            "runtime": r.get("runtime"),
            "capabilities": r["capabilities"], "status": r["status"]} for r in c["runs"]]
        c["limitations"] = [
            "Scores evaluate generated implementations only. Efficiency is not part of quality scores.",
            "Native MCP servers, hooks and tools retain their configured remote authority; use isolated service credentials.",
            "Token totals are partial unless the native harness proves aggregate coverage; Kiro credit/context usage is not converted to tokens.",
            "Only the explicitly listed behaviors were independently verified.",
        ]
        return c

    def retry_archive(self, comparison_id: str):
        c = self.store.get("comparison", comparison_id)
        if c["status"] not in ("storage_failed", "complete", "incomplete", "interrupted", "waiting_test_review"):
            raise ValueError("Wait for the comparison to stop before retrying artifact storage.")
        if not self.storage.enabled:
            raise ValueError("S3 artifact storage is not configured.")
        self.store.update("comparison", comparison_id, status="archive_queued",
                          artifact_final_status=c.get("artifact_final_status") or c["status"])
        return self.report(comparison_id)

    def retry_artifact_recovery(self):
        state = readiness(self.storage)
        if not state.get("can_retry_recovery"):
            raise ArtifactsNotReady(state)
        try:
            with self.storage.lock("maintenance", blocking=False):
                state = readiness(self.storage)
                if not state.get("can_retry_recovery"):
                    raise ArtifactsNotReady(state)
                previous = self.store.get("system", "artifact-recovery")
                self.store.put("system", {**previous, "status": "queued", "error": None,
                                         "updated_at": now(), "finished_at": None})
        except BlockingIOError:
            raise ArtifactsNotReady({**state, "ready": False, "message": "S3 recovery is already running."}) from None
        return readiness(self.storage)
