# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A fresh workstation starts the API and worker together; both open a brand-new database."""
import fcntl
import multiprocessing as mp
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from arena.store import Store


def _open_and_write(root, barrier, results):
    try:
        barrier.wait()
        store = Store(Path(root))
        store.put("system", {"id": "opener-" + mp.current_process().name})
        results.put("ok")
    except Exception as exc:  # reported to the parent, which asserts on it
        results.put(f"{type(exc).__name__}: {exc}")


class StoreInitTests(unittest.TestCase):
    def test_simultaneous_first_opens_never_fail_with_database_is_locked(self):
        # Before the fix about one opener in twelve failed; 40 openers then miss it ~3% of runs.
        context = mp.get_context("spawn")
        failures = []
        for _ in range(10):
            with tempfile.TemporaryDirectory() as root:
                barrier, results = context.Barrier(4), context.Queue()
                workers = [context.Process(target=_open_and_write, args=(root, barrier, results))
                           for _ in range(4)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(60)
                outcomes = [results.get(timeout=10) for _ in workers]
                failures += [o for o in outcomes if o != "ok"]
                with sqlite3.connect(Path(root) / "arena.sqlite3") as db:
                    self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(failures, [])

    def test_initialisation_waits_for_another_process_holding_the_init_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            opened = threading.Event()
            with open(root / "arena.sqlite3.init.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                thread = threading.Thread(target=lambda: (Store(root), opened.set()))
                thread.start()
                time.sleep(0.3)
                self.assertFalse(opened.is_set())
            thread.join(10)
            self.assertTrue(opened.is_set())

    def test_an_existing_wal_database_opens_without_switching_modes(self):
        with tempfile.TemporaryDirectory() as td:
            Store(Path(td)).put("system", {"id": "kept"})
            self.assertEqual(Store(Path(td)).get("system", "kept"), {"id": "kept"})


if __name__ == "__main__":
    unittest.main()
