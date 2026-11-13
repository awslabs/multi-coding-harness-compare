# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for arena/harness_tests.py and the related scoring / review changes.

Covers:
  - load_manifest: path traversal, symlinks, size limits, missing/invalid manifest,
    test_files confinement, UTF-8, checks schema, duplicate ids
  - build_plan: compile_reviewed_tests integration, alignment/quality issues,
    unknown scenario_ids, focused cardinality, placeholder commands
  - create_lock / enforce_lock: modified/deleted/replaced files, restoration
  - format_repair_feedback / format_fix_feedback: content, budget, round info
  - review_harness_tests: structured output validation (every scenario once,
    rating values, verdict consistency, evidence naming, unknown ids), failure mapping
  - assess_accuracy / assess_tests with ratings: effective score, criterion met only
    with rating 1, legacy path unchanged
  - evaluate_candidate no_manifest_reason: score 0 (not incomplete)
  - prompts: write_tests prompt content (scenarios, task, manifest schema,
    fix-implementation rule, lock notice, remaining budget)
  - Config: test_active_seconds, test_fix_rounds validated
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from arena.config import Config
from arena.evaluation import assess_accuracy, assess_tests, GROUP_WEIGHTS, EVALUATOR_VERSION
from arena.harness_tests import (
    MANIFEST_NAME,
    bounded_log_tail,
    build_plan,
    create_lock,
    enforce_lock,
    format_fix_feedback,
    format_repair_feedback,
    load_manifest,
)
from arena.prompts import (
    TEMPLATE_VERSION,
    effective_prompt,
    fix_tests_prompt,
    repair_tests_prompt,
)
from arena.test_review import (
    TestAgentReviewOutput,
    TestAgentReviewScenario,
    _validate_review_output,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _write_manifest(ws: Path, data: dict) -> None:
    (ws / MANIFEST_NAME).write_text(json.dumps(data), encoding="utf-8")


def _minimal_manifest(test_file: str = "tests/test_foo.py") -> dict:
    return {
        "version": 1,
        "checks": [
            {"id": "S1-basic", "scenario_id": "S1", "command": "python -m pytest tests/test_foo.py -q", "junit": None}
        ],
        "test_files": [test_file],
        "python_requirements": [],
    }


def _make_test_file(ws: Path, rel_path: str, content: str = "def test_foo(): assert True\n") -> Path:
    full = ws / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _approved_review(scenario_id: str = "S1", group: str = "acceptance",
                     coverage: str = "focused") -> dict:
    return {
        "id": "review-001",
        "proposal": {
            "summary": "Test the slug function",
            "test_cases": [
                {
                    "id": scenario_id,
                    "title": "Basic slugification",
                    "group": group,
                    "steps": ["Call slug('Hello World')"],
                    "expected_result": "Returns 'hello-world'",
                    "test_data": "input='Hello World'",
                }
            ],
            "assumptions": ["No special chars"],
            "limitations": [],
        },
        "answers": {},
        "test_coverage": coverage,
    }


# ── load_manifest tests ───────────────────────────────────────────────────────

class LoadManifestTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "workspace"
        self.ws.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_missing_manifest_returns_error(self):
        manifest, errors = load_manifest(self.ws)
        self.assertIsNone(manifest)
        self.assertTrue(any("not found" in e for e in errors))

    def test_valid_manifest_loads(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        _write_manifest(self.ws, _minimal_manifest())
        manifest, errors = load_manifest(self.ws)
        self.assertEqual(errors, [])
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["version"], 1)

    def test_manifest_too_large_is_rejected(self):
        # Write a manifest that's just over 256 KiB
        big = {"version": 1, "checks": [], "test_files": [], "notes": "x" * (257 * 1024)}
        (self.ws / MANIFEST_NAME).write_bytes(json.dumps(big).encode("utf-8"))
        manifest, errors = load_manifest(self.ws)
        self.assertIsNone(manifest)
        self.assertTrue(any("256 KiB" in e or "KiB limit" in e for e in errors))

    def test_manifest_not_utf8_is_rejected(self):
        (self.ws / MANIFEST_NAME).write_bytes(b"\xff\xfe invalid utf-8")
        manifest, errors = load_manifest(self.ws)
        self.assertIsNone(manifest)
        self.assertTrue(any("UTF-8" in e for e in errors))

    def test_manifest_not_json_is_rejected(self):
        (self.ws / MANIFEST_NAME).write_text("not json at all {{{", encoding="utf-8")
        manifest, errors = load_manifest(self.ws)
        self.assertIsNone(manifest)
        self.assertTrue(any("JSON" in e for e in errors))

    def test_manifest_wrong_version_collects_error(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        data = _minimal_manifest()
        data["version"] = 2
        _write_manifest(self.ws, data)
        manifest, errors = load_manifest(self.ws)
        self.assertIsNotNone(manifest)  # partial parse; version error collected
        self.assertTrue(any("version" in e.lower() for e in errors))

    def test_path_traversal_in_test_files_is_rejected(self):
        # A path with ".." that escapes the workspace
        _make_test_file(self.ws, "tests/test_foo.py")
        data = _minimal_manifest()
        data["test_files"] = ["../outside.py"]
        _write_manifest(self.ws, data)
        manifest, errors = load_manifest(self.ws)
        self.assertTrue(any("traversal" in e.lower() or ".." in e for e in errors))

    def test_absolute_path_in_test_files_is_rejected(self):
        data = _minimal_manifest()
        data["test_files"] = ["/etc/passwd"]
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("absolute" in e.lower() for e in errors))

    def test_symlink_in_test_files_is_rejected(self):
        # Create a real file and a symlink to it
        real = self.ws / "real.py"
        real.write_text("", encoding="utf-8")
        link = self.ws / "tests" / "link.py"
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(real)
        data = _minimal_manifest("tests/link.py")
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("symlink" in e.lower() for e in errors))

    def test_manifest_as_symlink_is_rejected(self):
        real = self.ws / "real_manifest.json"
        real.write_text(json.dumps(_minimal_manifest()), encoding="utf-8")
        (self.ws / MANIFEST_NAME).symlink_to(real)
        manifest, errors = load_manifest(self.ws)
        self.assertIsNone(manifest)
        self.assertTrue(any("symlink" in e.lower() for e in errors))

    def test_nonexistent_test_file_is_error(self):
        # test_files lists a file that doesn't exist
        data = _minimal_manifest("tests/nonexistent.py")
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("does not exist" in e for e in errors))

    def test_test_file_too_large_is_rejected(self):
        big_file = self.ws / "tests" / "big.py"
        big_file.parent.mkdir(exist_ok=True)
        big_file.write_bytes(b"x" * (1024 * 1024 + 1))
        data = _minimal_manifest("tests/big.py")
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("KiB" in e or "MiB" in e for e in errors))

    def test_total_size_limit_enforced(self):
        # Create multiple large files totalling > 8 MiB
        total_errors = []
        files = []
        for i in range(9):
            p = self.ws / "tests" / f"test_{i}.py"
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(b"x" * (1024 * 1024))  # 1 MiB each
            files.append(f"tests/test_{i}.py")
        data = {
            "version": 1,
            "checks": [
                {"id": f"S{i}", "scenario_id": f"S{i}", "command": "echo ok", "junit": None}
                for i in range(9)
            ],
            "test_files": files,
        }
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("MiB" in e or "aggregate" in e for e in errors))

    def test_too_many_test_files_is_rejected(self):
        # Create 201 test files
        ws_tests = self.ws / "tests"
        ws_tests.mkdir()
        files = []
        for i in range(201):
            p = ws_tests / f"t{i}.py"
            p.write_text("", encoding="utf-8")
            files.append(f"tests/t{i}.py")
        data = {"version": 1, "checks": [], "test_files": files}
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("200" in e or "limit" in e for e in errors))

    def test_duplicate_check_ids_are_rejected(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        data = {
            "version": 1,
            "checks": [
                {"id": "dup", "scenario_id": "S1", "command": "echo a", "junit": None},
                {"id": "dup", "scenario_id": "S1", "command": "echo b", "junit": None},
            ],
            "test_files": ["tests/test_foo.py"],
        }
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("duplicated" in e.lower() or "duplicate" in e.lower() for e in errors))

    def test_duplicate_test_files_are_rejected(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        data = _minimal_manifest()
        data["test_files"] = ["tests/test_foo.py", "tests/test_foo.py"]
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("duplicate" in e.lower() for e in errors))

    def test_all_errors_collected_at_once(self):
        # Multiple errors should all be collected in one pass
        # Missing test file + path traversal + wrong version
        data = {
            "version": 2,
            "checks": [{"id": "c1", "scenario_id": "S1", "command": "echo ok", "junit": None}],
            "test_files": ["../outside.py", "nonexistent.py"],
        }
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        # Should have multiple errors
        self.assertGreater(len(errors), 1)

    def test_non_utf8_test_file_is_rejected(self):
        p = self.ws / "tests" / "bad.py"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"\xff\xfe" + b"hello")
        data = _minimal_manifest("tests/bad.py")
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("UTF-8" in e for e in errors))

    def test_python_requirements_non_list_is_error(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        data = _minimal_manifest()
        data["python_requirements"] = "pytest"  # should be a list
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("python_requirements" in e for e in errors))


# ── build_plan tests ──────────────────────────────────────────────────────────

class BuildPlanTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "workspace"
        self.ws.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def _basic_setup(self, scenario_id="S1"):
        _make_test_file(self.ws, "tests/test_foo.py",
                        f"def test_{scenario_id}(): assert True\n")
        review = _approved_review(scenario_id)
        manifest = {
            "version": 1,
            "checks": [
                {"id": f"{scenario_id}-check", "scenario_id": scenario_id,
                 "command": "python -m pytest tests/test_foo.py -q", "junit": None}
            ],
            "test_files": ["tests/test_foo.py"],
        }
        return manifest, review

    def test_valid_manifest_builds_plan(self):
        manifest, review = self._basic_setup()
        plan, issues = build_plan(self.ws, manifest, review)
        self.assertFalse(issues, issues)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.checks), 1)
        self.assertEqual(len(plan.criteria), 1)
        self.assertEqual(plan.checks[0].scenario_id if hasattr(plan.checks[0], 'scenario_id') else
                         plan.review_coverage.get("S1"), ["S1-check"])

    def test_unknown_scenario_id_is_error(self):
        manifest = {
            "version": 1,
            "checks": [
                {"id": "X1-check", "scenario_id": "X1",  # X1 not in review
                 "command": "echo ok", "junit": None}
            ],
            "test_files": [],
        }
        review = _approved_review("S1")
        plan, issues = build_plan(self.ws, manifest, review)
        self.assertIsNone(plan)
        self.assertTrue(any("X1" in i for i in issues))

    def test_missing_scenario_is_error(self):
        # Only covers S1, but review also has S1 - that's fine
        # Scenario with NO check is a missing scenario
        # We need 2 scenarios in the review but only 1 check
        review = {
            "id": "r1",
            "proposal": {
                "summary": "Tests",
                "test_cases": [
                    {"id": "S1", "title": "S1", "group": "acceptance",
                     "steps": ["step"], "expected_result": "ok", "test_data": "x"},
                    {"id": "S2", "title": "S2", "group": "acceptance",
                     "steps": ["step"], "expected_result": "ok", "test_data": "x"},
                ],
                "assumptions": [], "limitations": [],
            },
            "answers": {},
            "test_coverage": "comprehensive",
        }
        manifest = {
            "version": 1,
            "checks": [
                {"id": "S1-check", "scenario_id": "S1", "command": "echo ok", "junit": None}
                # S2 has no check
            ],
            "test_files": [],
        }
        plan, issues = build_plan(self.ws, manifest, review)
        self.assertIsNone(plan)
        self.assertTrue(any("S2" in i for i in issues))

    def test_focused_mode_requires_exactly_one_check_per_scenario(self):
        # Focused mode: 2 checks for one scenario is an error
        review = _approved_review("S1", coverage="focused")
        manifest = {
            "version": 1,
            "checks": [
                {"id": "S1-a", "scenario_id": "S1", "command": "echo a", "junit": None},
                {"id": "S1-b", "scenario_id": "S1", "command": "echo b", "junit": None},
            ],
            "test_files": [],
        }
        plan, issues = build_plan(self.ws, manifest, review)
        self.assertIsNone(plan)
        self.assertTrue(any("exactly one" in i.lower() or "small suite" in i.lower() for i in issues))

    def test_placeholder_command_is_quality_issue(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        review = _approved_review("S1")
        manifest = {
            "version": 1,
            "checks": [
                {"id": "S1-check", "scenario_id": "S1",
                 "command": "echo ok",  # placeholder
                 "junit": None}
            ],
            "test_files": ["tests/test_foo.py"],
        }
        plan, issues = build_plan(self.ws, manifest, review)
        # Placeholder commands are caught by plan_quality_issues
        self.assertTrue(any("placeholder" in i.lower() for i in issues))

    def test_failure_hiding_pipeline_is_quality_issue(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        review = _approved_review("S1")
        manifest = {
            "version": 1,
            "checks": [
                {"id": "S1-check", "scenario_id": "S1",
                 "command": "python -m pytest tests/test_foo.py || true",
                 "junit": None}
            ],
            "test_files": ["tests/test_foo.py"],
        }
        plan, issues = build_plan(self.ws, manifest, review)
        self.assertTrue(any("hides failures" in i.lower() for i in issues))

    def test_python_ast_checks_applied_to_test_files(self):
        # test_file has blind SDK mock — should be caught
        content = (
            "from unittest.mock import patch\n"
            "with patch('strands.Agent'):\n"
            "    pass\n"
        )
        _make_test_file(self.ws, "tests/test_bad.py", content)
        review = _approved_review("S1")
        manifest = {
            "version": 1,
            "checks": [
                {"id": "S1-check", "scenario_id": "S1",
                 "command": "python -m pytest tests/test_bad.py -q", "junit": None}
            ],
            "test_files": ["tests/test_bad.py"],
        }
        plan, issues = build_plan(self.ws, manifest, review)
        self.assertTrue(any("strands" in i.lower() or "BedrockModel" in i or "Agent" in i for i in issues))


# ── Lock tests ────────────────────────────────────────────────────────────────

class LockTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "workspace"
        self.ws.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def _setup(self):
        _make_test_file(self.ws, "tests/test_foo.py", "def test_foo(): assert True\n")
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        return manifest

    def test_lock_captures_manifest_and_test_files(self):
        manifest = self._setup()
        lock = create_lock(self.ws, manifest)
        # New format: lock["files"] has the per-file entries
        self.assertIn("files", lock)
        self.assertIn(MANIFEST_NAME, lock["files"])
        self.assertIn("tests/test_foo.py", lock["files"])
        # Each entry has sha256 and bytes
        for entry in lock["files"].values():
            self.assertIn("sha256", entry)
            self.assertIn("bytes", entry)
            self.assertIsInstance(entry["bytes"], bytes)

    def test_lock_sha256_matches_file_content(self):
        manifest = self._setup()
        lock = create_lock(self.ws, manifest)
        # Verify SHA-256 for the test file (new format: lock["files"])
        content = (self.ws / "tests/test_foo.py").read_bytes()
        expected = hashlib.sha256(content).hexdigest()
        self.assertEqual(lock["files"]["tests/test_foo.py"]["sha256"], expected)

    def test_enforce_detects_modified_file(self):
        manifest = self._setup()
        lock = create_lock(self.ws, manifest)
        # Modify the test file
        (self.ws / "tests/test_foo.py").write_text("# modified\n", encoding="utf-8")
        violations = enforce_lock(self.ws, lock, round_number=1)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["kind"], "modified")
        self.assertEqual(violations[0]["path"], "tests/test_foo.py")
        self.assertEqual(violations[0]["round"], 1)

    def test_enforce_restores_modified_file(self):
        manifest = self._setup()
        lock = create_lock(self.ws, manifest)
        original = (self.ws / "tests/test_foo.py").read_bytes()
        # Modify the test file
        (self.ws / "tests/test_foo.py").write_text("# modified\n", encoding="utf-8")
        enforce_lock(self.ws, lock, round_number=1)
        # File should be restored
        self.assertEqual((self.ws / "tests/test_foo.py").read_bytes(), original)

    def test_enforce_detects_deleted_file(self):
        manifest = self._setup()
        lock = create_lock(self.ws, manifest)
        (self.ws / "tests/test_foo.py").unlink()
        violations = enforce_lock(self.ws, lock, round_number=2)
        kinds = [v["kind"] for v in violations]
        self.assertIn("deleted", kinds)

    def test_enforce_restores_deleted_file(self):
        manifest = self._setup()
        lock = create_lock(self.ws, manifest)
        original = (self.ws / "tests/test_foo.py").read_bytes()
        (self.ws / "tests/test_foo.py").unlink()
        enforce_lock(self.ws, lock, round_number=2)
        self.assertEqual((self.ws / "tests/test_foo.py").read_bytes(), original)

    def test_enforce_detects_replaced_by_symlink(self):
        manifest = self._setup()
        lock = create_lock(self.ws, manifest)
        # Replace test file with a symlink
        (self.ws / "tests/test_foo.py").unlink()
        target = self.ws / "other.py"
        target.write_text("", encoding="utf-8")
        (self.ws / "tests/test_foo.py").symlink_to(target)
        violations = enforce_lock(self.ws, lock, round_number=1)
        kinds = [v["kind"] for v in violations]
        self.assertTrue(any(k in ("replaced", "modified") for k in kinds))

    def test_enforce_no_violation_when_unchanged(self):
        manifest = self._setup()
        lock = create_lock(self.ws, manifest)
        violations = enforce_lock(self.ws, lock, round_number=1)
        self.assertEqual(violations, [])

    def test_lock_includes_manifest_itself(self):
        manifest = self._setup()
        lock = create_lock(self.ws, manifest)
        self.assertIn(MANIFEST_NAME, lock["files"])
        # Verify bytes match the manifest file
        expected = (self.ws / MANIFEST_NAME).read_bytes()
        self.assertEqual(lock["files"][MANIFEST_NAME]["bytes"], expected)

    def test_enforce_lock_manifest_violation_detected(self):
        manifest = self._setup()
        lock = create_lock(self.ws, manifest)
        # Modify the manifest itself
        (self.ws / MANIFEST_NAME).write_text("{}", encoding="utf-8")
        violations = enforce_lock(self.ws, lock, round_number=1)
        paths = [v["path"] for v in violations]
        self.assertIn(MANIFEST_NAME, paths)


# ── Feedback formatting tests ─────────────────────────────────────────────────

class FeedbackFormattingTests(unittest.TestCase):
    def test_repair_feedback_contains_errors(self):
        errors = ["tests/test_foo.py not found", "unknown scenario X1"]
        text = format_repair_feedback(errors)
        self.assertIn("tests/test_foo.py not found", text)
        self.assertIn("unknown scenario X1", text)
        self.assertIn("lock", text.lower())

    def test_repair_feedback_says_never_delete(self):
        # The feedback should explicitly warn against deleting tests to pass
        text = format_repair_feedback(["some error"])
        self.assertIn("never delete", text.lower())

    def test_fix_feedback_contains_check_details(self):
        checks = [
            {
                "id": "S1-check",
                "scenario_id": "S1",
                "scenario_title": "Basic slugification",
                "command": "python -m pytest tests/test_foo.py::test_basic -q",
                "exit_code": 1,
                "counts": {"passed": 0, "failed": 1, "errored": 0},
                "log_tail": "FAILED tests/test_foo.py::test_basic\n1 failed in 0.5s",
            }
        ]
        text = format_fix_feedback(
            checks, ["tests/test_foo.py"], round_k=1, total_rounds=2,
            budget_remaining_seconds=3200
        )
        self.assertIn("S1-check", text)
        self.assertIn("S1", text)
        self.assertIn("Basic slugification", text)
        self.assertIn("python -m pytest", text)
        self.assertIn("1 failed", text)

    def test_fix_feedback_mentions_locked_files(self):
        text = format_fix_feedback(
            [], ["tests/test_foo.py", "tests/conftest.py"],
            round_k=1, total_rounds=2, budget_remaining_seconds=3000
        )
        self.assertIn("tests/test_foo.py", text)
        self.assertIn("tests/conftest.py", text)
        self.assertIn("lock", text.lower())

    def test_fix_feedback_shows_round_and_budget(self):
        text = format_fix_feedback(
            [], [], round_k=2, total_rounds=2, budget_remaining_seconds=1500
        )
        self.assertIn("2", text)
        self.assertIn("1500", text)

    def test_fix_feedback_says_fix_implementation(self):
        text = format_fix_feedback(
            [], [], round_k=1, total_rounds=2, budget_remaining_seconds=100
        )
        lower = text.lower()
        self.assertIn("implementation", lower)
        # Must NOT say to fix tests
        self.assertNotIn("fix the tests", lower)
        self.assertNotIn("fix your tests", lower)

    def test_bounded_log_tail_no_truncation(self):
        log = "line\n" * 10
        self.assertEqual(bounded_log_tail(log, 10000), log)

    def test_bounded_log_tail_truncates(self):
        log = "a" * 5000
        result = bounded_log_tail(log, 100)
        self.assertIn("truncated", result)
        self.assertTrue(result.endswith("a" * 100))


# ── Test agent review output validation tests ─────────────────────────────────

class ReviewOutputValidationTests(unittest.TestCase):
    def _scenario(self, scenario_id="S1", rating=1, verdict="faithful",
                  evidence="tests/test_foo.py::test_basic"):
        return TestAgentReviewScenario(
            scenario_id=scenario_id,
            rating=rating,
            verdict=verdict,
            reason="The test asserts the expected result.",
            evidence=evidence,
        )

    def test_valid_output_passes_validation(self):
        output = TestAgentReviewOutput(
            scenarios=[self._scenario("S1")],
            summary="Good tests.",
            concerns=[],
        )
        issues = _validate_review_output(output, ["S1"], ["tests/test_foo.py"])
        self.assertEqual(issues, [])

    def test_missing_scenario_is_flagged(self):
        output = TestAgentReviewOutput(
            scenarios=[self._scenario("S1")],
            summary="ok",
            concerns=[],
        )
        # S2 is in approved list but not in scenarios
        issues = _validate_review_output(output, ["S1", "S2"], ["tests/test_foo.py"])
        self.assertTrue(any("S2" in i for i in issues))

    def test_duplicate_scenario_is_flagged(self):
        output = TestAgentReviewOutput(
            scenarios=[self._scenario("S1"), self._scenario("S1")],
            summary="ok",
            concerns=[],
        )
        issues = _validate_review_output(output, ["S1"], ["tests/test_foo.py"])
        self.assertTrue(any("S1" in i and ("twice" in i or "2 times" in i or "duplicate" in i.lower())
                             for i in issues))

    def test_unknown_scenario_id_is_flagged(self):
        output = TestAgentReviewOutput(
            scenarios=[self._scenario("X99")],
            summary="ok",
            concerns=[],
        )
        issues = _validate_review_output(output, ["S1"], ["tests/test_foo.py"])
        self.assertTrue(any("X99" in i for i in issues))

    def test_evidence_not_in_test_files_is_flagged(self):
        output = TestAgentReviewOutput(
            scenarios=[self._scenario("S1", evidence="src/other.py::test_basic")],
            summary="ok",
            concerns=[],
        )
        issues = _validate_review_output(output, ["S1"], ["tests/test_foo.py"])
        self.assertTrue(any("evidence" in i.lower() for i in issues))

    def test_evidence_missing_allowed_for_rating_zero(self):
        s = TestAgentReviewScenario(
            scenario_id="S1", rating=0, verdict="missing",
            reason="No test exists.", evidence="missing",
        )
        output = TestAgentReviewOutput(scenarios=[s], summary="ok", concerns=[])
        issues = _validate_review_output(output, ["S1"], ["tests/test_foo.py"])
        self.assertEqual(issues, [])

    def test_verdict_inconsistent_with_rating_raises(self):
        with self.assertRaises(Exception):
            TestAgentReviewScenario(
                scenario_id="S1", rating=1, verdict="missing",
                reason="ok", evidence="tests/test_foo.py::test_basic",
            )

    def test_rating_half_requires_partial_verdict(self):
        s = TestAgentReviewScenario(
            scenario_id="S1", rating=0.5, verdict="partial",
            reason="Weak assertion.", evidence="tests/test_foo.py::test_basic",
        )
        self.assertEqual(s.rating, 0.5)
        self.assertEqual(s.verdict, "partial")

    def test_rating_zero_allows_unfaithful_verdict(self):
        s = TestAgentReviewScenario(
            scenario_id="S1", rating=0, verdict="unfaithful",
            reason="Mocks the behavior.", evidence="tests/test_foo.py::test_basic",
        )
        self.assertEqual(s.rating, 0)

    def test_missing_verdict_requires_missing_evidence(self):
        with self.assertRaises(Exception):
            TestAgentReviewScenario(
                scenario_id="S1", rating=0, verdict="missing",
                reason="ok", evidence="tests/test_foo.py::test_basic",  # not "missing"
            )


# ── Scoring with ratings tests ────────────────────────────────────────────────

_SENTINEL = object()


def _make_plan_dict(
    criterion_id="S1",
    check_id="S1-check",
    group="acceptance",
    review_coverage=_SENTINEL,
):
    if review_coverage is _SENTINEL:
        review_coverage = {criterion_id: [check_id]}
    return {
        "criteria": [{"id": criterion_id, "requirement": "req", "weight": 1.0,
                       "critical": True, "check_ids": [check_id]}],
        "checks": [{"id": check_id, "group": group, "command": "echo",
                    "mandatory": True, "scored": True, "weight": 1.0,
                    "junit": None, "intentional_change": None}],
        "acceptance_files": {},
        "python_requirements": [],
        "review_id": "r1",
        "review_coverage": review_coverage,
        "applicability": {},
        "gaps": [],
        "limitations": [],
        "excluded_groups": {},
        "source": "test",
        "rubric_version": "v1",
        "group_weights": dict(GROUP_WEIGHTS),
    }


def _make_evidence(check_id="S1-check", group="acceptance", status="passed", exit_code=0):
    return {
        "checks": [
            {
                "id": check_id, "group": group, "command": "echo",
                "status": status, "exit_code": exit_code,
                "counts": {"passed": 1, "failed": 0, "errored": 0, "skipped": 0, "not_run": 0},
                "scored": True, "mandatory": True, "weight": 1.0,
                "classification": "passed",
            }
        ],
        "gaps": [],
    }


def _dummy_source_review():
    return {"files": [], "changed_files": [], "deleted_context_files": [],
            "omitted_files": [], "snapshot_sha256": "abc",
            "scope": "test"}


class AssessAccuracyRatingsTests(unittest.TestCase):
    def test_legacy_path_no_ratings_unchanged(self):
        plan = _make_plan_dict()
        evidence = _make_evidence()
        result = assess_accuracy(plan, evidence, _dummy_source_review(), ratings=None)
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["criteria"][0]["status"], "met")

    def test_rating_1_passing_check_is_met(self):
        plan = _make_plan_dict()
        evidence = _make_evidence()
        result = assess_accuracy(plan, evidence, _dummy_source_review(),
                                 ratings={"S1": 1})
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["criteria"][0]["status"], "met")

    def test_rating_0_passing_check_is_unmet(self):
        plan = _make_plan_dict()
        evidence = _make_evidence()
        result = assess_accuracy(plan, evidence, _dummy_source_review(),
                                 ratings={"S1": 0})
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["criteria"][0]["status"], "unmet")

    def test_rating_half_passing_check_is_partially_met(self):
        plan = _make_plan_dict()
        evidence = _make_evidence()
        result = assess_accuracy(plan, evidence, _dummy_source_review(),
                                 ratings={"S1": 0.5})
        self.assertEqual(result["criteria"][0]["status"], "partially_met")
        # Score should be 50 (1.0 * 0.5 * 100)
        self.assertAlmostEqual(result["score"], 50.0)

    def test_failing_check_with_rating_1_is_unmet(self):
        plan = _make_plan_dict()
        evidence = _make_evidence(status="failed", exit_code=1)
        result = assess_accuracy(plan, evidence, _dummy_source_review(),
                                 ratings={"S1": 1})
        self.assertEqual(result["criteria"][0]["status"], "unmet")
        self.assertEqual(result["score"], 0.0)

    def test_missing_scenario_in_ratings_defaults_to_zero(self):
        plan = _make_plan_dict()
        evidence = _make_evidence()
        # ratings has no entry for "S1" → defaults to 0
        result = assess_accuracy(plan, evidence, _dummy_source_review(),
                                 ratings={"OTHER": 1})
        self.assertEqual(result["criteria"][0]["status"], "unmet")
        self.assertEqual(result["score"], 0.0)


