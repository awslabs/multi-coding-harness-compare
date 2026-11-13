# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Coverage choices across the API, planner, approval and S3 checkpoints."""
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from arena.api import create_app
from arena.artifact_storage import ArtifactStorage
from arena.auth import access_token
from arena.config import Config
from arena.evaluation import Check, Criterion, Plan, prepare_plan
from arena.service import Service
from arena.store import Store
from arena.test_review import (FOCUSED_LIMITATION, FocusedTestProposal, ReviewedCheck,
                               ReviewedTestCode, TestProposal, TestScenario,
                               compile_reviewed_tests, new_review, propose_tests)
from test_artifact_storage import FakeS3


def proposal(count=6):
    return TestProposal(summary="Public application behavior", test_cases=[
        TestScenario(id=f"case-{index}", title=f"Public behavior {index}",
                     steps=["Invoke the public command with a controlled input"],
                     expected_result="The required output is returned",
                     test_data="Deterministic input fixture")
        for index in range(count)
    ])


def code(count=6):
    return ReviewedTestCode(checks=[
        ReviewedCheck(id=f"check-{index}", scenario_id=f"case-{index}",
                      command=f"python /acceptance/checks.py {index}")
        for index in range(count)
    ], acceptance_files={"checks.py": "from pathlib import Path\nassert Path('/workspace/main.py').is_file()\n"})


class CoverageAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(data_dir=str(Path(self.temp.name) / "data"), artifact_s3_uri=None)
        with patch("arena.api.load_config", return_value=self.cfg):
            self.app = create_app()
        self.service = self.app.state.service
        self.client = self.enterContext(TestClient(self.app, base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer " + access_token(self.cfg.root)}))

    def test_new_comparison_defaults_and_explicit_choices_are_persisted(self):
        for selection, expected in (({}, "focused"), ({"test_coverage": "focused"}, "focused"),
                                    ({"test_coverage": "comprehensive"}, "comprehensive")):
            with self.subTest(selection=selection):
                response = self.client.post("/api/comparisons", json={"prompt": "Build a CLI", **selection})
                self.assertEqual(response.status_code, 201)
                created = response.json()
                saved = self.client.get("/api/comparisons/" + created["id"]).json()
                self.assertEqual(saved["test_coverage"], expected)
                self.assertEqual(saved["test_review"]["test_coverage"], expected)
        before = self.service.store.list("comparison")
        response = self.client.post("/api/comparisons", json={"prompt": "CLI", "test_coverage": "unknown"})
        self.assertEqual(response.status_code, 422)
        with self.assertRaisesRegex(ValueError, "coverage"):
            self.service.create("CLI", test_coverage="unknown")
        self.assertEqual(self.service.store.list("comparison"), before)

    def test_changing_coverage_requires_fresh_review_and_keeps_code_requirements(self):
        c = self.service.create("Build a CLI", test_coverage="comprehensive")
        review = c["test_review"]
        review.update(status="awaiting_review", proposal=proposal(12).model_dump())
        self.service.store.update("comparison", c["id"], test_review=review, status="planning")
        self.service.review_tests(c["id"], review["id"], answers={})
        approved = self.service.store.detail(c["id"])
        changed = self.client.post(f"/api/comparisons/{c['id']}/test-review/changes", json={
            "review_id": review["id"], "feedback": "Use a smaller representative suite.",
            "test_coverage": "focused"})
        self.assertEqual(changed.status_code, 200)
        current = changed.json()
        self.assertEqual(current["test_coverage"], "focused")
        self.assertEqual(current["test_review"]["test_coverage"], "focused")
        self.assertEqual(current["test_review"]["status"], "queued")
        self.assertNotEqual(current["test_review"]["id"], review["id"])
        self.assertIsNone(current["test_review"].get("approved_at"))
        self.assertIsNone(current["test_review"]["proposal"])
        self.assertEqual(current["spec_version"], approved["spec_version"])
        self.assertEqual(current["test_review"]["requirements_sha256"],
                         approved["test_review"]["requirements_sha256"])
        self.assertEqual(current["runs"], approved["runs"])
        self.assertEqual(current["test_review"]["history"][-1]["test_coverage"], "comprehensive")
        self.assertEqual(current["test_review"]["history"][-1]["approved_at"],
                         approved["test_review"]["approved_at"])
        stale = self.client.post(f"/api/comparisons/{c['id']}/test-review/approve", json={
            "review_id": review["id"], "answers": {}})
        self.assertEqual(stale.status_code, 400)
        with self.assertRaisesRegex(ValueError, "new proposal"):
            self.service.review_tests(c["id"], current["test_review"]["id"],
                                      answers={}, test_coverage="comprehensive")

    def test_legacy_reviews_and_requirement_revisions_keep_their_scope(self):
        c = self.service.create("Build a CLI", test_coverage="focused")
        self.service.revise(c["id"], "Support empty input.")
        self.assertEqual(self.service.store.get("comparison", c["id"])["test_review"]["test_coverage"], "focused")
        c.pop("test_coverage")
        c["test_review"].pop("test_coverage")
        self.assertEqual(new_review(c, c["test_review"])["test_coverage"], "comprehensive")

    def test_outdated_worker_cannot_silently_ignore_the_coverage_choice(self):
        worker = {"id": "worker", "heartbeat": time.time(), "pid": 123}
        self.service.store.put("system", worker)
        status = self.client.get("/api/system").json()
        self.assertFalse(status["test_coverage"]["worker_ready"])
        response = self.client.post("/api/comparisons", json={"prompt": "CLI", "test_coverage": "focused"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.service.store.list("comparison"), [])
        self.service.store.put("system", {**worker, "test_coverage_version": 1})
        response = self.client.post("/api/comparisons", json={"prompt": "CLI", "test_coverage": "focused"})
        self.assertEqual(response.status_code, 201)


class CoveragePlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_receives_choice_and_uses_bounded_small_proposal_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(data_dir=str(root / "data"), judge_model="test-only")
            for mode, count, expected_schema in (("focused", 6, FocusedTestProposal),
                                                 ("comprehensive", 14, TestProposal)):
                seen = []
                class Agent:
                    def __init__(self, **kwargs):
                        seen.append(kwargs["system_prompt"])
                    async def invoke_async(self, prompt, structured_output_model):
                        seen.append((json.loads(prompt), structured_output_model))
                        return SimpleNamespace(structured_output=proposal(count),
                            metrics=SimpleNamespace(get_summary=lambda: {"accumulated_usage": {}}))
                with patch("strands.Agent", Agent), patch("arena.evaluation.judge_model"):
                    proposed, metadata = await propose_tests({"prompt": "Build a CLI"}, root, cfg,
                                                               {"test_coverage": mode})
                self.assertEqual(seen[1][0]["test_coverage"]["mode"], mode)
                self.assertIs(seen[1][1], expected_schema)
                self.assertEqual(len(proposed["test_cases"]), count)
                self.assertEqual(metadata["test_coverage"], mode)
                self.assertEqual(FOCUSED_LIMITATION in proposed["limitations"], mode == "focused")
        with self.assertRaises(ValueError):
            FocusedTestProposal.model_validate(proposal(9).model_dump())
        too_many_questions = proposal().model_dump()
        too_many_questions["questions"] = [{"id": f"q-{i}", "question": "Which behavior?"} for i in range(4)]
        with self.assertRaises(ValueError):
            FocusedTestProposal.model_validate(too_many_questions)

    async def test_executable_generation_uses_approved_scope_over_old_task_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(data_dir=str(root / "data"), judge_model="test-only")
            seen = []
            class Agent:
                def __init__(self, **kwargs):
                    pass
                async def invoke_async(self, prompt, structured_output_model):
                    seen.append(json.loads(prompt))
                    return SimpleNamespace(structured_output=code(),
                        metrics=SimpleNamespace(get_summary=lambda: {"accumulated_usage": {}}))
            model = SimpleNamespace(update_config=lambda **kwargs: None)
            review = {"id": "approved-review", "test_coverage": "focused",
                      "proposal": proposal().model_dump(), "answers": {}}
            with patch("strands.Agent", Agent), patch("arena.evaluation.judge_model", return_value=model):
                plan, _ = await prepare_plan({"prompt": "Build a CLI", "test_coverage": "comprehensive",
                                              "approved_test_review": review}, root, cfg)
            self.assertEqual(seen[0]["test_coverage"]["mode"], "focused")
            self.assertEqual(len(plan.checks), 6)
            self.assertEqual(set(plan.review_coverage), {case["id"] for case in review["proposal"]["test_cases"]})
            self.assertIn(FOCUSED_LIMITATION, plan.limitations)

    async def test_explicit_supplied_checks_are_never_truncated_by_small_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(data_dir=str(root / "data"), artifact_s3_uri=None)
            supplied = Plan(criteria=[Criterion(id="all", requirement="Supplied contract",
                                                check_ids=[f"c-{i}" for i in range(12)])],
                checks=[Check(id=f"c-{i}", group="acceptance", command=f"python test_cli.py {i}") for i in range(12)])
            task = {"prompt": "Build a CLI", "test_coverage": "focused", "validation_plan": supplied.model_dump()}
            proposed, metadata = await propose_tests(task, root, cfg, {"test_coverage": "focused"})
            self.assertEqual(len(proposed["test_cases"]), 12)
            actual, _ = await prepare_plan({**task, "approved_test_review": {
                "id": "supplied-review", "test_coverage": "focused", "proposal": proposed,
                "answers": {}, "supplied_plan_sha256": metadata["plan_sha256"]}}, root, cfg)
            self.assertEqual(len(actual.checks), 12)


class CoverageCompilerTests(unittest.TestCase):
    def test_small_generation_cannot_expand_each_approved_scenario(self):
        review = {"id": "review", "test_coverage": "focused", "proposal": proposal().model_dump()}
        expanded = code()
        expanded.checks.append(ReviewedCheck(id="extra", scenario_id="case-0", command="python extra.py"))
        with self.assertRaisesRegex(ValueError, "exactly one executable check"):
            compile_reviewed_tests(expanded, review)
        for record in ({**review, "test_coverage": "comprehensive"},
                       {key: value for key, value in review.items() if key != "test_coverage"}):
            self.assertEqual(len(compile_reviewed_tests(expanded, record).checks), 7)
        missing = code()
        missing.checks.pop()
        with self.assertRaisesRegex(ValueError, "no executable checks"):
            compile_reviewed_tests(missing, review)

    def test_s3_recovery_preserves_choice_and_review_history_without_local_files(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(data_dir=str(Path(directory) / "original"),
                         artifact_s3_uri="s3://test-bucket/output/mchc/")
            service = Service(cfg)
            c = service.create("Build a CLI", test_coverage="comprehensive")
            prior = c["test_review"]
            prior.update(status="awaiting_review", proposal=proposal().model_dump())
            service.store.update("comparison", c["id"], test_review=prior)
            service.review_tests(c["id"], prior["id"], feedback="Use representative tests.",
                                 test_coverage="focused")
            expected = service.store.get("comparison", c["id"])["test_review"]
            s3 = FakeS3()
            storage = ArtifactStorage(cfg, service.store, s3)
            storage.save_checkpoint(c["id"])
            with storage.lock(c["id"]):
                storage.archive(c["id"])
            self.assertFalse(storage.root(c["id"]).exists())
            restored_cfg = cfg.model_copy(update={"data_dir": str(Path(directory) / "restored")})
            restored = ArtifactStorage(restored_cfg, Store(restored_cfg.root), s3)
            recovered = restored.import_comparison(c["id"])
            self.assertFalse(restored.root(c["id"]).exists())
            self.assertEqual(recovered["test_coverage"], "focused")
            self.assertEqual(recovered["test_review"], expected)
            self.assertEqual(recovered["test_review"]["history"][-1]["test_coverage"], "comprehensive")


if __name__ == "__main__":
    unittest.main(verbosity=2)
