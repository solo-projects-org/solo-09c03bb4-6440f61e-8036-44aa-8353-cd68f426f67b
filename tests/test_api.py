"""End-to-end tests for the privacy API."""

from __future__ import annotations

import importlib
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.importer import import_csv


@pytest.fixture()
def csv_path(tmp_path: Path) -> Path:
    path = tmp_path / "followup.csv"
    path.write_text(
        "subject_id,region,outcome\n"
        "A,north,1\n"
        "B,north,1\n"
        "C,north,0\n"
        "D,south,1\n"
        "E,south,0\n"
        "F,east,0\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def settings(tmp_path: Path, csv_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "test.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        max_concurrency=4,
    )


@pytest.fixture()
def app_client(settings: Settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    application = create_app(settings)
    with TestClient(application) as client:
        yield client


# --------------------------------------------------------------------------
# importer
# --------------------------------------------------------------------------
def test_import_is_idempotent_and_never_replaces(tmp_path: Path, csv_path: Path):
    db = Database(tmp_path / "import.db")
    db.initialise()

    first = import_csv(db, csv_path, Decimal("1"))
    assert first.imported is True
    assert first.row_count == 6

    second = import_csv(db, csv_path, Decimal("1"))
    assert second.imported is False
    assert second.row_count == 6

    # A *different* file must not replace the original data set.
    other = tmp_path / "other.csv"
    other.write_text(
        "subject_id,region,outcome\nZ,north,1\n", encoding="utf-8"
    )
    third = import_csv(db, other, Decimal("1"))
    assert third.imported is False
    assert third.row_count == 6

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 6
    assert conn.execute("SELECT budget_spent FROM dataset").fetchone()[0] == "0"


def test_import_rejects_duplicate_subject_ids(tmp_path: Path):
    path = tmp_path / "dup.csv"
    path.write_text(
        "subject_id,region,outcome\nA,north,1\nA,south,0\n", encoding="utf-8"
    )
    db = Database(tmp_path / "dup.db")
    with pytest.raises(Exception):
        import_csv(db, path, Decimal("1"))
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dataset").fetchone()[0] == 0


def test_import_rejects_bad_outcome(tmp_path: Path):
    path = tmp_path / "bad.csv"
    path.write_text(
        "subject_id,region,outcome\nA,north,2\n", encoding="utf-8"
    )
    db = Database(tmp_path / "bad.db")
    with pytest.raises(Exception):
        import_csv(db, path, Decimal("1"))


# --------------------------------------------------------------------------
# API behaviour
# --------------------------------------------------------------------------
def test_budget_endpoint_and_health(app_client):
    health = app_client.get("/health").json()
    assert health["status"] == "ok"
    assert health["dataset_loaded"] is True
    assert health["row_count"] == 6

    budget = app_client.get("/v1/budget").json()
    assert budget == {"total_budget": "1", "remaining_budget": "1"}


def test_query_charges_budget_and_returns_noisy_count(app_client):
    response = app_client.post(
        "/v1/counts",
        json={
            "request_id": "r1",
            "region": "north",
            "outcome": 1,
            "epsilon": "0.1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["noisy_count"], int)
    assert body["noisy_count"] >= 0
    assert body["remaining_budget"] == "0.9"
    assert body["replayed"] is False
    assert body["charged"] is True
    assert "0.1" in response.text

    budget = app_client.get("/v1/budget").json()
    assert budget["remaining_budget"] == "0.9"


def test_retry_same_id_same_params_returns_same_value_free(app_client):
    payload = {"request_id": "r1", "region": "north", "outcome": 1, "epsilon": "0.1"}
    first = app_client.post("/v1/counts", json=payload).json()
    second = app_client.post("/v1/counts", json=payload).json()

    assert second["noisy_count"] == first["noisy_count"]
    assert second["remaining_budget"] == first["remaining_budget"] == "0.9"
    assert second["replayed"] is True
    assert second["charged"] is False

    assert app_client.get("/v1/budget").json()["remaining_budget"] == "0.9"


def test_retry_same_id_different_params_is_rejected(app_client):
    payload = {"request_id": "r1", "region": "north", "outcome": 1, "epsilon": "0.1"}
    assert app_client.post("/v1/counts", json=payload).status_code == 200

    changed = dict(payload, epsilon="0.2")
    response = app_client.post("/v1/counts", json=changed)
    assert response.status_code == 409
    assert response.json()["error"] == "request_id_conflict"

    # Budget unchanged by the rejected request.
    assert app_client.get("/v1/budget").json()["remaining_budget"] == "0.9"


def test_epsilon_equivalent_forms_are_the_same_parameters(app_client):
    first = app_client.post(
        "/v1/counts",
        json={"request_id": "r1", "region": "north", "outcome": 1, "epsilon": 0.1},
    )
    assert first.status_code == 200
    second = app_client.post(
        "/v1/counts",
        json={"request_id": "r1", "region": "north", "outcome": 1, "epsilon": "0.10"},
    )
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["noisy_count"] == first.json()["noisy_count"]


@pytest.mark.parametrize(
    "payload",
    [
        {"request_id": "r", "region": "north", "outcome": 1, "epsilon": 0},
        {"request_id": "r", "region": "north", "outcome": 1, "epsilon": 1.5},
        {"request_id": "r", "region": "north", "outcome": 1, "epsilon": -0.1},
        {"request_id": "r", "region": "north", "outcome": 2, "epsilon": 0.1},
        {"request_id": "r", "region": "north", "outcome": True, "epsilon": 0.1},
        {"request_id": "r", "region": "", "outcome": 1, "epsilon": 0.1},
        {"request_id": "bad id!", "region": "north", "outcome": 1, "epsilon": 0.1},
        {"region": "north", "outcome": 1, "epsilon": 0.1},
        {"request_id": "r", "region": "north", "outcome": 1, "epsilon": 0.1, "x": 1},
    ],
)
def test_invalid_requests_do_not_charge_budget(app_client, payload):
    before = app_client.get("/v1/budget").json()["remaining_budget"]
    response = app_client.post("/v1/counts", json=payload)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    assert app_client.get("/v1/budget").json()["remaining_budget"] == before
    assert app_client.get(f"/v1/requests/{payload.get('request_id', 'x')}").status_code == 404


def test_budget_exhaustion_blocks_fresh_queries_but_not_replays(app_client):
    payload = {"request_id": "a", "region": "north", "outcome": 1, "epsilon": "0.6"}
    assert app_client.post("/v1/counts", json=payload).status_code == 200

    payload_b = dict(payload, request_id="b", epsilon="0.6")
    response = app_client.post("/v1/counts", json=payload_b)
    assert response.status_code == 429
    assert response.json()["error"] == "budget_exhausted"
    assert app_client.get("/v1/budget").json()["remaining_budget"] == "0.4"

    # The original request can still be replayed for free.
    replay = app_client.post("/v1/counts", json=payload)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True


def test_budget_boundary_is_exact(app_client):
    # 0.1 + 0.2 styled decimal arithmetic must not leave 0.7-ish float dust.
    for index in range(10):
        payload = {
            "request_id": f"step-{index}",
            "region": "north",
            "outcome": 0,
            "epsilon": "0.1",
        }
        assert app_client.post("/v1/counts", json=payload).status_code == 200
    assert app_client.get("/v1/budget").json()["remaining_budget"] == "0"

    extra = {
        "request_id": "step-10",
        "region": "north",
        "outcome": 0,
        "epsilon": "0.1",
    }
    assert app_client.post("/v1/counts", json=extra).status_code == 429


def test_concurrent_requests_never_overspend(tmp_path: Path, csv_path: Path):
    """Twenty simultaneous 0.1 queries against a budget of 1.0: exactly ten
    may succeed, the rest must be rejected."""
    settings = Settings(
        database_path=tmp_path / "concurrent.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=60_000,
        max_concurrency=8,
    )
    from fastapi.testclient import TestClient

    from app.main import create_app

    application = create_app(settings)
    with TestClient(application) as client:
        statuses: list[int] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            response = client.post(
                "/v1/counts",
                json={
                    "request_id": f"concurrent-{index}",
                    "region": "north",
                    "outcome": 1,
                    "epsilon": "0.1",
                },
            )
            with lock:
                statuses.append(response.status_code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert statuses.count(200) == 10
        assert statuses.count(429) == 10
        assert client.get("/v1/budget").json()["remaining_budget"] == "0"


def test_persistence_across_restart_returns_original_value(tmp_path: Path, csv_path: Path):
    settings = Settings(
        database_path=tmp_path / "restart.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        max_concurrency=4,
    )
    from fastapi.testclient import TestClient

    from app.main import create_app

    payload = {"request_id": "durable", "region": "north", "outcome": 1, "epsilon": "0.1"}

    with TestClient(create_app(settings)) as client:
        first = client.post("/v1/counts", json=payload).json()

    # Simulate a fresh process: new app object, same database file.  The
    # importer must not re-import and the stored answer must still be there.
    with TestClient(create_app(settings)) as client:
        replay = client.post("/v1/counts", json=payload).json()
        assert replay["noisy_count"] == first["noisy_count"]
        assert replay["remaining_budget"] == first["remaining_budget"]
        assert replay["replayed"] is True
        assert client.get("/v1/budget").json()["remaining_budget"] == "0.9"


def test_save_failure_does_not_charge_budget(tmp_path: Path, csv_path: Path, monkeypatch):
    settings = Settings(
        database_path=tmp_path / "failure.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        max_concurrency=4,
    )
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import SqliteRepository

    application = create_app(settings)
    with TestClient(application, raise_server_exceptions=False) as client:
        original_apply = SqliteRepository.apply_query

        def failing_apply(self, **kwargs):
            raise sqlite3.OperationalError("simulated disk failure")

        monkeypatch.setattr(SqliteRepository, "apply_query", failing_apply)
        response = client.post(
            "/v1/counts",
            json={
                "request_id": "fail-1",
                "region": "north",
                "outcome": 1,
                "epsilon": "0.5",
            },
        )
        assert response.status_code == 500

        monkeypatch.setattr(SqliteRepository, "apply_query", original_apply)
        assert client.get("/v1/budget").json()["remaining_budget"] == "1"

        # Nothing was stored, so the id is still usable.
        retry = client.post(
            "/v1/counts",
            json={
                "request_id": "fail-1",
                "region": "north",
                "outcome": 1,
                "epsilon": "0.5",
            },
        )
        assert retry.status_code == 200
        assert retry.json()["replayed"] is False
        assert retry.json()["remaining_budget"] == "0.5"


def test_partial_write_failure_rolls_back_both_rows(tmp_path: Path, csv_path: Path):
    """Force the budget UPDATE to fail after the request row was inserted."""
    db = Database(tmp_path / "partial.db")
    db.initialise()
    import_csv(db, csv_path, Decimal("1"))
    repository = __import__("app.models", fromlist=["SqliteRepository"]).SqliteRepository(db)

    conn = db.connect()
    conn.execute(
        """
        CREATE TRIGGER block_budget BEFORE UPDATE ON dataset
        BEGIN SELECT RAISE(ABORT, 'blocked'); END
        """
    )

    with pytest.raises(sqlite3.Error):
        repository.apply_query(
            request_id="partial",
            fingerprint="fp",
            region="north",
            outcome=1,
            epsilon=Decimal("0.5"),
            noisy_count=1,
        )

    conn.execute("DROP TRIGGER block_budget")
    assert conn.execute("SELECT budget_spent FROM dataset").fetchone()[0] == "0"
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


def test_openapi_schema_available(app_client):
    response = app_client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/v1/counts" in schema["paths"]
    assert "/docs" not in {p for p in schema["paths"]}
    response_docs = app_client.get("/docs")
    assert response_docs.status_code == 200


def test_no_exact_count_field_in_responses(app_client):
    response = app_client.post(
        "/v1/counts",
        json={"request_id": "r", "region": "north", "outcome": 1, "epsilon": "1"},
    ).json()
    for key in response:
        assert key not in {"count", "true_count", "exact_count"}


def test_unknown_region_returns_noiseless_free_answer(app_client):
    # An unknown region has a true count of zero; the noisy answer may be
    # anything, but it is still charged and stored.
    response = app_client.post(
        "/v1/counts",
        json={"request_id": "missing", "region": "nowhere", "outcome": 1, "epsilon": "0.1"},
    )
    assert response.status_code == 200
    assert response.json()["remaining_budget"] == "0.9"


def test_lookup_endpoint(app_client):
    payload = {"request_id": "lookup", "region": "north", "outcome": 1, "epsilon": "0.2"}
    created = app_client.post("/v1/counts", json=payload).json()
    lookup = app_client.get("/v1/requests/lookup").json()
    assert lookup["noisy_count"] == created["noisy_count"]
    assert lookup["replayed"] is True
    assert lookup["charged"] is False
    assert lookup["remaining_budget"] == created["remaining_budget"]