class AssessTestsRatingsTests(unittest.TestCase):
    def test_legacy_path_no_ratings_unchanged(self):
        plan = _make_plan_dict()
        evidence = _make_evidence()
        result = assess_tests(plan, evidence, ratings=None)
        # Acceptance group passes; other groups have no checks (gaps) → overall score None
        # The key invariant is that the acceptance group itself scored 100
        self.assertEqual(result["groups"]["acceptance"], 100.0)

    def test_rating_1_full_pass_gives_full_group_score(self):
        plan = _make_plan_dict()
        evidence = _make_evidence()
        result = assess_tests(plan, evidence, ratings={"S1": 1})
        self.assertEqual(result["groups"]["acceptance"], 100.0)

    def test_rating_0_gives_zero_group_score(self):
        plan = _make_plan_dict()
        evidence = _make_evidence()
        result = assess_tests(plan, evidence, ratings={"S1": 0})
        self.assertEqual(result["groups"]["acceptance"], 0.0)

    def test_rating_half_gives_half_group_score(self):
        plan = _make_plan_dict()
        evidence = _make_evidence()
        result = assess_tests(plan, evidence, ratings={"S1": 0.5})
        self.assertAlmostEqual(result["groups"]["acceptance"], 50.0)

    def test_check_without_review_coverage_defaults_to_zero_rating(self):
        plan = _make_plan_dict(review_coverage={})  # no coverage mapping
        evidence = _make_evidence()
        result = assess_tests(plan, evidence, ratings={"S1": 1})
        # Without coverage mapping, check_ratings is empty → rating defaults to 0
        self.assertEqual(result["groups"]["acceptance"], 0.0)


# ── no_manifest_reason in evaluate_candidate ─────────────────────────────────

class EvaluateCandidateNoManifestTests(unittest.TestCase):
    def _run(self, **kwargs):
        from arena.evaluation import evaluate_candidate, Plan, Criterion
        plan = Plan(
            criteria=[Criterion(id="task", requirement="Do the thing")],
            checks=[],
            source="test",
        )
        candidate = {
            "snapshot_sha256": "abc123",
            "diff": "--- a\n+++ b\n@@ -1 +1 @@\n+change\n",
            "files": [{"path": "foo.py", "content": "x = 1\n"}],
            "evidence": {"checks": [], "gaps": []},
            "repository_context": [],
        }
        config = Config(judge_model=None)  # no judge to avoid AWS calls
        task = {"prompt": "Do the thing", "answers": [], "spec_version": 1, "id": "cmp-1"}
        return asyncio.run(
            evaluate_candidate(task, plan, candidate, config, **kwargs)
        )

    def test_no_manifest_reason_gives_accuracy_zero(self):
        result = self._run(no_manifest_reason="Budget exhausted before manifest accepted.")
        self.assertEqual(result["accuracy"]["score"], 0)

    def test_no_manifest_reason_gives_tests_zero(self):
        result = self._run(no_manifest_reason="Budget exhausted before manifest accepted.")
        self.assertEqual(result["tests"]["score"], 0)

    def test_no_manifest_reason_records_reason_in_gaps(self):
        reason = "No manifest was produced within the test budget."
        result = self._run(no_manifest_reason=reason)
        self.assertIn(reason, result["accuracy"]["gaps"])
        self.assertIn(reason, result["tests"]["gaps"])

    def test_no_manifest_reason_verified_complete_is_false(self):
        result = self._run(no_manifest_reason="Budget exhausted.")
        self.assertFalse(result["verified_complete"])

    def test_normal_path_not_affected(self):
        result = self._run()
        # With no judge and no plan checks, score may be None (incomplete), but
        # not a forced 0 from no_manifest_reason
        self.assertNotIn("No manifest", result["accuracy"].get("gaps", []))


# ── Config validation tests ───────────────────────────────────────────────────

class ConfigTestStageTests(unittest.TestCase):
    def test_default_test_active_seconds(self):
        config = Config()
        self.assertEqual(config.test_active_seconds, 3600)

    def test_default_test_fix_rounds(self):
        config = Config()
        self.assertEqual(config.test_fix_rounds, 2)

    def test_custom_test_active_seconds(self):
        config = Config(test_active_seconds=1800)
        self.assertEqual(config.test_active_seconds, 1800)

    def test_custom_test_fix_rounds(self):
        config = Config(test_fix_rounds=5)
        self.assertEqual(config.test_fix_rounds, 5)

    def test_test_active_seconds_must_be_positive(self):
        with self.assertRaises(Exception):
            Config(test_active_seconds=0)

    def test_test_fix_rounds_zero_is_allowed(self):
        config = Config(test_fix_rounds=0)
        self.assertEqual(config.test_fix_rounds, 0)

    def test_test_fix_rounds_negative_is_rejected(self):
        with self.assertRaises(Exception):
            Config(test_fix_rounds=-1)


# ── EVALUATOR_VERSION bumped ──────────────────────────────────────────────────

class EvaluatorVersionTests(unittest.TestCase):
    def test_evaluator_version_is_10(self):
        self.assertEqual(EVALUATOR_VERSION, "10")


# ── TEMPLATE_VERSION bumped ───────────────────────────────────────────────────

class TemplateVersionTests(unittest.TestCase):
    def test_template_version_bumped(self):
        # Version was "mandatory-1"; must be different now
        self.assertNotEqual(TEMPLATE_VERSION, "mandatory-1")


# ── Prompt content tests ──────────────────────────────────────────────────────

class WriteTestsPromptTests(unittest.TestCase):
    def _review(self, scenario_id="S1"):
        return _approved_review(scenario_id)

    def _task(self):
        return {"prompt": "Build a slug function", "answers": [], "spec_version": 1}

    def test_write_tests_prompt_contains_task(self):
        prompt = effective_prompt(self._task(), "write_tests", approved_review=self._review())
        self.assertIn("Build a slug function", prompt)

    def test_write_tests_prompt_contains_scenario_id(self):
        prompt = effective_prompt(self._task(), "write_tests", approved_review=self._review())
        self.assertIn("S1", prompt)

    def test_write_tests_prompt_contains_scenario_steps(self):
        prompt = effective_prompt(self._task(), "write_tests", approved_review=self._review())
        self.assertIn("Call slug", prompt)

    def test_write_tests_prompt_contains_expected_result(self):
        prompt = effective_prompt(self._task(), "write_tests", approved_review=self._review())
        self.assertIn("hello-world", prompt)

    def test_write_tests_prompt_contains_manifest_schema(self):
        prompt = effective_prompt(self._task(), "write_tests", approved_review=self._review())
        self.assertIn(MANIFEST_NAME, prompt)
        self.assertIn("scenario_id", prompt)

    def test_write_tests_prompt_fix_implementation_rule(self):
        prompt = effective_prompt(self._task(), "write_tests", approved_review=self._review())
        lower = prompt.lower()
        self.assertIn("fix the implementation", lower)
        # Must NOT say "fix the test"
        self.assertNotIn("fix the test", lower)

    def test_write_tests_prompt_lock_notice(self):
        prompt = effective_prompt(self._task(), "write_tests", approved_review=self._review())
        self.assertIn("lock", prompt.lower())

    def test_write_tests_prompt_has_mandatory_block(self):
        prompt = effective_prompt(self._task(), "write_tests", approved_review=self._review())
        self.assertIn("MANDATORY", prompt)

    def test_write_tests_prompt_no_approved_review_still_works(self):
        # Should not crash when no approved_review is passed
        prompt = effective_prompt(self._task(), "write_tests", approved_review=None)
        self.assertIn("TEST STAGE", prompt)

    def test_repair_tests_prompt_contains_errors(self):
        errors = ["unknown scenario X1", "tests/test_foo.py: syntax error"]
        prompt = repair_tests_prompt(self._task(), self._review(), errors, round_k=1)
        self.assertIn("unknown scenario X1", prompt)
        self.assertIn("syntax error", prompt)

    def test_repair_tests_prompt_says_lock_not_set(self):
        prompt = repair_tests_prompt(self._task(), self._review(), ["err"], round_k=1)
        self.assertIn("lock", prompt.lower())

    def test_fix_tests_prompt_contains_budget(self):
        prompt = fix_tests_prompt(
            self._task(), self._review(),
            failing_checks=[],
            locked_paths=[],
            round_k=1, total_rounds=2,
            budget_remaining_seconds=3200
        )
        self.assertIn("3200", prompt)

    def test_fix_tests_prompt_says_fix_implementation(self):
        prompt = fix_tests_prompt(
            self._task(), self._review(),
            failing_checks=[],
            locked_paths=["tests/test_foo.py"],
            round_k=1, total_rounds=2,
            budget_remaining_seconds=100
        )
        lower = prompt.lower()
        self.assertIn("implementation", lower)
        self.assertIn("tests/test_foo.py", prompt)


