# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A schema-valid proposal can still describe nothing; no model calls here."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arena.config import Config
from arena.test_review import (FocusedTestProposal, ReviewQuestion, TestProposal, TestScenario,
                               compile_reviewed_tests, new_review, propose_tests,
                               proposal_content_issues, requirements_digest)
from arena.worker import Worker

OPUS = "us.anthropic.claude-opus-5"


def real_proposal(**overrides):
    return FocusedTestProposal(summary="Current weather and forecast behavior of the CLI",
        test_cases=[TestScenario(id="ACC-CURRENT", title="Current conditions for a city name",
            steps=["Run the CLI with 'weather in Boston right now'."],
            expected_result="Temperature, humidity and wind appear for Boston.",
            test_data="Controlled geocoding and forecast fixtures"),
            TestScenario(id="ACC-FORECAST", title="Seven day forecast for a zipcode",
            steps=["Run the CLI with 'week forecast for 01746'."],
            expected_result="Seven dated rows with high, low and precipitation chance.",
            test_data="Controlled zipcode fixture")],
        questions=[ReviewQuestion(id="Q1", question="Which model id?",
                                  suggested_answer="A Sonnet id from the catalogue")], **overrides)


def filler_proposal():
    """The exact shape the planner produced: real questions, empty scenarios."""
    return FocusedTestProposal(summary="placeholder",
        test_cases=[TestScenario(id="placeholder", title="placeholder", steps=["placeholder"],
                                 expected_result="placeholder", test_data="placeholder")],
        questions=[ReviewQuestion(id="Q1", question="Which model id?",
                                  suggested_answer="ACC-SDK-WIRING proves construction")],
        assumptions=["ACC-SDK-WIRING covers the real BedrockModel constructor."])


class ContentIssueTests(unittest.TestCase):
    def test_real_proposal_is_accepted(self):
        self.assertEqual(proposal_content_issues(real_proposal()), [])

    def test_filler_scenario_and_summary_are_reported(self):
        issues = proposal_content_issues(filler_proposal())
        self.assertTrue(any("summary" in issue for issue in issues))
        self.assertTrue(any("placeholder: id" in issue for issue in issues))
        self.assertTrue(any("steps" in issue for issue in issues))

    def test_prose_mentioning_placeholders_is_not_filler(self):
        proposal = real_proposal()
        proposal.test_cases[0].title = "Placeholder rows are rejected by the parser"
        proposal.summary = "Covers TODO markers and n/a fields in the response"
        self.assertEqual(proposal_content_issues(proposal), [])

    def test_scenario_repeating_one_string_states_no_behavior(self):
        proposal = real_proposal()
        for field in ("title", "expected_result", "test_data"):
            setattr(proposal.test_cases[0], field, "It works")
        self.assertTrue(any("states no observable behavior" in issue
                            for issue in proposal_content_issues(proposal)))

    def test_accepts_a_dict_as_stored_in_the_database(self):
        self.assertEqual(proposal_content_issues(real_proposal().model_dump()), [])

    def test_whitespace_and_bare_punctuation_pass_min_length_but_are_filler(self):
        # min_length=1 admits both, so the guard cannot rely on emptiness alone.
        for value in ("   ", "...", ".", " . "):
            with self.subTest(value=value):
                proposal = real_proposal()
                proposal.test_cases[0].expected_result = value
                self.assertTrue(any("expected_result is filler" in issue
                                    for issue in proposal_content_issues(proposal)))

    def test_a_stored_record_with_missing_or_null_fields_does_not_raise(self):
        self.assertTrue(proposal_content_issues({"summary": None, "test_cases": [
            {"id": "s", "title": None, "steps": [], "expected_result": "", "test_data": None}]}))
        self.assertTrue(proposal_content_issues({}))


class ProposalRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.baseline = Path(self.temp.name) / "baseline"
        self.baseline.mkdir()
        self.cfg = Config(data_dir=str(Path(self.temp.name) / "data"), planning_model=OPUS,
                          artifact_s3_uri=None)

    async def propose(self, *responses):
        self.prompts = []
        queue = list(responses)
        outer = self

        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            async def invoke_async(self, prompt, structured_output_model):
                outer.prompts.append(prompt)
                reply = queue.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return SimpleNamespace(structured_output=reply, metrics=SimpleNamespace(
                    get_summary=lambda: {"accumulated_usage": {"outputTokens": 10}}))

        with patch("strands.Agent", FakeAgent), patch("arena.evaluation.judge_model",
                return_value=SimpleNamespace(update_config=lambda **kwargs: None)):
            return await propose_tests({"prompt": "Build a weather agent"}, self.baseline,
                                       self.cfg, {"test_coverage": "focused"})

    async def test_filler_is_never_stored_and_the_model_is_asked_again(self):
        proposal, metadata = await self.propose(filler_proposal(), real_proposal())
        self.assertEqual([case["id"] for case in proposal["test_cases"]],
                         ["ACC-CURRENT", "ACC-FORECAST"])
        self.assertEqual([attempt["outcome"] for attempt in metadata["attempts"]],
                         ["filler_content", "complete"])
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("filler instead of real content", self.prompts[1])
        self.assertIn("a scenario for every behavior they reference", self.prompts[1])

    async def test_a_real_proposal_is_accepted_without_a_second_call(self):
        proposal, metadata = await self.propose(real_proposal())
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual([a["outcome"] for a in metadata["attempts"]], ["complete"])
        self.assertTrue(proposal["summary"])

    async def test_repeated_filler_fails_the_proposal_instead_of_storing_it(self):
        with self.assertRaisesRegex(ValueError, "filler instead of test scenarios"):
            await self.propose(filler_proposal(), filler_proposal())
        self.assertEqual(len(self.prompts), 2)

    async def test_a_truncated_response_is_continued_once_then_succeeds(self):
        from strands.types.exceptions import MaxTokensReachedException
        proposal, metadata = await self.propose(
            MaxTokensReachedException("output limit"), real_proposal())
        self.assertEqual([a["outcome"] for a in metadata["attempts"]], ["output_limit", "complete"])
        self.assertIn("hit the output limit", self.prompts[1])
        self.assertEqual(proposal["test_cases"][0]["id"], "ACC-CURRENT")

    async def test_truncation_then_filler_still_gets_one_correction(self):
        from strands.types.exceptions import MaxTokensReachedException
        proposal, metadata = await self.propose(
            MaxTokensReachedException("output limit"), filler_proposal(), real_proposal())
        self.assertEqual([a["outcome"] for a in metadata["attempts"]],
                         ["output_limit", "filler_content", "complete"])
        self.assertEqual(proposal["summary"], real_proposal().summary)

    async def test_repeated_truncation_reports_the_output_limit_plainly(self):
        from strands.types.exceptions import MaxTokensReachedException
        with self.assertRaisesRegex(ValueError, "exceeded its output limit"):
            await self.propose(MaxTokensReachedException("first"),
                               MaxTokensReachedException("second"))


class ApprovalGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w = Worker(Config(data_dir=str(Path(self.temp.name) / "data"), artifact_s3_uri=None))
        self.c = self.w.service.create("Build a weather agent.")
        self.id = self.c["id"]

    def stage(self, proposal):
        comparison = self.w.store.get("comparison", self.id)
        review = {**comparison["test_review"], "status": "awaiting_review",
                  "proposal": proposal.model_dump(),
                  "requirements_sha256": requirements_digest(comparison)}
        self.w.store.update("comparison", self.id, test_review=review)
        return review["id"]

    def test_filler_cannot_be_approved(self):
        review_id = self.stage(filler_proposal())
        with self.assertRaisesRegex(ValueError, "describe no real scenarios"):
            self.w.service.review_tests(self.id, review_id, answers={"Q1": "Sonnet 4.5"})
        self.assertEqual(self.w.store.get("comparison", self.id)["test_review"]["status"],
                         "awaiting_review")

    def test_the_guard_separates_a_real_proposal_from_filler(self):
        # Pinned as a pair: the guard must admit one and reject the other.
        review_id = self.stage(real_proposal())
        self.w.service.review_tests(self.id, review_id, answers={"Q1": "Sonnet 4.5"})
        review = self.w.store.get("comparison", self.id)["test_review"]
        self.assertEqual(review["status"], "approved")
        self.assertTrue(review["approval_sha256"])
        filler_id = self.stage(filler_proposal())
        with self.assertRaises(ValueError):
            self.w.service.review_tests(self.id, filler_id, answers={"Q1": "Sonnet 4.5"})

    def test_scenarios_mirrored_from_a_supplied_plan_are_exempt(self):
        # A user's own check IDs are not the model's filler, even when one of
        # them happens to be a sentinel word.
        supplied = FocusedTestProposal(summary="Review the supplied tests before evaluation.",
            test_cases=[TestScenario(id="none", title="none",
                steps=["Run the supplied check: python none.py"],
                expected_result="The supplied check completes successfully.",
                test_data="Use the supplied validation files.")])
        self.assertTrue(proposal_content_issues(supplied))
        comparison = self.w.store.get("comparison", self.id)
        review = {**comparison["test_review"], "status": "awaiting_review",
                  "proposal": supplied.model_dump(), "supplied_plan_sha256": "a" * 64,
                  "requirements_sha256": requirements_digest(comparison)}
        self.w.store.update("comparison", self.id, test_review=review)
        self.w.service.review_tests(self.id, review["id"], answers={})
        self.assertEqual(self.w.store.get("comparison", self.id)["test_review"]["status"],
                         "approved")

    def test_requesting_changes_discards_a_filler_proposal(self):
        self.stage(filler_proposal())
        comparison = self.w.store.get("comparison", self.id)
        review = new_review(comparison, comparison["test_review"], "Cover the forecast path.")
        self.assertIsNone(review["previous_proposal"])
        self.assertTrue(any("discarded" in message["message"] for message in review["feedback"]))

    def test_requesting_changes_keeps_a_real_previous_proposal(self):
        self.stage(real_proposal())
        comparison = self.w.store.get("comparison", self.id)
        review = new_review(comparison, comparison["test_review"], "Also cover errors.")
        self.assertEqual(review["previous_proposal"]["test_cases"][0]["id"], "ACC-CURRENT")
        # Pinned against the filler case so the branch, not the default, is tested.
        filler = new_review(comparison, {**comparison["test_review"],
                                         "proposal": filler_proposal().model_dump()}, "Retry.")
        self.assertIsNone(filler["previous_proposal"])

    def test_a_supplied_plan_proposal_is_carried_forward_unchanged(self):
        supplied = {**self.w.store.get("comparison", self.id)["test_review"],
                    "proposal": FocusedTestProposal(summary="Supplied", test_cases=[TestScenario(
                        id="none", title="none", steps=["Run it"], expected_result="Exit zero",
                        test_data="Supplied files")]).model_dump(),
                    "supplied_plan_sha256": "b" * 64}
        review = new_review(self.w.store.get("comparison", self.id), supplied, "Cover errors.")
        self.assertEqual(review["previous_proposal"]["test_cases"][0]["id"], "none")


