# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""bootstrap.sh: the entrypoint contract an AI assistant relies on."""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "bootstrap.sh"
EXITS = {0, 2, 10, 11, 12, 13, 20, 21, 22, 30, 31, 32, 33, 40, 41, 42}


def bootstrap(*args):
    # /bin/bash is bash 3.2 on macOS: the script must not need a newer one.
    return subprocess.run(["/bin/bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30)


class BootstrapContractTests(unittest.TestCase):
    def test_the_script_parses_under_bash_3_2_and_is_executable(self):
        self.assertEqual(subprocess.run(["/bin/bash", "-n", str(SCRIPT)]).returncode, 0)
        self.assertTrue(SCRIPT.stat().st_mode & 0o111)

    def test_help_documents_every_exit_code_the_script_uses(self):
        result = bootstrap("--help")
        self.assertEqual(result.returncode, 0)
        documented = {int(code) for code in re.findall(r"^\s+(\d+)\s{2}", result.stdout, re.M)}
        used = {int(code) for code in re.findall(r"\b(?:finish|exit) (\d+)\b", SCRIPT.read_text())}
        self.assertEqual(documented, EXITS)
        self.assertLessEqual(used, documented)
        for option in ("--set KEY=VALUE", "--deploy", "--approve TOKEN", "--start", "--reconfigure",
                       "--skip-tests", "--offline", "--install-uv"):
            self.assertIn(option, result.stdout)
        self.assertNotIn("set -u", result.stdout)

    def test_usage_errors_are_one_json_summary_with_exit_2(self):
        for args in (("--bogus",), ("--set",), ('--odd"\\name',)):
            with self.subTest(args=args):
                result = bootstrap(*args)
                self.assertEqual(result.returncode, 2)
                summary = json.loads(result.stdout)["summary"]
                self.assertEqual((summary["ok"], summary["exit"]), (False, 2))
                self.assertTrue(summary["next"].startswith("assistant: "))

    def test_details_from_command_output_stay_valid_json(self):
        helper = re.search(r"^json\(\) \{.*?^\}", SCRIPT.read_text(), re.S | re.M).group(0)
        text = 'quote " back \\ line\nret\rtab\t' + "".join(map(chr, range(1, 32))) + " \x1b[31mred\x1b[0m é ✓"
        result = subprocess.run(["/bin/bash", "-c", helper + '\njson "$1"', "json", text],
                                capture_output=True, text=True, timeout=10)
        # Newlines and tabs are escaped; carriage returns and other control characters are dropped.
        self.assertEqual(json.loads(result.stdout), "".join(c for c in text if c in "\n\t" or ord(c) >= 32))

    def test_the_tests_stage_summary_reports_skipped_tests_too(self):
        pattern = re.search(r"summary=\$\(grep -E '([^']+)'", SCRIPT.read_text()).group(1)
        for line in ("573 passed, 19 warnings in 70.81s", "570 passed, 3 skipped in 60.00s", "7 skipped in 0.42s"):
            with self.subTest(line=line):
                found = subprocess.run(["grep", "-E", pattern], input=line + "\n", capture_output=True, text=True)
                self.assertEqual(found.stdout, line + "\n")

    def test_every_exit_code_is_documented_for_the_assistant(self):
        agents = (ROOT / "AGENTS.md").read_text()
        runbook = (ROOT / "docs" / "launch-runbook.md").read_text()
        for code in EXITS:
            with self.subTest(code=code):
                self.assertRegex(agents, rf"\|[^|\n]*\b{code}\b[^|\n]*\|")
                self.assertRegex(runbook, rf"\|[^|\n]*\b{code}\b[^|\n]*\|")


if __name__ == "__main__":
    unittest.main()
