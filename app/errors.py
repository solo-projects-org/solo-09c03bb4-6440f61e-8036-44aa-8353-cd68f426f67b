"""Domain exceptions shared by the service and the HTTP layer."""

from __future__ import annotations

from decimal import Decimal


class DatasetMissing(Exception):
    """No data set has been imported yet."""


class BudgetExhausted(Exception):
    """The requested epsilon does not fit in the remaining global budget."""

    def __init__(self, requested: Decimal, remaining: Decimal, total: Decimal) -> None:
        self.requested = requested
        self.remaining = remaining
        self.total = total
        super().__init__(
            f"epsilon {requested} exceeds remaining budget {remaining}"
        )


class RequestIdConflict(Exception):
    """A request id was reused with a different parameter set."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(
            f"request id {request_id!r} was already used with different parameters"
        )


class ReconciliationConflict(Exception):
    """A reconciliation id was reused with a different uploaded file."""

    def __init__(self, reconciliation_id: str) -> None:
        self.reconciliation_id = reconciliation_id
        super().__init__(
            f"reconciliation id {reconciliation_id!r} is already bound to a "
            "different file fingerprint"
        )


class ReconciliationCsvError(Exception):
    """The uploaded reconciliation CSV is structurally invalid."""


class UploadTooLarge(ReconciliationCsvError):
    """The uploaded file exceeds the configured size limit."""


class AdminUnauthorized(Exception):
    """The caller did not prove possession of the configured admin token."""


class AdminApiDisabled(Exception):
    """No admin token is configured, so the reconciliation API is switched off."""
