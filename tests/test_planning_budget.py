# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance planning is bounded by output tokens and attempts, never by a clock.

Every harness in the last run reached `waiting_test_review` and stayed there:
approved test development spent 1,188 seconds on an attempt that hit the
100,000-token output ceiling, started the one mandated continuation with 612 of
1,800 seconds left, ran out of time, and discarded the finished proposal and its
human approval while reporting only "Acceptance planning exceeded 1800 seconds".
The shared deadline is gone; these tests hold it gone. No model calls here.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arena.config import Config
from arena.evaluation import prepare_plan
from arena.test_review import (FocusedTestProposal, ReviewedCheck, ReviewedTestCode,
                               ReviewQuestion, TestScenario, propose_tests)

OPUS = "us.anthropic.claude-opus-5"
# Longer than the deadline that used to discard this work, so any reintroduced
# wall-clock budget below roughly an hour fails these tests.
CEILING_SECONDS = 1188
ACCEPTANCE = """import json


def test_current_conditions():
    assert json.loads('{"temperature": 54}')["temperature"] == 54


def test_weekly_forecast():
    assert len([1, 2, 3, 4, 5, 6, 7]) == 7
"""


def proposal():
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


def approved_review():
    return {"id": "review", "test_coverage": "focused", "answers": {},
            "proposal": proposal().model_dump()}


def bound_code():
    return ReviewedTestCode(
        checks=[ReviewedCheck(id="acc-current", scenario_id="ACC-CURRENT",
                    command="python -m pytest /acceptance/test_weather.py::test_current_conditions"),
                ReviewedCheck(id="acc-forecast", scenario_id="ACC-FORECAST",
                    command="python -m pytest /acceptance/test_weather.py::test_weekly_forecast")],
        acceptance_files={"test_weather.py": ACCEPTANCE},
        limitations=["Live Bedrock inference and live Open-Meteo calls are out of scope."])


class FakeClock:
    """A clock the planner's own turns advance, so elapsed time is exact."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


def planner(clock, turns):
    """Each turn is (seconds the turn costs, structured result or exception)."""
    calls = []

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        async def invoke_async(self, prompt, **kwargs):
            seconds, outcome = turns[len(calls)]
            calls.append(prompt)
            clock.now += seconds
            if isinstance(outcome, BaseException):
                raise outcome
            return SimpleNamespace(structured_output=outcome, metrics=SimpleNamespace(
                get_summary=lambda: {"accumulated_usage": {"outputTokens": 90_000}}))

    return FakeAgent, calls


class UnboundedPlanningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(data_dir=str(self.root / "data"), planning_model=OPUS,
                             planning_effort="max", artifact_s3_uri=None)

    def test_configuration_has_no_planning_time_budget(self):
        self.assertNotIn("planning_seconds", Config.model_fields)
        # Config ignores unknown keys, so a stale planning_seconds in an existing
        # config.toml loads and has no effect rather than reinstating a deadline.
        stale = Config(data_dir=str(self.root / "data"), planning_seconds=1800)
        self.assertFalse(hasattr(stale, "planning_seconds"))

    async def test_a_ceiling_length_attempt_still_gets_its_continuation(self):
        from strands.types.exceptions import MaxTokensReachedException
        clock = FakeClock()
        agent, calls = planner(clock, [(CEILING_SECONDS, MaxTokensReachedException("output limit")),
                                       (CEILING_SECONDS, bound_code())])
        task = {"id": "comparison", "prompt": "Build a weather agent",
                "approved_test_review": approved_review()}
        with patch("arena.evaluation.time", clock), patch("strands.Agent", agent), \
                patch("arena.evaluation.judge_model",
                      return_value=SimpleNamespace(update_config=lambda **kwargs: None)):
            plan, metadata = await prepare_plan(task, self.root / "baseline", self.config)
        self.assertEqual(len(calls), 2)
        self.assertIn("hit the output limit", calls[1])
        self.assertEqual(sorted(plan.review_coverage), ["ACC-CURRENT", "ACC-FORECAST"])
        self.assertEqual([a["outcome"] for a in metadata["attempts"]],
                         ["output_limit", "complete"])
        # The two attempts together outlast the deadline that used to discard them.
        self.assertGreater(clock.now - 1_000_000.0, 1800)

    async def test_a_slow_proposal_and_its_correction_both_complete(self):
        from strands.types.exceptions import MaxTokensReachedException
        clock = FakeClock()
        agent, calls = planner(clock, [(CEILING_SECONDS, MaxTokensReachedException("output limit")),
                                       (CEILING_SECONDS, proposal())])
        with patch("strands.Agent", agent), patch("arena.evaluation.judge_model",
                return_value=SimpleNamespace(update_config=lambda **kwargs: None)):
            result, metadata = await propose_tests(
                {"prompt": "Build a weather agent"}, self.root / "baseline", self.config,
                {"test_coverage": "focused"})
        self.assertEqual(len(calls), 2)
        self.assertEqual([a["outcome"] for a in metadata["attempts"]],
                         ["output_limit", "complete"])
        self.assertEqual([case["id"] for case in result["test_cases"]],
                         ["ACC-CURRENT", "ACC-FORECAST"])

    def test_the_planner_transport_still_guards_against_a_dead_stream(self):
        from arena.evaluation import PLANNING_STALL_SECONDS, judge_model
        captured = {}

        def record(**kwargs):
            captured.update(kwargs)
            return object()

        with patch("arena.planning_model.ThinkingPlanningModel", record):
            judge_model(self.config, max_tokens=100000)
        boto = captured["boto_client_config"]
        self.assertEqual(boto.read_timeout, PLANNING_STALL_SECONDS)
        self.assertEqual(boto.connect_timeout, 10)
        # Thinking streams as summaries, so the guard measures a dead stream rather than
        # the length of a silent reasoning phase; it still allows 15 minutes of silence.
        self.assertEqual(captured["additional_request_fields"]["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertGreaterEqual(PLANNING_STALL_SECONDS, 900)


if __name__ == "__main__":
    unittest.main()
