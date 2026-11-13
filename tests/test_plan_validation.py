# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Local regressions for discovery validation and approved-draft recovery."""
import asyncio
import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena.config import Config
from arena.check_python import plan_python_dependencies
from arena.evaluation import Check, Criterion, Plan, plan_quality_issues, prepare_plan
from arena.judge_models import catalogue_fingerprint
from arena.test_review import (ReviewedTestCode, TestProposal, TestScenario,
                               digest, recover_reviewed_plan)
from arena.worker import Worker


def validation(command):
    return plan_quality_issues(Plan(
        criteria=[Criterion(id="behavior", requirement="Public result", check_ids=["run"])],
        checks=[Check(id="run", group="acceptance", command=command)],
    ))


class DiscoveryTests(unittest.TestCase):
    def test_file_lists_do_not_mask_the_outer_test_command(self):
        for command in (
            "python -m pytest $(find . -name 'test_*.py' | head -20)",
            'python -m pytest "$(find . -name test.py | grep -v cache | head -20)"',
            r"python -m pytest $(find . \( -name 'test_*.py' -o -name '*_test.py' \) | head -20)",
            "FILES=$(/usr/bin/find . -name test.py | /usr/bin/head -20); pytest $FILES",
            "python check.py $(find . -name test.py | tail -2)",
        ):
            with self.subTest(command=command):
                self.assertFalse(validation(command))

    def test_test_output_and_executable_substitutions_remain_rejected(self):
        for command in (
            "python -m pytest | head -20",
            "pytest $(find . -name test.py | head -20) | tail -10",
            "python check.py 2>&1 | tee output.log",
            "RESULT=$(pytest | head -20); echo \"$RESULT\"",
            "pytest $(find .; python failing.py | head -20)",
            r"pytest $(find . -exec python failing.py \; | head -20)",
            "pytest $(find . -delete | head -20)",
            "pytest $(find . | python failing.py | head -20)",
            "pytest $(find . | head -20 > files.txt)",
            "pytest $(find . -name $(python failing.py | head -20))",
            "pytest $(find . -name `python failing.py | head -20`)",
            "pytest $(find . | sort -o output | head -20)",
        ):
            with self.subTest(command=command):
                self.assertTrue(any("output-filter" in issue for issue in validation(command)))

    def test_existing_failure_and_sdk_guards_are_retained(self):
        self.assertTrue(validation("pytest || true"))
        self.assertTrue(validation("python cli.py || python -m compileall ."))
        self.assertFalse(validation("set -o pipefail; pytest | tee output.log"))
        plan = Plan(criteria=[Criterion(id="behavior", requirement="Result", check_ids=["run"])],
                    checks=[Check(id="run", group="acceptance", command="python check.py")],
                    acceptance_files={"check.py":
                        'from unittest.mock import patch\n'
                        'with patch("strands.models.BedrockModel"):\n    pass\n'})
        self.assertTrue(any("preserve real SDK" in issue for issue in plan_quality_issues(plan)))

    def test_shell_preserves_failure_with_file_discovery(self):
        # A local stub substitutes only for the test process; it exits nonzero.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_one.py").write_text("")
            command = ("python() { return 37; }; "
                       "python -m pytest $(find . -name 'test_*.py' | head -20); "
                       "RC=$?; COUNT=$(find . -name 'test_*.py' | wc -l); "
                       'echo "$COUNT"; exit $RC')
            self.assertFalse(validation(command))
            result = subprocess.run(["/bin/sh", "-c", command], cwd=root, capture_output=True)
            self.assertEqual(result.returncode, 37)
            self.assertEqual(result.stdout.strip(), b"1")


