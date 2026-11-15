"""Reconciliation of a partner's revised follow-up CSV against the base table.

The base ``subjects`` table is read-only input here: a reconciliation never
inserts, updates or deletes any subject row and never touches the privacy
budget.  Each uploaded file is validated in full and compared against the
base table; the resulting per-subject report and the file's SHA-256
fingerprint are then published in one ``BEGIN IMMEDIATE`` transaction.

Atomicity and idempotency
-------------------------
* Parsing and validation happen before any database write, so a file with a
  bad row or a trailing duplicate subject id leaves no report behind.
* The header row, every detail row and the summary row are inserted inside a
  single transaction.  A disk failure rolls the whole report back, so an
  older report under a different id stays intact and the failed id becomes
  visible only once a complete report exists.
* The reconciliation id is an idempotency key.  Retrying the same id with the
  same file returns the stored report; reusing it with a different file
  fingerprint is rejected.  Because every write is keyed by the caller's id
  and every detail row carries that id, concurrent uploads of different ids
  can never interleave their data.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .errors import (
    DatasetMissing,
    ReconciliationConflict,
    ReconciliationCsvError,
)
from .importer import InvalidCsv, _parse_rows, file_fingerprint

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# result structures
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ChangedField:
    field: str
    before: str | int | None
    after: str | int | None


@dataclass(frozen=True)
class ReconciliationEntry:
    subject_id: str
    change_type: str  # 'added' | 'missing' | 'changed'
    before_region: str | None = None
    after_region: str | None = None
    before_outcome: int | None = None
    after_outcome: int | None = None
    changed_fields: tuple[ChangedField, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReconciliationSummary:
    added: int
    missing: int
    changed: int


@dataclass(frozen=True)
class ReconciliationResult:
    reconciliation_id: str
    file_fingerprint: str
    source_filename: str
    row_count: int
    created_at: str
    added: tuple[ReconciliationEntry, ...]
    missing: tuple[ReconciliationEntry, ...]
    changed: tuple[ReconciliationEntry, ...]
    replayed: bool

    @property
    def summary(self) -> ReconciliationSummary:
        return ReconciliationSummary(
            added=len(self.added),
            missing=len(self.missing),
            changed=len(self.changed),
        )


@dataclass(frozen=True)
class ReconciliationSummaryRow:
    reconciliation_id: str
    file_fingerprint: str
    source_filename: str
    row_count: int
    added_count: int
    missing_count: int
    changed_count: int
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# pure comparison logic
# --------------------------------------------------------------------------
def _diff(
    base: dict[str, tuple[str, int]],
    uploaded: dict[str, tuple[str, int]],
) -> tuple[
    list[ReconciliationEntry],
    list[ReconciliationEntry],
    list[ReconciliationEntry],
]:
    """Compute per-subject added / missing / changed records."""
    added: list[ReconciliationEntry] = []
    missing: list[ReconciliationEntry] = []
    changed: list[ReconciliationEntry] = []

    for subject_id in sorted(uploaded):
        new_region, new_outcome = uploaded[subject_id]
        if subject_id not in base:
            added.append(
                ReconciliationEntry(
                    subject_id=subject_id,
                    change_type="added",
                    after_region=new_region,
                    after_outcome=new_outcome,
                )
            )
            continue
        old_region, old_outcome = base[subject_id]
        if old_region != new_region or old_outcome != new_outcome:
            changed_fields: list[ChangedField] = []
            if old_region != new_region:
                changed_fields.append(
                    ChangedField("region", old_region, new_region)
                )
            if old_outcome != new_outcome:
                changed_fields.append(
                    ChangedField("outcome", old_outcome, new_outcome)
                )
            changed.append(
                ReconciliationEntry(
                    subject_id=subject_id,
                    change_type="changed",
                    before_region=old_region,
                    after_region=new_region,
                    before_outcome=old_outcome,
                    after_outcome=new_outcome,
                    changed_fields=tuple(changed_fields),
                )
            )

    for subject_id in sorted(base):
        if subject_id not in uploaded:
            old_region, old_outcome = base[subject_id]
            missing.append(
                ReconciliationEntry(
                    subject_id=subject_id,
                    change_type="missing",
                    before_region=old_region,
                    before_outcome=old_outcome,
                )
            )

    added.sort(key=lambda entry: entry.subject_id)
    missing.sort(key=lambda entry: entry.subject_id)
    changed.sort(key=lambda entry: entry.subject_id)
    return added, missing, changed


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------
class ReconciliationService:
    def __init__(self, database) -> None:  # noqa: ANN001 - avoids an import cycle
        self._db = database

    # -- upload / publish --------------------------------------------------
    def submit(
        self,
        *,
        reconciliation_id: str,
        raw: bytes,
        source_filename: str,
    ) -> ReconciliationResult:
        """Validate ``raw`` and publish its reconciliation report atomically.

        Raises :class:`ReconciliationCsvError` for a structurally invalid
        file and :class:`ReconciliationConflict` when the id is already
        bound to a different fingerprint.  Both happen before any write.
        """
        fingerprint = file_fingerprint(raw)
        try:
            parsed_rows = _parse_rows(raw)
        except InvalidCsv as exc:
            raise ReconciliationCsvError(str(exc)) from exc

        uploaded = {
            subject_id: (region, outcome)
            for subject_id, region, outcome in parsed_rows
        }

        with self._db.write_transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM reconciliations WHERE reconciliation_id = ?",
                (reconciliation_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["file_fingerprint"]) != fingerprint:
                    raise ReconciliationConflict(reconciliation_id)
                # Same id + same file: the stored report is the authoritative
                # result, even if two upload requests raced.
                return self._load_result(conn, reconciliation_id, replayed=True)

            base_rows = conn.execute(
                "SELECT subject_id, region, outcome FROM subjects"
            ).fetchall()
            if not base_rows:
                raise DatasetMissing("no dataset imported")
            base = {
                str(row["subject_id"]): (str(row["region"]), int(row["outcome"]))
                for row in base_rows
            }

            added, missing, changed = _diff(base, uploaded)

            # Header plus every detail row commit together: any failure here
            # rolls the whole report back and leaves prior reports untouched.
            conn.execute(
                """
                INSERT INTO reconciliations
                    (reconciliation_id, file_fingerprint, source_filename,
                     row_count, added_count, missing_count, changed_count,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reconciliation_id,
                    fingerprint,
                    source_filename,
                    len(parsed_rows),
                    len(added),
                    len(missing),
                    len(changed),
                    _now(),
                ),
            )
            conn.executemany(
                """
                INSERT INTO reconciliation_entries
                    (reconciliation_id, subject_id, change_type,
                     before_region, after_region, before_outcome,
                     after_outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        reconciliation_id,
                        entry.subject_id,
                        entry.change_type,
                        entry.before_region,
                        entry.after_region,
                        entry.before_outcome,
                        entry.after_outcome,
                    )
                    for entry in (*added, *missing, *changed)
                ],
            )
            result = self._load_result(conn, reconciliation_id, replayed=False)
        logger.info(
            "reconciliation %s published: rows=%d added=%d missing=%d changed=%d",
            reconciliation_id,
            len(parsed_rows),
            len(added),
            len(missing),
            len(changed),
        )
        return result

    # -- reads -------------------------------------------------------------
    def get(self, reconciliation_id: str) -> ReconciliationResult | None:
        with self._db.read_transaction() as conn:
            header = conn.execute(
                "SELECT * FROM reconciliations WHERE reconciliation_id = ?",
                (reconciliation_id,),
            ).fetchone()
            if header is None:
                return None
            return self._load_result(conn, reconciliation_id, replayed=False)

    def list_summaries(self) -> list[ReconciliationSummaryRow]:
        with self._db.read_transaction() as conn:
            rows = conn.execute(
                """
                SELECT reconciliation_id, file_fingerprint, source_filename,
                       row_count, added_count, missing_count, changed_count,
                       created_at
                FROM reconciliations
                ORDER BY created_at DESC, reconciliation_id ASC
                """
            ).fetchall()
            return [
                ReconciliationSummaryRow(
                    reconciliation_id=str(row["reconciliation_id"]),
                    file_fingerprint=str(row["file_fingerprint"]),
                    source_filename=str(row["source_filename"]),
                    row_count=int(row["row_count"]),
                    added_count=int(row["added_count"]),
                    missing_count=int(row["missing_count"]),
                    changed_count=int(row["changed_count"]),
                    created_at=str(row["created_at"]),
                )
                for row in rows
            ]

    # -- row mapping -------------------------------------------------------
    @staticmethod
    def _load_result(
        conn: sqlite3.Connection, reconciliation_id: str, *, replayed: bool
    ) -> ReconciliationResult:
        header = conn.execute(
            "SELECT * FROM reconciliations WHERE reconciliation_id = ?",
            (reconciliation_id,),
        ).fetchone()
        if header is None:  # pragma: no cover - only called for existing rows
            raise ReconciliationCsvError("reconciliation disappeared mid-transaction")

        entry_rows = conn.execute(
            """
            SELECT subject_id, change_type, before_region, after_region,
                   before_outcome, after_outcome
            FROM reconciliation_entries
            WHERE reconciliation_id = ?
            ORDER BY id ASC
            """,
            (reconciliation_id,),
        ).fetchall()

        added: list[ReconciliationEntry] = []
        missing: list[ReconciliationEntry] = []
        changed: list[ReconciliationEntry] = []
        for row in entry_rows:
            change_type = str(row["change_type"])
            before_region = row["before_region"]
            after_region = row["after_region"]
            before_outcome = (
                int(row["before_outcome"])
                if row["before_outcome"] is not None
                else None
            )
            after_outcome = (
                int(row["after_outcome"])
                if row["after_outcome"] is not None
                else None
            )
            changed_fields: list[ChangedField] = []
            if change_type == "changed":
                if before_region != after_region:
                    changed_fields.append(
                        ChangedField("region", before_region, after_region)
                    )
                if before_outcome != after_outcome:
                    changed_fields.append(
                        ChangedField("outcome", before_outcome, after_outcome)
                    )
            entry = ReconciliationEntry(
                subject_id=str(row["subject_id"]),
                change_type=change_type,
                before_region=(
                    str(before_region) if before_region is not None else None
                ),
                after_region=(
                    str(after_region) if after_region is not None else None
                ),
                before_outcome=before_outcome,
                after_outcome=after_outcome,
                changed_fields=tuple(changed_fields),
            )
            if change_type == "added":
                added.append(entry)
            elif change_type == "missing":
                missing.append(entry)
            else:
                changed.append(entry)

        return ReconciliationResult(
            reconciliation_id=str(header["reconciliation_id"]),
            file_fingerprint=str(header["file_fingerprint"]),
            source_filename=str(header["source_filename"]),
            row_count=int(header["row_count"]),
            created_at=str(header["created_at"]),
            added=tuple(added),
            missing=tuple(missing),
            changed=tuple(changed),
            replayed=replayed,
        )
