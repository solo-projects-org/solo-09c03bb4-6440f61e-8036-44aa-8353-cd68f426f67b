"""Public data structures and the SQL-backed repository.

All budget quantities are decimals and are stored as canonical decimal
strings, never as SQLite REAL values, so ``1 - 0.1`` stays exactly ``0.9``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from .privacy import canonical_decimal


@dataclass(frozen=True)
class StoredRequest:
    request_id: str
    fingerprint: str
    region: str
    outcome: int
    epsilon: str
    noisy_count: int
    budget_after: str
    created_at: str


@dataclass(frozen=True)
class ReleaseResult:
    """Outcome of a query: a fresh release, or a replay of a stored one."""

    noisy_count: int
    remaining_budget: Decimal
    replayed: bool


@dataclass(frozen=True)
class BudgetSnapshot:
    total: Decimal
    spent: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.total - self.spent


@dataclass(frozen=True)
class LookupResult:
    """A stored answer (if any) plus the *current* global remaining budget."""

    stored: StoredRequest | None
    remaining_budget: Decimal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository(Protocol):
    """Storage interface used by the query service."""

    def budget_snapshot(self) -> BudgetSnapshot: ...

    def lookup(self, request_id: str) -> LookupResult: ...

    def count_subjects(self, region: str, outcome: int) -> int: ...

    def apply_query(
        self,
        *,
        request_id: str,
        fingerprint: str,
        region: str,
        outcome: int,
        epsilon: Decimal,
        noisy_count: int,
    ) -> ReleaseResult: ...


class SqliteRepository:
    """Repository whose mutations run inside one serialised transaction."""

    def __init__(self, database) -> None:  # noqa: ANN001 - avoids an import cycle
        self._db = database

    # -- reads -------------------------------------------------------------
    def budget_snapshot(self) -> BudgetSnapshot:
        from .errors import DatasetMissing

        with self._db.read_transaction() as conn:
            return _budget_snapshot(conn)

    def lookup(self, request_id: str) -> LookupResult:
        """Read the stored answer and the current remaining budget atomically."""
        with self._db.read_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            remaining = _budget_snapshot(conn).remaining
            return LookupResult(
                stored=_row_to_request(row) if row else None,
                remaining_budget=remaining,
            )

    def count_subjects(self, region: str, outcome: int) -> int:
        with self._db.read_transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM subjects "
                "WHERE region = ? AND outcome = ?",
                (region, int(outcome)),
            ).fetchone()
            return int(row["n"])

    # -- the critical section ---------------------------------------------
    def apply_query(
        self,
        *,
        request_id: str,
        fingerprint: str,
        region: str,
        outcome: int,
        epsilon: Decimal,
        noisy_count: int,
    ) -> ReleaseResult:
        """Atomically consume budget and persist the answer.

        ``noisy_count`` is ignored when a stored answer for ``request_id``
        already exists: a replay always returns the originally released
        value and never charges budget.
        """
        from .errors import BudgetExhausted, RequestIdConflict

        with self._db.write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is not None:
                stored = _row_to_request(row)
                if stored.fingerprint != fingerprint:
                    raise RequestIdConflict(request_id)
                return ReleaseResult(
                    noisy_count=stored.noisy_count,
                    remaining_budget=_budget_snapshot(conn).remaining,
                    replayed=True,
                )

            snapshot = _budget_snapshot(conn)
            if epsilon > snapshot.remaining:
                raise BudgetExhausted(epsilon, snapshot.remaining, snapshot.total)
            budget_after = snapshot.remaining - epsilon
            new_spent = snapshot.total - budget_after

            # The request row and the budget update commit together.  Any
            # failure while writing raises out of the transaction, which
            # rolls both back: no charge and no stored answer.
            conn.execute(
                """
                INSERT INTO requests
                    (request_id, request_hash, region, outcome, epsilon,
                     noisy_count, budget_after, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    fingerprint,
                    region,
                    int(outcome),
                    canonical_decimal(epsilon),
                    int(noisy_count),
                    canonical_decimal(budget_after),
                    _now(),
                ),
            )
            conn.execute(
                "UPDATE dataset SET budget_spent = ? WHERE id = 1",
                (canonical_decimal(new_spent),),
            )
            return ReleaseResult(
                noisy_count=int(noisy_count),
                remaining_budget=budget_after,
                replayed=False,
            )


def _budget_snapshot(conn: sqlite3.Connection) -> BudgetSnapshot:
    from .errors import DatasetMissing

    row = conn.execute(
        "SELECT total_budget, budget_spent FROM dataset WHERE id = 1"
    ).fetchone()
    if row is None:
        raise DatasetMissing("no dataset imported")
    return BudgetSnapshot(
        total=Decimal(str(row["total_budget"])),
        spent=Decimal(str(row["budget_spent"])),
    )


def _row_to_request(row: sqlite3.Row) -> StoredRequest:
    return StoredRequest(
        request_id=str(row["request_id"]),
        fingerprint=str(row["request_hash"]),
        region=str(row["region"]),
        outcome=int(row["outcome"]),
        epsilon=str(row["epsilon"]),
        noisy_count=int(row["noisy_count"]),
        budget_after=str(row["budget_after"]),
        created_at=str(row["created_at"]),
    )
