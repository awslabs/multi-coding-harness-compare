# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import fcntl
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

TERMINAL = {"blocked", "failed", "cancelled", "limit_exhausted", "interrupted",
            "verified_complete", "evaluated_failed", "evaluated_incomplete"}
HARNESS_NAMES = ("claude", "codex", "kiro")
COMPARISON_PHASE_PRIORITY = ("developing", "evaluating", "clarifying", "setup", "planning",
                             "waiting_test_review", "waiting_user", "waiting_approval")


def uid() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


class Store:
    """SQLite WAL is the durable queue, event journal and source of UI state."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "arena.sqlite3"
        # The API and worker start together. On a new database both would switch it to WAL at
        # once; that switch needs exclusive access and the loser fails with "database is locked"
        # instead of waiting. Initialise one process at a time, and switch only when needed.
        with open(root / "arena.sqlite3.init.lock", "a") as lock, self.connect() as db:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(kind,id)
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                comparison_id TEXT NOT NULL, run_id TEXT,
                at REAL NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS event_comparison ON events(comparison_id,seq);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def put(self, kind: str, data: dict):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO objects VALUES (?,?,?)",
                       (kind, data["id"], json.dumps(data)))
        return data

    def get(self, kind: str, id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT data FROM objects WHERE kind=? AND id=?", (kind, id)).fetchone()
        if not row:
            raise KeyError(id)
        return json.loads(row["data"])

    def list(self, kind: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT data FROM objects WHERE kind=?", (kind,)).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def update(self, kind: str, id: str, **fields) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM objects WHERE kind=? AND id=?", (kind, id)).fetchone()
            if row is None:
                raise KeyError(id)
            data = json.loads(row["data"])
            data.update(fields)
            db.execute("UPDATE objects SET data=? WHERE kind=? AND id=?",
                       (json.dumps(data), kind, id))
        return data

    def event(self, comparison: str, type: str, data: dict, run: str | None = None):
        with self.connect() as db:
            db.execute("INSERT INTO events(comparison_id,run_id,at,type,data) VALUES(?,?,?,?,?)",
                       (comparison, run, now(), type, json.dumps(data)))

    def events(self, comparison: str, after: int = 0, limit: int = 1000):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE comparison_id=? AND seq>? ORDER BY seq LIMIT ?",
                              (comparison, after, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def transition(self, run_id: str, status: str, at: float | None = None, **fields):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.transition_in_transaction(db, run_id, status, at=at, **fields)

    def transition_in_transaction(self, db, run_id: str, status: str, at: float | None = None, **fields):
        """Update a run and its event inside the caller's existing write transaction."""
        r = json.loads(db.execute("SELECT data FROM objects WHERE kind='run' AND id=?", (run_id,)).fetchone()[0])
        at = now() if at is None else at
        intervals = r.get("intervals", [])
        if intervals and intervals[-1]["end"] is None:
            intervals[-1]["end"] = at
        intervals.append({"status": status, "start": at, "end": at if status in TERMINAL else None})
        r.update(fields, status=status, intervals=intervals, updated_at=at)
        if status in TERMINAL:
            r["ended_at"] = at
        db.execute("UPDATE objects SET data=? WHERE kind='run' AND id=?", (json.dumps(r), run_id))
        # Clients fetch the current run after a status event. Keep the durable
        # stream small; evaluations and evidence remain in the run and artifacts.
        event = {"status": status, "updated_fields": sorted(fields)}
        event.update({key: fields[key] for key in ("error", "note", "model", "session_id", "artifacts")
                      if key in fields})
        db.execute("INSERT INTO events(comparison_id,run_id,at,type,data) VALUES(?,?,?,?,?)",
                   (r["comparison_id"], run_id, at, "status", json.dumps(event)))
        return r

    def detail(self, comparison_id: str):
        c = self.get("comparison", comparison_id)
        with self.connect() as db:
            rows = db.execute("""SELECT kind,data FROM objects WHERE kind IN ('run','question')
                                 AND json_extract(data,'$.comparison_id')=?""", (comparison_id,)).fetchall()
        c["runs"] = [json.loads(r["data"]) for r in rows if r["kind"] == "run"]
        c["questions"] = [json.loads(r["data"]) for r in rows if r["kind"] == "question"]
        return c


def durations(run: dict) -> dict:
    end = run.get("ended_at") or now()
    sums = {}
    for i in run.get("intervals", []):
        sums[i["status"]] = sums.get(i["status"], 0) + max(0, (i["end"] or end) - i["start"])
    return {
        "queue_setup": sums.get("queued", 0) + sums.get("setup", 0),
        "clarification": sums.get("clarifying", 0),
        "user_wait": sums.get("waiting_user", 0) + sums.get("waiting_approval", 0) + sums.get("waiting_test_review", 0),
        "test_review_wait": sums.get("waiting_test_review", 0),
        "active": sums.get("developing", 0),
        "evaluation": sums.get("evaluating", 0) + sums.get("planning", 0),
        "evaluation_preparation": sums.get("planning", 0),
        "barrier_wait": sums.get("ready", 0),
        "awaiting_other_implementations": sums.get("implemented", 0),
        "unobserved": sums.get("unobserved", 0),
        "inactive_between_attempts": max(0, end - run["submitted_at"] - sum(sums.values())),
        "total": max(0, end - run["submitted_at"]),
        "measurement": "orchestrator UTC intervals; active deadlines use monotonic time",
    }