# ── C1: Manifest limits (§9.11) ──────────────────────────────────────────────

class ManifestLimitsTests(unittest.TestCase):
    """C1: at most 400 checks, 10 per scenario_id, notes ≤ 2000 bytes."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "workspace"
        self.ws.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_exactly_400_checks_allowed(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        checks = [
            {"id": f"S{i}-check", "scenario_id": f"S{i}", "command": "echo ok", "junit": None}
            for i in range(400)
        ]
        data = {"version": 1, "checks": checks, "test_files": ["tests/test_foo.py"]}
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertFalse(any("400" in e and "exceeding" in e for e in errors),
                         f"400 checks should be allowed: {errors}")

    def test_401_checks_is_rejected(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        checks = [
            {"id": f"S{i}-check", "scenario_id": f"S{i}", "command": "echo ok", "junit": None}
            for i in range(401)
        ]
        data = {"version": 1, "checks": checks, "test_files": ["tests/test_foo.py"]}
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("400" in e for e in errors), f"401 checks should be rejected: {errors}")

    def test_10_checks_per_scenario_allowed(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        checks = [
            {"id": f"S1-{i}", "scenario_id": "S1", "command": "echo ok", "junit": None}
            for i in range(10)
        ]
        data = {"version": 1, "checks": checks, "test_files": ["tests/test_foo.py"]}
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertFalse(any("10 checks" in e or "per scenario" in e for e in errors),
                         f"10 checks per scenario should be allowed: {errors}")

    def test_11_checks_for_one_scenario_is_rejected(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        checks = [
            {"id": f"S1-{i}", "scenario_id": "S1", "command": "echo ok", "junit": None}
            for i in range(11)
        ]
        data = {"version": 1, "checks": checks, "test_files": ["tests/test_foo.py"]}
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("S1" in e and "10" in e for e in errors),
                         f"11 checks per scenario should be rejected: {errors}")

    def test_notes_at_exactly_2000_bytes_allowed(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        data = _minimal_manifest()
        data["notes"] = "a" * 2000
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertFalse(any("notes" in e.lower() and "bytes" in e for e in errors),
                         f"2000-byte notes should be allowed: {errors}")

    def test_notes_2001_bytes_is_rejected(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        data = _minimal_manifest()
        data["notes"] = "a" * 2001  # 2001 bytes (ASCII only)
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("notes" in e.lower() and "bytes" in e for e in errors),
                         f"2001-byte notes should be rejected: {errors}")

    def test_notes_multibyte_utf8_counted_in_bytes(self):
        # 1000 two-byte chars = 2000 bytes → allowed
        _make_test_file(self.ws, "tests/test_foo.py")
        data = _minimal_manifest()
        data["notes"] = "é" * 1000  # each 'é' = 2 bytes in UTF-8 → 2000 bytes total
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertFalse(any("notes" in e.lower() and "bytes" in e for e in errors),
                         f"2000-byte (multibyte) notes should be allowed: {errors}")
        # 1001 two-byte chars = 2002 bytes → rejected
        data["notes"] = "é" * 1001
        _write_manifest(self.ws, data)
        _, errors = load_manifest(self.ws)
        self.assertTrue(any("notes" in e.lower() and "bytes" in e for e in errors),
                         f"2002-byte (multibyte) notes should be rejected: {errors}")


# ── C2: create_lock auto-locks (§9.9) ────────────────────────────────────────

class CreateLockAutoLockTests(unittest.TestCase):
    """C2: auto-lock conftest.py at any depth, pytest.ini, tox.ini."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "workspace"
        self.ws.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_lock_has_new_format_keys(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        self.assertIn("files", lock)
        self.assertIn("auto_locked", lock)
        self.assertIn("warnings", lock)
        self.assertIn("config_snapshots", lock)
        self.assertIn("special_file_baseline", lock)

    def test_nested_conftest_is_auto_locked(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        _make_test_file(self.ws, "tests/conftest.py", "# conftest\n")
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        self.assertIn("tests/conftest.py", lock["files"], "nested conftest.py should be auto-locked")
        self.assertIn("tests/conftest.py", lock["auto_locked"])

    def test_pytest_ini_is_auto_locked(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        (self.ws / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        self.assertIn("pytest.ini", lock["files"])
        self.assertIn("pytest.ini", lock["auto_locked"])

    def test_tox_ini_is_auto_locked(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        (self.ws / "tox.ini").write_text("[tox]\n", encoding="utf-8")
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        self.assertIn("tox.ini", lock["files"])
        self.assertIn("tox.ini", lock["auto_locked"])

    def test_conftest_in_test_files_is_not_in_auto_locked(self):
        # When conftest.py is listed in test_files, it should not be in auto_locked
        _make_test_file(self.ws, "tests/test_foo.py")
        _make_test_file(self.ws, "tests/conftest.py", "# conftest\n")
        manifest = _minimal_manifest()
        manifest["test_files"] = ["tests/test_foo.py", "tests/conftest.py"]
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        self.assertIn("tests/conftest.py", lock["files"])
        self.assertNotIn("tests/conftest.py", lock["auto_locked"])

    def test_auto_locked_unlisted_file_produces_warning(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        _make_test_file(self.ws, "tests/conftest.py", "# conftest\n")
        manifest = _minimal_manifest()  # test_files does NOT include conftest.py
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        self.assertTrue(any("conftest.py" in w for w in lock["warnings"]),
                        f"Should warn about unlisted conftest.py: {lock['warnings']}")

    def test_config_sections_recorded_at_lock_time(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        (self.ws / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            encoding="utf-8"
        )
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        self.assertIn("pyproject.toml", lock["config_snapshots"])
        self.assertIn("testpaths", lock["config_snapshots"]["pyproject.toml"])

    def test_sitecustomize_recorded_in_baseline(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        (self.ws / "sitecustomize.py").write_text("# existing\n", encoding="utf-8")
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        self.assertIn("sitecustomize.py", lock["special_file_baseline"])


# ── C3: enforce_lock new violation types (§9.9) ───────────────────────────────

class EnforceLockNewViolationsTests(unittest.TestCase):
    """C3: created special files, test_config_changed."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "workspace"
        self.ws.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def _basic_lock(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        return create_lock(self.ws, manifest)

    def test_created_sitecustomize_is_flagged_and_deleted(self):
        lock = self._basic_lock()
        # Create sitecustomize.py AFTER lock
        sc = self.ws / "sitecustomize.py"
        sc.write_text("import sys\n", encoding="utf-8")
        violations = enforce_lock(self.ws, lock, round_number=1)
        kinds = {v["kind"] for v in violations}
        paths = {v["path"] for v in violations}
        self.assertIn("created", kinds, f"sitecustomize.py should be flagged 'created': {violations}")
        self.assertIn("sitecustomize.py", paths)
        # Should be deleted
        self.assertFalse(sc.exists(), "sitecustomize.py should be deleted after enforcement")

    def test_created_pth_file_is_flagged_and_deleted(self):
        lock = self._basic_lock()
        pth = self.ws / "tests" / "extra.pth"
        pth.parent.mkdir(exist_ok=True)
        pth.write_text("/some/path\n", encoding="utf-8")
        violations = enforce_lock(self.ws, lock, round_number=1)
        kinds = {v["kind"] for v in violations}
        self.assertIn("created", kinds, f".pth file should be flagged 'created': {violations}")
        self.assertFalse(pth.exists(), ".pth file should be deleted")

    def test_created_nested_conftest_is_flagged_and_deleted(self):
        # conftest.py created after lock (not locked originally)
        _make_test_file(self.ws, "tests/test_foo.py")
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        # Now create a new conftest.py that wasn't locked
        new_conftest = self.ws / "tests" / "conftest.py"
        new_conftest.write_text("# injected\n", encoding="utf-8")
        violations = enforce_lock(self.ws, lock, round_number=1)
        kinds = {v["kind"] for v in violations}
        self.assertIn("created", kinds, f"new conftest.py should be flagged: {violations}")
        self.assertFalse(new_conftest.exists(), "new conftest.py should be deleted")

    def test_preexisting_sitecustomize_not_flagged(self):
        # sitecustomize.py that existed AT lock time should not be flagged
        _make_test_file(self.ws, "tests/test_foo.py")
        sc = self.ws / "sitecustomize.py"
        sc.write_text("# pre-existing\n", encoding="utf-8")
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        # sc is in special_file_baseline, so it should not be flagged
        violations = enforce_lock(self.ws, lock, round_number=1)
        created = [v for v in violations if v["kind"] == "created"]
        self.assertEqual(created, [], f"Pre-existing sitecustomize.py should not be flagged: {created}")

    def test_pyproject_test_config_change_flagged(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        (self.ws / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            encoding="utf-8"
        )
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        # Change the pyproject.toml test config section
        (self.ws / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths = ['tests', 'src']\n",
            encoding="utf-8"
        )
        violations = enforce_lock(self.ws, lock, round_number=1)
        kinds = {v["kind"] for v in violations}
        self.assertIn("test_config_changed", kinds,
                      f"pyproject.toml test-config change should be flagged: {violations}")

    def test_pyproject_test_config_change_not_restored(self):
        # test_config_changed is flagged but NOT restored (file should still have new content)
        _make_test_file(self.ws, "tests/test_foo.py")
        (self.ws / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            encoding="utf-8"
        )
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        new_content = "[tool.pytest.ini_options]\ntestpaths = ['tests', 'src']\n"
        (self.ws / "pyproject.toml").write_text(new_content, encoding="utf-8")
        enforce_lock(self.ws, lock, round_number=1)
        # File should still have the new (changed) content
        actual = (self.ws / "pyproject.toml").read_text(encoding="utf-8")
        self.assertEqual(actual, new_content, "test_config_changed should not restore the file")

    def test_setup_cfg_test_config_change_flagged(self):
        _make_test_file(self.ws, "tests/test_foo.py")
        (self.ws / "setup.cfg").write_text(
            "[tool:pytest]\naddopts = -v\n", encoding="utf-8"
        )
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        # Change the setup.cfg [tool:pytest] section
        (self.ws / "setup.cfg").write_text(
            "[tool:pytest]\naddopts = -v --tb=short\n", encoding="utf-8"
        )
        violations = enforce_lock(self.ws, lock, round_number=1)
        kinds = {v["kind"] for v in violations}
        self.assertIn("test_config_changed", kinds,
                      f"setup.cfg [tool:pytest] change should be flagged: {violations}")

    def test_non_test_config_change_not_flagged(self):
        # A change to a non-test section in pyproject.toml should not trigger test_config_changed
        _make_test_file(self.ws, "tests/test_foo.py")
        (self.ws / "pyproject.toml").write_text(
            "[tool.black]\nline-length = 88\n",
            encoding="utf-8"
        )
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        # Change the [tool.black] section — not a test config
        (self.ws / "pyproject.toml").write_text(
            "[tool.black]\nline-length = 100\n",
            encoding="utf-8"
        )
        violations = enforce_lock(self.ws, lock, round_number=1)
        kinds = {v["kind"] for v in violations}
        self.assertNotIn("test_config_changed", kinds,
                         f"Non-test-config change should not be flagged: {violations}")

    def test_enforce_lock_backward_compatible_flat_format(self):
        # enforce_lock should still work with the old flat format {rel_path: {sha256, bytes}}
        _make_test_file(self.ws, "tests/test_foo.py", "original\n")
        manifest = _minimal_manifest()
        _write_manifest(self.ws, manifest)
        lock = create_lock(self.ws, manifest)
        # Simulate old flat format
        old_lock = lock["files"]
        (self.ws / "tests/test_foo.py").write_text("modified\n", encoding="utf-8")
        violations = enforce_lock(self.ws, old_lock, round_number=1)
        self.assertTrue(any(v["kind"] == "modified" for v in violations))


# ── C4: build_plan workspace-path check (§9.10) ───────────────────────────────

class BuildPlanWorkspacePathTests(unittest.TestCase):
    """C4: command argument resolving to workspace file must be in test_files."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "workspace"
        self.ws.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def _build(self, command, test_files=None, file_to_create="tests/test_foo.py"):
        _make_test_file(self.ws, file_to_create)
        review = _approved_review("S1")
        manifest = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1", "command": command, "junit": None}],
            "test_files": test_files if test_files is not None else [file_to_create],
        }
        return build_plan(self.ws, manifest, review)

    def test_script_in_workspace_not_in_test_files_is_error(self):
        # Create a script that isn't in test_files
        script = self.ws / "scripts" / "run_tests.py"
        script.parent.mkdir(exist_ok=True)
        script.write_text("print('running')\n", encoding="utf-8")
        _make_test_file(self.ws, "tests/test_foo.py")
        review = _approved_review("S1")
        manifest = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1",
                         "command": "python scripts/run_tests.py", "junit": None}],
            "test_files": ["tests/test_foo.py"],  # scripts/run_tests.py NOT here
        }
        plan, issues = build_plan(self.ws, manifest, review)
        self.assertTrue(any("scripts/run_tests.py" in i for i in issues),
                        f"Workspace script not in test_files should be an error: {issues}")

    def test_script_in_test_files_is_ok(self):
        # Script listed in test_files → no error
        script = self.ws / "scripts" / "run_tests.py"
        script.parent.mkdir(exist_ok=True)
        script.write_text("print('running')\n", encoding="utf-8")
        _make_test_file(self.ws, "tests/test_foo.py")
        review = _approved_review("S1")
        manifest = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1",
                         "command": "python scripts/run_tests.py", "junit": None}],
            "test_files": ["tests/test_foo.py", "scripts/run_tests.py"],
        }
        plan, issues = build_plan(self.ws, manifest, review)
        self.assertFalse(any("scripts/run_tests.py" in i for i in issues),
                         f"Script in test_files should not trigger error: {issues}")

    def test_workspace_prefix_path_checked(self):
        # /workspace/scripts/run.py → resolves inside workspace
        script = self.ws / "scripts" / "run.py"
        script.parent.mkdir(exist_ok=True)
        script.write_text("pass\n", encoding="utf-8")
        _make_test_file(self.ws, "tests/test_foo.py")
        review = _approved_review("S1")
        manifest = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1",
                         "command": "python /workspace/scripts/run.py", "junit": None}],
            "test_files": ["tests/test_foo.py"],  # scripts/run.py NOT here
        }
        plan, issues = build_plan(self.ws, manifest, review)
        self.assertTrue(any("scripts/run.py" in i or "/workspace/scripts/run.py" in i
                             for i in issues),
                        f"/workspace/ path not in test_files should be error: {issues}")

    def test_dotslash_path_checked(self):
        # ./scripts/run.py → resolves inside workspace
        script = self.ws / "scripts" / "run.py"
        script.parent.mkdir(exist_ok=True)
        script.write_text("pass\n", encoding="utf-8")
        _make_test_file(self.ws, "tests/test_foo.py")
        review = _approved_review("S1")
        manifest = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1",
                         "command": "python ./scripts/run.py", "junit": None}],
            "test_files": ["tests/test_foo.py"],  # not in test_files
        }
        plan, issues = build_plan(self.ws, manifest, review)
        self.assertTrue(any("scripts/run.py" in i for i in issues),
                        f"./workspace-path not in test_files should be error: {issues}")

    def test_nonexistent_path_not_flagged(self):
        # A path that doesn't exist in workspace → no error (not a workspace file)
        _make_test_file(self.ws, "tests/test_foo.py")
        review = _approved_review("S1")
        manifest = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1",
                         "command": "python scripts/nonexistent.py", "junit": None}],
            "test_files": ["tests/test_foo.py"],
        }
        plan, issues = build_plan(self.ws, manifest, review)
        self.assertFalse(any("nonexistent.py" in i for i in issues),
                         f"Non-existent path should not be flagged: {issues}")


