# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Separate Kiro conversation data from its combined local authentication DB."""
import sqlite3
from pathlib import Path


SESSION_TABLES = {"conversations", "conversations_v2", "history"}


def is_session_database(name):
    path = Path(name)
    return "kiro-cli" in path.parts and path.suffix in (".sqlite3", ".db")


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def split_database(source, sessions, runtime):
    """SQLite backup includes committed WAL data; VACUUM removes deleted secrets."""
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        for destination, keep_sessions in ((sessions, True), (runtime, False)):
            destination.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(str(destination) + suffix).unlink(missing_ok=True)
            with sqlite3.connect(destination) as db:
                original.backup(db)
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA secure_delete=ON")
                tables = [r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                for table in tables:
                    if table == "migrations":
                        continue
                    if (table in SESSION_TABLES) != keep_sessions:
                        db.execute("DELETE FROM " + quote(table))
                db.commit()
                db.execute("VACUUM")
            destination.chmod(0o600)


def restore_runtime(destination, source):
    """Apply current local credentials without replacing archived conversations."""
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as runtime, \
            sqlite3.connect(destination) as sessions:
        tables = {r[0] for r in sessions.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, in runtime.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if table in SESSION_TABLES or table == "migrations" or table.startswith("sqlite_") or table not in tables:
                continue
            columns = [r[1] for r in runtime.execute("PRAGMA table_info(" + quote(table) + ")")]
            target_columns = {r[1] for r in sessions.execute("PRAGMA table_info(" + quote(table) + ")")}
            if not set(columns) <= target_columns:
                continue
            rows = runtime.execute("SELECT * FROM " + quote(table))
            sessions.executemany("INSERT OR REPLACE INTO " + quote(table) + " ("
                                 + ",".join(map(quote, columns)) + ") VALUES ("
                                 + ",".join("?" for _ in columns) + ")", rows)
