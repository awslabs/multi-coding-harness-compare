# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import json

TEMPLATE_VERSION = "mandatory-2"
MANDATORY = """BEGIN MANDATORY EXECUTION INSTRUCTIONS

Complete the user's development task autonomously within your assigned workspace and configured permissions.

Before coding, inspect the relevant repository context. Ask the user through the provided clarification channel when missing information materially affects correctness or scope. Otherwise state reasonable assumptions and proceed.

Use your available native capabilities where useful, including MCP servers, tools, skills, repository instructions, subagents, builds, debugging, and validation.

Implement the smallest, simplest maintainable solution that satisfies the task. Reuse existing patterns and dependencies. Avoid unrelated changes, speculative features, unnecessary abstractions, and broad refactoring.

Create or update meaningful tests for the requested task when feasible. Cover requested behavior and relevant edge cases. For bug fixes, add a regression test when practical.

Run relevant tests and checks. Investigate failures and fix problems introduced by your changes.

Do not weaken or delete tests merely to obtain a passing result. Do not claim that checks passed unless they actually ran successfully.

If automated testing is impractical, explain why and document the alternative validation performed.

Continue through implementation and verification. Do not stop after proposing a plan or producing code snippets.

Respect configured execution boundaries and use the provided channel for necessary permissions or essential missing information.

At completion, report:
- What changed.
- Acceptance criteria satisfied.
- Files changed.
- Tests and checks executed, with actual outcomes.
- Remaining limitations or blockers.
- Locations of the implementation and diff.

Do not invent token counts, timing measurements, test results, or evaluation scores. The application collects and verifies these independently.

END MANDATORY EXECUTION INSTRUCTIONS"""


READINESS_REMINDER = (
    "\n\nREADINESS WAS NOT RECORDED. Your previous clarification turn finished without "
    "calling the arena ready tool, so development cannot start. Do not ask further "
    "questions and do not modify project files. Call arena ready now with your "
    "assumptions as its only argument, then finish this turn."
)

# ── Test-stage MANDATORY block (write / repair / fix turns) ──────────────────

MANDATORY_TEST = """BEGIN MANDATORY TEST INSTRUCTIONS

Write, run, and fix tests for the approved specification in your assigned workspace.

Derive every expected value from the specification's expected_result and test_data fields.
Never derive expected values from what the implementation currently returns.

Write tests for EVERY approved scenario using this project's own test suite and conventions.
Write the manifest .mchc-tests.json at the workspace root (see schema below).
Run the tests. When a test fails, fix the IMPLEMENTATION — not the test.
Only change a test file if the test cannot run or genuinely does not match the specification.
Never weaken, skip, or delete tests merely to get them to pass.

When this turn ends, the application locks the test files listed in the manifest, re-runs
them in its own isolated sandbox, and sends back any failures for you to fix in the
implementation.

Manifest schema (.mchc-tests.json at workspace root):
{
  "version": 1,
  "checks": [
    {"id": "S1-basic", "scenario_id": "S1",
     "command": "python -m pytest tests/test_slug.py::test_basic -q",
     "junit": null}
  ],
  "test_files": ["tests/test_slug.py", "tests/conftest.py"],
  "python_requirements": [],
  "notes": "optional free text"
}

Rules for the manifest:
- Every approved scenario_id must appear in at least one check.
- scenario_id values must exactly match approved scenario IDs in the specification.
- commands run with the workspace as the working directory; use relative paths.
- test_files lists every file that makes up the tests (modules, fixtures, conftest, data).
  These files will be locked after this turn; do not list implementation files here.
- paths in test_files must be relative, inside the workspace, with no ".." components.

Do not invent token counts, timing measurements, test results, or evaluation scores.
The application re-runs the tests independently and verifies the results.

END MANDATORY TEST INSTRUCTIONS"""


def follow_ups(task: dict) -> list[dict]:
    """The user's follow-ups for a comparison, oldest first, as {"id", "text"}.

    Read from the shared answers, so the raw store record and the API report agree.
    Records from before follow-ups were marked are recognised by their question.
    """
    return [{"id": a["question_id"], "text": a["answer"],
             **({"harnesses": a["harnesses"]} if "harnesses" in a else {})}
            for a in task.get("answers", [])
            if a.get("kind") == "follow_up" or a.get("question") == "User specification revision"]


