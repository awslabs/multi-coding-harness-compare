# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Selection is enforced at submission, resumption, and the native prompt boundary."""
import asyncio
import json
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from arena.api import create_app
from arena.auth import access_token
from arena.config import Config, HarnessConfig
from arena.evaluation import Plan, default_plan
from arena.prompts import for_harness
from arena.service import Service
from arena.store import now
from arena.worker import Worker


@pytest.fixture
def worker(tmp_path):
    return Worker(Config(data_dir=str(tmp_path / "data"), artifact_s3_uri=None))


@pytest.fixture
def client(tmp_path):
    config = Config(data_dir=str(tmp_path / "api"), artifact_s3_uri=None)
    with patch("arena.api.load_config", return_value=config):
        app = create_app()
    with TestClient(app, base_url="http://127.0.0.1",
                    headers={"Authorization": "Bearer " + access_token(config.root)}) as test_client:
        yield test_client


def _terminal_comparison(worker, harnesses=None):
    comparison = worker.service.create("Build a small CLI", harnesses=harnesses)
    worker.store.update("comparison", comparison["id"], test_review=None)
    worker.freeze_requirements(comparison["id"])
    for run in comparison["runs"]:
        worker.store.transition(run["id"], "verified_complete", clarified=True,
                                evaluation={"overall": 100},
                                artifacts={"snapshot": "original", "sha256": "original"})
    worker.store.update("comparison", comparison["id"], status="complete")
    return comparison


def _runs(worker, comparison_id):
    return {run["harness"]: run for run in worker.store.detail(comparison_id)["runs"]}


def test_new_comparison_creates_only_selected_jobs_and_outputs(worker):
    comparison = worker.service.create("Build a CLI", harnesses=["codex"])
    assert comparison["harnesses"] == ["codex"]
    assert [run["harness"] for run in comparison["runs"]] == ["codex"]
    assert comparison["models"] == {"codex": None}
    assert set(comparison["efforts"]) == {"codex"}
    project = Path(comparison["project_directory"])
    assert (project / "codex").is_dir()
    assert not (project / "claude").exists()
    assert not (project / "kiro").exists()
    submitted = next(event for event in worker.store.events(comparison["id"])
                     if event["type"] == "submitted")
    assert submitted["data"]["harnesses"] == ["codex"]


def test_off_harness_model_configuration_is_not_resolved(tmp_path):
    config = Config(data_dir=str(tmp_path / "data"), artifact_s3_uri=None,
                    harnesses={"claude": HarnessConfig(model="unavailable-model"),
                               "codex": HarnessConfig(), "kiro": HarnessConfig()})
    comparison = Service(config).create("Build a CLI", harnesses=["codex"])
    assert comparison["models"] == {"codex": None}


@pytest.mark.parametrize("selection", [[], ["unknown"], ["claude", "claude"], "codex", [None]])
def test_invalid_selection_has_no_records_or_project_side_effects(worker, selection):
    with patch("arena.service.prepare_outputs") as prepare:
        with pytest.raises(ValueError):
            worker.service.create("Build a CLI", harnesses=selection)
    prepare.assert_not_called()
    assert worker.store.list("comparison") == []
    assert worker.store.list("run") == []


@pytest.mark.parametrize("selection", [[], None, ["unknown"], ["codex", "codex"], "codex"])
def test_api_rejects_invalid_selection_for_creation_and_follow_up(client, selection):
    rejected = client.post("/api/comparisons", json={"prompt": "Build a CLI", "harnesses": selection})
    assert rejected.status_code in (400, 422)
    assert client.app.state.service.store.list("comparison") == []
    created = client.post("/api/comparisons", json={"prompt": "Build a CLI", "harnesses": ["codex"]})
    assert created.status_code == 201
    comparison_id = created.json()["id"]
    rejected = client.post(f"/api/comparisons/{comparison_id}/revisions",
                           json={"text": "Add colors", "harnesses": selection})
    assert rejected.status_code in (400, 422)
    assert client.app.state.service.store.get("comparison", comparison_id)["answers"] == []
    assert client.app.state.service.store.list("question") == []


