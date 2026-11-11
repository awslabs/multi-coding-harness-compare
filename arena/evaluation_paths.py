# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Materialize evaluator-owned paths without changing a frozen test plan."""
import re
import shlex
from pathlib import PurePosixPath


# Keep shell text and quoting intact; edit only recognized argument spans.
# Quoted Python/shell programs are single words, never recursively rewritten.
_TOKENS = re.compile(
    r"""(?P<comment>\#[^\n]*)|(?P<operator>[;&|()<>\n]+)|"""
    r"""(?P<word>(?:[^\s'"\\;&|()<>\n]+|\\[\s\S]|'[^']*'|"(?:\\[\s\S]|[^"\\])*")+)"""
)
_PYTEST_VALUES = {
    "-k", "-m", "-c", "-p", "-o", "--override-ini", "--rootdir", "--confcutdir",
    "--basetemp", "--log-file", "--deselect", "--ignore", "--ignore-glob",
    "--maxfail", "--tb", "--capture", "--color", "--durations", "--timeout",
    "--import-mode", "--junit-prefix", "--junitprefix",
}
_JUNIT_FLAGS = {"--junitxml", "--junit-xml"}


def _segments(command):
    # A here-document contains program/data text, not shell argument words.
    # Preserve such commands; their authors must use explicit logical paths.
    if any(match.lastgroup == "operator" and "<<" in match.group()
           for match in _TOKENS.finditer(command)):
        return
    words = []
    for match in _TOKENS.finditer(command):
        if match.lastgroup == "operator":
            if words:
                yield words
                words = []
        elif match.lastgroup == "word":
            try:
                value, = shlex.split(match.group(), comments=False)
            except ValueError:
                continue
            words.append((match.start(), match.end(), value))
    if words:
        yield words


def _invocation(words):
    index = 0
    while index < len(words) and re.match(r"^[A-Za-z_]\w*=", words[index][2]):
        index += 1
    if index == len(words):
        return None, []
    executable = PurePosixPath(words[index][2]).name
    args = words[index + 1:]
    if executable == "pytest":
        return "pytest", args
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", executable):
        return None, []
    index = 0
    while index < len(args):
        value = args[index][2]
        if value == "-c":
            return None, []
        if value == "-m":
            if index + 1 < len(args) and args[index + 1][2] == "pytest":
                return "pytest", args[index + 2:]
            return None, []
        if value in ("-W", "-X"):
            index += 2
        elif value == "--":
            return "script", args[index + 1:index + 2]
        elif value.startswith("-"):
            index += 1
        else:
            return "script", args[index:index + 1]
    return None, []


def prepare_check_command(command, acceptance_files, check_id, junit=None, mapping=None):
    """Resolve Python test inputs and JUnit output, retaining original assertions.

    Only exact generated filenames are relocated. Explicit candidate paths,
    selectors, inline programs, shell operators and unrelated arguments retain
    their meaning. This also works for legacy plans restored from S3.
    """
    mapping = mapping or {}
    files = {str(PurePosixPath(name)) for name in acceptance_files}
    replacements, adjustments, inputs, issues = {}, [], [], []
    report = None
    uses_pytest = False

    def physical(path):
        for logical, actual in mapping.items():
            if path == logical or path.startswith(logical + "/"):
                return actual + path[len(logical):]
        return path

    def replace(word, value, reason):
        start, end, previous = word
        replacements[start, end] = shlex.quote(physical(value))
        if previous != value:
            adjustments.append({"from": previous, "to": value, "reason": reason})

    def input_file(word):
        value = word[2]
        filename, separator, selector = value.partition("::")
        path = str(PurePosixPath(filename))
        if path in files:
            path = "/acceptance/" + path
        if not path.startswith("/acceptance/"):
            return
        relative = path[len("/acceptance/"):]
        if relative not in files:
            # Directory discovery and globs are not individual file references.
            if not any(c in relative for c in "*?[") and not any(
                    name.startswith(relative.rstrip("/") + "/") for name in files):
                issues.append("Generated test file is missing from acceptance_files: " + path)
            return
        inputs.append(path)
        replace(word, path + separator + selector, "generated test file")

    for words in _segments(command):
        kind, args = _invocation(words)
        if kind == "script":
            for word in args:
                input_file(word)
        elif kind == "pytest":
            uses_pytest = True
            index = 0
            report_flags = 0
            positional_only = False
            while index < len(args):
                word = args[index]
                value = word[2]
                flag, equal, _ = value.partition("=")
                if not positional_only and flag in _JUNIT_FLAGS:
                    report = "/reports/" + check_id + ".xml"
                    report_flags += 1
                    if equal:
                        replace(word, flag + "=" + physical(report), "isolated JUnit report")
                    elif index + 1 < len(args) and not args[index + 1][2].startswith("-"):
                        index += 1
                        replace(args[index], report, "isolated JUnit report")
                    else:
                        issues.append("JUnit report option has no output filename")
                elif value == "--":
                    positional_only = True
                elif not positional_only and value in _PYTEST_VALUES:
                    index += 1
                elif positional_only or not value.startswith("-"):
                    input_file(word)
                index += 1
            if junit and not report_flags:
                # A declared pytest report must actually be requested. Insert
                # before arguments so a trailing `--` retains its meaning.
                report = "/reports/" + check_id + ".xml"
                at = args[0][0] if args else words[-1][1]
                replacements[at, at] = (
                    ("" if args else " ") + shlex.quote("--junitxml=" + physical(report))
                    + (" " if args else ""))
                adjustments.append({"from": junit, "to": report, "reason": "isolated JUnit report"})
    for (start, end), replacement in sorted(replacements.items(), reverse=True):
        command = command[:start] + replacement + command[end:]
    return {"command": command, "junit": physical(report) if report else junit,
            "generated_inputs": list(dict.fromkeys(inputs)),
            "adjustments": adjustments, "issues": issues, "pytest": uses_pytest}


def pytest_setup_error(exit_code, log):
    """Recognize runner failures even when report teardown masks its exit code."""
    if exit_code in (3, 4, 5):
        return "Pytest could not execute the requested checks: internal error, invalid arguments, or no tests collected"
    if exit_code and re.search(
            r"^(?:_pytest\.config\.exceptions\.)?(?:pytest\.)?UsageError:|"
            r"^ERROR:\s+(?:file or directory not found:|not found:|unrecognized arguments:)|"
            r"^INTERNALERROR>|^collected 0 items\s*$",
            log, re.M):
        return "Pytest failed before executing the requested tests; inspect collection and setup errors"
    if exit_code and "_pytest/junitxml.py" in log and re.search(
            r"^(?:PermissionError|FileNotFoundError|NotADirectoryError|OSError):", log, re.M):
        return "Pytest could not write its JUnit report in the evaluation environment"
    return None