class ScenarioBindingTests(unittest.TestCase):
    def test_unknown_scenario_error_lists_the_approved_ids(self):
        from arena.test_review import ReviewedCheck, ReviewedTestCode
        review = {"id": "review", "test_coverage": "focused",
                  "proposal": real_proposal().model_dump()}
        code = ReviewedTestCode(checks=[ReviewedCheck(
            id="acc-sdk-wiring", scenario_id="ACC-SDK-WIRING", command="python /acceptance/t.py")])
        with self.assertRaises(ValueError) as caught:
            compile_reviewed_tests(code, review)
        message = str(caught.exception)
        self.assertIn("unknown approved scenario ACC-SDK-WIRING", message)
        self.assertIn("ACC-CURRENT", message)
        self.assertIn("ACC-FORECAST", message)


class GenerationRetryPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_prompt_names_the_approved_scenario_ids(self):
        from arena.evaluation import prepare_plan
        from arena.test_review import ReviewedCheck, ReviewedTestCode
        invalid = ReviewedTestCode(checks=[ReviewedCheck(
            id="invented", scenario_id="ACC-SDK-WIRING", command="python /acceptance/t.py")])
        prompts = []

        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            async def invoke_async(self, prompt, structured_output_model):
                prompts.append(prompt)
                return SimpleNamespace(structured_output=invalid, metrics=SimpleNamespace(
                    get_summary=lambda: {"accumulated_usage": {"outputTokens": 10 * len(prompts)}}))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(data_dir=str(root / "data"), planning_model=OPUS, artifact_s3_uri=None)
            task = {"prompt": "Build a weather agent", "approved_test_review": {
                "id": "review", "test_coverage": "focused", "answers": {},
                "proposal": real_proposal().model_dump()}}
            with patch("strands.Agent", FakeAgent), patch("arena.evaluation.judge_model",
                    return_value=SimpleNamespace(update_config=lambda **kwargs: None)):
                with self.assertRaises(RuntimeError) as caught:
                    await prepare_plan(task, root, cfg)
        self.assertIn("unknown approved scenario ACC-SDK-WIRING", str(caught.exception))
        self.assertEqual(len(prompts), 3)
        for retry in prompts[1:]:
            self.assertIn("The only approved scenario IDs are: ACC-CURRENT, ACC-FORECAST", retry)
            self.assertIn("never invent another ID", retry)

    async def test_each_attempt_reports_its_own_token_usage(self):
        from arena.evaluation import prepare_plan
        from arena.test_review import ReviewedCheck, ReviewedTestCode
        invalid = ReviewedTestCode(checks=[ReviewedCheck(
            id="invented", scenario_id="NOPE", command="python /acceptance/t.py")])
        calls = []

        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            async def invoke_async(self, prompt, structured_output_model):
                calls.append(prompt)
                # The real agent accumulates across turns; each attempt adds 100.
                return SimpleNamespace(structured_output=invalid, metrics=SimpleNamespace(
                    get_summary=lambda: {"accumulated_usage": {"outputTokens": 100 * len(calls)}}))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(data_dir=str(root / "data"), planning_model=OPUS, artifact_s3_uri=None)
            task = {"prompt": "Build a weather agent", "approved_test_review": {
                "id": "review", "test_coverage": "focused", "answers": {},
                "proposal": real_proposal().model_dump()}}
            with patch("strands.Agent", FakeAgent), patch("arena.evaluation.judge_model",
                    return_value=SimpleNamespace(update_config=lambda **kwargs: None)):
                with self.assertRaises(RuntimeError) as caught:
                    await prepare_plan(task, root, cfg)
        attempts = caught.exception.planning_attempts
        self.assertEqual([a["usage"]["outputTokens"] for a in attempts], [100, 100, 100])
        self.assertEqual([a["cumulative_usage"]["outputTokens"] for a in attempts], [100, 200, 300])


if __name__ == "__main__":
    unittest.main()