@pytest.mark.parametrize("heartbeat", [now(), 0])
def test_unknown_worker_capability_blocks_selected_work_even_if_heartbeat_is_stale(client, heartbeat):
    service = client.app.state.service
    comparison = service.create("Build a CLI", harnesses=["codex"])
    service.store.put("system", {"id": "worker", "heartbeat": heartbeat, "test_coverage_version": 1})
    assert client.get("/api/system").json()["harness_selection"]["worker_ready"] is False
    response = client.post("/api/comparisons", json={"prompt": "Another task", "harnesses": ["codex"]})
    assert response.status_code == 409
    response = client.post(f"/api/comparisons/{comparison['id']}/revisions",
                           json={"text": "Add colors", "harnesses": ["codex"]})
    assert response.status_code == 409
    assert service.store.get("comparison", comparison["id"])["answers"] == []
    assert len(service.store.list("comparison")) == 1


def test_all_three_in_any_order_needs_no_selection_capable_worker(client):
    """Switching a harness off and on reorders the list; it is still all three."""
    service = client.app.state.service
    service.store.put("system", {"id": "worker", "heartbeat": now(), "test_coverage_version": 1})
    assert client.get("/api/system").json()["harness_selection"]["worker_ready"] is False
    response = client.post("/api/comparisons",
                           json={"prompt": "Build a CLI", "harnesses": ["kiro", "claude", "codex"]})
    assert response.status_code == 201
    assert response.json()["harnesses"] == ["claude", "codex", "kiro"]
    assert client.post("/api/comparisons", json={"prompt": "Two", "harnesses": ["kiro", "claude"]}
                       ).status_code == 409


def test_omitted_selection_preserves_original_participants(client):
    original = client.post("/api/comparisons", json={"prompt": "Build a CLI"}).json()
    assert original["harnesses"] == ["claude", "codex", "kiro"]
    selected = client.post("/api/comparisons", json={"prompt": "One CLI", "harnesses": ["kiro"]}).json()
    revision = client.post(f"/api/comparisons/{selected['id']}/revisions", json={"text": "Add colors"})
    assert revision.status_code == 200
    assert revision.json()["answers"][-1]["harnesses"] == ["kiro"]
    assert client.post(f"/api/comparisons/{selected['id']}/revisions",
                       json={"text": "Other", "harnesses": ["claude"]}).status_code == 400


def test_follow_up_only_revives_selected_run_and_keeps_recipient_history(worker):
    comparison = _terminal_comparison(worker)
    before = _runs(worker, comparison["id"])
    worker.service.revise(comparison["id"], "Codex only change", harnesses=["codex"])
    after = _runs(worker, comparison["id"])
    assert after["codex"]["status"] == "queued"
    assert after["codex"]["requested_spec_version"] == 2
    assert after["codex"]["artifact_history"][0]["evaluation"] == {"overall": 100}
    for harness in ("claude", "kiro"):
        assert after[harness] == before[harness]
    report = worker.service.report(comparison["id"])
    assert report["answers"][-1]["harnesses"] == ["codex"]
    assert report["follow_ups"][-1]["harnesses"] == ["codex"]


def test_legacy_off_run_retains_previous_requested_version(worker):
    comparison = _terminal_comparison(worker)
    for run in comparison["runs"]:
        legacy = worker.store.get("run", run["id"])
        legacy.pop("requested_spec_version")
        worker.store.put("run", legacy)
    worker.service.revise(comparison["id"], "Codex only change", harnesses=["codex"])
    runs = _runs(worker, comparison["id"])
    assert runs["codex"]["requested_spec_version"] == 2
    assert runs["claude"]["requested_spec_version"] == 1
    assert runs["kiro"]["requested_spec_version"] == 1


def test_clarification_from_scoped_follow_up_cannot_broadcast_excluded_text(worker):
    comparison = _terminal_comparison(worker)
    worker.service.revise(comparison["id"], "Codex private requirement", harnesses=["codex"])
    current = worker.freeze_requirements(comparison["id"])
    runs = _runs(worker, comparison["id"])
    codex = worker.store.update("run", runs["codex"]["id"], spec_version=current["spec_version"])
    question = worker.service.register_question(codex, {
        "id": "mailbox", "turn_key": "turn", "kind": "requirement",
        "question": "Which detail for the Codex private requirement?", "choices": []})
    assert question["harnesses"] == ["codex"]
    worker.service.answer(question["id"], "Codex private answer")
    current = worker.store.get("comparison", comparison["id"])
    assert "Codex private answer" in json.dumps(for_harness(current, "codex"))
    assert "Codex private answer" not in json.dumps(for_harness(current, "claude")["answers"])
    assert _runs(worker, comparison["id"])["claude"]["requested_spec_version"] == 1