# ── C5: review_harness_tests limits and interface 2 ──────────────────────────

class ReviewHarnessTestsC5Tests(unittest.IsolatedAsyncioTestCase):
    """C5: 32 KiB per-check log, 512 KiB total, implementation_diff, manifest notes in block."""

    def _make_config(self):
        from arena.config import Config
        return Config(judge_model=None, planning_model="anthropic.claude-3-5-sonnet-20241022-v2:0")

    def _approved_review(self):
        return _approved_review("S1")

    def _task(self):
        return {"prompt": "Build a thing", "answers": [], "spec_version": 1, "id": "cmp-1"}

    def _manifest(self, notes=None):
        m = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1",
                         "command": "python -m pytest tests/test_foo.py -q", "junit": None}],
            "test_files": ["tests/test_foo.py"],
        }
        if notes is not None:
            m["notes"] = notes
        return m

    def _verified_results(self, log="short log", check_id="S1-check"):
        return {
            "checks": [{
                "id": check_id, "group": "acceptance",
                "command": "python -m pytest tests/test_foo.py -q",
                "status": "passed", "exit_code": 0,
                "counts": {"passed": 1, "failed": 0},
                "classification": "passed",
                "log": log,
            }],
            "gaps": [],
        }

    async def test_implementation_diff_none_produces_no_change_message(self):
        """When implementation_diff is None → 'No implementation files changed...'"""
        from arena.test_review import review_harness_tests

        config = self._make_config()

        # We need to actually call the function but it needs a planning model.
        # Patch the agent call to return a valid response.
        from arena.test_review import TestAgentReviewOutput, TestAgentReviewScenario
        mock_output = TestAgentReviewOutput(
            scenarios=[TestAgentReviewScenario(
                scenario_id="S1", rating=1, verdict="faithful",
                reason="Good test.", evidence="tests/test_foo.py::test_basic"
            )],
            summary="Tests look good.",
            concerns=[],
        )

        class MockResult:
            class metrics:
                @staticmethod
                def get_summary():
                    return {"accumulated_usage": {}}
            structured_output = mock_output

        with patch("strands.Agent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.invoke_async = AsyncMock(return_value=MockResult())
            MockAgent.return_value = mock_agent
            result = await review_harness_tests(
                self._task(), self._approved_review(), self._manifest(),
                {"tests/test_foo.py": "def test_x(): pass"},
                self._verified_results(), config,
                implementation_diff=None,
            )
        self.assertNotIn("failure", result)
        # Verify the prompt payload included the no-change message
        call_args = mock_agent.invoke_async.call_args
        message = call_args[0][0] if call_args[0] else call_args[1].get("message", "")
        self.assertIn("No implementation files changed", message)

    async def test_implementation_diff_included_in_untrusted_block(self):
        """implementation_diff is included in the UNTRUSTED_DATA block."""
        from arena.test_review import review_harness_tests

        config = self._make_config()
        from arena.test_review import TestAgentReviewOutput, TestAgentReviewScenario
        mock_output = TestAgentReviewOutput(
            scenarios=[TestAgentReviewScenario(
                scenario_id="S1", rating=1, verdict="faithful",
                reason="Good test.", evidence="tests/test_foo.py::test_basic"
            )],
            summary="Tests look good.",
            concerns=[],
        )

        class MockResult:
            class metrics:
                @staticmethod
                def get_summary():
                    return {"accumulated_usage": {}}
            structured_output = mock_output

        diff = "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n+# changed\n"
        with patch("strands.Agent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.invoke_async = AsyncMock(return_value=MockResult())
            MockAgent.return_value = mock_agent
            await review_harness_tests(
                self._task(), self._approved_review(), self._manifest(),
                {"tests/test_foo.py": "def test_x(): pass"},
                self._verified_results(), config,
                implementation_diff=diff,
            )
        call_args = mock_agent.invoke_async.call_args
        message = call_args[0][0] if call_args[0] else ""
        # The diff should appear in the UNTRUSTED_DATA block
        payload = json.loads(message)
        diff_in_payload = payload["UNTRUSTED_DATA_FROM_HARNESS"]["implementation_diff"]
        self.assertIn("main.py", diff_in_payload)
        self.assertIn("changed", diff_in_payload)

    async def test_manifest_notes_in_untrusted_block(self):
        """Manifest notes are included inside the UNTRUSTED_DATA block, not raw instructions."""
        from arena.test_review import review_harness_tests

        config = self._make_config()
        from arena.test_review import TestAgentReviewOutput, TestAgentReviewScenario
        mock_output = TestAgentReviewOutput(
            scenarios=[TestAgentReviewScenario(
                scenario_id="S1", rating=1, verdict="faithful",
                reason="Good test.", evidence="tests/test_foo.py::test_basic"
            )],
            summary="Tests look good.",
            concerns=[],
        )

        class MockResult:
            class metrics:
                @staticmethod
                def get_summary():
                    return {"accumulated_usage": {}}
            structured_output = mock_output

        notes = "These are my test notes."
        with patch("strands.Agent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.invoke_async = AsyncMock(return_value=MockResult())
            MockAgent.return_value = mock_agent
            await review_harness_tests(
                self._task(), self._approved_review(), self._manifest(notes=notes),
                {"tests/test_foo.py": "def test_x(): pass"},
                self._verified_results(), config,
            )
        call_args = mock_agent.invoke_async.call_args
        message = call_args[0][0] if call_args[0] else ""
        payload = json.loads(message)
        # Notes should be inside UNTRUSTED_DATA_FROM_HARNESS, not at top level
        self.assertIn("UNTRUSTED_DATA_FROM_HARNESS", payload)
        manifest_in_block = payload["UNTRUSTED_DATA_FROM_HARNESS"]["manifest"]
        self.assertEqual(manifest_in_block["notes"], notes)

    async def test_32kib_per_check_log_truncated_from_top(self):
        """Per-check log > 32 KiB is truncated from the top with a marker."""
        from arena.test_review import review_harness_tests

        config = self._make_config()
        from arena.test_review import TestAgentReviewOutput, TestAgentReviewScenario
        mock_output = TestAgentReviewOutput(
            scenarios=[TestAgentReviewScenario(
                scenario_id="S1", rating=1, verdict="faithful",
                reason="Good test.", evidence="tests/test_foo.py::test_basic"
            )],
            summary="Tests look good.",
            concerns=[],
        )

        class MockResult:
            class metrics:
                @staticmethod
                def get_summary():
                    return {"accumulated_usage": {}}
            structured_output = mock_output

        # 40 KiB log — should be truncated to last 32 KiB
        big_log = "EARLY_LINE\n" + "x" * (40 * 1024) + "\nLAST_LINE"
        with patch("strands.Agent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.invoke_async = AsyncMock(return_value=MockResult())
            MockAgent.return_value = mock_agent
            await review_harness_tests(
                self._task(), self._approved_review(), self._manifest(),
                {"tests/test_foo.py": "def test_x(): pass"},
                self._verified_results(log=big_log), config,
            )
        call_args = mock_agent.invoke_async.call_args
        message = call_args[0][0] if call_args[0] else ""
        payload = json.loads(message)
        checks = payload["UNTRUSTED_DATA_FROM_HARNESS"]["verified_results"]["checks"]
        log_tail = checks[0]["log_tail"]
        self.assertIn("truncated", log_tail, "Should have truncation marker")
        self.assertNotIn("EARLY_LINE", log_tail, "Early part should be truncated")
        self.assertIn("LAST_LINE", log_tail, "Last line should be preserved")

    async def test_512kib_total_log_budget(self):
        """After 512 KiB total logs, further checks listed with status/counts only."""
        from arena.test_review import review_harness_tests

        # Build a manifest and results with many checks
        review = {
            "id": "r1",
            "proposal": {
                "summary": "Tests",
                "test_cases": [
                    {"id": f"S{i}", "title": f"Scenario {i}", "group": "acceptance",
                     "steps": ["step"], "expected_result": "ok", "test_data": "x"}
                    for i in range(20)
                ],
                "assumptions": [], "limitations": [],
            },
            "answers": {},
            "test_coverage": "comprehensive",
        }
        checks_data = [
            {"id": f"S{i}-check", "scenario_id": f"S{i}",
             "command": "python -m pytest", "junit": None}
            for i in range(20)
        ]
        manifest = {
            "version": 1,
            "checks": checks_data,
            "test_files": ["tests/test_foo.py"],
        }
        # Each check has 30 KiB log → 20 × 30 KiB = 600 KiB total → exceeds 512 KiB
        big_log = "x" * (30 * 1024)
        verified_results = {
            "checks": [
                {"id": f"S{i}-check", "group": "acceptance", "command": "python -m pytest",
                 "status": "passed", "exit_code": 0, "counts": {"passed": 1},
                 "classification": "passed", "log": big_log}
                for i in range(20)
            ],
            "gaps": [],
        }

        config_obj = Config(judge_model=None,
                            planning_model="anthropic.claude-3-5-sonnet-20241022-v2:0")

        from arena.test_review import TestAgentReviewOutput, TestAgentReviewScenario
        scenarios = [
            TestAgentReviewScenario(
                scenario_id=f"S{i}", rating=1, verdict="faithful",
                reason="ok", evidence="tests/test_foo.py::test_x"
            )
            for i in range(20)
        ]
        mock_output = TestAgentReviewOutput(scenarios=scenarios, summary="ok", concerns=[])

        class MockResult:
            class metrics:
                @staticmethod
                def get_summary():
                    return {"accumulated_usage": {}}
            structured_output = mock_output

        task = {"prompt": "Build a thing", "answers": [], "spec_version": 1, "id": "cmp-1"}
        with patch("strands.Agent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.invoke_async = AsyncMock(return_value=MockResult())
            MockAgent.return_value = mock_agent
            result = await review_harness_tests(
                task, review, manifest, {"tests/test_foo.py": "def test_x(): pass"},
                verified_results, config_obj,
            )
        self.assertNotIn("failure", result)
        # Verify that at least one check has log_omitted
        call_args = mock_agent.invoke_async.call_args
        message = call_args[0][0] if call_args[0] else ""
        payload = json.loads(message)
        checks_in_payload = payload["UNTRUSTED_DATA_FROM_HARNESS"]["verified_results"]["checks"]
        omitted = [c for c in checks_in_payload if c.get("log_omitted")]
        self.assertGreater(len(omitted), 0, "Some checks should have logs omitted after 512 KiB")

    async def test_implementation_diff_capped_at_256kib(self):
        """implementation_diff > 256 KiB is capped with a truncation marker."""
        from arena.test_review import review_harness_tests

        config = self._make_config()
        from arena.test_review import TestAgentReviewOutput, TestAgentReviewScenario
        mock_output = TestAgentReviewOutput(
            scenarios=[TestAgentReviewScenario(
                scenario_id="S1", rating=1, verdict="faithful",
                reason="ok", evidence="tests/test_foo.py::test_x"
            )],
            summary="ok.", concerns=[],
        )

        class MockResult:
            class metrics:
                @staticmethod
                def get_summary():
                    return {"accumulated_usage": {}}
            structured_output = mock_output

        # 300 KiB diff
        big_diff = "HEADER_LINE\n" + "x" * (300 * 1024) + "\nFOOTER_LINE"
        with patch("strands.Agent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.invoke_async = AsyncMock(return_value=MockResult())
            MockAgent.return_value = mock_agent
            await review_harness_tests(
                self._task(), self._approved_review(), self._manifest(),
                {"tests/test_foo.py": "def test_x(): pass"},
                self._verified_results(), config,
                implementation_diff=big_diff,
            )
        call_args = mock_agent.invoke_async.call_args
        message = call_args[0][0] if call_args[0] else ""
        payload = json.loads(message)
        diff_in_payload = payload["UNTRUSTED_DATA_FROM_HARNESS"]["implementation_diff"]
        self.assertIn("truncated", diff_in_payload,
                      "Big diff should have truncation marker")
        self.assertNotIn("FOOTER_LINE", diff_in_payload,
                         "Diff should be truncated before the footer")


# ── C6: assess_tests mandatory_failure (§9.15) ───────────────────────────────

class AssessTestsMandatoryFailureTests(unittest.TestCase):
    """C6: mandatory_failure uses raw sandbox result, not the rated value."""

    def test_rating_zero_passing_check_is_not_mandatory_failure(self):
        """A passing check of a scenario rated 0 reduces contribution but is NOT mandatory failure."""
        plan = _make_plan_dict()
        # check is "passed" with exit_code=0 (raw pass)
        evidence = _make_evidence(status="passed", exit_code=0)
        result = assess_tests(plan, evidence, ratings={"S1": 0})
        # The check passes raw; mandatory_failure should be False even with rating=0
        self.assertFalse(result["mandatory_failure"],
                         "rating-0 passing check should not be a mandatory failure")

    def test_rating_half_passing_check_is_not_mandatory_failure(self):
        """A passing check of a scenario rated 0.5 is not a mandatory failure."""
        plan = _make_plan_dict()
        evidence = _make_evidence(status="passed", exit_code=0)
        result = assess_tests(plan, evidence, ratings={"S1": 0.5})
        self.assertFalse(result["mandatory_failure"],
                         "rating-0.5 passing check should not be a mandatory failure")

    def test_failing_check_is_mandatory_failure_regardless_of_rating(self):
        """A failing check (exit_code != 0) IS a mandatory failure regardless of rating."""
        plan = _make_plan_dict()
        evidence = _make_evidence(status="failed", exit_code=1)
        result_with_rating = assess_tests(plan, evidence, ratings={"S1": 1})
        self.assertTrue(result_with_rating["mandatory_failure"],
                        "failing check should be mandatory failure even with rating=1")
        result_legacy = assess_tests(plan, evidence, ratings=None)
        self.assertTrue(result_legacy["mandatory_failure"],
                        "failing check should be mandatory failure in legacy mode")

    def test_legacy_failing_check_is_mandatory_failure(self):
        plan = _make_plan_dict()
        evidence = _make_evidence(status="failed", exit_code=1)
        result = assess_tests(plan, evidence, ratings=None)
        self.assertTrue(result["mandatory_failure"])


# ── C7: assess_accuracy rating-0 skipped checks (§9.15) ─────────────────────

class AssessAccuracyRatingZeroSkippedTests(unittest.TestCase):
    """C7: rating-0 scenario's skipped checks → unmet, not unavailable."""

    def _make_skipped_evidence(self, check_id="S1-check"):
        return {
            "checks": [{
                "id": check_id, "group": "acceptance", "command": "echo",
                "status": "passed", "exit_code": 0,
                "counts": {"passed": 0, "failed": 0, "errored": 0, "skipped": 1, "not_run": 0},
                "scored": True, "mandatory": True, "weight": 1.0,
                "classification": "skipped",
            }],
            "gaps": [],
        }

    def test_rating_zero_with_skipped_check_is_unmet_not_unavailable(self):
        plan = _make_plan_dict()
        evidence = self._make_skipped_evidence()
        result = assess_accuracy(plan, evidence, _dummy_source_review(), ratings={"S1": 0})
        criterion = result["criteria"][0]
        self.assertEqual(criterion["status"], "unmet",
                         f"rating-0 skipped check should be 'unmet', got {criterion['status']}")
        # score should be 0 (not None / incomplete)
        self.assertEqual(result["score"], 0.0)
        self.assertTrue(result["complete"],
                        "With rating=0, criteria should not be marked incomplete")

    def test_rating_nonzero_with_skipped_check_is_still_unavailable(self):
        """With a non-zero rating, skipped checks still make criterion unavailable."""
        plan = _make_plan_dict()
        evidence = self._make_skipped_evidence()
        result = assess_accuracy(plan, evidence, _dummy_source_review(), ratings={"S1": 1})
        criterion = result["criteria"][0]
        self.assertEqual(criterion["status"], "unverified",
                         f"rating-1 skipped check should still be 'unverified': {criterion['status']}")
        # score should be None (incomplete)
        self.assertIsNone(result["score"])

    def test_legacy_mode_skipped_check_is_unavailable(self):
        """Legacy mode (ratings=None): skipped checks still make criterion unavailable."""
        plan = _make_plan_dict()
        evidence = self._make_skipped_evidence()
        result = assess_accuracy(plan, evidence, _dummy_source_review(), ratings=None)
        criterion = result["criteria"][0]
        self.assertEqual(criterion["status"], "unverified")


# ── C8: evaluate_candidate incomplete_reason (interface 1) ───────────────────

class EvaluateCandidateIncompleteReasonTests(unittest.TestCase):
    """C8: incomplete_reason → accuracy/tests None → evaluated_incomplete."""

    def _run(self, **kwargs):
        from arena.evaluation import evaluate_candidate, Plan, Criterion
        plan = Plan(
            criteria=[Criterion(id="task", requirement="Do the thing")],
            checks=[],
            source="test",
        )
        candidate = {
            "snapshot_sha256": "abc123",
            "diff": "--- a\n+++ b\n@@ -1 +1 @@\n+change\n",
            "files": [{"path": "foo.py", "content": "x = 1\n"}],
            "evidence": {"checks": [], "gaps": []},
            "repository_context": [],
        }
        config = Config(judge_model=None)
        task = {"prompt": "Do the thing", "answers": [], "spec_version": 1, "id": "cmp-1"}
        return asyncio.run(
            evaluate_candidate(task, plan, candidate, config, **kwargs)
        )

    def test_incomplete_reason_gives_accuracy_none(self):
        result = self._run(incomplete_reason="Test agent review failed.")
        self.assertIsNone(result["accuracy"]["score"])

    def test_incomplete_reason_gives_tests_none(self):
        result = self._run(incomplete_reason="Test agent review failed.")
        self.assertIsNone(result["tests"]["score"])

    def test_incomplete_reason_gives_overall_none(self):
        result = self._run(incomplete_reason="Test agent review failed.")
        self.assertIsNone(result["overall"])

    def test_incomplete_reason_records_reason_in_gaps(self):
        reason = "Infrastructure prevented the test stage."
        result = self._run(incomplete_reason=reason)
        self.assertIn(reason, result["accuracy"]["gaps"])
        self.assertIn(reason, result["tests"]["gaps"])

    def test_incomplete_reason_verified_complete_is_false(self):
        result = self._run(incomplete_reason="Review failed.")
        self.assertFalse(result["verified_complete"])

    def test_no_manifest_overall_always_numeric(self):
        """C8: no_manifest_reason → overall is numeric even when no judge."""
        result = self._run(no_manifest_reason="Budget exhausted.")
        self.assertIsNotNone(result["overall"],
                             "no_manifest_reason should produce numeric overall (relevance=0)")
        self.assertIsInstance(result["overall"], float)

    def test_no_manifest_overall_is_zero_without_relevance(self):
        """C8: without judge, relevance is 0 → overall = 0.45*0 + 0.20*0 + 0.35*0 = 0."""
        result = self._run(no_manifest_reason="Budget exhausted.")
        self.assertEqual(result["overall"], 0.0)


# ── C9: RUBRIC_VERSION for new-flow plans ────────────────────────────────────

class RubricVersionTests(unittest.TestCase):
    """C9: compile_reviewed_tests sets rubric_version = 'harness-tests-rubric-1'."""

    def _compile(self, scenario_id="S1"):
        from arena.test_review import ReviewedCheck, ReviewedTestCode, compile_reviewed_tests
        code = ReviewedTestCode(
            checks=[ReviewedCheck(
                id=f"{scenario_id}-check", scenario_id=scenario_id,
                command="python -m pytest tests/test_foo.py -q", junit=None
            )],
            acceptance_files={"tests/test_foo.py": "def test_foo(): assert True\n"},
        )
        review = _approved_review(scenario_id)
        return compile_reviewed_tests(code, review)

    def test_new_flow_plan_has_harness_rubric_version(self):
        from arena.test_review import HARNESS_TESTS_RUBRIC_VERSION
        plan = self._compile()
        self.assertEqual(plan.rubric_version, HARNESS_TESTS_RUBRIC_VERSION,
                         f"New-flow plan should have rubric_version={HARNESS_TESTS_RUBRIC_VERSION!r}")

    def test_harness_rubric_version_constant(self):
        from arena.test_review import HARNESS_TESTS_RUBRIC_VERSION
        self.assertEqual(HARNESS_TESTS_RUBRIC_VERSION, "harness-tests-rubric-1")

    def test_legacy_rubric_version_unchanged(self):
        from arena.evaluation import RUBRIC_VERSION
        self.assertEqual(RUBRIC_VERSION, "implementation-rubric-1")

    def test_evaluate_candidate_uses_plan_rubric_version(self):
        """evaluate_candidate returns the plan's rubric_version, not the global."""
        import asyncio
        from arena.evaluation import evaluate_candidate, Plan, Criterion, RUBRIC_VERSION
        from arena.test_review import HARNESS_TESTS_RUBRIC_VERSION, compile_reviewed_tests, ReviewedCheck, ReviewedTestCode

        # Create a new-flow plan
        code = ReviewedTestCode(
            checks=[ReviewedCheck(id="S1-check", scenario_id="S1",
                                   command="python -m pytest tests/test_foo.py -q")],
            acceptance_files={"tests/test_foo.py": "def test_foo(): assert True\n"},
        )
        new_flow_plan = compile_reviewed_tests(code, _approved_review("S1"))

        candidate = {
            "snapshot_sha256": "abc",
            "diff": "--- a\n+++ b\n@@ -1 +1 @@\n+x\n",
            "files": [{"path": "foo.py", "content": "x = 1\n"}],
            "evidence": {"checks": [], "gaps": []},
            "repository_context": [],
        }
        config = Config(judge_model=None)
        task = {"prompt": "Do the thing", "answers": [], "spec_version": 1, "id": "cmp-1"}
        result = asyncio.run(
            evaluate_candidate(task, new_flow_plan, candidate, config,
                               no_manifest_reason="test")
        )
        self.assertEqual(result["rubric_version"], HARNESS_TESTS_RUBRIC_VERSION)


# ── R2-8: Review timeout message includes elapsed seconds ────────────────────

class ReviewTimeoutMessageTests(unittest.IsolatedAsyncioTestCase):
    """R2-8: §10 timeout message must include '({seconds} s)' between attempt count and period."""

    def _make_config(self):
        return Config(
            judge_model=None,
            planning_model="anthropic.claude-3-5-sonnet-20241022-v2:0",
            judge_max_attempts=1,
        )

    async def test_timeout_message_includes_elapsed_seconds(self):
        """When review times out, message contains '(N s)' with elapsed seconds."""
        from arena.test_review import review_harness_tests

        config = self._make_config()
        task = {"prompt": "Build a thing", "answers": [], "spec_version": 1, "id": "cmp-1"}
        review = _approved_review()
        manifest = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1",
                         "command": "python -m pytest tests/test_foo.py -q", "junit": None}],
            "test_files": ["tests/test_foo.py"],
        }
        verified = {
            "checks": [{"id": "S1-check", "group": "acceptance", "command": "...",
                         "status": "passed", "exit_code": 0, "counts": {}, "log": "",
                         "classification": "passed"}],
            "gaps": [],
        }

        with patch("strands.Agent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.invoke_async = AsyncMock(side_effect=TimeoutError("timed out"))
            MockAgent.return_value = mock_agent
            result = await review_harness_tests(task, review, manifest, {}, verified, config)

        self.assertIn("failure", result)
        self.assertEqual(result["failure"]["code"], "timeout")
        msg = result["failure"]["message"]
        self.assertIn("timed out", msg.lower())
        # §10 template: "timed out after {attempts} attempt(s) ({seconds} s)."
        import re
        self.assertTrue(
            re.search(r"attempt\(s\)\s+\(\d+ s\)", msg),
            f"Timeout message should include '(N s)' after attempt count, got: {msg!r}",
        )

    async def test_timeout_message_seconds_format(self):
        """The elapsed seconds in the message are rounded to a whole number."""
        from arena.test_review import review_harness_tests

        config = self._make_config()
        task = {"prompt": "Build a thing", "answers": [], "spec_version": 1, "id": "cmp-1"}
        review = _approved_review()
        manifest = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1",
                         "command": "python -m pytest tests/test_foo.py -q", "junit": None}],
            "test_files": ["tests/test_foo.py"],
        }
        verified: dict = {"checks": [], "gaps": []}

        with patch("strands.Agent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.invoke_async = AsyncMock(side_effect=TimeoutError("timed out"))
            MockAgent.return_value = mock_agent
            result = await review_harness_tests(task, review, manifest, {}, verified, config)

        self.assertIn("failure", result)
        msg = result["failure"]["message"]
        import re
        # Must match '(N s)' where N is an integer (no decimal point)
        m = re.search(r"\((\d+) s\)", msg)
        self.assertIsNotNone(m, f"Expected '(N s)' in message, got: {msg!r}")

    async def test_non_timeout_failure_message_unchanged(self):
        """api_error message does NOT include elapsed seconds in the parenthetical form."""
        from arena.test_review import review_harness_tests

        config = self._make_config()
        task = {"prompt": "Build a thing", "answers": [], "spec_version": 1, "id": "cmp-1"}
        review = _approved_review()
        manifest = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1",
                         "command": "python -m pytest tests/test_foo.py -q", "junit": None}],
            "test_files": ["tests/test_foo.py"],
        }
        verified: dict = {"checks": [], "gaps": []}

        with patch("strands.Agent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.invoke_async = AsyncMock(side_effect=RuntimeError("some API error"))
            MockAgent.return_value = mock_agent
            result = await review_harness_tests(task, review, manifest, {}, verified, config)

        self.assertIn("failure", result)
        self.assertEqual(result["failure"]["code"], "api_error")
        # api_error does not need elapsed seconds
        msg = result["failure"]["message"]
        self.assertIn("API error", msg)


# ── R2-9(a): bare interpreter invocation workspace check ─────────────────────

class InterpreterInvocationIssuesTests(unittest.TestCase):
    """R2-9(a): interpreter invoking a workspace file by bare name must list it in test_files."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "workspace"
        self.ws.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def _build(self, command, test_files=None, extra_files=None):
        """Build a plan for the given command; always creates tests/test_foo.py."""
        _make_test_file(self.ws, "tests/test_foo.py")
        if extra_files:
            for rel_path, content in extra_files.items():
                p = self.ws / rel_path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
        review = _approved_review("S1")
        manifest = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1", "command": command, "junit": None}],
            "test_files": test_files if test_files is not None else ["tests/test_foo.py"],
        }
        return build_plan(self.ws, manifest, review)

    # ── python main.py ────────────────────────────────────────────────────────

    def test_python_bare_name_not_in_test_files_is_error(self):
        """python main.py where main.py is in workspace but not test_files → error."""
        (self.ws / "main.py").write_text("print('hello')\n", encoding="utf-8")
        plan, issues = self._build("python main.py", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("main.py" in i for i in issues),
            f"python main.py not in test_files should be error: {issues}",
        )

    def test_python3_bare_name_not_in_test_files_is_error(self):
        """python3 main.py where main.py is in workspace but not test_files → error."""
        (self.ws / "main.py").write_text("print('hello')\n", encoding="utf-8")
        plan, issues = self._build("python3 main.py", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("main.py" in i for i in issues),
            f"python3 main.py not in test_files should be error: {issues}",
        )

    def test_python_bare_name_in_test_files_is_allowed(self):
        """python test_foo.py where test_foo.py IS in test_files → allowed."""
        plan, issues = self._build(
            "python tests/test_foo.py",
            test_files=["tests/test_foo.py"],
        )
        # test_foo.py has a path separator so it's handled by C4; either way it
        # should be allowed since it's in test_files.
        self.assertFalse(
            any("test_foo.py" in i and "test_files" in i for i in issues),
            f"test file in test_files should be allowed: {issues}",
        )

    def test_python_bare_name_root_level_in_test_files_is_allowed(self):
        """python run_tests.py where run_tests.py is listed in test_files → allowed."""
        (self.ws / "run_tests.py").write_text("pass\n", encoding="utf-8")
        plan, issues = self._build(
            "python run_tests.py",
            test_files=["tests/test_foo.py", "run_tests.py"],
        )
        self.assertFalse(
            any("run_tests.py" in i and "test_files" in i for i in issues),
            f"run_tests.py in test_files should be allowed: {issues}",
        )

    def test_python_bare_name_nonexistent_file_is_not_flagged(self):
        """python main.py where main.py does NOT exist in workspace → no error."""
        # main.py is never created
        plan, issues = self._build("python main.py", test_files=["tests/test_foo.py"])
        self.assertFalse(
            any("main.py" in i and "test_files" in i for i in issues),
            f"Non-existent file should not be flagged: {issues}",
        )

    # ── bash / sh run.sh ──────────────────────────────────────────────────────

    def test_bash_bare_name_not_in_test_files_is_error(self):
        """bash run.sh where run.sh is in workspace but not test_files → error."""
        (self.ws / "run.sh").write_text("#!/bin/bash\npython main.py\n", encoding="utf-8")
        plan, issues = self._build("bash run.sh", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("run.sh" in i for i in issues),
            f"bash run.sh not in test_files should be error: {issues}",
        )

    def test_sh_bare_name_not_in_test_files_is_error(self):
        """sh run.sh where run.sh is in workspace but not test_files → error."""
        (self.ws / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        plan, issues = self._build("sh run.sh", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("run.sh" in i for i in issues),
            f"sh run.sh not in test_files should be error: {issues}",
        )

    def test_bash_bare_name_in_test_files_is_allowed(self):
        """bash run.sh where run.sh IS in test_files → allowed."""
        (self.ws / "run.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        plan, issues = self._build("bash run.sh", test_files=["tests/test_foo.py", "run.sh"])
        self.assertFalse(
            any("run.sh" in i and "test_files" in i for i in issues),
            f"run.sh in test_files should be allowed: {issues}",
        )

    def test_bash_bare_name_nonexistent_file_is_not_flagged(self):
        """bash run.sh where run.sh does NOT exist in workspace → no error."""
        plan, issues = self._build("bash run.sh", test_files=["tests/test_foo.py"])
        self.assertFalse(
            any("run.sh" in i and "test_files" in i for i in issues),
            f"Non-existent file should not be flagged: {issues}",
        )

    # ── node x.js ─────────────────────────────────────────────────────────────

    def test_node_bare_name_not_in_test_files_is_error(self):
        """node x.js where x.js is in workspace but not test_files → error."""
        (self.ws / "x.js").write_text("process.exit(0);\n", encoding="utf-8")
        plan, issues = self._build("node x.js", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("x.js" in i for i in issues),
            f"node x.js not in test_files should be error: {issues}",
        )

    def test_node_bare_name_in_test_files_is_allowed(self):
        """node x.js where x.js IS in test_files → allowed."""
        (self.ws / "x.js").write_text("process.exit(0);\n", encoding="utf-8")
        plan, issues = self._build("node x.js", test_files=["tests/test_foo.py", "x.js"])
        self.assertFalse(
            any("x.js" in i and "test_files" in i for i in issues),
            f"x.js in test_files should be allowed: {issues}",
        )

    # ── python -m runner (not affected by R2-9(a)) ────────────────────────────

    def test_python_m_pytest_is_still_allowed(self):
        """python -m pytest tests/test_foo.py -q → allowed (not R2-9(a), handled elsewhere)."""
        (self.ws / "main.py").write_text("print('hello')\n", encoding="utf-8")
        plan, issues = self._build(
            "python -m pytest tests/test_foo.py -q",
            test_files=["tests/test_foo.py"],
        )
        self.assertFalse(
            any("pytest" in i and "test_files" in i for i in issues),
            f"python -m pytest should be allowed: {issues}",
        )

    def test_python_m_does_not_trigger_r2_9a(self):
        """python -m myapp where myapp.py exists → R2-9(a) skips; R2-9(b) handles it."""
        (self.ws / "myapp.py").write_text("pass\n", encoding="utf-8")
        plan, issues = self._build("python -m myapp", test_files=["tests/test_foo.py"])
        # R2-9(a) should not fire for python -m invocations (R2-9(b) handles that).
        # The issue should mention -m (from R2-9(b)), not plain interpreter invocation.
        if any("myapp" in i for i in issues):
            self.assertTrue(
                any("-m" in i or "module" in i.lower() for i in issues),
                f"R2-9(a) should not fire for python -m; R2-9(b) should: {issues}",
            )

    def test_error_message_names_the_file(self):
        """The validation error message names the workspace file."""
        (self.ws / "main.py").write_text("pass\n", encoding="utf-8")
        plan, issues = self._build("python main.py", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("main.py" in i for i in issues),
            f"Error message should name the file: {issues}",
        )


# ── R2-9(b): python -m X workspace module check ───────────────────────────────

class ModuleInvocationIssuesTests(unittest.TestCase):
    """R2-9(b): python -m X where X resolves to workspace module must be in test_files."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "workspace"
        self.ws.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def _build(self, command, test_files=None):
        _make_test_file(self.ws, "tests/test_foo.py")
        review = _approved_review("S1")
        manifest = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1", "command": command, "junit": None}],
            "test_files": test_files if test_files is not None else ["tests/test_foo.py"],
        }
        return build_plan(self.ws, manifest, review)

    def test_python_m_pytest_on_listed_file_is_allowed(self):
        """python -m pytest tests/test_foo.py -q → allowed (pytest is known runner)."""
        plan, issues = self._build("python -m pytest tests/test_foo.py -q")
        self.assertFalse(
            any("pytest" in i and "test_files" in i for i in issues),
            f"pytest invocation should be allowed: {issues}",
        )

    def test_python3_m_pytest_is_allowed(self):
        """python3 -m pytest tests/test_foo.py → allowed."""
        plan, issues = self._build("python3 -m pytest tests/test_foo.py -q")
        self.assertFalse(
            any("pytest" in i and "test_files" in i for i in issues),
            f"python3 -m pytest should be allowed: {issues}",
        )

    def test_python_m_unittest_is_allowed(self):
        """python -m unittest → allowed (known runner)."""
        plan, issues = self._build("python -m unittest discover -s tests")
        self.assertFalse(
            any("unittest" in i and "test_files" in i for i in issues),
            f"python -m unittest should be allowed: {issues}",
        )

    def test_python_m_coverage_is_allowed(self):
        """python -m coverage run -m pytest → allowed (coverage is a known runner)."""
        plan, issues = self._build("python -m coverage run -m pytest tests/test_foo.py -q")
        self.assertFalse(
            any("coverage" in i and "test_files" in i for i in issues),
            f"python -m coverage should be allowed: {issues}",
        )

    def test_python_m_workspace_module_not_in_test_files_is_error(self):
        """python -m myapp where myapp.py is in workspace but not test_files → error."""
        myapp = self.ws / "myapp.py"
        myapp.write_text("def main(): pass\n", encoding="utf-8")
        plan, issues = self._build("python -m myapp", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("myapp" in i for i in issues),
            f"workspace module not in test_files should be an error: {issues}",
        )

    def test_python_m_workspace_module_in_test_files_is_ok(self):
        """python -m myapp where myapp.py is in workspace AND in test_files → allowed."""
        myapp = self.ws / "myapp.py"
        myapp.write_text("def main(): pass\n", encoding="utf-8")
        plan, issues = self._build(
            "python -m myapp", test_files=["tests/test_foo.py", "myapp.py"]
        )
        self.assertFalse(
            any("myapp" in i and "test_files" in i for i in issues),
            f"module in test_files should not be an error: {issues}",
        )

    def test_python_m_package_dunder_main_not_in_test_files_is_error(self):
        """python -m mypackage where mypackage/__main__.py exists → error if not in test_files."""
        pkg_dir = self.ws / "mypackage"
        pkg_dir.mkdir()
        (pkg_dir / "__main__.py").write_text("pass\n", encoding="utf-8")
        plan, issues = self._build("python -m mypackage", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("mypackage" in i for i in issues),
            f"package with __main__.py not in test_files should be error: {issues}",
        )

    def test_python_m_nonexistent_module_not_flagged(self):
        """python -m nonexistent where nonexistent.py doesn't exist → no error."""
        plan, issues = self._build("python -m nonexistent", test_files=["tests/test_foo.py"])
        self.assertFalse(
            any("nonexistent" in i and "test_files" in i for i in issues),
            f"Non-existent module should not be flagged: {issues}",
        )

    def test_python_m_nested_module_checked(self):
        """python -m mypackage.module where mypackage/module.py exists → error if not in test_files."""
        pkg_dir = self.ws / "mypackage"
        pkg_dir.mkdir()
        (pkg_dir / "module.py").write_text("pass\n", encoding="utf-8")
        plan, issues = self._build("python -m mypackage.module", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("mypackage" in i for i in issues),
            f"nested module not in test_files should be error: {issues}",
        )

    def test_pytest_m_flag_in_pytest_args_not_confused_with_python_m(self):
        """python -m pytest tests/test_foo.py -m mymarker → pytest is runner; -m mymarker not checked."""
        plan, issues = self._build(
            "python -m pytest tests/test_foo.py -m mymarker -q",
            test_files=["tests/test_foo.py"],
        )
        self.assertFalse(
            any("mymarker" in i and "test_files" in i for i in issues),
            f"-m flag in pytest args should not be mis-parsed: {issues}",
        )

    def test_locked_test_running_implementation_via_subprocess_is_ok(self):
        """A test that internally calls subprocess is fine — only manifest command is checked.

        'python -m pytest tests/test_foo.py' is allowed even when the test file
        internally runs the program under test via subprocess.run(['python', 'main.py']).
        The command rule applies only to the manifest's check command, not to what
        the test code does internally.
        """
        (self.ws / "main.py").write_text("print('hello')\n", encoding="utf-8")
        test_content = (
            "import subprocess\n"
            "def test_main():\n"
            "    result = subprocess.run(['python', 'main.py'], capture_output=True)\n"
            "    assert result.returncode == 0\n"
        )
        _make_test_file(self.ws, "tests/test_foo.py", test_content)
        plan, issues = self._build(
            "python -m pytest tests/test_foo.py -q",
            test_files=["tests/test_foo.py"],
        )
        self.assertFalse(
            any("pytest" in i and "test_files" in i for i in issues),
            f"pytest command should be allowed even when test calls main.py: {issues}",
        )


