# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Regression coverage for archived plans, command paths and evaluator failures."""
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from arena.config import Config
from arena.evaluation import (
    CandidateTestRunner, Check, Criterion, Plan, assess_accuracy, assess_tests,
    plan_quality_issues, resolve_logical_paths,
)
from arena.evaluation_paths import prepare_check_command, pytest_setup_error
from arena.preflight import shell_restrictions


class CommandPathTests(unittest.TestCase):
    def prepare(self, command, junit=None, mapping=None):
        return prepare_check_command(command, {
            "test_behavior.py": "", "nested/test_other.py": "", "run_candidate_tests.py": "",
        }, "behavior", junit, mapping)

    def test_legacy_pytest_paths_and_selectors_are_materialized_without_plan_changes(self):
        plan = Plan(criteria=[Criterion(id="behavior", requirement="Public behavior", check_ids=["behavior"])],
                    checks=[Check(id="behavior", group="acceptance", command=(
                        "python -m pytest ./test_behavior.py::TestAPI::test_result "
                        "-v -p no:cacheprovider --junitxml=/tmp/cc/A01.xml"), junit="/tmp/cc/A01.xml")],
                    acceptance_files={"test_behavior.py": "def test_result():\n    assert True\n"})
        frozen = plan.model_dump_json()
        restored = Plan.model_validate_json(frozen)
        check = restored.checks[0]
        result = prepare_check_command(check.command, restored.acceptance_files,
                                       check.id, check.junit)
        self.assertEqual(shlex.split(result["command"]), [
            "python", "-m", "pytest", "/acceptance/test_behavior.py::TestAPI::test_result",
            "-v", "-p", "no:cacheprovider", "--junitxml=/reports/behavior.xml"])
        self.assertEqual(result["junit"], "/reports/behavior.xml")
        self.assertEqual(result["generated_inputs"], ["/acceptance/test_behavior.py"])
        self.assertFalse(result["issues"])
        self.assertEqual(restored.model_dump_json(), frozen)

    def test_native_paths_with_spaces_apostrophes_and_shell_metacharacters_remain_single_arguments(self):
        mapping = {"/acceptance": "/private/tmp/a b's $(never execute)/acceptance",
                   "/reports": "/private/tmp/a b's $(never execute)/reports"}
        result = self.prepare(
            "python -m pytest 'nested/test_other.py::test_result[param with spaces]' "
            "--junit-xml '/tmp/old report.xml'", mapping=mapping)
        self.assertEqual(shlex.split(result["command"]), [
            "python", "-m", "pytest", mapping["/acceptance"] + "/nested/test_other.py::test_result[param with spaces]",
            "--junit-xml", mapping["/reports"] + "/behavior.xml"])
        self.assertEqual(result["junit"], mapping["/reports"] + "/behavior.xml")

    def test_helper_relocation_keeps_candidate_working_directory_and_shell_failure_semantics(self):
        original = "cd /workspace && python3 -B run_candidate_tests.py --mode test_behavior.py; exit $?"
        result = self.prepare(original)
        self.assertEqual(result["command"],
                         "cd /workspace && python3 -B /acceptance/run_candidate_tests.py --mode test_behavior.py; exit $?")
        self.assertEqual(result["generated_inputs"], ["/acceptance/run_candidate_tests.py"])
        self.assertFalse(result["pytest"])

    def test_candidate_targets_inline_programs_comments_and_pytest_filters_are_unchanged(self):
        for command in (
            "python /workspace/test_behavior.py",
            "pytest /workspace/test_behavior.py -k test_behavior.py",
            "python candidate_test.py",
            "python -c 'print(\"python test_behavior.py\")'",
            'sh -c "python test_behavior.py"',
            "echo test_behavior.py # python run_candidate_tests.py",
            "cat <<'PY'\npython test_behavior.py\nPY\n",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.prepare(command)["command"], command)

    def test_explicit_acceptance_paths_are_checked_but_candidate_paths_are_not_invented(self):
        self.assertFalse(self.prepare("pytest /acceptance/nested")["issues"])
        self.assertTrue(self.prepare("pytest /acceptance/missing.py::test_case")["issues"])
        self.assertTrue(self.prepare("python /acceptance/missing.py")["issues"])
        self.assertFalse(self.prepare("python missing_candidate_file.py")["issues"])
        plan = Plan(criteria=[Criterion(id="behavior", requirement="Public output", check_ids=["run"])],
                    checks=[Check(id="run", group="acceptance", command="pytest /acceptance/missing.py")])
        self.assertTrue(any("missing from acceptance_files" in issue for issue in plan_quality_issues(plan)))

    def test_declared_report_is_requested_even_when_flag_was_omitted(self):
        for command in ("pytest test_behavior.py", "python -m pytest", "pytest -- test_behavior.py"):
            with self.subTest(command=command):
                result = self.prepare(command, junit="/tmp/old.xml")
                words = shlex.split(result["command"])
                self.assertIn("--junitxml=/reports/behavior.xml", words)
                if "--" in words:
                    self.assertLess(words.index("--junitxml=/reports/behavior.xml"), words.index("--"))
                self.assertEqual(result["junit"], "/reports/behavior.xml")

    def test_logical_reports_root_does_not_rewrite_similar_names_or_physical_paths(self):
        mapping = {"/reports": "/private/tmp/reports", "/acceptance": "/tmp/already/acceptance"}
        value = '"/reports/result.xml" /reports.py /tmp/already/acceptance/check.py /acceptance/check.py'
        self.assertEqual(resolve_logical_paths(value, mapping),
                         '"/private/tmp/reports/result.xml" /reports.py /tmp/already/acceptance/check.py /tmp/already/acceptance/check.py')