def for_harness(task: dict, harness: str) -> dict:
    """Keep routing attached to history so skipped instructions cannot leak on retry."""
    return {**task, "answers": [a for a in task.get("answers", [])
                                if "harnesses" not in a or harness in a["harnesses"]]}


def effective_prompt(task: dict, phase: str, plan: dict | None = None,
                     readiness_reminder: bool = False, follow_ups: list | None = None,
                     approved_review: dict | None = None) -> str:
    """Generate the prompt for a harness turn.

    Handles clarify, develop, and write_tests phases.  Repair and fix turns use
    repair_tests_prompt() / fix_tests_prompt() instead.
    """
    if phase == "write_tests":
        return _write_tests_prompt(task, approved_review)
    intro = (
        "INITIAL CLARIFICATION ONLY. Inspect the read-only repository. Do not modify code. "
        "Use the arena MCP ask_user tool for material questions. Then call arena ready "
        "exactly once with your assumptions and finish this turn. The ready call is required "
        "to end this phase even when you have no questions at all. "
        "Implementation and verification in the mandatory instructions occur in the next phase."
        if phase == "clarify" else
        "DEVELOPMENT. Implement and verify the frozen specification below. "
        "Continue independently through implementation, tests, fixes, and the final report. "
        "Choose reasonable defaults for ordinary implementation decisions and document assumptions. "
        "Do not wait for another harness, an evaluation plan, or a separate instruction to proceed. "
        "Use arena ask_user only for a genuinely blocking requirement or a required permission. "
        "A requirement answer creates a shared specification revision; finish only against "
        "the current revision. Tools execute inside your assigned OS sandbox. "
        "Use the current working directory as the project root. For local services, use "
        "the assigned PORT or ARENA_PORTS environment variables."
    )
    follow_up_block = ""
    if phase != "clarify" and follow_ups:
        items = "\n".join(f"{i + 1}. {fu['text']}" for i, fu in enumerate(follow_ups))
        follow_up_block = (
            "\n\nFOLLOW-UP INSTRUCTIONS FROM THE USER — selected for this harness; "
            "act on all of them (later ones win where they conflict):\n" + items
        )
    specification = {
        "original_task": task["prompt"],
        "shared_answers": task.get("answers", []),
        "specification_version": task.get("spec_version", 1),
        "acceptance_criteria": (plan or {}).get("criteria", []),
    }
    phase_reminder = (
        "\n\nCURRENT PHASE: clarification only. Do not write project files in this turn. "
        "Call arena ready before finishing, whether or not you asked anything; a "
        "clarification turn that ends without it cannot start development. "
        "The application automatically starts your development turn next."
        if phase == "clarify" else ""
    )
    if phase == "clarify" and readiness_reminder:
        phase_reminder += READINESS_REMINDER
    return intro + follow_up_block + "\n\n" + json.dumps(specification, ensure_ascii=False, indent=2) + "\n\n" + MANDATORY + phase_reminder


def _write_tests_prompt(task: dict, approved_review: dict | None) -> str:
    """Generate the write-tests turn prompt carrying the full approved specification."""
    proposal = (approved_review or {}).get("proposal") or {}
    test_cases = proposal.get("test_cases", [])
    # Build a compact scenario list for the prompt; include every field the harness needs.
    scenarios = [
        {
            "id": case["id"],
            "title": case["title"],
            "group": case.get("group", "acceptance"),
            "steps": case.get("steps", []),
            "expected_result": case.get("expected_result", ""),
            "test_data": case.get("test_data", ""),
        }
        for case in test_cases
    ]
    test_specification = {
        "original_task": task["prompt"],
        "shared_answers": task.get("answers", []),
        "specification_version": task.get("spec_version", 1),
        "test_specification": {
            "summary": proposal.get("summary", ""),
            "assumptions": proposal.get("assumptions", []),
            "limitations": proposal.get("limitations", []),
            "scenarios": scenarios,
            "approved_scenario_ids": [case["id"] for case in test_cases],
        },
    }
    intro = (
        "TEST STAGE. Write executable tests for every approved scenario in this specification. "
        "Your implementation is already saved. Resume it, inspect it, then write tests that "
        "verify it against the specification — not against what it currently does. "
        "Fix the implementation to pass the tests; never weaken or delete a test to pass. "
        "Write the .mchc-tests.json manifest at the workspace root when done."
    )
    return (
        intro
        + "\n\n"
        + json.dumps(test_specification, ensure_ascii=False, indent=2)
        + "\n\n"
        + MANDATORY_TEST
    )


