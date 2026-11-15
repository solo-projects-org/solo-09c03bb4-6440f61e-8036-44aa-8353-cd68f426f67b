"""End-to-end tests for the admin reconciliation API."""

from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Settings
from app.importer import file_fingerprint

ADMIN_TOKEN = "test-admin-secret"
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
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
        admin_token=ADMIN_TOKEN,
    )


@pytest.fixture()
def app_client(settings: Settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    application = create_app(settings)
    with TestClient(application) as client:
        yield client


def _upload(client, reconciliation_id: str, content: bytes, *, token=None, filename="revised.csv"):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else AUTH
    return client.post(
        "/v1/reconciliations",
        headers=headers,
        data={"reconciliation_id": reconciliation_id},
        files={"file": (filename, content, "text/csv")},
    )


REVISED = (
    "subject_id,region,outcome\n"
    # A: unchanged.  B: outcome 1 -> 0.  C: region north -> east.
    "A,north,1\n"
    "B,north,0\n"
    "C,east,0\n"
    # D and E are absent from the upload -> two missing.
    "F,east,0\n"
    # G and H are new -> two added.
    "G,west,1\n"
    "H,west,0\n"
)


# --------------------------------------------------------------------------
# happy path: per-subject diff report
# --------------------------------------------------------------------------
def test_report_lists_added_missing_and_changed_with_before_after(app_client):
    response = _upload(app_client, "rec-1", REVISED.encode())
    assert response.status_code == 200
    body = response.json()

    assert body["reconciliation_id"] == "rec-1"
    assert body["file_fingerprint"] == file_fingerprint(REVISED.encode())
    assert body["source_filename"] == "revised.csv"
    assert body["row_count"] == 6
    assert body["replayed"] is False
    assert body["summary"] == {"added": 2, "missing": 2, "changed": 2}

    added = {entry["subject_id"]: entry for entry in body["added"]}
    assert set(added) == {"G", "H"}
    assert added["G"]["after_region"] == "west"
    assert added["G"]["after_outcome"] == 1
    assert added["G"]["before_region"] is None
    assert added["G"]["changed_fields"] == []

    missing = {entry["subject_id"]: entry for entry in body["missing"]}
    assert set(missing) == {"D", "E"}
    assert missing["D"]["before_region"] == "south"
    assert missing["D"]["before_outcome"] == 1
    assert missing["D"]["after_region"] is None

    changed = {entry["subject_id"]: entry for entry in body["changed"]}
    assert set(changed) == {"B", "C"}
    assert changed["B"]["before_outcome"] == 1
    assert changed["B"]["after_outcome"] == 0
    assert changed["B"]["changed_fields"] == [
        {"field": "outcome", "before": 1, "after": 0}
    ]
    assert changed["C"]["changed_fields"] == [
        {"field": "region", "before": "north", "after": "east"}
    ]

    # The unchanged subject is not reported at all.
    reported = (
        {e["subject_id"] for e in body["added"]}
        | {e["subject_id"] for e in body["missing"]}
        | {e["subject_id"] for e in body["changed"]}
    )
    assert "A" not in reported


def test_report_is_fetchable_and_listed(app_client):
    created = _upload(app_client, "rec-1", REVISED.encode()).json()

    fetched = app_client.get("/v1/reconciliations/rec-1", headers=AUTH)
    assert fetched.status_code == 200
    assert fetched.json() == created
    assert fetched.json()["replayed"] is False

    listing = app_client.get("/v1/reconciliations", headers=AUTH)
    assert listing.status_code == 200
    rows = listing.json()["reconciliations"]
    assert len(rows) == 1
    assert rows[0]["reconciliation_id"] == "rec-1"
    assert rows[0]["file_fingerprint"] == created["file_fingerprint"]
    assert rows[0]["added_count"] == 2
    assert rows[0]["missing_count"] == 2
    assert rows[0]["changed_count"] == 2


def test_reconciliation_never_touches_baseline_or_budget(app_client):
    assert _upload(app_client, "rec-1", REVISED.encode()).status_code == 200

    budget = app_client.get("/v1/budget").json()
    assert budget == {"total_budget": "1", "remaining_budget": "1"}

    health = app_client.get("/health").json()
    assert health["dataset_loaded"] is True
    assert health["row_count"] == 6

    # A query against the original data still sees six base subjects:
    # north/outcome=1 remains A and B even though the upload changed B.
    from app.db import Database

    db: Database = app_client.app.state.database
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 6
    assert conn.execute(
        "SELECT COUNT(*) FROM subjects WHERE region = 'north' AND outcome = 1"
    ).fetchone()[0] == 2


# --------------------------------------------------------------------------
# authorization
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/v1/reconciliations", "/v1/reconciliations/rec-1"])
def test_reports_require_admin_token(app_client, path):
    assert app_client.get(path).status_code == 401
    assert app_client.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert (
        app_client.get(path, headers={"Authorization": "Basic abc"}).status_code
        == 401
    )

    response = _upload(app_client, "rec-1", REVISED.encode(), token="wrong-token")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"

    # No token means no report was created.
    assert app_client.get("/v1/reconciliations", headers=AUTH).json() == {
        "reconciliations": []
    }


def test_privacy_endpoints_remain_open_without_admin_token(app_client):
    assert app_client.get("/health").status_code == 200
    assert app_client.get("/v1/budget").status_code == 200
    assert app_client.post(
        "/v1/counts",
        json={"request_id": "q1", "region": "north", "outcome": 1, "epsilon": "0.1"},
    ).status_code == 200


def test_disabled_admin_api_when_no_token_configured(tmp_path, csv_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    disabled = Settings(
        database_path=tmp_path / "disabled.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        max_concurrency=4,
        admin_token=None,
    )
    with TestClient(create_app(disabled)) as client:
        response = _upload(client, "rec-1", REVISED.encode(), token="anything")
        assert response.status_code == 503
        assert response.json()["error"] == "reconciliation_disabled"
        assert client.get("/v1/reconciliations").status_code == 503
        # The non-admin surface still works.
        assert client.get("/health").status_code == 200


# --------------------------------------------------------------------------
# idempotency and conflicts
# --------------------------------------------------------------------------
def test_same_id_same_file_replays_original_report(app_client):
    first = _upload(app_client, "rec-1", REVISED.encode())
    assert first.status_code == 200
    assert first.json()["replayed"] is False

    second = _upload(app_client, "rec-1", REVISED.encode(), filename="copy.csv")
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["file_fingerprint"] == first.json()["file_fingerprint"]
    first_body, second_body = first.json(), second.json()
    second_body.pop("replayed")
    first_body.pop("replayed")
    assert second_body == first_body

    listing = app_client.get("/v1/reconciliations", headers=AUTH).json()
    assert len(listing["reconciliations"]) == 1


def test_same_id_different_file_is_rejected_and_old_report_survives(app_client):
    assert _upload(app_client, "rec-1", REVISED.encode()).status_code == 200

    other = REVISED.replace("G,west,1", "G,west,0")
    conflict = _upload(app_client, "rec-1", other.encode())
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "reconciliation_conflict"

    fetched = app_client.get("/v1/reconciliations/rec-1", headers=AUTH).json()
    assert fetched["file_fingerprint"] == file_fingerprint(REVISED.encode())
    assert fetched["summary"] == {"added": 2, "missing": 2, "changed": 2}
    assert app_client.get("/v1/reconciliations", headers=AUTH).json()[
        "reconciliations"
    ].__len__() == 1


def test_unknown_reconciliation_id_is_404(app_client):
    response = app_client.get("/v1/reconciliations/nope", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"] == "reconciliation_not_found"


# --------------------------------------------------------------------------
# validation: bad files publish nothing
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"subject_id,region,outcome\n",  # header only, no data rows
        b"subject_id,region\nA,north\n",  # missing column
        b"subject_id,region,outcome\nA,north,2\n",  # bad outcome
        b"subject_id,region,outcome\nA,north,1\nA,south,0\n",  # trailing duplicate
        b"subject_id,region,outcome\nA,north\n",  # short row
        b"subject_id,region,outcome\n,north,1\n",  # empty subject id
        b"\xff\xfe\x00bad-bytes",
    ],
)
def test_invalid_csv_leaves_no_report(app_client, content):
    response = _upload(app_client, "bad", content)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_csv"

    assert app_client.get("/v1/reconciliations/bad", headers=AUTH).status_code == 404
    assert (
        app_client.get("/v1/reconciliations", headers=AUTH).json()["reconciliations"]
        == []
    )
    # The failed id is not poisoned: a good upload under the same id works.
    good = _upload(app_client, "bad", REVISED.encode())
    assert good.status_code == 200


def test_oversized_upload_is_rejected_with_413(tmp_path, csv_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    small_limit = Settings(
        database_path=tmp_path / "small.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        max_concurrency=4,
        admin_token=ADMIN_TOKEN,
        max_upload_bytes=16,
    )
    with TestClient(create_app(small_limit)) as client:
        response = _upload(client, "big", REVISED.encode())
        assert response.status_code == 413
        assert response.json()["error"] == "file_too_large"
        assert client.get("/v1/reconciliations", headers=AUTH).json() == {
            "reconciliations": []
        }


def test_invalid_reconciliation_id_is_422(app_client):
    response = _upload(app_client, "bad id!", REVISED.encode())
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


# --------------------------------------------------------------------------
# atomicity and concurrency
# --------------------------------------------------------------------------
def test_write_failure_publishes_nothing_and_keeps_old_report(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    application = create_app(settings)

    # An older report must survive a failed new upload.
    with TestClient(application) as client:
        assert _upload(client, "old", REVISED.encode()).status_code == 200

    # A trigger aborts every insert into the detail table, simulating a write
    # failure *inside* the publish transaction.  The header insert must roll
    # back together with it, leaving no half report.
    db = application.state.database
    conn = db.connect()
    conn.execute(
        """
        CREATE TRIGGER block_recon_entries
        BEFORE INSERT ON reconciliation_entries
        BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END
        """
    )

    with TestClient(application, raise_server_exceptions=False) as client:
        response = _upload(client, "new", REVISED.encode())
        assert response.status_code == 500
        assert response.json()["error"] == "internal_error"

    conn.execute("DROP TRIGGER block_recon_entries")

    with TestClient(application) as client:
        # No half report for the failed id; the old report is untouched.
        assert (
            client.get("/v1/reconciliations/new", headers=AUTH).status_code == 404
        )
        old = client.get("/v1/reconciliations/old", headers=AUTH)
        assert old.status_code == 200
        assert old.json()["summary"] == {"added": 2, "missing": 2, "changed": 2}

        # The budget and the base table were never involved.
        assert client.get("/v1/budget").json()["remaining_budget"] == "1"

        # A retry after the transient failure succeeds.
        assert _upload(client, "new", REVISED.encode()).status_code == 200


def test_concurrent_same_id_same_file_returns_one_shared_report(app_client):
    results: list[dict] = []
    barrier = threading.Barrier(5)
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        response = _upload(app_client, "race", REVISED.encode())
        with lock:
            results.append(
                {"status": response.status_code, "body": response.json()}
            )

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(item["status"] == 200 for item in results)
    fingerprints = {item["body"]["file_fingerprint"] for item in results}
    assert fingerprints == {file_fingerprint(REVISED.encode())}
    assert all(
        item["body"]["summary"] == {"added": 2, "missing": 2, "changed": 2}
        for item in results
    )
    # Exactly one published report, and every participant got that one.
    rows = app_client.get("/v1/reconciliations", headers=AUTH).json()[
        "reconciliations"
    ]
    assert [row["reconciliation_id"] for row in rows] == ["race"]


def test_concurrent_different_ids_never_mix_data(app_client):
    payloads = {
        "p1": REVISED.encode(),
        "p2": (
            "subject_id,region,outcome\n"
            "A,south,1\n"  # A changes region; B..F missing; no additions
        ).encode(),
        "p3": (
            "subject_id,region,outcome\n"
            "A,north,1\nB,north,1\nC,north,0\nD,south,1\nE,south,0\nF,east,0\n"
            "X,north,1\n"  # identical base plus one addition
        ).encode(),
    }
    statuses: list[tuple[str, int]] = []
    barrier = threading.Barrier(3)
    lock = threading.Lock()

    def worker(rec_id: str, content: bytes) -> None:
        barrier.wait()
        response = _upload(app_client, rec_id, content)
        with lock:
            statuses.append((rec_id, response.status_code))

    threads = [
        threading.Thread(target=worker, args=(rec_id, content))
        for rec_id, content in payloads.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert dict(statuses) == {"p1": 200, "p2": 200, "p3": 200}

    r1 = app_client.get("/v1/reconciliations/p1", headers=AUTH).json()
    r2 = app_client.get("/v1/reconciliations/p2", headers=AUTH).json()
    r3 = app_client.get("/v1/reconciliations/p3", headers=AUTH).json()

    assert r1["file_fingerprint"] == file_fingerprint(payloads["p1"])
    assert r2["file_fingerprint"] == file_fingerprint(payloads["p2"])
    assert r3["file_fingerprint"] == file_fingerprint(payloads["p3"])

    assert r1["summary"] == {"added": 2, "missing": 2, "changed": 2}
    assert r2["summary"] == {"added": 0, "missing": 5, "changed": 1}
    assert {e["subject_id"] for e in r2["missing"]} == {"B", "C", "D", "E", "F"}
    assert r3["summary"] == {"added": 1, "missing": 0, "changed": 0}
    assert [e["subject_id"] for e in r3["added"]] == ["X"]


def test_concurrent_same_id_different_files_only_one_wins(app_client):
    other = REVISED.replace("H,west,0", "H,west,1")
    outcomes: list[int] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def worker(content: bytes) -> None:
        barrier.wait()
        response = _upload(app_client, "clash", content)
        with lock:
            outcomes.append(response.status_code)

    threads = [
        threading.Thread(target=worker, args=(REVISED.encode(),)),
        threading.Thread(target=worker, args=(other.encode(),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == [200, 409]
    stored = app_client.get("/v1/reconciliations/clash", headers=AUTH).json()
    assert stored["file_fingerprint"] in {
        file_fingerprint(REVISED.encode()),
        file_fingerprint(other.encode()),
    }
    # The loser keeps getting rejected while the winner's report is stable.
    retry = _upload(
        app_client,
        "clash",
        other.encode()
        if stored["file_fingerprint"] == file_fingerprint(REVISED.encode())
        else REVISED.encode(),
    )
    assert retry.status_code == 409


# --------------------------------------------------------------------------
# durability
# --------------------------------------------------------------------------
def test_report_survives_restart(tmp_path, csv_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = Settings(
        database_path=tmp_path / "restart.db",
        csv_path=csv_path,
        total_budget=Decimal("1"),
        import_on_startup=True,
        sqlite_busy_timeout_ms=30_000,
        max_concurrency=4,
        admin_token=ADMIN_TOKEN,
    )
    with TestClient(create_app(settings)) as client:
        first = _upload(client, "durable", REVISED.encode()).json()

    with TestClient(create_app(settings)) as client:
        fetched = client.get("/v1/reconciliations/durable", headers=AUTH).json()
        assert fetched["file_fingerprint"] == first["file_fingerprint"]
        assert fetched["summary"] == first["summary"]
        # Retrying with the same file after a restart is a free replay.
        replay = _upload(client, "durable", REVISED.encode()).json()
        assert replay["replayed"] is True
        assert replay["added"] == first["added"]
