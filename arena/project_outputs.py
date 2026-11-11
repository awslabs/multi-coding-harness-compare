# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Writable project deliverables, separate from private evaluation snapshots."""
import fcntl
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .store import HARNESS_NAMES, now, uid

OUTPUT_MARKER = ".codecouncil-output.json"
PROJECT_METADATA = ".codecouncil"


def output_marker(directory: Path) -> dict | None:
    path = directory / OUTPUT_MARKER
    try:
        if directory.is_symlink() or path.is_symlink() or path.stat().st_size > 16384:
            return None
        data = json.loads(path.read_text())
        if (isinstance(data, dict) and data.get("owner") == "CodeCouncil"
                and data.get("version") == 1 and data.get("harness") in HARNESS_NAMES):
            return data
    except (OSError, ValueError):
        pass
    return None


def _directory(path: Path):
    if path.is_symlink():
        raise ValueError(f"multi-coding-harness-compare output storage must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _project_lock(project: Path):
    metadata = _directory(project / PROJECT_METADATA)
    fd = os.open(metadata / "outputs.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield metadata


def _existing(destination: Path) -> dict | None:
    marker = output_marker(destination)
    if destination.is_symlink() or (destination.exists() and marker is None):
        raise ValueError(
            f"Cannot save {destination.name} output: {destination} already exists and is not "
            "a multi-coding-harness-compare output folder. Rename or move that folder before assigning this project.")
    return marker


def _record(comparison: dict, run: dict, **fields):
    return {"owner": "CodeCouncil", "version": 1, "harness": run["harness"],
            "comparison_id": comparison["id"], "comparison_created_at": comparison["created_at"],
            "run_id": run["id"], "spec_version": run["spec_version"], **fields}


def _rank(record: dict):
    return (record.get("comparison_created_at", 0), record.get("comparison_id", ""),
            record.get("spec_version", 0))


def _replace(project: Path, metadata: Path, record: dict, snapshot: Path | None = None):
    # Import lazily: baseline copying also uses output_marker to omit deliverables.
    from .isolation import copy_tree, make_writable, tree_hash

    destination = project / record["harness"]
    staging = Path(tempfile.mkdtemp(prefix="staging-", dir=metadata))
    archived = None
    try:
        if snapshot is not None:
            copy_tree(snapshot, staging)
            if tree_hash(staging) != record["sha256"]:
                raise ValueError("Project output copy did not match the saved snapshot")
            make_writable(staging)
        (staging / OUTPUT_MARKER).write_text(json.dumps(record, indent=2) + "\n")
        if destination.exists():
            history = _directory(_directory(metadata / "history") / record["harness"])
            archived = history / uid()
            destination.rename(archived)
        try:
            staging.rename(destination)
        except Exception:
            if archived is not None:
                archived.rename(destination)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {**record, "path": str(destination)}


def prepare_outputs(project: Path, comparison: dict, runs: list[dict]) -> dict:
    """Reserve selected harness folders, preserving earlier writable copies."""
    project = project.resolve(strict=True)
    with _project_lock(project) as metadata:
        # Validate the complete cohort before moving any existing output.
        for run in runs:
            existing = _existing(project / run["harness"])
            if existing and _rank(existing) > _rank(_record(comparison, run)):
                raise ValueError("A newer task already owns this project's output folders")
        return {run["id"]: _replace(project, metadata,
                                   _record(comparison, run, status="pending"))
                for run in runs}


def publish_output(project: Path, comparison: dict, run: dict) -> dict:
    """Publish a complete copy; failed exports never mutate the candidate snapshot."""
    artifacts = run["artifacts"]
    record = _record(comparison, run, status="published", published_at=now(),
                     snapshot=artifacts["snapshot"], sha256=artifacts["sha256"],
                     partial=bool(artifacts.get("partial")))
    project = project.resolve(strict=True)
    destination = project / run["harness"]
    with _project_lock(project) as metadata:
        existing = _existing(destination)
        if existing and _rank(existing) > _rank(record):
            return {**record, "path": str(destination), "status": "superseded",
                    "note": "A newer task owns this project's harness folder. Use the saved snapshot for this run."}
        if (existing and existing.get("run_id") == run["id"]
                and existing.get("snapshot") == record["snapshot"]):
            # Preserve any edits the user made after this snapshot was published.
            return {**existing, "path": str(destination)}
        return _replace(project, metadata, record, Path(artifacts["snapshot"]))


def output_status(output: dict) -> dict:
    """Do not label an older task's path as its code after a newer task replaces it."""
    if output.get("status") not in ("published", "pending"):
        return output
    current = output_marker(Path(output["path"]))
    if (not current or current.get("run_id") != output.get("run_id")
            or current.get("snapshot") != output.get("snapshot")):
        return {**output, "status": "superseded",
                "note": "This folder was replaced or moved. Use this run's saved snapshot."}
    return output
