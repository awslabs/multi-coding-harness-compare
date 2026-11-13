# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

from .project_outputs import OUTPUT_MARKER, PROJECT_METADATA, output_marker


def remove_tree(root):
    """Unlink symlinks; never chmod or traverse their targets.

    Moved here from artifact_storage so agentcore_sandbox can import it without
    pulling in the entire storage+store dependency chain. artifact_storage
    re-imports this function for backward compatibility.
    """
    root = Path(root)
    if root.is_symlink() or root.is_file():
        root.unlink()
        return
    if not root.exists():
        return
    root.chmod(0o700)
    for directory, dirs, files in os.walk(root, followlinks=False):
        os.chmod(directory, 0o700)
        for name in dirs:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chmod(path, 0o700)
    shutil.rmtree(root)

EXCLUDED = {".git", ".arena", ".venv", ".pydeps", "node_modules", "__pycache__", ".DS_Store",
            OUTPUT_MARKER, PROJECT_METADATA}


def is_virtual_environment(path: Path) -> bool:
    """Recognize environments by their layout, without excluding source folders named venv."""
    if path.is_symlink() or not path.is_dir():
        return False
    marker = path / "pyvenv.cfg"
    if marker.is_symlink() or not marker.is_file():
        return False
    return any((path / directory / activate).is_file() for directory, activate in
               (("bin", "activate"), ("Scripts", "activate.bat")))


def copy_tree(source: Path, target: Path, *, excluded_roots=(), exclude_outputs=False,
              _boundary=None, _ancestors=()):
    """Copy a checkout without sharing Git metadata or following outside symlinks."""
    source = source.resolve()
    boundary = _boundary or source
    if source in _ancestors:
        raise ValueError(f"Cyclic directory symlink cannot be snapshotted: {source}")
    ancestors = (*_ancestors, source)
    excluded = tuple(Path(p).resolve() for p in excluded_roots)
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name in EXCLUDED or is_virtual_environment(item) or any(
                item.resolve().is_relative_to(p) for p in excluded):
            continue
        if exclude_outputs and item.is_dir() and output_marker(item.resolve()):
            continue
        destination = target / item.name
        if item.is_symlink():
            resolved = item.resolve()
            if not resolved.is_relative_to(boundary):
                raise ValueError(f"External symlink is not an isolated artifact: {item.relative_to(boundary)}")
            # Dereference internal links so nested links cannot escape a new root.
            if resolved.is_dir():
                copy_tree(resolved, destination, excluded_roots=excluded,
                          exclude_outputs=exclude_outputs, _boundary=boundary, _ancestors=ancestors)
            else:
                shutil.copy2(resolved, destination)
        elif item.is_dir():
            copy_tree(item, destination, excluded_roots=excluded,
                      exclude_outputs=exclude_outputs, _boundary=boundary, _ancestors=ancestors)
        elif item.is_file():
            shutil.copy2(item, destination)


def manifest(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.relative_to(root).parts:
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def tree_hash(root: Path):
    return hashlib.sha256(json.dumps(manifest(root), sort_keys=True).encode()).hexdigest()


def immutable(root: Path):
    for item in root.rglob("*"):
        if item.is_file():
            item.chmod(0o555 if os.access(item, os.X_OK) else 0o444)
    for item in sorted(root.rglob("*"), reverse=True):
        if item.is_dir():
            item.chmod(0o555)
    root.chmod(0o555)


def make_writable(root: Path):
    root.chmod(0o755)
    for item in root.rglob("*"):
        item.chmod(0o755 if item.is_dir() or os.access(item, os.X_OK) else 0o644)


def patch(baseline: Path, candidate: Path, destination: Path):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "diff", "--no-index", "--binary",
         "--no-ext-diff", "--no-textconv", str(baseline), str(candidate)],
        env=env, capture_output=True)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode(errors="replace"))
    data = result.stdout
    # Git --no-index prefixes absolute paths with a/ and b/. Only rewrite headers.
    out = []
    for line in data.splitlines(keepends=True):
        if line.startswith((b"diff --git ", b"--- ", b"+++ ", b"Binary files ", b"rename from ", b"rename to ")):
            for old in (b"a" + str(baseline).encode() + b"/", b"a" + str(candidate).encode() + b"/"):
                line = line.replace(old, b"a/")
            for old in (b"b" + str(baseline).encode() + b"/", b"b" + str(candidate).encode() + b"/"):
                line = line.replace(old, b"b/")
        out.append(line)
    destination.write_bytes(b"".join(out))


async def command(*args, timeout=30, input=None, env=None) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(*map(str, args),
        stdin=asyncio.subprocess.PIPE if input is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        out, err = await asyncio.wait_for(process.communicate(input), timeout)
    except (TimeoutError, asyncio.CancelledError):
        # A child can exit between the deadline/cancellation and kill(). Keep
        # the original exception, including cancellation during a spec revision.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        # Continue draining pipes while reaping; wait() alone can deadlock when
        # the cancelled communicate() left a full stdout/stderr buffer.
        await process.communicate()
        raise
    return process.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def initialize_checkout(root: Path):
    if (root / ".git").exists():
        return
    # Never run repository-supplied hooks or host-configured clean filters while
    # constructing the private baseline commit.
    prefix = ["env", "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null",
              "GIT_AUTHOR_NAME=Multi Coding-Harness Compare", "GIT_AUTHOR_EMAIL=mchc@localhost",
              "GIT_COMMITTER_NAME=Multi Coding-Harness Compare", "GIT_COMMITTER_EMAIL=mchc@localhost",
              "GIT_AUTHOR_DATE=2000-01-01T00:00:00+00:00",
              "GIT_COMMITTER_DATE=2000-01-01T00:00:00+00:00",
              "git", "-C", str(root), "-c", "core.hooksPath=/dev/null"]
    for args in (["-c", "init.defaultBranch=main", "init", "-q"],
                 ["add", "--all"], ["commit", "-q", "--allow-empty", "-m", "Arena immutable baseline"]):
        code, _, err = await command(*prefix, *args)
        if code:
            raise RuntimeError("Could not initialize isolated Git checkout: " + err)


def execution_runtime(config):
    if config.runtime == "agentcore":
        from .agentcore_sandbox import AgentCore
        return AgentCore(config)
    from .native_sandbox import Native
    return Native(config)
