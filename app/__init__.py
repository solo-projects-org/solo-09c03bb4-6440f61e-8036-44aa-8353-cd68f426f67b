"""Privacy-preserving follow-up count service.

Layout
------
- ``app.config``      runtime settings (paths, budget).
- ``app.db``          SQLite connection helpers, schema, transactions.
- ``app.importer``    one-shot CSV import.
- ``app.privacy``     Laplace mechanism and budget bookkeeping.
- ``app.main``        FastAPI application and request handling.
"""

__version__ = "1.0.0"
