# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Focused local regressions; no provider calls or real comparison changes."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from arena.config import Config
from arena.evaluation import (assess_accuracy, assess_tests, plan_quality_issues,
                              prepare_plan, Plan, Check, Criterion)
from arena.test_review import (ReviewedTestCode, ReviewedCheck, TestProposal, TestScenario,
                               compile_reviewed_tests, review_alignment_issues)


def proposal():
    return TestProposal(summary="Approved behavior", test_cases=[
        TestScenario(id=group, group=group, title=group + " scenario", steps=["Check the public behavior"],
                     expected_result="The required result is produced", test_data="Controlled fixture")
        for group in ("acceptance", "regression", "candidate", "project")
    ])


def review():
    return {"id": "review-123", "proposal": proposal().model_dump(), "answers": {}}


def code():
    return ReviewedTestCode(checks=[
        ReviewedCheck(id=group + "-check", scenario_id=group, command="python check.py")
        for group in ("acceptance", "regression", "candidate", "project")
    ])


class CompilerTests(unittest.TestCase):
    def test_groups_and_accuracy_links_come_from_approved_scenarios(self):
        plan = compile_reviewed_tests(code(), review())
        self.assertEqual({c.id: c.group for c in plan.checks},
                         {group + "-check": group for group in
                          ("acceptance", "regression", "candidate", "project")})
        self.assertEqual([c.id for c in plan.criteria], ["acceptance"])
        self.assertEqual(plan.criteria[0].check_ids, ["acceptance-check"])
        self.assertEqual(plan.checks[0].group, "project")
        self.assertFalse(review_alignment_issues(plan, review()))
        self.assertFalse(plan_quality_issues(plan))

    def test_generator_cannot_reassign_groups_or_supply_scoring_criteria(self):
        item = code().model_dump()
        item["criteria"] = [{"id": "wrong", "check_ids": ["project-check"]}]
        with self.assertRaises(ValueError):
            ReviewedTestCode.model_validate(item)
        item = code().model_dump()
        item["checks"][0]["group"] = "project"
        with self.assertRaises(ValueError):
            ReviewedTestCode.model_validate(item)

    def test_missing_extra_and_duplicate_checks_are_rejected(self):
        item = code()
        item.checks.pop()
        with self.assertRaisesRegex(ValueError, "no executable checks"):
            compile_reviewed_tests(item, review())
        item = code()
        item.checks[0].scenario_id = "unapproved"
        with self.assertRaisesRegex(ValueError, "unknown approved scenario"):
            compile_reviewed_tests(item, review())
        item = code()
        item.checks.append(item.checks[0])
        with self.assertRaisesRegex(ValueError, "unique IDs"):
            compile_reviewed_tests(item, review())

    def test_false_pass_and_sdk_bypass_guards_remain_effective(self):
        item = code()
        item.checks[0].command = "python check.py || true"
        item.acceptance_files = {"test_checks.py":
            'from unittest.mock import patch\nwith patch("strands.models.BedrockModel"):\n    pass\n'}
        issues = plan_quality_issues(compile_reviewed_tests(item, review()))
        self.assertTrue(any("hides failures" in i for i in issues))
        self.assertTrue(any("preserve real SDK" in i for i in issues))

    def test_setup_failure_is_not_an_accuracy_pass_and_supplements_earn_no_points(self):
        plan = compile_reviewed_tests(code(), review()).model_dump()
        evidence = {"checks": [{**c, "status": "passed", "exit_code": 0, "counts": {}}
                                for c in plan["checks"]]}
        candidate = next(c for c in evidence["checks"] if c["group"] == "candidate")
        candidate.update(status="not_applicable")
        self.assertFalse(candidate["scored"])
        project = next(c for c in evidence["checks"] if c["group"] == "project")
        project.update(status="failed", exit_code=1)
        accuracy = assess_accuracy(plan, evidence, {"files": []})
        tests = assess_tests(plan, evidence)
        self.assertTrue(accuracy["complete"])
        self.assertEqual(accuracy["score"], 100)
        self.assertTrue(tests["mandatory_failure"])
        self.assertIsNone(tests["groups"]["candidate"])
        next(c for c in evidence["checks"] if c["group"] == "acceptance").update(
            status="failed", exit_code=1)
        self.assertEqual(assess_accuracy(plan, evidence, {"files": []})["score"], 0)


class PlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_compiles_real_schema_and_saves_failed_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            baseline.mkdir()
            cfg = Config(data_dir=str(root / "data"), judge_model="test-only")
            seen = []
            partial = code()
            partial.checks.pop()
            outputs = [partial, code()]
            class Agent:
                def __init__(self, **kwargs):
                    pass
                async def invoke_async(self, prompt, structured_output_model):
                    seen.append((prompt, structured_output_model))
                    return SimpleNamespace(structured_output=outputs.pop(0),
                        metrics=SimpleNamespace(get_summary=lambda: {"accumulated_usage": {}}))
            model = SimpleNamespace(update_config=lambda **kwargs: None)
            with patch("strands.Agent", Agent), patch("arena.evaluation.judge_model", return_value=model):
                plan, metadata = await prepare_plan({
                    "prompt": "A reviewed application", "approved_test_review": review(),
                    "planning_artifact_dir": str(root / "generation"),
                }, baseline, cfg)
            self.assertEqual([kind for _, kind in seen], [ReviewedTestCode, ReviewedTestCode])
            self.assertEqual(len(metadata["attempts"]), 2)
            self.assertEqual(metadata["attempts"][0]["outcome"], "invalid_tests")
            self.assertEqual(plan.review_id, review()["id"])
            first = json.loads((root / "generation/attempt-1.json").read_text())
            self.assertEqual(first["generated"], partial.model_dump())
            self.assertTrue(first["attempt"]["issues"])
            self.assertFalse(plan_quality_issues(plan))

    async def test_legacy_plans_keep_their_schema_and_scoring(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(data_dir=str(root / "data"), judge_model="test-only")
            original = Plan(criteria=[Criterion(id="task", requirement="Public behavior", weight=3,
                                              check_ids=["cli"])],
                            checks=[Check(id="cli", group="acceptance", command="python cli_test.py")],
                            excluded_groups={"regression": "None", "candidate": "None", "project": "None"})
            seen = []
            class Agent:
                def __init__(self, **kwargs):
                    pass
                async def invoke_async(self, prompt, structured_output_model):
                    seen.append(structured_output_model)
                    return SimpleNamespace(structured_output=original,
                        metrics=SimpleNamespace(get_summary=lambda: {"accumulated_usage": {}}))
            model = SimpleNamespace(update_config=lambda **kwargs: None)
            with patch("strands.Agent", Agent), patch("arena.evaluation.judge_model", return_value=model):
                actual, _ = await prepare_plan({"prompt": "Legacy task"}, root, cfg)
            self.assertEqual(seen, [Plan])
            self.assertEqual(actual.criteria[0].weight, 3)
            self.assertIsNone(actual.review_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