class DependencyTests(unittest.TestCase):
    def dependencies(self, command):
        return plan_python_dependencies([Check(id="install", group="project", command=command)], {})

    def test_redirections_are_not_package_names(self):
        for suffix in ("2>&1", ">install.log 2>&1", "1>/dev/null 2>>errors",
                       "&>output", "<input >output", "2>&1; echo 1"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.dependencies("pip install boto3 requests " + suffix)["project"],
                                 ["boto3", "requests"])

    def test_redirections_between_packages_keep_real_dependencies(self):
        self.assertEqual(self.dependencies("python -m pip install boto3 >output requests 2>&1")["project"],
                         ["boto3", "requests"])
        self.assertEqual(self.dependencies('pip install "boto3>=1" 2>errors')["project"], ["boto3>=1"])

    def test_runner_detection_after_redirection_is_preserved(self):
        dependencies = self.dependencies("pip install boto3 2>&1 && python -m pytest -q")
        self.assertEqual(dependencies["project"], ["boto3"])
        self.assertEqual(dependencies["tools"], ["pytest==8.4.1"])


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(data_dir=str(self.root), judge_model="must-not-be-contacted")
        proposal = TestProposal(summary="Reviewed behavior", test_cases=[
            TestScenario(id="behavior", title="Public result", steps=["Run it"],
                         expected_result="Expected output", test_data="Local fixture")
        ]).model_dump()
        self.review = {"id": "review", "revision": 1, "spec_version": 1,
                       "requirements_sha256": "unchanged", "proposal": proposal,
                       "answers": {}, "approved_at": 10}
        self.review["approval_sha256"] = digest({"proposal": proposal, "answers": {}})
        self.path = self.root / "comparisons/comparison/test-reviews/review/generation/attempt-3.json"
        self.path.parent.mkdir(parents=True)
        self.attempt = {"artifact": str(self.path), "outcome": "invalid_tests",
                        "started_at": 11, "ended_at": 12, "issues": ["Old validator rejection"]}
        self.saved = {"review_id": "review", "attempt": self.attempt,
                      "approval_sha256": self.review["approval_sha256"], "model": "recorded-planner",
                      "generated": ReviewedTestCode(checks=[{
                          "id": "check", "scenario_id": "behavior",
                          "command": "pytest $(find . -name 'test_*.py' | head -20)",
                      }]).model_dump()}
        self.path.write_text(json.dumps(self.saved))
        self.task = {"id": "comparison", "prompt": "Public behavior", "approved_test_review": self.review,
                     "planning_retry": {"requested_at": 13, "attempts": [self.attempt]}}

    async def test_explicit_retry_reuses_valid_draft_without_provider_call(self):
        with patch("arena.evaluation.judge_model", side_effect=AssertionError("New provider call")):
            plan, metadata = await prepare_plan(self.task, self.root / "baseline", self.cfg)
        self.assertEqual(plan.review_id, "review")
        self.assertEqual(metadata["model"], "recorded-planner")
        self.assertFalse(metadata["recovered_draft"]["new_model_call"])
        self.assertEqual(json.loads(self.path.read_text()), self.saved)

    async def test_worker_forwards_only_the_approved_retry_context(self):
        worker = Worker(self.cfg)
        # Seed a matching judge catalogue so judge_selection validates "must-not-be-contacted".
        fp = catalogue_fingerprint(self.cfg)
        worker.store.put("system", {"id": "judge-model-catalogue", "fingerprint": fp, "checked_at": 1.0,
                                    "models": [{"id": "must-not-be-contacted", "selectable": True,
                                                "streaming": True}]})
        comparison = worker.service.create("Public behavior")
        from arena.test_review import requirements_digest
        self.review["requirements_sha256"] = requirements_digest(comparison)
        # plan_sha256 must be set so approved_plan() uses the legacy (prepare_plan) path.
        live_review = {**self.review, "status": "approved", "plan_sha256": "legacy-forced",
                       "generation_retry_requested_at": 13, "generation_attempts": [self.attempt]}
        worker.store.update("comparison", comparison["id"], test_review=live_review)
        async def prepare(task, *_):
            self.assertEqual(task["planning_retry"]["attempts"], [self.attempt])
            self.assertEqual(task["planning_retry"]["requested_at"], 13)
            self.assertEqual(task["approved_test_review"]["approval_sha256"], self.review["approval_sha256"])
            return recover_reviewed_plan(self.task, self.cfg)
        with patch("arena.worker.prepare_plan", side_effect=prepare):
            result = await asyncio.wait_for(
                worker.approved_plan(comparison["id"], self.root / "baseline", comparison, lambda: False), 3)
        # approved_plan() now returns a dict; callers check result["_new_flow"].
        self.assertFalse(result["_new_flow"])
        plan, metadata, approval = result["plan"], result["metadata"], result["review"]
        self.assertEqual(approval["approved_at"], 10)
        self.assertEqual(plan.review_id, "review")
        self.assertFalse(metadata["recovered_draft"]["new_model_call"])

    def test_retry_is_required_and_changed_approval_is_not_reused(self):
        task = copy.deepcopy(self.task)
        task.pop("planning_retry")
        self.assertIsNone(recover_reviewed_plan(task, self.cfg))
        for field, value in (("approved_at", 14), ("approval_sha256", "different"),
                             ("id", "different-review")):
            task = copy.deepcopy(self.task)
            task["approved_test_review"][field] = value
            self.assertIsNone(recover_reviewed_plan(task, self.cfg))

    def test_other_paths_and_mismatched_artifacts_are_not_reused(self):
        task = copy.deepcopy(self.task)
        task["planning_retry"]["attempts"][-1]["artifact"] = str(self.root / "elsewhere.json")
        self.assertIsNone(recover_reviewed_plan(task, self.cfg))
        for key, value in (("review_id", "other-review"), ("approval_sha256", "changed"),
                           ("attempt", {**self.attempt, "started_at": 9})):
            self.path.write_text(json.dumps({**self.saved, key: value}))
            self.assertIsNone(recover_reviewed_plan(self.task, self.cfg))

    def test_still_invalid_or_missing_drafts_are_not_reused(self):
        for command in ("pytest | head -20", "pytest || true"):
            saved = copy.deepcopy(self.saved)
            saved["generated"]["checks"][0]["command"] = command
            self.path.write_text(json.dumps(saved))
            self.assertIsNone(recover_reviewed_plan(self.task, self.cfg))
        self.path.unlink()
        self.assertIsNone(recover_reviewed_plan(self.task, self.cfg))

    def test_legacy_draft_is_bound_to_original_approval_time(self):
        self.saved.pop("approval_sha256")
        self.saved.pop("model")
        self.path.write_text(json.dumps(self.saved))
        self.assertIsNotNone(recover_reviewed_plan(self.task, self.cfg))
        self.review["approved_at"] = 14
        self.assertIsNone(recover_reviewed_plan(self.task, self.cfg))

    def test_changed_planner_model_or_effort_requires_new_generation(self):
        cfg = self.cfg.model_copy(update={
            "planning_model": "us.anthropic.claude-opus-5", "planning_effort": "max"})
        original = self.path.read_bytes()
        self.assertIsNone(recover_reviewed_plan(self.task, cfg))
        self.assertEqual(self.path.read_bytes(), original)
        self.saved.update(model=cfg.planning_model, effort="high")
        self.path.write_text(json.dumps(self.saved))
        self.assertIsNone(recover_reviewed_plan(self.task, cfg))
        self.saved["effort"] = "max"
        self.path.write_text(json.dumps(self.saved))
        _, metadata = recover_reviewed_plan(self.task, cfg)
        self.assertEqual(metadata["model"], cfg.planning_model)
        self.assertEqual(metadata["effort"], "max")
        self.assertEqual(self.task["approved_test_review"], self.review)


if __name__ == "__main__":
    unittest.main(verbosity=2)
