"""Privacy-preserving follow-up count service.

Layout
------
- ``app.config``          runtime settings (paths, budget, admin token).
- ``app.db``              SQLite connection helpers, schema, transactions.
- ``app.importer``        one-shot CSV import.
- ``app.privacy``         Laplace mechanism and budget bookkeeping.
- ``app.reconciliation``  admin CSV reconciliation reports (exact per-subject diffs).
- ``app.main``            FastAPI application and request handling.
"""

__version__ = "1.0.0"