def test_original_clarifications_stay_shared_between_participants(worker):
    comparison = worker.service.create("Build a CLI", harnesses=["claude", "kiro"])
    question = worker.service.register_question(comparison["runs"][0], {
        "id": "mailbox", "turn_key": "turn", "kind": "requirement",
        "question": "Which output format?", "choices": []})
    assert question["harnesses"] == ["claude", "kiro"]


class _Stream:
    def __init__(self, lines=()):
        self.lines = list(lines)

    async def readline(self):
        return self.lines.pop(0) if self.lines else b""


class _Process:
    def __init__(self, running=False):
        self.returncode = None if running else 0
        self.stdout = _Stream([b'{"type":"turn_complete","data":{}}\n'])
        self.stderr = _Stream()

    async def wait(self):
        self.returncode = 0
        return 0


def _runtime(worker, process=None):
    async def start(run, root, phase):
        (root / "bridge" / "requests").mkdir(parents=True, exist_ok=True)
        (root / "bridge" / "answers").mkdir(parents=True, exist_ok=True)
        return {}

    worker.runtime.start = AsyncMock(side_effect=start)
    worker.runtime.launch = AsyncMock(return_value=process or _Process())
    worker.runtime.stop = AsyncMock()
    worker.runtime.supervise = AsyncMock()


def test_reenabled_harness_never_receives_previously_skipped_prompt_or_rubric(worker):
    async def scenario():
        comparison = _terminal_comparison(worker)
        comparison_id = comparison["id"]
        worker.service.revise(comparison_id, "Codex first change", harnesses=["codex"])
        worker.freeze_requirements(comparison_id)
        worker.service.revise(comparison_id, "Kiro excluded instruction", harnesses=["kiro"])
        worker.freeze_requirements(comparison_id)
        worker.service.revise(comparison_id, "Codex latest change", harnesses=["codex"])
        spec = worker.freeze_requirements(comparison_id)
        plan = Plan.model_validate(spec["development_plan"])
        plan.criteria[0].requirement = "Kiro excluded instruction paraphrased as a common criterion"
        codex = _runs(worker, comparison_id)["codex"]
        (worker.run_root(codex) / "workspace").mkdir(parents=True)
        _runtime(worker)
        with patch("arena.worker.initialize_checkout", AsyncMock()):
            outcome = await worker.phase(codex["id"], "develop", spec, plan)
        assert outcome == "implemented"
        current = worker.store.get("run", codex["id"])
        prompt = current["attempts"][-1]["prompt"]
        bridge = (worker.run_root(codex) / "bridge" / "specification.json").read_text()
        for payload in (prompt, bridge):
            assert "Codex first change" in payload
            assert "Codex latest change" in payload
            assert "Kiro excluded instruction" not in payload
        answers = for_harness(spec, "codex")["answers"]
        assert current["attempts"][-1]["follow_ups"] == [answer["question_id"] for answer in answers]
        assert current["follow_ups_delivered"] == [answer["question_id"] for answer in answers]

    asyncio.run(scenario())


@pytest.mark.parametrize("recipient,expected", [("kiro", "implemented"), ("codex", "revised")])
def test_inflight_turn_restarts_only_for_its_recipient(worker, recipient, expected):
    async def scenario():
        comparison = worker.service.create("Build a CLI")
        comparison_id = comparison["id"]
        spec = worker.freeze_requirements(comparison_id)
        plan = Plan.model_validate(spec["development_plan"])
        codex = _runs(worker, comparison_id)["codex"]
        (worker.run_root(codex) / "workspace").mkdir(parents=True)
        process = _Process(running=True)
        _runtime(worker, process)

        async def follow_up_during_turn(container):
            worker.service.revise(comparison_id, "Selected new instruction", harnesses=[recipient])
            process.returncode = 0

        worker.runtime.supervise = AsyncMock(side_effect=follow_up_during_turn)
        with patch("arena.worker.initialize_checkout", AsyncMock()):
            outcome = await worker.phase(codex["id"], "develop", spec, plan)
        assert outcome == expected
        assert "Selected new instruction" not in worker.store.get("run", codex["id"])["attempts"][-1]["prompt"]
        assert worker.runtime.launch.await_count == 1

    asyncio.run(scenario())


