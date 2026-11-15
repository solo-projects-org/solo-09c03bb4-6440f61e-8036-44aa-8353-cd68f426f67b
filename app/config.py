"""Runtime configuration.

All paths and the total privacy budget can be overridden with environment
variables so the container does not need code changes to be relocated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

#: Total lifetime privacy budget.  Parsed straight into a ``Decimal`` from the
#: environment string so no float rounding ever touches budget arithmetic.
DEFAULT_TOTAL_BUDGET = "1"

#: CSV that is imported on first start when no data set is present.
DEFAULT_CSV_PATH = "seed/followup.csv"

#: Reconciliation uploads larger than this are rejected.
DEFAULT_MAX_UPLOAD_MB = 10


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value


@dataclass(frozen=True)
class Settings:
    database_path: Path
    csv_path: Path
    total_budget: Decimal
    import_on_startup: bool
    sqlite_busy_timeout_ms: int
    #: Accepted for deployment parity; request handlers are threadpool tasks
    #: whose write path is serialised by SQLite's ``BEGIN IMMEDIATE``, so this
    #: value documents intent rather than gating anything.
    max_concurrency: int = 4
    #: Bearer token guarding the admin-only reconciliation API.  ``None`` means
    #: the API is configured off and every call to it answers 503.
    admin_token: str | None = None
    #: Maximum accepted reconciliation upload size in bytes.
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_MB * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        total = Decimal(_env("TOTAL_BUDGET", DEFAULT_TOTAL_BUDGET))
        if not total.is_finite() or total <= 0:
            raise ValueError("TOTAL_BUDGET must be a positive finite number")
        token = os.environ.get("ADMIN_TOKEN")
        admin_token = token.strip() if token and token.strip() else None
        max_upload_mb = max(1, int(_env("MAX_UPLOAD_MB", str(DEFAULT_MAX_UPLOAD_MB))))
        return cls(
            database_path=Path(_env("DATABASE_PATH", "data/followup.db")),
            csv_path=Path(_env("CSV_PATH", DEFAULT_CSV_PATH)),
            total_budget=total,
            import_on_startup=_env("IMPORT_ON_STARTUP", "1").lower()
            not in {"0", "false", "no"},
            sqlite_busy_timeout_ms=int(_env("SQLITE_BUSY_TIMEOUT_MS", "30000")),
            max_concurrency=max(1, int(_env("MAX_CONCURRENCY", "4"))),
            admin_token=admin_token,
            max_upload_bytes=max_upload_mb * 1024 * 1024,
        )
