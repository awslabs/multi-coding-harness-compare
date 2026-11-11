# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Structural tests for deploy.properties.example.

Verified properties:
- Every key declared in arena/properties.py KEYS appears in the template.
- A copy with all placeholders replaced passes preflight.inputs with exit 0
  (nothing missing), and a second run changes nothing (idempotent).
- No internal "(G" labels in the file.
- Commented harness-version examples match the pinned versions in
  harness_image/versions.py.
- The commented KIRO_DOWNLOAD_URL example equals the code-default template.
"""
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
TEMPLATE = ROOT / "deploy.properties.example"

# Valid replacements for every placeholder pattern used in the template.
_PLACEHOLDER_SUBSTITUTIONS = {
    "YOUR_RUNTIME": "native",
    "YOUR_KIRO_AUTH": "copy-host-login",
    "YOUR_AWS_PROFILE": "test-profile",
    "YOUR_AWS_REGION": "us-east-1",
    "YOUR_BUCKET": "test-bucket",
    "/absolute/path/to/projects": "/tmp",
}


def _filled_template() -> str:
    """Return the template text with all placeholders replaced by valid values."""
    text = TEMPLATE.read_text(encoding="utf-8")
    for placeholder, replacement in _PLACEHOLDER_SUBSTITUTIONS.items():
        text = text.replace(placeholder, replacement)
    return text


class TemplateKeysTests(unittest.TestCase):
    """Every key in arena/properties.py KEYS must appear in the template."""

    def _keys(self):
        from arena.properties import KEYS
        return KEYS

    def test_every_key_documented_in_template(self):
        template = TEMPLATE.read_text(encoding="utf-8")
        for key in self._keys():
            with self.subTest(key=key.name):
                # Accept both active lines (KEY =) and commented lines (# KEY =)
                # with any amount of whitespace between the name and the '='.
                pattern = r"(?:^|\n)#?\s*" + re.escape(key.name) + r"\s*="
                self.assertTrue(
                    re.search(pattern, template),
                    f"Key {key.name!r} from properties.KEYS not found in deploy.properties.example "
                    f"(neither as an active line nor as a commented example)",
                )

    def test_key_count_at_least_expected_minimum(self):
        """Sanity guard: KEYS must not shrink below 73 entries."""
        from arena.properties import KEYS
        self.assertGreaterEqual(len(KEYS), 73)


class FilledTemplateInputsTests(unittest.TestCase):
    """A hand-filled copy of the template must satisfy preflight.inputs (exit 0)."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._props = Path(self._dir.name) / "deploy.properties"

    def test_filled_template_gives_exit_zero_from_inputs(self):
        """Replacing every placeholder leaves no missing values."""
        self._props.write_text(_filled_template(), encoding="utf-8")
        from arena.preflight import inputs
        code, report = inputs(self._props, [])
        self.assertEqual(code, 0,
                         f"Expected exit 0 (no placeholders remaining), got {code}; "
                         f"missing = {report.get('missing')}")
        self.assertEqual(report.get("missing", []), [],
                         "No placeholder values should remain after filling the template")

    def test_second_inputs_run_changes_nothing(self):
        """inputs() is idempotent: re-running it must not modify the file."""
        self._props.write_text(_filled_template(), encoding="utf-8")
        from arena.preflight import inputs
        inputs(self._props, [])   # first run (may auto-derive USER_NAMESPACE if it were a placeholder)
        content_before = self._props.read_text(encoding="utf-8")
        inputs(self._props, [])   # second run
        content_after = self._props.read_text(encoding="utf-8")
        self.assertEqual(content_before, content_after,
                         "A second call to inputs() must not change a fully-filled deploy.properties")


class NoInternalLabelsTests(unittest.TestCase):
    """Template must not contain internal development labels."""

    def test_no_g_label_in_template(self):
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn(
            "(G",
            template,
            "deploy.properties.example must not contain internal '(G...' labels",
        )


