# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Offline regressions for planner selection and real constructor observation."""
import asyncio
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import boto3
from strands import Agent
from strands.models import BedrockModel
from strands.types.exceptions import StructuredOutputException
from pydantic import BaseModel

from arena.config import Config
from arena.constructor_observation import observe_constructors
from arena.evaluation import Check, Criterion, Plan, judge_model, plan_quality_issues, prepare_plan
from arena.planning_support import OBSERVER_FILENAME, with_constructor_observer
from arena.test_review import TestProposal, TestScenario, propose_tests


OPUS = "us.anthropic.claude-opus-5"


class PlanningModelTests(unittest.TestCase):
    def model(self, config, *, planning=True):
        # Use the real SDK and capture its request formatter without networking.
        session = boto3.Session(aws_access_key_id="synthetic", aws_secret_access_key="synthetic",
                                region_name="us-east-1")
        with patch("boto3.Session", return_value=session):
            return judge_model(config, **({"max_tokens": 100000} if planning else {}))

    def test_opus_max_wire_request_and_structured_output_repair(self):
        cfg = Config(judge_model="us.xai.grok-4.6", planning_model=OPUS, planning_effort="max")
        model = self.model(cfg)
        messages = [{"role": "user", "content": [{"text": "A synthetic planning request"}]}]
        tools = [{"name": "Result", "description": "Format result", "inputSchema": {
            "json": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}}]
        for choice in (None, {"auto": {}}):
            with self.subTest(choice=choice):
                request = model.format_request(messages, tools, tool_choice=choice)
                self.assertEqual(request["modelId"], OPUS)
                self.assertEqual(request["additionalModelRequestFields"], {
                    "thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "max"}})
                self.assertEqual(request["inferenceConfig"], {"maxTokens": 100000})
                self.assertEqual(request["toolConfig"]["toolChoice"], {"auto": {}})
        # The repair turn keeps its forced tool so the model cannot answer in
        # prose or submit filler; Bedrock forbids reasoning options alongside it.
        for choice in ({"any": {}}, {"tool": {"name": "Result"}}):
            with self.subTest(choice=choice):
                request = model.format_request(messages, tools, tool_choice=choice)
                self.assertEqual(request["toolConfig"]["toolChoice"], choice)
                self.assertNotIn("additionalModelRequestFields", request)
                self.assertEqual(request["inferenceConfig"], {"maxTokens": 100000})
        judge = self.model(cfg, planning=False)
        request = judge.format_request(messages)
        self.assertEqual(request["modelId"], cfg.judge_model)
        self.assertNotIn("additionalModelRequestFields", request)
        self.assertNotIn("maxTokens", request["inferenceConfig"])
        self.assertNotIn("temperature", request["inferenceConfig"])

    def test_legacy_planner_configuration_still_uses_judge_model(self):
        model = self.model(Config(judge_model="legacy-planner"))
        request = model.format_request([{"role": "user", "content": [{"text": "Test"}]}])
        self.assertEqual(request["modelId"], "legacy-planner")
        self.assertEqual(request["inferenceConfig"], {"maxTokens": 100000, "temperature": 0})
        self.assertNotIn("additionalModelRequestFields", request)

    def test_effort_rejects_unsupported_provider_and_invalid_values(self):
        with self.assertRaises(ValueError):
            Config(planning_model="us.xai.grok-4.6", planning_effort="max")
        with self.assertRaises(ValueError):
            Config(planning_model=OPUS, planning_effort="maximum")
        cfg = Config(judge_model="legacy", planning_model=OPUS, planning_effort="max")
        selected = cfg.judge_for_comparison({"judge": {
            "model": "us.xai.grok-4.6", "region": "us-east-1", "profile": None}})
        self.assertEqual(selected.effective_planning_model, OPUS)
        self.assertEqual(selected.planning_effort, "max")


class ConstructorObservationTests(unittest.TestCase):
    def test_real_sdk_cli_constructors_run_and_session_identity_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "aws-config").write_text("[profile fixture-profile]\nregion = us-east-1\n")
            (root / "aws-credentials").write_text(
                "[fixture-profile]\naws_access_key_id = synthetic\naws_secret_access_key = synthetic\n")
            script = root / "public_cli.py"
            script.write_text(
                "import boto3\nfrom strands import Agent\nfrom strands.models import BedrockModel\n"
                "if __name__ == '__main__':\n"
                "    session = boto3.Session(profile_name='fixture-profile')\n"
                "    model = BedrockModel(model_id='fixture-model', boto_session=session)\n"
                "    agent = Agent(model=model, tools=[], callback_handler=None)\n")
            original = BedrockModel.__init__
            clients = []
            sentinel = SimpleNamespace(meta=SimpleNamespace(region_name="us-east-1"))
            def fixture_client(session, service_name, **kwargs):
                clients.append((session, service_name))
                return sentinel
            environment = {
                "AWS_CONFIG_FILE": str(root / "aws-config"),
                "AWS_SHARED_CREDENTIALS_FILE": str(root / "aws-credentials"),
                "AWS_EC2_METADATA_DISABLED": "true",
            }
            with patch.dict(os.environ, environment), \
                    patch.object(boto3.session.Session, "client", new=fixture_client), \
                    observe_constructors(boto3.Session, BedrockModel, Agent) as calls:
                result = runpy.run_path(str(script), run_name="__main__")
            self.assertIs(BedrockModel.__init__, original)
            self.assertEqual(calls[boto3.Session][0]["profile_name"], "fixture-profile")
            self.assertEqual(calls[BedrockModel][0]["model_id"], "fixture-model")
            self.assertIs(calls[BedrockModel][0]["boto_session"], result["session"])
            self.assertIs(clients[0][0], result["session"])
            self.assertEqual(clients[0][1], "bedrock-runtime")
            self.assertIs(result["model"].client, sentinel)
            self.assertEqual(result["model"].config["model_id"], "fixture-model")
            self.assertIs(result["agent"].model, result["model"])
            self.assertIs(calls[Agent][0]["model"], result["model"])

    def test_exceptions_nested_observers_and_previous_profile_are_preserved(self):
        class Construct:
            def __init__(self, value, *, fail=False, **kwargs):
                if fail:
                    raise ValueError("real validation failed")
                self.value = value
        events = []
        original = sys.getprofile()
        def previous(frame, event, arg):
            if frame.f_code is Construct.__init__.__code__ and event == "call":
                events.append(event)
        sys.setprofile(previous)
        try:
            with observe_constructors(Construct) as outer:
                Construct(1)
                outer_hook = sys.getprofile()
                with self.assertRaisesRegex(ValueError, "real validation failed"):
                    with observe_constructors(Construct) as inner:
                        Construct(2, fail=True, model_id="requested")
                self.assertIs(sys.getprofile(), outer_hook)
                Construct(3)
            self.assertIs(sys.getprofile(), previous)
            self.assertEqual([c["value"] for c in outer[Construct]], [1, 2, 3])
            self.assertEqual(inner[Construct][0], {
                "value": 2, "fail": True, "model_id": "requested"})
            self.assertEqual(events, ["call"] * 3)
        finally:
            sys.setprofile(original)

    def test_unsupported_constructor_fails_before_changing_profiler(self):
        previous = sys.getprofile()
        with self.assertRaisesRegex(TypeError, "Python function"):
            with observe_constructors(object):
                self.fail("An unsupported constructor must not be silently unobserved")
        self.assertIs(sys.getprofile(), previous)

    def test_failed_wraps_pattern_still_rejected_with_executable_remediation(self):
        source = (
            "from unittest.mock import patch\n"
            "class RealBedrockModel:\n"
            "    def __init__(self):\n        self.initialized = True\n"
            "with patch.object(RealBedrockModel, '__init__', autospec=True,\n"
            "                  side_effect=lambda self: None, wraps=RealBedrockModel.__init__):\n"
            "    model = RealBedrockModel()\n"
            "assert not hasattr(model, 'initialized')\n")
        # Demonstrate why merely allowing wraps would create a false pass.
        exec(source, {})
        plan = Plan(criteria=[Criterion(id="wiring", requirement="Real SDK wiring", check_ids=["run"])],
                    checks=[Check(id="run", group="acceptance", command="python check.py")],
                    acceptance_files={"check.py": source})
        issues = plan_quality_issues(plan)
        self.assertTrue(any("observe_constructors" in issue for issue in issues))

    def test_frozen_helper_runs_without_application_or_prior_run_files(self):
        source = (
            "from _mchc_observe import observe_constructors\n"
            "class Constructor:\n"
            "    def __init__(self, value):\n        self.value = value\n"
            "with observe_constructors(Constructor) as calls:\n    instance = Constructor(42)\n"
            "assert instance.value == calls[Constructor][0]['value'] == 42\n")
        plan = Plan(criteria=[Criterion(id="behavior", requirement="Real construction", check_ids=["run"])],
                    checks=[Check(id="run", group="acceptance", command="python check.py")],
                    acceptance_files=with_constructor_observer({"check.py": source}))
        self.assertFalse(plan_quality_issues(plan))
        restored = Plan.model_validate_json(plan.model_dump_json())
        with tempfile.TemporaryDirectory() as directory:
            for name, text in restored.acceptance_files.items():
                (Path(directory) / name).write_text(text)
            result = subprocess.run([sys.executable, "-I", "-c",
                "import sys,runpy;sys.path.insert(0,sys.argv[1]);"
                "runpy.run_path(sys.argv[1]+'/check.py',run_name='__main__')",
                directory], cwd=directory, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaisesRegex(ValueError, "reserved"):
            with_constructor_observer({OBSERVER_FILENAME: "raise RuntimeError('replacement')"})


class PlannerProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_agent_repair_keeps_max_effort_and_validates_output(self):
        class Result(BaseModel):
            value: int
        for valid in (True, False):
            with self.subTest(valid=valid):
                requests = []
                def converse(**request):
                    requests.append(request)
                    content, stop = [{"text": "Needs structured formatting"}], "end_turn"
                    if valid and len(requests) == 2:
                        name = request["toolConfig"]["tools"][0]["toolSpec"]["name"]
                        content = [{"toolUse": {
                            "toolUseId": "result-1", "name": name, "input": {"value": 42}}}]
                        stop = "tool_use"
                    return {"output": {"message": {"role": "assistant", "content": content}},
                            "stopReason": stop, "usage": {
                                "inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                            "metrics": {"latencyMs": 1}}
                cfg = Config(planning_model=OPUS, planning_effort="max", judge_streaming=False)
                model = PlanningModelTests().model(cfg)
                with patch.object(model.client, "converse", side_effect=converse):
                    agent = Agent(model=model, tools=[], callback_handler=None)
                    invocation = asyncio.wait_for(agent.invoke_async(
                        "Return 42 using the result tool.", structured_output_model=Result), 5)
                    if valid:
                        self.assertEqual((await invocation).structured_output.value, 42)
                    else:
                        with self.assertRaises(StructuredOutputException):
                            await invocation
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[0]["additionalModelRequestFields"], {
                    "thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "max"}})
                self.assertEqual(requests[0]["toolConfig"]["toolChoice"], {"auto": {}})
                # Reasoning earns the answer on turn one; turn two must submit it.
                self.assertEqual(requests[1]["toolConfig"]["toolChoice"], {"any": {}})
                self.assertNotIn("additionalModelRequestFields", requests[1])

    async def test_planner_only_config_works_for_proposal_and_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(data_dir=str(root / "data"), planning_model=OPUS, planning_effort="max")
            proposal = TestProposal(summary="Behavior", test_cases=[TestScenario(
                id="behavior", title="Behavior", steps=["Exercise entry point"],
                expected_result="Required output", test_data="Controlled input")])
            plan = Plan(criteria=[Criterion(id="behavior", requirement="Behavior", check_ids=["run"])],
                        checks=[Check(id="run", group="acceptance", command="python check.py")])
            prompts = []
            class FakeAgent:
                def __init__(self, **kwargs):
                    prompts.append(kwargs["system_prompt"])
                async def invoke_async(self, prompt, structured_output_model):
                    result = proposal if structured_output_model is TestProposal else plan
                    return SimpleNamespace(structured_output=result, metrics=SimpleNamespace(
                        get_summary=lambda: {"accumulated_usage": {}}))
            with patch("strands.Agent", FakeAgent), patch("arena.evaluation.judge_model",
                    return_value=SimpleNamespace(update_config=lambda **kwargs: None)):
                _, proposal_meta = await propose_tests({"prompt": "Behavior"}, root, cfg, {})
                result, metadata = await prepare_plan({
                    "prompt": "Behavior", "planning_artifact_dir": str(root / "generation")}, root, cfg)
            for item in (proposal_meta, metadata, metadata["attempts"][0],
                         json.loads((root / "generation/attempt-1.json").read_text())):
                self.assertEqual(item["model"], OPUS)
                self.assertEqual(item["effort"], "max")
            self.assertIn(OBSERVER_FILENAME, result.acceptance_files)
            self.assertIn("sys.setprofile", prompts[0])
            self.assertIn("from _mchc_observe import observe_constructors", prompts[1])


if __name__ == "__main__":
    unittest.main()
