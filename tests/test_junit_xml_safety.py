# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""JUnit reports are written by AI-generated tests: unsafe or malformed XML is rejected, not counted."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from defusedxml.common import DefusedXmlException

from arena.config import Config
from arena.evaluation import ET, CandidateTestRunner, Check, Criterion, Plan

# Nested entities in the billion-laughs shape. Kept small so that a regression to the
# standard-library parser fails these assertions instead of exhausting memory.
BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE testsuites [
  <!ENTITY a "aaaa">
  <!ENTITY b "&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;">
  <!ENTITY d "&c;&c;&c;&c;">
]>
<testsuites><testsuite><testcase classname="test_behavior" name="&d;"/></testsuite></testsuites>"""
EXTERNAL_ENTITY = """<?xml version="1.0"?>
<!DOCTYPE testsuites [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
<testsuites><testsuite><testcase classname="test_behavior" name="&secret;"/></testsuite></testsuites>"""
MALFORMED = '<testsuites><testsuite><testcase classname="test_behavior" name="test_result">'
NORMAL = ('<testsuites><testsuite>'
          '<testcase classname="test_behavior" name="test_pass"/>'
          '<testcase classname="test_behavior" name="test_fail"><failure message="wrong"/></testcase>'
          '<testcase classname="test_behavior" name="test_error"><error message="boom"/></testcase>'
          '<testcase classname="test_behavior" name="test_skip"><skipped/></testcase>'
          '</testsuite></testsuites>')
UNKNOWN_COUNTS = {k: None for k in ("passed", "failed", "errored", "skipped", "not_run")}


class ParserTests(unittest.TestCase):
    def test_entity_declarations_are_refused(self):
        for report in (BILLION_LAUGHS, EXTERNAL_ENTITY):
            with self.subTest(report=report.splitlines()[1][:40]):
                with self.assertRaises(DefusedXmlException):
                    ET.fromstring(report)


class JUnitReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        (self.snapshot / "application.py").write_text("value = 42\n")
        self.config = Config(data_dir=str(self.root / "state"), artifact_s3_uri=None)
        self.plan = Plan(
            criteria=[Criterion(id="behavior", requirement="Required result", check_ids=["behavior"])],
            checks=[Check(id="behavior", group="acceptance",
                          command="python -m pytest test_behavior.py --junitxml=/tmp/cc/test.xml",
                          junit="/tmp/cc/test.xml")],
            acceptance_files={"test_behavior.py": "def test_result():\n    assert True\n"},
            excluded_groups={group: "No approved scenarios." for group in ("candidate", "project", "regression")})

    async def run_check(self, code, log, xml):
        async def execute(name, execution, acceptance, regression, command, cancellation=None):
            if name.startswith("arena-prepare-"):
                return 0, "tools ready", ""
            (execution.parent / "reports/behavior.xml").write_text(xml)
            return code, log, ""

        with patch("arena.native_sandbox.Native.check", new=AsyncMock(return_value={})), \
                patch("arena.native_sandbox.Native.execute_check", side_effect=execute), \
                patch("arena.check_python.prepare_check_python", new=AsyncMock(return_value={})):
            result = await CandidateTestRunner(self.config).run(self.snapshot, self.plan, self.root / "evidence")
        return result["checks"][0]

    def assert_rejected(self, check):
        self.assertEqual(check["status"], "infrastructure_error")
        self.assertIn("could not be parsed safely", check["classification"])
        self.assertNotEqual(check["counts"].get("source"), "JUnit XML")
        self.assertNotIn("test_cases", check)

    async def test_billion_laughs_report_is_rejected_and_not_counted(self):
        check = await self.run_check(1, "runner output without a summary", BILLION_LAUGHS)
        self.assert_rejected(check)
        self.assertEqual(check["counts"], UNKNOWN_COUNTS)

    async def test_external_entity_report_is_rejected_and_not_counted(self):
        check = await self.run_check(1, "runner output without a summary", EXTERNAL_ENTITY)
        self.assert_rejected(check)
        self.assertEqual(check["counts"], UNKNOWN_COUNTS)

    async def test_rejected_report_cannot_make_a_check_pass(self):
        check = await self.run_check(0, "1 passed in 0.01s", BILLION_LAUGHS)
        self.assert_rejected(check)

    async def test_malformed_report_is_rejected(self):
        check = await self.run_check(1, "runner output without a summary", MALFORMED)
        self.assert_rejected(check)
        self.assertEqual(check["counts"], UNKNOWN_COUNTS)

    async def test_normal_report_counts_are_unchanged(self):
        check = await self.run_check(1, "1 passed, 1 failed, 1 skipped, 1 error in 0.01s", NORMAL)
        self.assertEqual(check["counts"], {"passed": 1, "failed": 1, "errored": 1, "skipped": 1,
                                           "not_run": 0, "source": "JUnit XML"})
        self.assertEqual([c["outcome"] for c in check["test_cases"]], ["passed", "failed", "errored", "skipped"])
        self.assertEqual(check["status"], "failed")
        self.assertTrue((self.root / "evidence/behavior.xml").is_file())


if __name__ == "__main__":
    unittest.main()
