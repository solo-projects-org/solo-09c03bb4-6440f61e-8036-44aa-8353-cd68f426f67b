"""SQLite persistence.

Design notes
------------
* One connection per thread, WAL journal, ``synchronous=FULL`` and a busy
  timeout.  Together with ``BEGIN IMMEDIATE`` transactions this gives us
  durable commits and a serialised write path, so two concurrent requests can
  never both spend the same remaining budget.
* SQLite's ``REAL`` cannot represent budget values such as ``0.1`` exactly,
  so the total budget, spent budget and every epsilon are stored as the plain
  decimal strings produced by :class:`decimal.Decimal`; all arithmetic is done
  in Python after the row has been locked, never in SQL.
* The singleton data-set row in ``dataset`` is created exactly once by the
  importer and doubles as the budget counter.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS dataset (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    imported_at     TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    row_count       INTEGER NOT NULL,
    total_budget    TEXT    NOT NULL,
    budget_spent    TEXT    NOT NULL,
    fingerprint     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS subjects (
    id          INTEGER PRIMARY KEY,
    subject_id  TEXT    NOT NULL UNIQUE,
    region      TEXT    NOT NULL,
    outcome     INTEGER NOT NULL CHECK (outcome IN (0, 1))
);

CREATE INDEX IF NOT EXISTS subjects_region_outcome
    ON subjects (region, outcome);

CREATE TABLE IF NOT EXISTS requests (
    request_id    TEXT PRIMARY KEY,
    request_hash  TEXT NOT NULL,
    region        TEXT NOT NULL,
    outcome       INTEGER NOT NULL,
    epsilon       TEXT NOT NULL,
    noisy_count   INTEGER NOT NULL,
    budget_after  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""


class Database:
    """Thread-safe wrapper around a single SQLite file."""

    def __init__(self, path: Path, busy_timeout_ms: int = 30_000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._all_connections: set[sqlite3.Connection] = set()
        self._all_lock = threading.Lock()

    # -- connection management -------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,  # explicit transactions only
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        self._local.conn = conn
        with self._all_lock:
            self._all_connections.add(conn)
        return conn

    def initialise(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA)

    def close_thread_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        with self._all_lock:
            self._all_connections.discard(conn)
        conn.close()
        self._local.conn = None

    def close_all(self) -> None:
        with self._all_lock:
            conns = list(self._all_connections)
            self._all_connections.clear()
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local.__dict__.clear()

    # -- transactions ------------------------------------------------------
    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialised read-modify-write transaction.

        ``BEGIN IMMEDIATE`` acquires the write lock up front: only one writer
        can be inside this block at a time, which is what makes the budget
        check and the budget deduction atomic with respect to concurrent
        requests.
        """
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            conn.execute("COMMIT")

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            conn.execute("COMMIT")


def as_decimal(value: Any) -> Decimal:
    """Read a decimal string back from SQLite without float conversion."""
    return Decimal(str(value))


def dataset_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM dataset WHERE id = 1").fetchone()