def _mock_comparison_execution(worker):
    dispatches = []
    evaluations = []

    async def phase(run_id, kind, spec, plan, **kwargs):
        harness = worker.store.get("run", run_id)["harness"]
        dispatches.append((harness, kind, spec["spec_version"]))
        worker.store.transition(run_id, "ready" if kind == "clarify" else "implemented",
                                clarified=True, spec_version=spec["spec_version"])
        return "ready" if kind == "clarify" else "implemented"

    async def snapshot(run_id, spec):
        worker.store.update("run", run_id, artifacts={"snapshot": "saved", "sha256": "saved"})

    async def freeze(comparison_id, baseline, spec=None):
        return default_plan(spec, baseline), {}, spec

    async def evaluate(run_id, spec, plan, baseline_results):
        evaluations.append(run_id)
        worker.store.transition(run_id, "verified_complete", evaluation={"overall": 100})

    worker.phase = AsyncMock(side_effect=phase)
    worker.snapshot_run = AsyncMock(side_effect=snapshot)
    worker.freeze = AsyncMock(side_effect=freeze)
    worker.evaluate_run = AsyncMock(side_effect=evaluate)
    return dispatches, evaluations


def test_single_harness_completes_and_off_harnesses_are_never_dispatched(worker):
    comparison = worker.service.create("Build a CLI", harnesses=["kiro"])
    worker.store.update("comparison", comparison["id"], test_review=None)
    dispatches, evaluations = _mock_comparison_execution(worker)
    asyncio.run(worker.compare(comparison["id"]))
    assert [entry[:2] for entry in dispatches] == [("kiro", "clarify"), ("kiro", "develop")]
    assert len(evaluations) == 1
    assert worker.service.report(comparison["id"])["comparison_complete"] is True
    assert worker.store.get("comparison", comparison["id"])["status"] == "complete"


def test_failed_follow_up_initialization_stops_without_relabeling_previous_artifacts(worker):
    comparison = _terminal_comparison(worker, harnesses=["codex"])
    comparison_id = comparison["id"]
    worker.service.revise(comparison_id, "Add JSON output", harnesses=["codex"])
    with patch("arena.worker.default_plan", side_effect=RuntimeError("Preparation unavailable")) as prepare, \
            patch.object(worker, "phase", AsyncMock()) as phase:
        asyncio.run(worker.compare(comparison_id))
        asyncio.run(worker.compare(comparison_id))
    prepare.assert_called_once()
    phase.assert_not_awaited()
    run = _runs(worker, comparison_id)["codex"]
    assert run["status"] == "failed"
    assert run["attempted_requested_spec_version"] == run["requested_spec_version"] == 2
    assert run["spec_version"] == run["artifact_history"][0]["spec_version"] == 1
    assert worker.store.get("comparison", comparison_id)["status"] == "incomplete"


def test_disjoint_follow_ups_same_version_preserve_code_without_scoring_wrong_scope(worker):
    comparison = _terminal_comparison(worker)
    comparison_id = comparison["id"]
    worker.service.revise(comparison_id, "Codex uses Python", harnesses=["codex"])
    worker.service.revise(comparison_id, "Claude uses TypeScript", harnesses=["claude"])
    dispatches, evaluations = _mock_comparison_execution(worker)
    asyncio.run(worker.compare(comparison_id))
    assert Counter(dispatches) == Counter([("claude", "develop", 2), ("codex", "develop", 2)])
    assert evaluations == []
    runs = _runs(worker, comparison_id)
    for harness in ("claude", "codex"):
        assert runs[harness]["status"] == "evaluated_incomplete"
        assert runs[harness]["artifacts"]["snapshot"] == "saved"
        assert "was not sent that follow-up" in runs[harness]["evaluation_scope_note"]
    assert runs["kiro"]["status"] == "verified_complete"
    assert runs["kiro"]["artifacts"]["snapshot"] == "original"
    assert worker.service.report(comparison_id)["comparison_complete"] is False