class CommentedVersionsTests(unittest.TestCase):
    """Commented harness version examples must match pinned versions in harness_image/versions.py."""

    def _versions(self):
        from harness_image.versions import CLAUDE_SDK_VERSION, CODEX_VERSION, KIRO_VERSION
        return CLAUDE_SDK_VERSION, CODEX_VERSION, KIRO_VERSION

    def test_commented_claude_version_matches_pinned(self):
        template = TEMPLATE.read_text(encoding="utf-8")
        claude_v, _, _ = self._versions()
        m = re.search(r"#\s*CLAUDE_HARNESS_VERSION\s*=\s*(\S+)", template)
        self.assertIsNotNone(m, "Commented CLAUDE_HARNESS_VERSION not found in template")
        self.assertEqual(
            m.group(1), claude_v,
            f"CLAUDE_HARNESS_VERSION example {m.group(1)!r} does not match "
            f"harness_image/versions.py CLAUDE_SDK_VERSION {claude_v!r}",
        )

    def test_commented_codex_version_matches_pinned(self):
        template = TEMPLATE.read_text(encoding="utf-8")
        _, codex_v, _ = self._versions()
        m = re.search(r"#\s*CODEX_HARNESS_VERSION\s*=\s*(\S+)", template)
        self.assertIsNotNone(m, "Commented CODEX_HARNESS_VERSION not found in template")
        self.assertEqual(
            m.group(1), codex_v,
            f"CODEX_HARNESS_VERSION example {m.group(1)!r} does not match "
            f"harness_image/versions.py CODEX_VERSION {codex_v!r}",
        )

    def test_commented_kiro_version_matches_pinned(self):
        template = TEMPLATE.read_text(encoding="utf-8")
        _, _, kiro_v = self._versions()
        m = re.search(r"#\s*KIRO_HARNESS_VERSION\s*=\s*(\S+)", template)
        self.assertIsNotNone(m, "Commented KIRO_HARNESS_VERSION not found in template")
        self.assertEqual(
            m.group(1), kiro_v,
            f"KIRO_HARNESS_VERSION example {m.group(1)!r} does not match "
            f"harness_image/versions.py KIRO_VERSION {kiro_v!r}",
        )


class KiroDownloadUrlTests(unittest.TestCase):
    """Commented KIRO_DOWNLOAD_URL example must equal the code-default template."""

    def test_kiro_download_url_example_matches_code_template(self):
        from harness_image.versions import KIRO_CDN_URL_TEMPLATE
        template = TEMPLATE.read_text(encoding="utf-8")
        m = re.search(r"#\s*KIRO_DOWNLOAD_URL\s*=\s*(.+)", template)
        self.assertIsNotNone(m, "Commented KIRO_DOWNLOAD_URL not found in deploy.properties.example")
        found = m.group(1).strip()
        self.assertEqual(
            found,
            KIRO_CDN_URL_TEMPLATE,
            f"KIRO_DOWNLOAD_URL example {found!r} does not match "
            f"harness_image/versions.py KIRO_CDN_URL_TEMPLATE {KIRO_CDN_URL_TEMPLATE!r}",
        )


class KeyCommentCoverageTests(unittest.TestCase):
    """Every key line must have a definition comment directly above it."""

    # Pattern matching a KEY = value line (active or commented).
    _KEY_LINE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")
    # Pattern matching any comment line (not a blank line, not a key line).
    _COMMENT_LINE = re.compile(r"^\s*#")

    def _lines(self):
        return TEMPLATE.read_text(encoding="utf-8").splitlines()

    def test_every_key_line_has_comment_directly_above(self):
        """No key line (active or commented) may be preceded by a blank line or another key."""
        lines = self._lines()
        for i, line in enumerate(lines):
            if not self._KEY_LINE.match(line):
                continue
            with self.subTest(line=i + 1, content=line.strip()):
                self.assertGreater(
                    i, 0,
                    f"Line {i + 1} is a key line at the very start of the file with no comment above it",
                )
                prev = lines[i - 1]
                self.assertTrue(
                    self._COMMENT_LINE.match(prev) and not self._KEY_LINE.match(prev),
                    f"Line {i + 1} ({line.strip()!r}) must be directly preceded by a "
                    f"non-key comment line; got {prev.strip()!r}",
                )

    def test_every_section_header_followed_by_description(self):
        """Every ══ section header and ── sub-group divider must be followed by a description comment."""
        _HEADER = re.compile(r"^#\s*(══|──)")
        lines = self._lines()
        for i, line in enumerate(lines):
            if not _HEADER.match(line):
                continue
            with self.subTest(line=i + 1, header=line.strip()):
                self.assertLess(
                    i + 1, len(lines),
                    f"Header at line {i + 1} is the last line of the file",
                )
                nxt = lines[i + 1]
                self.assertTrue(
                    self._COMMENT_LINE.match(nxt) and not _HEADER.match(nxt),
                    f"Header at line {i + 1} ({line.strip()[:60]!r}) must be followed by a "
                    f"description comment; got {nxt.strip()!r}",
                )


if __name__ == "__main__":
    unittest.main()
