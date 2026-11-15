"""HTTP interface (FastAPI).

Endpoints
---------
``GET  /health``                 liveness plus whether a data set is loaded
``GET  /v1/budget``              remaining global privacy budget
``POST /v1/counts``              privatized count for one (region, outcome)
``GET  /v1/requests/{id}``       look up a previously released answer

Only noisy counts and budget figures ever leave the process; exact counts are
never part of any response model.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .config import Settings
from .db import Database
from .errors import BudgetExhausted, DatasetMissing, RequestIdConflict
from .importer import ImportError_, import_csv
from .models import SqliteRepository
from .privacy import MIN_EPSILON, canonical_decimal, canonical_epsilon
from .service import QueryService

logger = logging.getLogger("followup_counts")

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REGION_MAX_LEN = 128
MAX_EPSILON = Decimal("1")


# --------------------------------------------------------------------------
# request / response models
# --------------------------------------------------------------------------
class CountQuery(BaseModel):
    """Parameters of a single privatized count query."""

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "examples": [
                {
                    "request_id": "req-0001",
                    "region": "north",
                    "outcome": 1,
                    "epsilon": "0.1",
                }
            ]
        },
    }

    request_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Client-generated idempotency key (letters, digits, . _ : -).",
    )
    region: str = Field(
        ...,
        min_length=1,
        max_length=REGION_MAX_LEN,
        description="Region exactly as it appears in the imported CSV.",
    )
    outcome: int = Field(
        ...,
        strict=True,
        ge=0,
        le=1,
        description="Follow-up outcome: 0 or 1.",
    )
    epsilon: Decimal = Field(
        ...,
        gt=0,
        le=1,
        description="Privacy budget for this answer; Laplace scale is 1/epsilon.",
    )

    @field_validator("request_id")
    @classmethod
    def _valid_request_id(cls, value: str) -> str:
        if not REQUEST_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "request_id must start with a letter or digit and contain only "
                "letters, digits, '.', '_', ':' or '-'"
            )
        return value

    @field_validator("region")
    @classmethod
    def _valid_region(cls, value: str) -> str:
        if not value:
            raise ValueError("region must not be empty")
        if value != value.strip():
            raise ValueError("region must not have leading or trailing whitespace")
        return value

    @field_validator("epsilon")
    @classmethod
    def _valid_epsilon(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("epsilon must be finite")
        try:
            canonical = canonical_epsilon(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if not (MIN_EPSILON <= value <= MAX_EPSILON):
            raise ValueError("epsilon is outside the representable range")
        return Decimal(canonical)


class CountResponse(BaseModel):
    request_id: str
    region: str
    outcome: int
    epsilon: str = Field(description="Canonical epsilon charged for this answer.")
    noisy_count: int = Field(
        description="Laplace-perturbed count (never the exact count)."
    )
    remaining_budget: str = Field(
        description="Global privacy budget left after this query, as a decimal."
    )
    replayed: bool = Field(
        description="True when a stored answer was reused; no budget charged."
    )
    charged: bool = Field(description="True when this call spent budget.")


class BudgetResponse(BaseModel):
    total_budget: str
    remaining_budget: str


class HealthResponse(BaseModel):
    status: str
    version: str
    dataset_loaded: bool
    row_count: int | None = None


class ErrorBody(BaseModel):
    error: str
    message: str


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": code, "message": message, **extra},
    )


def _dataset_info(database: Database) -> tuple[bool, int | None]:
    conn = database.connect()
    row = conn.execute("SELECT row_count FROM dataset WHERE id = 1").fetchone()
    return (row is not None, int(row["row_count"]) if row else None)


# --------------------------------------------------------------------------
# application factory
# --------------------------------------------------------------------------
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path, settings.sqlite_busy_timeout_ms)
    repository = SqliteRepository(database)
    service = QueryService(repository, settings.total_budget)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.initialise()
        if settings.import_on_startup:
            try:
                result = import_csv(
                    database, settings.csv_path, settings.total_budget
                )
                logger.info(
                    "dataset ready: rows=%d imported_now=%s",
                    result.row_count,
                    result.imported,
                )
            except ImportError_ as exc:
                # A missing or malformed seed is not fatal: the API reports
                # "no dataset" rather than refusing to boot, which keeps
                # health checks and Swagger reachable for operators.
                logger.warning("CSV import skipped: %s", exc)
        yield
        database.close_thread_connection()

    app = FastAPI(
        title="Follow-up Count Privacy API",
        version=__version__,
        description=(
            "Differentially private follow-up counts.\n\n"
            "The data set is imported once at startup and is never replaced. "
            "Each successful query releases a Laplace-noised count "
            "(scale = 1/epsilon, sensitivity 1) and charges epsilon against a "
            "global budget of 1.0. Retrying the same `request_id` with "
            "identical parameters returns the original answer for free; "
            "reusing the id with different parameters is rejected with 409. "
            "Invalid requests and failed saves never spend budget."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.service = service
    app.state.repository = repository

    # -- endpoints ---------------------------------------------------------
    @app.get(
        "/health",
        response_model=HealthResponse,
        summary="Liveness and data-set availability",
        tags=["operations"],
    )
    def health() -> HealthResponse:
        loaded, row_count = _dataset_info(database)
        return HealthResponse(
            status="ok",
            version=__version__,
            dataset_loaded=loaded,
            row_count=row_count,
        )

    @app.get(
        "/v1/budget",
        response_model=BudgetResponse,
        summary="Remaining global privacy budget",
        tags=["privacy"],
        responses={503: {"model": ErrorBody, "description": "No data set imported"}},
    )
    def budget():
        try:
            snapshot = repository.budget_snapshot()
        except DatasetMissing as exc:
            return _error(503, "dataset_missing", str(exc))
        return BudgetResponse(
            total_budget=canonical_decimal(snapshot.total),
            remaining_budget=canonical_decimal(snapshot.remaining),
        )

    @app.post(
        "/v1/counts",
        response_model=CountResponse,
        summary="Privatized count for a region and outcome",
        tags=["queries"],
        responses={
            200: {"description": "Fresh release or idempotent replay"},
            409: {"model": ErrorBody, "description": "Request id parameter conflict"},
            422: {"model": ErrorBody, "description": "Invalid request"},
            429: {"model": ErrorBody, "description": "Privacy budget exhausted"},
            503: {"model": ErrorBody, "description": "No data set imported"},
        },
    )
    def counts(body: CountQuery):
        try:
            outcome = service.query(
                request_id=body.request_id,
                region=body.region,
                outcome=body.outcome,
                epsilon=body.epsilon,
            )
        except RequestIdConflict as exc:
            return _error(409, "request_id_conflict", str(exc))
        except BudgetExhausted as exc:
            return _error(
                429,
                "budget_exhausted",
                f"requested epsilon {exc.requested} exceeds remaining "
                f"budget {exc.remaining}",
                remaining_budget=canonical_decimal(exc.remaining),
            )
        except DatasetMissing as exc:
            return _error(503, "dataset_missing", str(exc))

        return CountResponse(
            request_id=body.request_id,
            region=body.region,
            outcome=body.outcome,
            epsilon=outcome.epsilon,
            noisy_count=outcome.noisy_count,
            remaining_budget=canonical_decimal(outcome.remaining_budget),
            replayed=outcome.replayed,
            charged=outcome.charged,
        )

    @app.get(
        "/v1/requests/{request_id}",
        response_model=CountResponse,
        summary="Replay a previously released answer",
        tags=["queries"],
        responses={404: {"model": ErrorBody, "description": "Unknown request id"}},
    )
    def get_request(request_id: str):
        lookup = repository.lookup(request_id)
        if lookup.stored is None:
            return _error(
                404, "request_not_found", "no released answer for this request id"
            )
        stored = lookup.stored
        return CountResponse(
            request_id=stored.request_id,
            region=stored.region,
            outcome=stored.outcome,
            epsilon=stored.epsilon,
            noisy_count=stored.noisy_count,
            remaining_budget=canonical_decimal(lookup.remaining_budget),
            replayed=True,
            charged=False,
        )

    # -- uniform error shape ----------------------------------------------
    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        details = []
        for error in exc.errors():
            if isinstance(error, dict):
                location = error.get("loc", ())
                message = error.get("msg", "")
                kind = error.get("type", "")
            else:  # pragma: no cover - defensive for future FastAPI versions
                location = getattr(error, "loc", ())
                message = getattr(error, "msg", "")
                kind = getattr(error, "type", "")
            details.append(
                {
                    "location": list(location),
                    "message": message,
                    "type": kind,
                }
            )
        return _error(
            422,
            "invalid_request",
            "request validation failed",
            details=details,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return _error(500, "internal_error", "internal server error")

    return app


app = create_app()