def repair_tests_prompt(task: dict, approved_review: dict | None,
                        validation_errors: list[str], round_k: int) -> str:
    """Generate a repair-tests turn prompt (manifest or test files had validation errors).

    The harness is allowed to change both tests and manifest because the lock does not
    exist yet — validation failed before the lock was established.
    """
    proposal = (approved_review or {}).get("proposal") or {}
    intro = (
        f"REPAIR TURN (round {round_k}). The manifest or test files you wrote have validation "
        "errors listed below. Fix your tests and manifest, re-run the tests, and re-write "
        "the manifest. The lock has not been set yet, so you may change any test file or "
        "the manifest freely. Fix the validation errors — do not weaken, skip, or delete "
        "tests merely to pass validation."
    )
    errors_block = "\n".join(f"  - {e}" for e in validation_errors)
    test_specification = {
        "original_task": task["prompt"],
        "specification_version": task.get("spec_version", 1),
        "approved_scenario_ids": [c["id"] for c in proposal.get("test_cases", [])],
    }
    return (
        intro
        + "\n\nVALIDATION ERRORS:\n"
        + errors_block
        + "\n\n"
        + json.dumps(test_specification, ensure_ascii=False, indent=2)
        + "\n\n"
        + MANDATORY_TEST
    )


def fix_tests_prompt(task: dict, approved_review: dict | None,
                     failing_checks: list[dict], locked_paths: list[str],
                     round_k: int, total_rounds: int,
                     budget_remaining_seconds: int) -> str:
    """Generate a fix-implementation turn prompt (verified checks have failures).

    The test files are locked.  The harness must fix the IMPLEMENTATION only.
    If it changes a locked test file the application will restore it and record a violation.
    """
    proposal = (approved_review or {}).get("proposal") or {}
    # Build scenario id/title lookup
    scenarios_by_id = {c["id"]: c["title"] for c in proposal.get("test_cases", [])}
    check_summaries = []
    for check in failing_checks:
        scenario_id = check.get("scenario_id", "")
        scenario_title = scenarios_by_id.get(scenario_id, scenario_id)
        summary = {
            "check_id": check.get("id"),
            "scenario_id": scenario_id,
            "scenario_title": scenario_title,
            "command": check.get("command"),
            "exit_code": check.get("exit_code"),
            "counts": check.get("counts"),
            "log_tail": check.get("log_tail", ""),
        }
        check_summaries.append(summary)
    intro = (
        f"FIX TURN (round {round_k} of {total_rounds}). "
        "The failing checks listed below are verified results from the application's "
        "isolated sandbox re-run. Fix the IMPLEMENTATION to make them pass. "
        "The test files are LOCKED — if you change or delete a locked file, the application "
        "will restore it from the lock and record a violation. "
        f"Remaining test budget: approximately {budget_remaining_seconds} seconds."
    )
    locked_block = "\n".join(f"  {p}" for p in sorted(locked_paths))
    test_specification = {
        "original_task": task["prompt"],
        "specification_version": task.get("spec_version", 1),
        "approved_scenario_ids": [c["id"] for c in proposal.get("test_cases", [])],
    }
    return (
        intro
        + "\n\nFAILING CHECKS:\n"
        + json.dumps(check_summaries, ensure_ascii=False, indent=2)
        + "\n\nLOCKED TEST FILES (do not change these):\n"
        + locked_block
        + "\n\n"
        + json.dumps(test_specification, ensure_ascii=False, indent=2)
        + "\n\n"
        + MANDATORY_TEST
    )
