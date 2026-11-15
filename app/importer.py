"""One-shot CSV import.

The data set is imported exactly once.  Subsequent starts (or a second
import attempt) never replace existing data: the presence of the singleton
``dataset`` row short-circuits the import.  The whole import runs in one
``BEGIN IMMEDIATE`` transaction, so a malformed row leaves no partial data
behind and no dataset row.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .db import Database, dataset_row

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("subject_id", "region", "outcome")

#: Column names accepted as aliases for the canonical ones.
COLUMN_ALIASES = {
    "subject_id": "subject_id",
    "subjectid": "subject_id",
    "subject": "subject_id",
    "id": "subject_id",
    "region": "region",
    "outcome": "outcome",
}


class ImportError_(Exception):
    """Base class for import failures."""


class InvalidCsv(ImportError_):
    """The CSV is structurally invalid."""


class DatasetAlreadyImported(ImportError_):
    """A data set already exists; replacement is forbidden."""


@dataclass(frozen=True)
class ImportResult:
    imported: bool
    row_count: int
    fingerprint: str
    reason: str | None = None


def file_fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _normalise_header(row: list[str]) -> list[str]:
    cleaned = [cell.replace("﻿", "").strip().lower() for cell in row]
    return [COLUMN_ALIASES.get(cell, cell) for cell in cleaned]


def _parse_rows(raw: bytes) -> list[tuple[str, str, int]]:
    """Validate the entire CSV in memory before touching the database."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidCsv(f"CSV must be UTF-8 encoded: {exc}") from exc

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise InvalidCsv("CSV is empty") from None

    header = _normalise_header(header)
    positions: dict[str, int] = {}
    for column in REQUIRED_COLUMNS:
        if column not in header:
            raise InvalidCsv(
                f"missing required column {column!r}; "
                f"expected columns: {', '.join(REQUIRED_COLUMNS)}"
            )
        if header.count(column) > 1:
            raise InvalidCsv(f"duplicate column {column!r}")
        positions[column] = header.index(column)

    rows: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for line_no, record in enumerate(reader, start=2):
        if not record or all(cell.strip() == "" for cell in record):
            continue  # tolerate blank lines
        if len(record) < len(header):
            raise InvalidCsv(f"line {line_no}: expected {len(header)} fields")
        try:
            subject_id = record[positions["subject_id"]].strip()
            region = record[positions["region"]].strip()
            outcome_raw = record[positions["outcome"]].strip()
        except IndexError as exc:
            raise InvalidCsv(f"line {line_no}: malformed row") from exc

        if not subject_id:
            raise InvalidCsv(f"line {line_no}: empty subject_id")
        if not region:
            raise InvalidCsv(f"line {line_no}: empty region")
        if outcome_raw not in {"0", "1"}:
            raise InvalidCsv(
                f"line {line_no}: outcome must be 0 or 1, got {outcome_raw!r}"
            )
        if subject_id in seen:
            raise InvalidCsv(f"line {line_no}: duplicate subject_id {subject_id!r}")
        seen.add(subject_id)
        rows.append((subject_id, region, int(outcome_raw)))

    if not rows:
        raise InvalidCsv("CSV contains a header but no data rows")
    return rows


def import_csv(database: Database, csv_path: Path, total_budget: Decimal) -> ImportResult:
    """Import ``csv_path`` if and only if no data set exists yet."""
    path = Path(csv_path)
    if not path.is_file():
        raise InvalidCsv(f"CSV file not found: {path}")
    raw = path.read_bytes()
    fingerprint = file_fingerprint(raw)

    database.initialise()
    conn = database.connect()
    existing = dataset_row(conn)
    if existing is not None:
        if existing["fingerprint"] == fingerprint:
            logger.info("dataset already imported from %s; skipping", path)
        else:
            logger.info(
                "dataset already imported (source %s); refusing to replace it "
                "with %s",
                existing["source"],
                path,
            )
        return ImportResult(
            imported=False,
            row_count=int(existing["row_count"]),
            fingerprint=str(existing["fingerprint"]),
            reason="dataset already imported",
        )

    # Validation happens before the transaction so a bad file cannot even
    # take the write lock.
    rows = _parse_rows(raw)
    source = str(path)

    with database.write_transaction() as conn:
        # Re-check under the write lock: another process may have imported
        # between our read and this transaction.
        existing = dataset_row(conn)
        if existing is not None:
            return ImportResult(
                imported=False,
                row_count=int(existing["row_count"]),
                fingerprint=str(existing["fingerprint"]),
                reason="dataset already imported",
            )
        conn.executemany(
            "INSERT INTO subjects (subject_id, region, outcome) VALUES (?, ?, ?)",
            rows,
        )
        conn.execute(
            """
            INSERT INTO dataset
                (id, imported_at, source, row_count, total_budget,
                 budget_spent, fingerprint)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                source,
                len(rows),
                str(total_budget),
                "0",
                fingerprint,
            ),
        )
    logger.info("imported %d subjects from %s", len(rows), path)
    return ImportResult(
        imported=True, row_count=len(rows), fingerprint=fingerprint
    )
