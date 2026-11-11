# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Repo-wide guard: no internal project-tracking labels in tracked files.

Goal tags (a G followed by a goal number) and work-package tags (WP- followed by a
letter) are internal development labels that mean nothing to a new reader. They must
not appear in any shipped file, untracked new files included, so the guard fails before
a commit rather than after it. This file names the pattern, so it is not scanned, and
the "Titan Text" model in tests/test_model_selection_ui.cjs is a vendor name, not a label.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Pattern that matches a goal tag or work-package tag.
_LABEL_RE = re.compile(r"\bG(10|[1-9])\b|\bWP-[A-Z]\b")

# Lines explicitly exempted — each entry is (relative_path, substring_of_line).
# The "Titan Text G1" occurrence is a vendor model name, not a project label.
_EXEMPT_SUBSTRINGS = [
    ("tests/test_model_selection_ui.cjs", "Titan Text G1"),
]

# Repo-relative path of this guard file, used to exclude it from its own scan.
# Computed once at import time so it works even in symlinked or relocated checkouts.
_OWN_REL = Path(__file__).relative_to(ROOT).as_posix()


def _tracked_files() -> list[Path]:
    """Files in the git index (tracked/staged), excluding this guard file.

    Using the index (git ls-files without -o) ensures that untracked files
    — a developer's notes.md, a filled deploy.properties symlinked outside
    the repo, etc. — never cause bootstrap's test gate to fail.
    """
    result = subprocess.run(
        ["git", "ls-files", "-c", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [ROOT / p for p in result.stdout.splitlines() if p and p != _OWN_REL]


def _is_exempt(rel: str, line: str) -> bool:
    for exempt_rel, substr in _EXEMPT_SUBSTRINGS:
        if rel == exempt_rel and substr in line:
            return True
    return False


class NoInternalLabelsTests(unittest.TestCase):
    """No tracked file may contain goal or work-package labels."""

    def test_no_goal_or_wp_labels_in_tracked_files(self):
        """Every shipped file must be free of goal and work-package labels."""
        violations: list[str] = []
        for path in _tracked_files():
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            rel = str(path.relative_to(ROOT))
            for lineno, line in enumerate(text.splitlines(), 1):
                if _LABEL_RE.search(line) and not _is_exempt(rel, line):
                    violations.append(f"{rel}:{lineno}: {line.rstrip()}")
        self.assertEqual(
            violations,
            [],
            "Internal project labels found in tracked files — remove or replace them:\n"
            + "\n".join(violations[:20])
            + ("\n  ... and more" if len(violations) > 20 else ""),
        )
