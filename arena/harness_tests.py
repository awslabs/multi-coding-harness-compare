# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Harness-written test pipeline: manifest handling, locking, plan building, feedback.

This module supports the test stage of the harness-written-tests flow.  Its
primary callers are in worker.py (Part B); it exposes a stable documented API so
that Part B can import and compose the pieces.

Public API
----------
MANIFEST_NAME          str            ".mchc-tests.json"
load_manifest(...)     (dict, errors) Parse and validate the manifest + test files.
build_plan(...)        (Plan, issues) Compile a per-harness Plan from a valid manifest.
create_lock(...)       dict           Snapshot hashes + bytes of all locked files.
enforce_lock(...)      list[dict]     Restore changed/deleted files; return violations.
format_repair_feedback(...)  str      Human-readable prompt block for a repair turn.
format_fix_feedback(...)     str      Human-readable prompt block for a fix turn.
"""
import ast
import hashlib
import json
import shlex
import tomllib
import configparser
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

MANIFEST_NAME = ".mchc-tests.json"

# Size limits from DESIGN 4.3
_MANIFEST_MAX_BYTES = 256 * 1024          # 256 KiB
_TEST_FILE_MAX_BYTES = 1024 * 1024        # 1 MiB per file
_TEST_FILES_MAX_TOTAL_BYTES = 8 * 1024 * 1024  # 8 MiB aggregate
_TEST_FILES_MAX_COUNT = 200
# Bounded log tail included in fix-turn feedback
_LOG_TAIL_MAX_CHARS = 4000
# C1: Manifest check limits (§9.11)
_MAX_CHECKS = 400
_MAX_CHECKS_PER_SCENARIO = 10
_NOTES_MAX_BYTES = 2000
# C3: Special files that cannot be created after lock
_AUTO_LOCK_NAMES = frozenset(("conftest.py", "pytest.ini", "tox.ini"))
_CREATED_SPECIAL_NAMES = frozenset((
    "conftest.py", "pytest.ini", "tox.ini",
    "sitecustomize.py", "usercustomize.py",
))
_SKIP_DIRS = frozenset((".git", ".venv", "node_modules", "__pycache__", ".tox"))


# ── Path safety helpers ───────────────────────────────────────────────────────

def _safe_relative(rel_path: str, workspace: Path) -> tuple[Path | None, str | None]:
    """Validate a path is relative, traversal-free, and resolves inside workspace.

    Returns (resolved_path, None) on success or (None, error_message) on failure.
    The workspace must already be resolved (absolute, no symlinks).
    """
    if not rel_path or not isinstance(rel_path, str):
        return None, f"empty or non-string path is not allowed"
    try:
        p = Path(rel_path)
    except (TypeError, ValueError):
        return None, f"{rel_path!r}: not a valid path string"
    if p.is_absolute():
        return None, f"{rel_path!r}: must be a relative path, not absolute"
    if ".." in p.parts:
        return None, f"{rel_path!r}: path traversal ('..') is not allowed"
    resolved = (workspace / p).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError:
        return None, f"{rel_path!r}: resolves outside the workspace"
    return resolved, None


def _check_no_symlink(path: Path, rel_path: str) -> str | None:
    """Return error if any component of path (up to and including it) is a symlink."""
    # Walk from workspace root downward; stop at the file itself.
    # We only have the final resolved path; check existence and symlink at each part.
    # The path may not exist yet (e.g. checking test file not yet written).
    try:
        # Only check if the path currently exists.
        if not path.exists():
            return None
        # check the final entry
        if path.is_symlink():
            return f"{rel_path!r}: must be a regular file, not a symlink"
        # Walk parents stopping at the workspace root (already resolved).
        # We rely on the workspace being an absolute real path.
        p = path
        visited = []
        while True:
            visited.append(p)
            parent = p.parent
            if parent == p:  # filesystem root
                break
            p = parent
        for ancestor in visited:
            if ancestor.is_symlink():
                return f"{rel_path!r}: path contains a symlink component"
    except OSError:
        pass  # Cannot check; allow and let later read fail naturally.
    return None


# ── Manifest loading and validation ──────────────────────────────────────────

def load_manifest(
    workspace: Path,
    *,
    approved_review: dict | None = None,
) -> tuple[dict | None, list[str]]:
    """Load and validate .mchc-tests.json from the workspace root.

    Collects ALL confinement and limit errors (DESIGN 4.2–4.3) into one list.
    Returns (manifest_dict, errors).  If a parse-level error prevents further
    validation, manifest_dict is None and errors contains a single message.

    Parameters
    ----------
    workspace:        Absolute path to the harness workspace/snapshot.
    approved_review:  Optional approved test-review record (not currently used by
                      load_manifest itself; callers pass it to build_plan).
    """
    workspace = workspace.resolve()
    errors: list[str] = []

    # ── Locate and read the manifest ─────────────────────────────────────────
    manifest_path = workspace / MANIFEST_NAME

    if manifest_path.is_symlink():
        return None, [f"{MANIFEST_NAME!r}: must be a regular file, not a symlink"]
    if not manifest_path.exists():
        return None, [f"{MANIFEST_NAME!r} was not found in the workspace root"]
    if not manifest_path.is_file():
        return None, [f"{MANIFEST_NAME!r}: must be a regular file (not a directory or special file)"]

    size = manifest_path.stat().st_size
    if size > _MANIFEST_MAX_BYTES:
        return None, [
            f"{MANIFEST_NAME!r} is {size:,} bytes, exceeding the {_MANIFEST_MAX_BYTES // 1024} KiB limit"
        ]

    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        return None, [f"Could not read {MANIFEST_NAME!r}: {exc}"]

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, [f"{MANIFEST_NAME!r} is not valid UTF-8: {exc}"]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"{MANIFEST_NAME!r} is not valid JSON: {exc}"]

    if not isinstance(data, dict):
        return None, [f"{MANIFEST_NAME!r} must be a JSON object at the top level"]

    # ── Schema validation ─────────────────────────────────────────────────────
    if data.get("version") != 1:
        errors.append(
            f"{MANIFEST_NAME!r} 'version' must be 1, got {data.get('version')!r}"
        )

    # Validate checks array
    checks = data.get("checks")
    if not isinstance(checks, list):
        errors.append(f"{MANIFEST_NAME!r} 'checks' must be a list")
    else:
        # C1: at most 400 checks total (§9.11)
        if len(checks) > _MAX_CHECKS:
            errors.append(
                f"{MANIFEST_NAME!r} has {len(checks)} checks, exceeding the {_MAX_CHECKS} check limit"
            )
        seen_check_ids: set[str] = set()
        checks_per_scenario: dict[str, int] = {}  # C1: per-scenario count
        for i, check in enumerate(checks):
            label = f"checks[{i}]"
            if not isinstance(check, dict):
                errors.append(f"{label} must be an object")
                continue
            for field in ("id", "scenario_id", "command"):
                val = check.get(field)
                if not isinstance(val, str) or not val.strip():
                    errors.append(f"{label} missing or empty field {field!r}")
            cid = check.get("id", "")
            if cid in seen_check_ids:
                errors.append(f"check id {cid!r} is duplicated in checks")
            elif cid:
                seen_check_ids.add(cid)
            # C1: at most 10 checks per scenario_id (§9.11)
            sid = check.get("scenario_id", "")
            if isinstance(sid, str) and sid.strip():
                checks_per_scenario[sid] = checks_per_scenario.get(sid, 0) + 1
                if checks_per_scenario[sid] == _MAX_CHECKS_PER_SCENARIO + 1:
                    errors.append(
                        f"scenario {sid!r} has more than {_MAX_CHECKS_PER_SCENARIO} checks "
                        f"(limit is {_MAX_CHECKS_PER_SCENARIO} per scenario)"
                    )
            # junit may be null or a non-empty string
            junit = check.get("junit")
            if junit is not None and (not isinstance(junit, str) or not junit.strip()):
                errors.append(f"{label} 'junit' must be null or a non-empty string")

    # Validate test_files array + confinement rules
    test_files = data.get("test_files")
    if not isinstance(test_files, list):
        errors.append(f"{MANIFEST_NAME!r} 'test_files' must be a list")
    else:
        if len(test_files) > _TEST_FILES_MAX_COUNT:
            errors.append(
                f"test_files has {len(test_files)} entries, exceeding the {_TEST_FILES_MAX_COUNT} file limit"
            )
        seen_paths: set[str] = set()
        total_bytes = 0
        for i, rel_path in enumerate(test_files):
            if not isinstance(rel_path, str):
                errors.append(f"test_files[{i}] must be a string, got {type(rel_path).__name__!r}")
                continue
            # Path safety
            resolved, err = _safe_relative(rel_path, workspace)
            if err:
                errors.append(err)
                continue
            # Duplicate check
            norm = str(Path(rel_path))
            if norm in seen_paths:
                errors.append(f"test_files contains duplicate path {rel_path!r}")
                continue
            seen_paths.add(norm)
            # Check the path in the workspace without resolving symlinks.
            # resolved follows symlinks; use the unresolved path for symlink detection.
            unresolved = workspace / Path(rel_path)
            if unresolved.is_symlink():
                errors.append(f"{rel_path!r}: must be a regular file, not a symlink")
                continue
            # Also check for symlinks in ancestor components
            sym_err = _check_no_symlink(unresolved, rel_path)
            if sym_err:
                errors.append(sym_err)
                continue
            if not resolved.exists():
                errors.append(f"{rel_path!r}: file does not exist in the workspace")
                continue
            if not resolved.is_file():
                errors.append(f"{rel_path!r}: must be a regular file")
                continue
            # Size limit per file
            file_size = resolved.stat().st_size
            if file_size > _TEST_FILE_MAX_BYTES:
                errors.append(
                    f"{rel_path!r} is {file_size:,} bytes, exceeding the "
                    f"{_TEST_FILE_MAX_BYTES // 1024} KiB per-file limit"
                )
                continue
            total_bytes += file_size
            # UTF-8 check
            try:
                resolved.read_bytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                errors.append(f"{rel_path!r} is not valid UTF-8: {exc}")
            # Aggregate size limit
            if total_bytes > _TEST_FILES_MAX_TOTAL_BYTES:
                errors.append(
                    f"test_files aggregate size exceeds the "
                    f"{_TEST_FILES_MAX_TOTAL_BYTES // (1024 * 1024)} MiB limit"
                )
                break  # one message is enough

    # C1: notes ≤ 2000 bytes UTF-8 (§9.11)
    notes = data.get("notes")
    if notes is not None:
        if not isinstance(notes, str):
            errors.append(f"{MANIFEST_NAME!r} 'notes' must be a string when present")
        else:
            notes_bytes = len(notes.encode("utf-8", errors="replace"))
            if notes_bytes > _NOTES_MAX_BYTES:
                errors.append(
                    f"{MANIFEST_NAME!r} 'notes' is {notes_bytes} bytes, "
                    f"exceeding the {_NOTES_MAX_BYTES}-byte limit"
                )

    # python_requirements should be a list of strings when present
    python_requirements = data.get("python_requirements")
    if python_requirements is not None and not isinstance(python_requirements, list):
        errors.append(f"{MANIFEST_NAME!r} 'python_requirements' must be a list when present")
    elif isinstance(python_requirements, list):
        for i, req in enumerate(python_requirements):
            if not isinstance(req, str):
                errors.append(
                    f"python_requirements[{i}] must be a string, got {type(req).__name__!r}"
                )

    return data, errors


# ── Workspace-path check (C4, §9.10) + R2-9 extended command rules ───────────

# R2-9(b): known test-runner module names that are always allowed in `python -m X`
_KNOWN_TEST_RUNNERS = frozenset((
    "pytest", "unittest", "nose2", "doctest", "coverage",
))

# R2-9(b): Python interpreter names
_PYTHON_INTERPRETERS = frozenset(("python", "python3"))

# R2-9(a): shell interpreters that may directly invoke a workspace file by bare name
_SHELL_INTERPRETERS = frozenset(("bash", "sh", "node"))

# R2-9: build tools and the workspace file that defines them
_BUILD_TOOL_DEFINING_FILES: dict[str, str] = {
    "make": "Makefile",
    "tox": "tox.ini",
    "nox": "noxfile.py",
}
# npm/yarn/pnpm use package.json and only apply on "run" or "test" subcommands
_NPM_LIKE_TOOLS = frozenset(("npm", "yarn", "pnpm"))


def _module_invocation_issues(
    tokens: list[str], workspace: Path, test_files_set: set[str]
) -> list[str]:
    """R2-9(b): python[-m X] where X resolves to a workspace module not in test_files.

    Known test runners (pytest, unittest, nose2, doctest, coverage) are always allowed.
    """
    if not tokens:
        return []
    if tokens[0].lower() not in _PYTHON_INTERPRETERS:
        return []
    # Scan for the -m flag after the interpreter
    m_idx: int | None = None
    for i in range(1, len(tokens)):
        if tokens[i] == "-m":
            m_idx = i
            break
    if m_idx is None or m_idx + 1 >= len(tokens):
        return []
    module_name = tokens[m_idx + 1]
    if not module_name or module_name.startswith("-"):
        return []
    # Allow known test runners (and coverage which orchestrates them)
    base_name = module_name.split(".")[0]
    if base_name in _KNOWN_TEST_RUNNERS:
        return []
    # Check if module resolves to a workspace file: X.py or X/__main__.py
    # (dots in module name map to path separators)
    module_as_path = module_name.replace(".", "/")
    candidates = [
        module_as_path + ".py",
        module_as_path + "/__main__.py",
    ]
    for rel in candidates:
        try:
            candidate = workspace / rel
            if candidate.is_file():
                norm = str((workspace / rel).relative_to(workspace))
                if norm not in test_files_set:
                    return [
                        f"-m {module_name!r} resolves to workspace module {norm!r} "
                        f"which must be listed in test_files (§9.10)"
                    ]
        except (ValueError, OSError):
            pass
    return []


def _interpreter_invocation_issues(
    tokens: list[str], workspace: Path, test_files_set: set[str]
) -> list[str]:
    """R2-9(a): interpreter invoking a workspace file by bare name (no path separator).

    Rejects ``python main.py``, ``python3 main.py``, ``bash run.sh``,
    ``sh run.sh``, ``node x.js`` when the named file exists in the workspace
    but is NOT listed in test_files.

    Allowed:
    - Interpreters running a file that IS listed in test_files.
    - ``python -m …`` / ``python3 -m …`` (handled by _module_invocation_issues).
    - Bare names that do not exist as workspace files.
    - Paths with separators (handled by the C4 path-separator rule).
    """
    if not tokens:
        return []
    interpreter = tokens[0].lower()
    if interpreter not in (_PYTHON_INTERPRETERS | _SHELL_INTERPRETERS):
        return []

    # For python/python3: if -m appears before the first positional arg this is a
    # module invocation handled by _module_invocation_issues — skip here.
    if interpreter in _PYTHON_INTERPRETERS:
        for tok in tokens[1:]:
            if tok == "-m":
                return []
            if not tok.startswith("-"):
                break  # found the positional arg without hitting -m first

    # Find the first non-flag argument after the interpreter.
    script: str | None = None
    for tok in tokens[1:]:
        if tok.startswith("-"):
            continue
        script = tok
        break

    if not script:
        return []

    # Only check bare names (no path separator) — paths with separators are
    # already handled by the C4 path-separator rule.
    if "/" in script or "\\" in script:
        return []

    # Strip pytest-style selector (file::test) — unlikely for bare names but safe.
    script_part = script.split("::")[0]
    if not script_part:
        return []

    # Check if this bare filename exists in the workspace.
    try:
        full = (workspace / script_part).resolve()
        if not full.is_relative_to(workspace):
            return []
        if not full.is_file():
            return []
    except (OSError, ValueError):
        return []

    # Normalize to match test_files entries.
    try:
        norm = str(full.relative_to(workspace))
    except ValueError:
        return []

    if norm not in test_files_set:
        return [
            f"interpreter invocation {script_part!r} resolves to workspace file {norm!r} "
            f"which must be listed in test_files (§9.10)"
        ]
    return []


def _build_tool_issues(
    tokens: list[str], workspace: Path, test_files_set: set[str]
) -> list[str]:
    """R2-9: build tool commands require their defining file to be in test_files.

    Allowed tools: make (Makefile), tox (tox.ini), nox (noxfile.py),
    npm/yarn/pnpm run|test (package.json).  If the defining file exists in the
    workspace but is not listed in test_files (and therefore not locked), the
    command is a validation error naming the file.
    """
    if not tokens:
        return []
    tool = tokens[0].lower()
    if tool in _NPM_LIKE_TOOLS:
        # Only apply to "run" and "test" subcommands
        if len(tokens) < 2 or tokens[1].lower() not in ("run", "test"):
            return []
        defining_file = "package.json"
    elif tool in _BUILD_TOOL_DEFINING_FILES:
        defining_file = _BUILD_TOOL_DEFINING_FILES[tool]
    else:
        return []
    # Defining file must exist in the workspace to apply this rule
    full_path = workspace / defining_file
    try:
        if not full_path.is_file():
            return []
    except OSError:
        return []
    if defining_file not in test_files_set:
        return [
            f"command uses {tool!r} whose defining file {defining_file!r} "
            f"exists in the workspace and must be listed in test_files (§9.10)"
        ]
    return []


def _workspace_path_issues(command: str, workspace: Path, test_files_set: set[str]) -> list[str]:
    """Return issues for command arguments that resolve to workspace files not in test_files.

    Handles:
    - C4 (§9.10): relative paths with separators (e.g. scripts/run.py), ./x, /workspace/x.
    - R2-9(a): interpreter invoking a workspace file by bare name (python main.py,
      python3 main.py, bash run.sh, sh run.sh, node x.js) — rejected unless listed.
    - R2-9(b): python -m X where X resolves to a workspace module not in test_files.
    - R2-9: build tool commands (make, tox, nox, npm/yarn/pnpm run|test) whose defining
      file exists in the workspace but is not in test_files.

    Bare names without a path separator that are NOT the first positional argument of a
    known interpreter are not checked for the path-separator rule.
    Flags (tokens starting with -) are skipped.
    """
    issues: list[str] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []  # Unparseable shell; skip (plan_quality_issues catches other problems)

    workspace = workspace.resolve()

    # R2-9(a): bare-name interpreter invocation (python main.py, bash run.sh, node x.js)
    issues.extend(_interpreter_invocation_issues(tokens, workspace, test_files_set))

    # R2-9(b): python -m X module invocation
    issues.extend(_module_invocation_issues(tokens, workspace, test_files_set))

    # R2-9: build tool defining-file check
    issues.extend(_build_tool_issues(tokens, workspace, test_files_set))

    # C4 (§9.10): path-separator path check (unchanged)
    for token in tokens:
        if not token or token.startswith("-"):
            continue
        # Strip pytest selector suffix (tests/test_x.py::test_case → tests/test_x.py)
        path_part = token.split("::")[0]
        # Determine relative path in workspace, applying the following rules:
        # - /workspace/x → rel_path = x (explicit workspace reference → check)
        # - ./x          → rel_path = x (explicit relative reference → check)
        # - x/y/z        → rel_path = x/y/z (contains separator → check)
        # - bare name    → skip (program under test or installed tool, e.g. main.py, pytest)
        if path_part.startswith("/workspace/"):
            rel_path = path_part[len("/workspace/"):]
        elif path_part.startswith("/"):
            continue  # Other absolute paths — not workspace
        elif path_part.startswith("./"):
            rel_path = path_part[2:]
        elif "/" in path_part:
            # Contains a path separator → specific file reference, check it
            rel_path = path_part
        else:
            # Bare name with no separator (e.g. main.py, pytest, python) — skip
            continue
        if not rel_path:
            continue
        try:
            full = (workspace / rel_path).resolve()
            if not full.is_relative_to(workspace):
                continue
            if not full.is_file():
                continue
        except (OSError, ValueError):
            continue
        # Normalize to match test_files entries
        try:
            norm = str(full.relative_to(workspace))
        except ValueError:
            continue
        if norm not in test_files_set:
            issues.append(
                f"command argument {path_part!r} resolves to workspace file {norm!r} "
                f"which must be listed in test_files (§9.10)"
            )
    return issues


# ── Plan compilation ──────────────────────────────────────────────────────────

def build_plan(
    workspace: Path,
    manifest_data: dict,
    approved_review: dict,
) -> tuple["Plan | None", list[str]]:
    """Compile a per-harness Plan from a validated manifest and approved review.

    Reuses compile_reviewed_tests, review_alignment_issues, plan_quality_issues,
    and Python AST checks from the existing pipeline.

    Returns (plan, issues).  If compilation fails plan is None and issues is non-empty.
    """
    from .evaluation import Plan, plan_quality_issues
    from .test_review import (
        ReviewedCheck, ReviewedTestCode,
        compile_reviewed_tests, review_alignment_issues,
    )
    workspace = workspace.resolve()
    issues: list[str] = []

    # Build ReviewedTestCode from the manifest
    raw_checks = manifest_data.get("checks") or []
    test_files_list = manifest_data.get("test_files") or []
    python_requirements = manifest_data.get("python_requirements") or []

    # Read test file contents for acceptance_files (harness-written tests live in workspace,
    # not in /acceptance, so we pass them as acceptance_files so CandidateTestRunner can
    # inject them into the sandbox's /acceptance directory for independent re-execution).
    acceptance_files: dict[str, str] = {}
    read_errors: list[str] = []
    for rel_path in test_files_list:
        resolved_path, path_err = _safe_relative(rel_path, workspace)
        if path_err:
            read_errors.append(path_err)
            continue
        try:
            acceptance_files[rel_path] = resolved_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            read_errors.append(f"Could not read test file {rel_path!r}: {exc}")
    if read_errors:
        return None, read_errors

    # Construct ReviewedTestCode
    try:
        reviewed_checks = [
            ReviewedCheck(
                id=c["id"],
                scenario_id=c["scenario_id"],
                command=c["command"],
                junit=c.get("junit"),
            )
            for c in raw_checks
        ]
        code = ReviewedTestCode(
            checks=reviewed_checks,
            acceptance_files=acceptance_files,
            python_requirements=python_requirements,
        )
    except Exception as exc:
        return None, [f"Manifest check structure is invalid: {exc}"]

    # Compile into a Plan (this validates scenario_id links and focused-mode cardinality)
    try:
        plan = compile_reviewed_tests(code, approved_review)
    except ValueError as exc:
        return None, [str(exc)]

    # Plan-level quality checks (placeholder commands, failure-hiding pipelines, mock guards)
    quality = plan_quality_issues(plan)
    issues.extend(quality)

    # Python AST checks on every listed .py test file (via acceptance_files)
    # These are already performed inside plan_quality_issues for acceptance_files.
    # No additional pass needed here.

    # C4 (§9.10): command arguments that resolve to workspace files must be in test_files
    test_files_set = {str(Path(p)) for p in (manifest_data.get("test_files") or [])}
    for check in raw_checks:
        cmd = check.get("command", "")
        if isinstance(cmd, str):
            path_issues = _workspace_path_issues(cmd, workspace, test_files_set)
            for pi in path_issues:
                issues.append(f"{check.get('id', '?')}: {pi}")

    # Alignment check
    alignment = review_alignment_issues(plan, approved_review)
    issues.extend(alignment)

    if issues:
        return None, issues

    return plan, []


# ── Lock ──────────────────────────────────────────────────────────────────────

def _extract_config_snapshots(workspace: Path) -> dict[str, str]:
    """Extract normalized test-configuration sections from pyproject.toml and setup.cfg.

    Returns {rel_path: normalized_json_string} for the test-config sections that exist.
    Used by create_lock to record state at lock time; enforce_lock compares against this.
    """
    snapshots: dict[str, str] = {}

    # pyproject.toml: [tool.pytest.ini_options] and [tool.coverage.*]
    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file() and not pyproject.is_symlink():
        try:
            raw = pyproject.read_bytes()
            data = tomllib.loads(raw.decode("utf-8"))
            tool = data.get("tool", {}) if isinstance(data, dict) else {}
            test_sections: dict = {}
            if "pytest" in tool and isinstance(tool["pytest"], dict):
                ini = tool["pytest"].get("ini_options")
                if ini is not None:
                    test_sections["tool.pytest.ini_options"] = ini
            # tool.coverage.* — any subsection under tool that starts with "coverage"
            for key, val in tool.items():
                if key == "coverage" or key.startswith("coverage."):
                    test_sections[f"tool.{key}"] = val
            if test_sections:
                snapshots["pyproject.toml"] = json.dumps(test_sections, sort_keys=True)
        except Exception:
            pass  # Non-parseable TOML; skip

    # setup.cfg: [tool:pytest] and [coverage:*]
    setupcfg = workspace / "setup.cfg"
    if setupcfg.is_file() and not setupcfg.is_symlink():
        try:
            content = setupcfg.read_text(encoding="utf-8")
            parser = configparser.ConfigParser()
            parser.read_string(content)
            test_sections = {}
            for section in parser.sections():
                if section == "tool:pytest" or section.startswith("coverage:"):
                    test_sections[section] = dict(parser[section])
            if test_sections:
                snapshots["setup.cfg"] = json.dumps(test_sections, sort_keys=True)
        except Exception:
            pass  # Non-parseable INI; skip

    return snapshots


def create_lock(workspace: Path, manifest_data: dict) -> dict:
    """Create a lock snapshot for the manifest and all test_files.

    C2 (§9.9): also locks every conftest.py at any depth, pytest.ini, and tox.ini,
    even if not listed in test_files (recorded in auto_locked; warnings emitted for
    files not listed in test_files).

    Returns a dict:
        files:                  {rel_path: {"sha256": str, "bytes": bytes}}
        auto_locked:            [rel_path]  — paths locked but not in manifest's test_files
        warnings:               [str]       — warnings for auto-locked files not in test_files
        config_snapshots:       {rel_path: str} — normalized test-config sections
        special_file_baseline:  [rel_path]  — sitecustomize.py etc. present at lock time
    """
    workspace = workspace.resolve()
    files: dict[str, dict] = {}

    def _record(rel_path: str) -> None:
        resolved = workspace / rel_path
        try:
            raw = resolved.read_bytes()
        except OSError:
            return
        files[rel_path] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": raw,
        }

    # Always lock the manifest itself
    _record(MANIFEST_NAME)

    # Lock every test file listed in the manifest
    test_files_in_manifest: set[str] = set()
    for rel_path in manifest_data.get("test_files") or []:
        if isinstance(rel_path, str):
            test_files_in_manifest.add(str(Path(rel_path)))
            _record(rel_path)

    # C2: auto-lock conftest.py at any depth, pytest.ini, tox.ini
    auto_locked: list[str] = []
    warnings: list[str] = []
    for path in sorted(workspace.rglob("*")):
        # Skip excluded directories
        try:
            rel = path.relative_to(workspace)
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        name = path.name
        if name in _AUTO_LOCK_NAMES:
            rel_str = str(rel)
            if rel_str not in files:  # not already locked via test_files
                _record(rel_str)
                auto_locked.append(rel_str)
                if rel_str not in test_files_in_manifest:
                    warnings.append(
                        f"Auto-locked {rel_str!r} (not listed in test_files); "
                        "if this file is part of your test suite, add it to test_files."
                    )

    # C3: record special files (sitecustomize.py, usercustomize.py, *.pth) present at lock time
    special_file_baseline: list[str] = []
    for path in sorted(workspace.rglob("*")):
        try:
            rel = path.relative_to(workspace)
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        name = path.name
        if name in ("sitecustomize.py", "usercustomize.py") or name.endswith(".pth"):
            special_file_baseline.append(str(rel))

    # C3: snapshot of test-config sections for pyproject.toml and setup.cfg
    config_snapshots = _extract_config_snapshots(workspace)

    return {
        "files": files,
        "auto_locked": auto_locked,
        "warnings": warnings,
        "config_snapshots": config_snapshots,
        "special_file_baseline": special_file_baseline,
    }


def enforce_lock(
    workspace: Path,
    lock: dict,
    round_number: int,
) -> list[dict]:
    """Compare workspace against lock; restore changed/deleted/replaced files.

    C3 (§9.9): also deletes and flags newly created conftest.py, pytest.ini, tox.ini,
    sitecustomize.py, usercustomize.py, *.pth anywhere in the workspace; flags (not
    restores) test_config_changed when the test-configuration sections of pyproject.toml
    or setup.cfg differ from lock time.

    lock is the dict returned by create_lock (has "files", "auto_locked", "warnings",
    "config_snapshots", "special_file_baseline").  The old flat format {rel_path: entry}
    is also accepted for backward compatibility.

    Returns a list of violations, each a dict with keys:
        round:   int    — round number passed in
        path:    str    — relative path
        kind:    str    — "modified" | "deleted" | "replaced" | "created" | "test_config_changed"
    """
    import shutil as _shutil
    workspace = workspace.resolve()
    violations: list[dict] = []

    # Support both old flat format and new structured format
    if "files" in lock and isinstance(lock.get("files"), dict):
        files_dict: dict = lock["files"]
        config_snapshots: dict = lock.get("config_snapshots") or {}
        special_file_baseline: set[str] = set(lock.get("special_file_baseline") or [])
    else:
        # Old flat format: entire lock is {rel_path: {sha256, bytes}}
        files_dict = lock
        config_snapshots = {}
        special_file_baseline = set()

    # 1. Restore locked files that were modified / deleted / replaced
    for rel_path, entry in files_dict.items():
        expected_sha256 = entry["sha256"]
        saved_bytes: bytes = entry["bytes"]
        full_path = workspace / rel_path

        kind: str | None = None

        if not full_path.exists():
            kind = "deleted"
        elif full_path.is_symlink():
            kind = "replaced"
        elif not full_path.is_file():
            kind = "replaced"
        else:
            try:
                current = full_path.read_bytes()
            except OSError:
                kind = "deleted"
            else:
                if hashlib.sha256(current).hexdigest() != expected_sha256:
                    kind = "modified"

        if kind is not None:
            # Restore the file from the lock
            try:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                if full_path.is_symlink() or full_path.exists():
                    if full_path.is_dir() and not full_path.is_symlink():
                        _shutil.rmtree(full_path)
                    else:
                        full_path.unlink()
                full_path.write_bytes(saved_bytes)
            except OSError:
                pass  # Best-effort restoration; violation is still recorded
            violations.append({"round": round_number, "path": rel_path, "kind": kind})

    # 2. C3: scan for newly created special files and *.pth
    for path in sorted(workspace.rglob("*")):
        try:
            rel = path.relative_to(workspace)
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        rel_str = str(rel)
        name = path.name
        is_special = (name in _CREATED_SPECIAL_NAMES or name.endswith(".pth"))
        if not is_special:
            continue
        # Flag if NOT in the locked files and NOT in the special_file_baseline
        if rel_str not in files_dict and rel_str not in special_file_baseline:
            # Delete and flag as "created"
            try:
                path.unlink()
            except OSError:
                pass
            violations.append({"round": round_number, "path": rel_str, "kind": "created"})

    # 3. C3: check test-config sections for changes
    if config_snapshots:
        current_snapshots = _extract_config_snapshots(workspace)
        for rel_path, old_content in config_snapshots.items():
            new_content = current_snapshots.get(rel_path, "")
            if new_content != old_content:
                violations.append({
                    "round": round_number,
                    "path": rel_path,
                    "kind": "test_config_changed",
                })

    return violations


# ── Feedback formatting ───────────────────────────────────────────────────────

def format_repair_feedback(validation_errors: list[str]) -> str:
    """Format feedback for a repair turn (manifest or test files failed validation).

    The harness is told to fix its tests/manifest; no lock exists yet.
    """
    lines = ["VALIDATION ERRORS — fix your manifest and test files:"]
    for err in validation_errors:
        lines.append(f"  - {err}")
    lines.append("")
    lines.append(
        "Edit the manifest (.mchc-tests.json) and test files to resolve every issue listed "
        "above. The lock has not been set; you may change any test file or the manifest. "
        "Re-run your tests after fixing. Never delete or weaken tests merely to pass validation."
    )
    return "\n".join(lines)


def format_fix_feedback(
    failing_checks: list[dict],
    locked_paths: list[str],
    round_k: int,
    total_rounds: int,
    budget_remaining_seconds: int,
) -> str:
    """Format feedback for a fix turn (verified checks have failures).

    Each entry in failing_checks should have keys:
        id, scenario_id, scenario_title, command, exit_code, counts, log_tail
    """
    lines = [
        f"FIX REQUIRED (round {round_k} of {total_rounds}) — "
        f"approximately {budget_remaining_seconds} seconds of test budget remaining."
    ]
    lines.append("")
    lines.append("FAILING CHECKS (from the application's isolated sandbox re-run):")
    for check in failing_checks:
        lines.append(f"")
        lines.append(f"  Check ID:       {check.get('id', '?')}")
        lines.append(f"  Scenario:       {check.get('scenario_id', '?')} — {check.get('scenario_title', '')}")
        lines.append(f"  Command:        {check.get('command', '?')}")
        lines.append(f"  Exit code:      {check.get('exit_code')}")
        counts = check.get("counts") or {}
        counts_str = ", ".join(
            f"{k}={v}" for k, v in counts.items() if v is not None
        )
        if counts_str:
            lines.append(f"  Counts:         {counts_str}")
        log_tail = (check.get("log_tail") or "").strip()
        if log_tail:
            lines.append(f"  Log tail:")
            for log_line in log_tail.splitlines():
                lines.append(f"    {log_line}")
    lines.append("")
    lines.append("LOCKED TEST FILES (the application restores these if changed):")
    for p in sorted(locked_paths):
        lines.append(f"  {p}")
    lines.append("")
    lines.append(
        "Fix the IMPLEMENTATION to make the failing checks pass. "
        "Do not change, delete, or weaken locked test files — the application will restore them. "
        "The test budget counts only your active time (not sandbox re-runs or waiting)."
    )
    return "\n".join(lines)


# ── Convenience: truncate log to bounded tail ─────────────────────────────────

def bounded_log_tail(log: str, max_chars: int = _LOG_TAIL_MAX_CHARS) -> str:
    """Return the last max_chars characters of log, with a truncation notice if cut."""
    if len(log) <= max_chars:
        return log
    return f"[... truncated {len(log) - max_chars} chars ...]\n" + log[-max_chars:]