class SetupClassificationTests(unittest.TestCase):
    def test_missing_tests_with_report_teardown_exit_one_are_infrastructure_errors(self):
        log = ("collected 0 items\npytest.UsageError: file or directory not found: test_behavior.py\n"
               '  File "/venv/site-packages/_pytest/junitxml.py", line 650\n'
               "PermissionError: [Errno 1] Operation not permitted: '/tmp/cc'\n")
        self.assertIsNotNone(pytest_setup_error(1, log))
        self.assertIsNotNone(pytest_setup_error(1, "ERROR: not found: /acceptance/test.py::missing\n"))
        self.assertIsNotNone(pytest_setup_error(1, (
            '  File "/venv/site-packages/_pytest/junitxml.py", line 650\n'
            "PermissionError: [Errno 1] Operation not permitted: '/tmp/cc'\n")))

    def test_real_assertions_and_candidate_import_errors_are_not_downgraded(self):
        for code, log in (
            (1, "collected 1 item\nE AssertionError: wrong result\n1 failed in 0.01s"),
            (2, "collected 0 items / 1 error\nSyntaxError: invalid candidate syntax"),
            (1, "PermissionError: candidate failed to read its required input"),
            (0, "1 passed in 0.01s"),
        ):
            with self.subTest(code=code, log=log):
                self.assertIsNone(pytest_setup_error(code, log))
        for code in (3, 4, 5):
            self.assertIsNotNone(pytest_setup_error(code, "runner setup failed"))


class RunnerEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        (self.snapshot / "application.py").write_text("value = 42\n")
        self.config = Config(data_dir=str(self.root / "state"), artifact_s3_uri=None)

    def plan(self, command="python -m pytest test_behavior.py --junitxml=/tmp/cc/test.xml", junit="/tmp/cc/test.xml"):
        return Plan(
            criteria=[Criterion(id="behavior", requirement="Required result", check_ids=["behavior"])],
            checks=[Check(id="behavior", group="acceptance", command=command, junit=junit)],
            acceptance_files={"test_behavior.py": "def test_result():\n    assert True\n",
                              "run_candidate_tests.py": "print('candidate tests')\n"},
            excluded_groups={group: "No approved scenarios." for group in ("candidate", "project", "regression")})

    async def run_check(self, plan, code, log, xml=None):
        observed = []

        async def execute(name, execution, acceptance, regression, command, cancellation=None):
            observed.append(command)
            if name.startswith("arena-prepare-"):
                return 0, "tools ready", ""
            if xml is not None:
                (execution.parent / "reports/behavior.xml").write_text(xml)
            return code, log, ""

        with patch("arena.native_sandbox.Native.check", new=AsyncMock(return_value={})), \
                patch("arena.native_sandbox.Native.execute_check", side_effect=execute), \
                patch("arena.check_python.prepare_check_python", new=AsyncMock(return_value={})):
            result = await CandidateTestRunner(self.config).run(
                self.snapshot, plan, self.root / "evidence",
                baseline_results={"checks": [{"id": "behavior", "status": "failed"}]})
        return result, observed

    async def test_masked_setup_failure_remains_unverified_in_accuracy_and_score(self):
        plan = self.plan()
        result, commands = await self.run_check(plan, 1, (
            "collected 0 items\npytest.UsageError: file or directory not found: test_behavior.py\n"
            "PermissionError: [Errno 1] Operation not permitted: '/tmp/cc'\n"))
        check = result["checks"][0]
        self.assertEqual(check["status"], "infrastructure_error")
        self.assertNotIn("baseline also failed", check["classification"])
        self.assertIsNone(assess_accuracy(plan.model_dump(), result, {"files": []})["score"])
        self.assertIsNone(assess_tests(plan.model_dump(), result)["score"])
        self.assertNotIn("/tmp/cc", commands[-1])
        self.assertIn(str(self.root / "evidence/acceptance/test_behavior.py"), commands[-1])
        self.assertIn("--junitxml=", commands[0])
        self.assertEqual(check["command"], plan.checks[0].command)
        self.assertEqual(result["environment"]["evaluator_version"], "10")

    async def test_genuine_assertion_failure_retains_junit_cases_and_failed_status(self):
        result, _ = await self.run_check(self.plan(), 1,
            "collected 1 item\nE AssertionError: wrong result\n1 failed in 0.01s",
            '<testsuites><testsuite><testcase classname="test_behavior" name="test_result">'
            '<failure message="wrong result"/></testcase></testsuite></testsuites>')
        check = result["checks"][0]
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["counts"]["failed"], 1)
        self.assertEqual(check["test_cases"][0]["outcome"], "failed")
        self.assertTrue((self.root / "evidence/behavior.xml").is_file())

    async def test_missing_report_cannot_be_scored_as_passed(self):
        result, _ = await self.run_check(self.plan(), 0, "1 passed in 0.01s")
        self.assertEqual(result["checks"][0]["status"], "infrastructure_error")
        self.assertIn("JUnit report is missing", result["checks"][0]["classification"])

    async def test_missing_generated_input_stops_before_executing_the_check(self):
        plan = self.plan(command="python /acceptance/missing.py", junit=None)
        result, commands = await self.run_check(plan, 0, "unexpected")
        self.assertEqual(len(commands), 1)
        self.assertEqual(result["checks"][0]["status"], "infrastructure_error")
        self.assertIn("preflight", result["checks"][0]["classification"])

    async def test_missing_candidate_script_is_still_an_application_failure(self):
        plan = self.plan(command="python run_candidate_tests.py", junit=None)
        result, _ = await self.run_check(plan, 2,
            "python: can't open file '/workspace/missing_application.py': [Errno 2] No such file or directory")
        self.assertEqual(result["checks"][0]["status"], "failed")

    async def test_empty_junit_cannot_be_scored_as_passed(self):
        result, _ = await self.run_check(self.plan(), 0, "no tests ran in 0.01s",
                                       '<testsuites><testsuite tests="0"/></testsuites>')
        self.assertEqual(result["checks"][0]["status"], "not_run")


@unittest.skipUnless(sys.platform == "darwin", "macOS native evaluator integration")
@unittest.skipIf(shell_restrictions(), "this shell is sandboxed; run from Terminal.app")
class NativeReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_helper_uses_candidate_cwd_and_writable_isolated_reports(self):
        # Only standard-library code; no downloads, model calls or live comparisons.
        from arena.native_sandbox import Native
        from arena.artifact_storage import remove_tree

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.addCleanup(remove_tree, root)
        snapshot = root / "snapshot"
        snapshot.mkdir()
        (snapshot / "application.py").write_text("value = 42\n")
        plan = Plan(
            criteria=[Criterion(id="behavior", requirement="Required value", check_ids=["behavior"])],
            checks=[Check(id="behavior", group="acceptance",
                          command="python check.py", junit="/reports/result.xml")],
            acceptance_files={"check.py": (
                "from pathlib import Path\n"
                "import application\n"
                "assert application.value == 42\n"
                "assert Path.cwd().name == 'checkout'\n"
                "assert Path('application.py').is_file()\n"
                "try:\n"
                "    Path(__file__).write_text('must remain read-only')\n"
                "except PermissionError:\n"
                "    pass\n"
                "else:\n"
                "    raise AssertionError('acceptance files must remain read-only')\n"
                "Path('/reports/result.xml').write_text("
                "'<testsuites><testsuite><testcase classname=\"api\" name=\"value\"/>"
                "</testsuite></testsuites>')\n")})
        frozen = plan.model_dump_json()
        config = Config(data_dir=str(root / "state"), artifact_s3_uri=None, check_seconds=30)
        native = Native(config)
        with patch("arena.native_sandbox.Native", return_value=native):
            result = await CandidateTestRunner(config).run(snapshot, plan, root / "evidence")
        check = result["checks"][0]
        self.assertEqual(check["status"], "passed", json.dumps(result, indent=2))
        self.assertEqual(check["counts"]["passed"], 1)
        self.assertFalse(native.jobs)
        self.assertEqual(plan.model_dump_json(), frozen)
        self.assertEqual((snapshot / "application.py").read_text(), "value = 42\n")


if __name__ == "__main__":
    unittest.main()
