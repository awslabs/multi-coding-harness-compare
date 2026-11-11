# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import json

TEMPLATE_VERSION = "mandatory-1"
MANDATORY = """BEGIN MANDATORY EXECUTION INSTRUCTIONS

Complete the user’s development task autonomously within your assigned workspace and configured permissions.

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
                     readiness_reminder: bool = False, follow_ups: list | None = None) -> str:
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