# ── R2-9: Build tool defining-file check ─────────────────────────────────────

class BuildToolIssuesTests(unittest.TestCase):
    """R2-9: build tool commands require their defining file to be in test_files."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "workspace"
        self.ws.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def _build(self, command, test_files=None):
        _make_test_file(self.ws, "tests/test_foo.py")
        review = _approved_review("S1")
        manifest = {
            "version": 1,
            "checks": [{"id": "S1-check", "scenario_id": "S1", "command": command, "junit": None}],
            "test_files": test_files if test_files is not None else ["tests/test_foo.py"],
        }
        return build_plan(self.ws, manifest, review)

    # ── make ──────────────────────────────────────────────────────────────────

    def test_make_without_makefile_in_workspace_is_ok(self):
        """make test when no Makefile exists → allowed (not workspace-defined)."""
        plan, issues = self._build("make test")
        self.assertFalse(
            any("Makefile" in i for i in issues),
            f"make without Makefile should be allowed: {issues}",
        )

    def test_make_with_unlocked_makefile_is_error(self):
        """make test with Makefile in workspace not in test_files → error."""
        (self.ws / "Makefile").write_text("test:\n\tpytest\n", encoding="utf-8")
        plan, issues = self._build("make test", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("Makefile" in i for i in issues),
            f"Makefile not in test_files should be error: {issues}",
        )

    def test_make_with_locked_makefile_is_ok(self):
        """make test with Makefile in test_files → allowed."""
        (self.ws / "Makefile").write_text("test:\n\tpytest\n", encoding="utf-8")
        plan, issues = self._build("make test", test_files=["tests/test_foo.py", "Makefile"])
        self.assertFalse(
            any("Makefile" in i and "test_files" in i for i in issues),
            f"Makefile in test_files should be allowed: {issues}",
        )

    # ── tox ───────────────────────────────────────────────────────────────────

    def test_tox_without_tox_ini_in_workspace_is_ok(self):
        """tox when no tox.ini exists → allowed."""
        plan, issues = self._build("tox")
        self.assertFalse(
            any("tox.ini" in i for i in issues),
            f"tox without tox.ini should be allowed: {issues}",
        )

    def test_tox_with_unlocked_tox_ini_is_error(self):
        """tox with tox.ini in workspace not in test_files → error."""
        (self.ws / "tox.ini").write_text("[tox]\n", encoding="utf-8")
        plan, issues = self._build("tox", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("tox.ini" in i for i in issues),
            f"tox.ini not in test_files should be error: {issues}",
        )

    def test_tox_with_locked_tox_ini_is_ok(self):
        """tox with tox.ini in test_files → allowed."""
        (self.ws / "tox.ini").write_text("[tox]\n", encoding="utf-8")
        plan, issues = self._build("tox", test_files=["tests/test_foo.py", "tox.ini"])
        self.assertFalse(
            any("tox.ini" in i and "test_files" in i for i in issues),
            f"tox.ini in test_files should be allowed: {issues}",
        )

    # ── nox ───────────────────────────────────────────────────────────────────

    def test_nox_without_noxfile_is_ok(self):
        """nox when no noxfile.py → allowed."""
        plan, issues = self._build("nox")
        self.assertFalse(
            any("noxfile" in i for i in issues),
            f"nox without noxfile.py should be allowed: {issues}",
        )

    def test_nox_with_unlocked_noxfile_is_error(self):
        """nox with noxfile.py not in test_files → error."""
        (self.ws / "noxfile.py").write_text("import nox\n", encoding="utf-8")
        plan, issues = self._build("nox", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("noxfile.py" in i for i in issues),
            f"noxfile.py not in test_files should be error: {issues}",
        )

    def test_nox_with_locked_noxfile_is_ok(self):
        """nox with noxfile.py in test_files → allowed."""
        (self.ws / "noxfile.py").write_text("import nox\n", encoding="utf-8")
        plan, issues = self._build("nox", test_files=["tests/test_foo.py", "noxfile.py"])
        self.assertFalse(
            any("noxfile.py" in i and "test_files" in i for i in issues),
            f"noxfile.py in test_files should be allowed: {issues}",
        )

    # ── npm/yarn/pnpm ─────────────────────────────────────────────────────────

    def test_npm_run_test_with_unlocked_package_json_is_error(self):
        """npm run test with package.json not in test_files → error."""
        (self.ws / "package.json").write_text('{"scripts":{"test":"jest"}}', encoding="utf-8")
        plan, issues = self._build("npm run test", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("package.json" in i for i in issues),
            f"package.json not in test_files should be error: {issues}",
        )

    def test_npm_test_with_unlocked_package_json_is_error(self):
        """npm test with package.json not in test_files → error."""
        (self.ws / "package.json").write_text('{"scripts":{"test":"jest"}}', encoding="utf-8")
        plan, issues = self._build("npm test", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("package.json" in i for i in issues),
            f"npm test with unlocked package.json should be error: {issues}",
        )

    def test_npm_run_with_locked_package_json_is_ok(self):
        """npm run test with package.json in test_files → allowed."""
        (self.ws / "package.json").write_text('{"scripts":{"test":"jest"}}', encoding="utf-8")
        plan, issues = self._build(
            "npm run test", test_files=["tests/test_foo.py", "package.json"]
        )
        self.assertFalse(
            any("package.json" in i and "test_files" in i for i in issues),
            f"package.json in test_files should be allowed: {issues}",
        )

    def test_yarn_test_with_unlocked_package_json_is_error(self):
        """yarn test with package.json not in test_files → error."""
        (self.ws / "package.json").write_text('{"scripts":{"test":"jest"}}', encoding="utf-8")
        plan, issues = self._build("yarn test", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("package.json" in i for i in issues),
            f"yarn test with unlocked package.json should be error: {issues}",
        )

    def test_pnpm_run_test_with_unlocked_package_json_is_error(self):
        """pnpm run test with package.json not in test_files → error."""
        (self.ws / "package.json").write_text('{"scripts":{"test":"jest"}}', encoding="utf-8")
        plan, issues = self._build("pnpm run test", test_files=["tests/test_foo.py"])
        self.assertIsNone(plan)
        self.assertTrue(
            any("package.json" in i for i in issues),
            f"pnpm run test with unlocked package.json should be error: {issues}",
        )

    def test_npm_install_does_not_trigger_check(self):
        """npm install is not 'run' or 'test' → not checked."""
        (self.ws / "package.json").write_text('{}', encoding="utf-8")
        plan, issues = self._build("npm install", test_files=["tests/test_foo.py"])
        self.assertFalse(
            any("package.json" in i for i in issues),
            f"npm install should not trigger defining-file check: {issues}",
        )

    def test_npm_without_package_json_is_ok(self):
        """npm run test when no package.json in workspace → allowed."""
        plan, issues = self._build("npm run test")
        self.assertFalse(
            any("package.json" in i for i in issues),
            f"npm run without package.json should be allowed: {issues}",
        )


if __name__ == "__main__":
    unittest.main()
