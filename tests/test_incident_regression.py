# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A comparison completes even when every failure from the stalled run recurs.

The stalled comparison died three ways at once: a harness finished its
clarification turn without calling the ready tool, the planner submitted filler
for the fields its schema required, and the test generator tagged its checks with
scenario IDs that were never approved. This drives all three through the real
review, generation and scoring path with only the model and the harness
subprocesses replaced.
"""
import asyncio
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arena.config import Config
from arena.judge_models import catalogue_fingerprint
from arena.test_review import (FocusedTestProposal, ReviewedCheck, ReviewedTestCode,
                               ReviewQuestion, TestScenario)
from arena.worker import Worker

OPUS = "us.anthropic.claude-opus-5"
ACCEPTANCE = """import json


def test_current_conditions():
    assert json.loads('{"temperature": 54}')["temperature"] == 54


def test_weekly_forecast():
    assert len([1, 2, 3, 4, 5, 6, 7]) == 7
"""


def filler_proposal():
    """What the planner actually submitted: real questions, empty scenarios."""
    return FocusedTestProposal(summary="placeholder",
        test_cases=[TestScenario(id="placeholder", title="placeholder", steps=["placeholder"],
                                 expected_result="placeholder", test_data="placeholder")],
        questions=[ReviewQuestion(id="Q1-MODEL-ID", question="Which Bedrock model id?",
                                  suggested_answer="A Sonnet id from the catalogue")],
        assumptions=["ACC-SDK-WIRING proves the real BedrockModel constructor runs."])


def real_proposal():
    return FocusedTestProposal(summary="Current conditions and the weekly forecast",
        test_cases=[
            TestScenario(id="ACC-CURRENT", title="Current conditions for a city name",
                steps=["Run the CLI with 'weather in Boston right now'."],
                expected_result="Temperature, humidity and wind are reported for Boston.",
                test_data="Controlled geocoding and forecast fixtures"),
            TestScenario(id="ACC-FORECAST", title="Seven day forecast for a zipcode",
                steps=["Run the CLI with 'week forecast for 01746'."],
                expected_result="Seven dated rows with high, low and precipitation chance.",
                test_data="Controlled zipcode fixture")],
        questions=[ReviewQuestion(id="Q1-MODEL-ID", question="Which Bedrock model id?",
                                  suggested_answer="A Sonnet id from the catalogue")])


def invented_scenario_code():
    """The generator's mistake: a scenario ID that was never approved."""
    return ReviewedTestCode(checks=[ReviewedCheck(
        id="acc-sdk-wiring", scenario_id="ACC-SDK-WIRING",
        command="python -m pytest /acceptance/test_weather.py")],
        acceptance_files={"test_weather.py": ACCEPTANCE})


def bound_code():
    return ReviewedTestCode(
        checks=[ReviewedCheck(id="acc-current", scenario_id="ACC-CURRENT",
                    command="python -m pytest /acceptance/test_weather.py::test_current_conditions"),
                ReviewedCheck(id="acc-forecast", scenario_id="ACC-FORECAST",
                    command="python -m pytest /acceptance/test_weather.py::test_weekly_forecast")],
        acceptance_files={"test_weather.py": ACCEPTANCE},
        limitations=["Live Bedrock inference and live Open-Meteo calls are out of scope."])


class IncidentRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w = Worker(Config(data_dir=str(Path(self.temp.name) / "data"), planning_model=OPUS,
                               artifact_s3_uri=None))
        self.s = self.w.store
        fp = catalogue_fingerprint(self.w.config)
        self.s.put("system", {"id": "judge-model-catalogue", "fingerprint": fp, "checked_at": 1.0,
                               "models": [{"id": OPUS, "selectable": True, "streaming": True}]})
        self.c = self.w.service.create("Build a weather agent on the Strands SDK.")
        self.id = self.c["id"]
        self.runs = {r["harness"]: r["id"] for r in self.c["runs"]}
        self.calls = Counter()
        self.prompts = []
        self.omit_ready = {"kiro"}
        self.task = None

        # The scripted planner reproduces both model mistakes in order.
        replies = [filler_proposal(), real_proposal(), invented_scenario_code(), bound_code()]
        outer = self

        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            async def invoke_async(self, prompt, structured_output_model):
                outer.prompts.append(prompt)
                reply = replies[min(len(outer.prompts), len(replies)) - 1]
                outer.calls["model"] += 1
                # Each phase builds its own agent, so its metrics accumulate
                # only across that phase's own attempts.
                outer.calls[structured_output_model.__name__] += 1
                spent = 100 * outer.calls[structured_output_model.__name__]
                return SimpleNamespace(structured_output=reply, metrics=SimpleNamespace(
                    get_summary=lambda: {"accumulated_usage": {"outputTokens": spent}}))

        async def phase(run_id, phase_name, spec, plan, readiness_reminder=False):
            harness = self.s.get("run", run_id)["harness"]
            self.calls[(harness, phase_name)] += 1
            self.s.transition(run_id, "clarifying" if phase_name == "clarify" else "developing",
                              spec_version=spec["spec_version"])
            if phase_name == "clarify":
                if harness in self.omit_ready and not readiness_reminder:
                    return "readiness_unverified"
                self.s.transition(run_id, "ready", clarified=True)
                return "ready"
            workspace = self.w.run_root(self.s.get("run", run_id)) / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "weather_agent.py").write_text("print('weather')\n")
            self.s.transition(run_id, "implemented")
            return "implemented"

        async def checks(snapshot, plan, path, *args, **kwargs):
            self.calls["checks"] += 1
            return {"checks": [{"id": check.id, "group": check.group, "status": "passed",
                                "exit_code": 0, "log": "2 passed", "counts": {}}
                               for check in plan.checks], "environment": {}}

        async def score(*args):
            self.calls["score"] += 1
            return {"overall": 90, "verified_complete": True}

        self.w.phase = phase
        self.w.score_candidate = score
        for target, func in [("arena.worker.CandidateTestRunner.run", checks)]:
            p = patch(target, side_effect=func)
            p.start()
            self.addCleanup(p.stop)
        for target, value in [("strands.Agent", FakeAgent),
                              ("arena.evaluation.judge_model",
                               SimpleNamespace(update_config=lambda **kwargs: None))]:
            p = (patch(target, value) if target == "strands.Agent"
                 else patch(target, return_value=value))
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def review(self):
        return self.s.get("comparison", self.id)["test_review"]

    async def wait(self, predicate):
        for _ in range(300):
            if predicate():
                return
            if self.task and self.task.done():
                self.task.result()
            await asyncio.sleep(.05)
        self.fail("Timed out: " + json.dumps(self.s.detail(self.id), default=str)[:900])

    async def test_all_three_incident_failures_recur_and_the_comparison_completes(self):
        self.task = asyncio.create_task(self.w.compare(self.id))
        await self.wait(lambda: self.review()["status"] == "awaiting_review")

        # The filler proposal was never stored; the planner was asked again.
        proposal = self.review()["proposal"]
        self.assertEqual([case["id"] for case in proposal["test_cases"]],
                         ["ACC-CURRENT", "ACC-FORECAST"])
        self.assertNotEqual(proposal["summary"], "placeholder")
        self.assertEqual([a["outcome"] for a in self.review()["planning"]["attempts"]],
                         ["filler_content", "complete"])
        self.assertIn("filler instead of real content", self.prompts[1])

        # Every harness reached the review gate, including the one that forgot
        # to call ready and needed a second clarification turn.
        await self.wait(lambda: all(r["status"] == "waiting_test_review"
                                    for r in self.s.detail(self.id)["runs"]))
        self.assertEqual(self.calls[("kiro", "clarify")], 2)
        self.assertEqual(self.calls[("claude", "clarify")], 1)
        self.assertEqual(self.calls[("codex", "clarify")], 1)
        self.assertTrue(self.s.get("run", self.runs["kiro"])["clarified"])

        self.w.service.review_tests(self.id, self.review()["id"],
                                    answers={"Q1-MODEL-ID": "us.anthropic.claude-sonnet-4-5"})
        await asyncio.wait_for(self.task, 30)

        # Generation recovered from the invented scenario ID instead of
        # exhausting its attempts, and the retry named the legal IDs.
        attempts = self.review()["generation"]["attempts"]
        self.assertEqual([a["outcome"] for a in attempts], ["invalid_tests", "complete"])
        self.assertIn("unknown approved scenario ACC-SDK-WIRING", attempts[0]["issues"][0])
        self.assertIn("The only approved scenario IDs are: ACC-CURRENT, ACC-FORECAST",
                      self.prompts[3])
        self.assertEqual([a["usage"]["outputTokens"] for a in attempts], [100, 100])

        comparison = self.s.get("comparison", self.id)
        self.assertEqual(comparison["status"], "complete")
        self.assertEqual(self.review()["status"], "approved")
        self.assertTrue(self.review()["plan_sha256"])
        self.assertEqual(comparison["plan"]["review_id"], self.review()["id"])
        self.assertEqual(sorted(comparison["plan"]["review_coverage"]),
                         ["ACC-CURRENT", "ACC-FORECAST"])
        for harness, run_id in self.runs.items():
            run = self.s.get("run", run_id)
            with self.subTest(harness=harness):
                self.assertEqual(run["status"], "verified_complete")
                self.assertEqual(run["evaluation"]["overall"], 90)
        self.assertEqual(self.calls["score"], 3)

    async def test_persistent_filler_fails_loudly_and_feedback_still_recovers(self):
        # The worst case: the planner never produces real scenarios. The review
        # must fail visibly instead of storing filler, and asking for changes
        # must still get the comparison to a complete evaluation.
        replies = [filler_proposal(), filler_proposal(), real_proposal(), bound_code()]
        outer = self

        class StubbornAgent:
            def __init__(self, **kwargs):
                pass

            async def invoke_async(self, prompt, structured_output_model):
                outer.prompts.append(prompt)
                outer.calls["model"] += 1
                return SimpleNamespace(
                    structured_output=replies[min(len(outer.prompts), len(replies)) - 1],
                    metrics=SimpleNamespace(get_summary=lambda: {"accumulated_usage": {}}))

        self.omit_ready = set()
        with patch("strands.Agent", StubbornAgent):
            self.task = asyncio.create_task(self.w.compare(self.id))
            await self.wait(lambda: self.review()["status"] == "proposal_failed")
            self.assertIsNone(self.review()["proposal"])
            self.assertIn("filler instead of test scenarios", self.review()["error"])
            # Proposing runs alongside clarification, so the comparison keeps
            # its own progress; a failed proposal must not cancel or end it.
            self.assertFalse(self.s.get("comparison", self.id)["cancel_requested"])
            self.assertNotIn(self.s.get("comparison", self.id)["status"],
                             ("complete", "incomplete", "interrupted"))

            # Approval is impossible while there is nothing to review.
            with self.assertRaises(ValueError):
                self.w.service.review_tests(self.id, self.review()["id"], answers={})

            self.w.service.review_tests(self.id, self.review()["id"],
                                        feedback="Write concrete weather scenarios.")
            await self.wait(lambda: self.review()["status"] == "awaiting_review")
            self.assertEqual(self.review()["revision"], 2)
            self.assertEqual([case["id"] for case in self.review()["proposal"]["test_cases"]],
                             ["ACC-CURRENT", "ACC-FORECAST"])
            self.w.service.review_tests(self.id, self.review()["id"],
                                        answers={"Q1-MODEL-ID": "us.anthropic.claude-sonnet-4-5"})
            await asyncio.wait_for(self.task, 30)
        self.assertEqual(self.s.get("comparison", self.id)["status"], "complete")
        for run_id in self.runs.values():
            self.assertEqual(self.s.get("run", run_id)["status"], "verified_complete")

    async def test_a_clean_run_needs_no_retries_at_all(self):
        # The same wiring with no injected mistakes must not spend extra turns.
        self.omit_ready = set()
        replies = [real_proposal(), bound_code()]
        outer = self

        class CleanAgent:
            def __init__(self, **kwargs):
                pass

            async def invoke_async(self, prompt, structured_output_model):
                outer.prompts.append(prompt)
                outer.calls["model"] += 1
                return SimpleNamespace(
                    structured_output=replies[min(len(outer.prompts), len(replies)) - 1],
                    metrics=SimpleNamespace(get_summary=lambda: {"accumulated_usage": {}}))

        with patch("strands.Agent", CleanAgent):
            self.task = asyncio.create_task(self.w.compare(self.id))
            await self.wait(lambda: self.review()["status"] == "awaiting_review")
            self.w.service.review_tests(self.id, self.review()["id"],
                                        answers={"Q1-MODEL-ID": "us.anthropic.claude-sonnet-4-5"})
            await asyncio.wait_for(self.task, 30)
        self.assertEqual([a["outcome"] for a in self.review()["planning"]["attempts"]], ["complete"])
        self.assertEqual([a["outcome"] for a in self.review()["generation"]["attempts"]],
                         ["complete"])
        self.assertEqual(self.calls["model"], 2)
        for harness in ("claude", "codex", "kiro"):
            self.assertEqual(self.calls[(harness, "clarify")], 1)
        self.assertEqual(self.s.get("comparison", self.id)["status"], "complete")


if __name__ == "__main__":
    unittest.main()
