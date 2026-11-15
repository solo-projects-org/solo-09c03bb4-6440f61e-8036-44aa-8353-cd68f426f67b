# Follow-up Count Privacy API

A small statistical API that hands collaborating research groups **noisy**
follow-up counts, never exact ones. Subject-level data is imported once from
a CSV and is never replaced afterwards. Each query for a
(`region`, `outcome`) cell answers with a count perturbed by Laplace noise of
scale `1/ε`, and charges `ε` against a single global privacy budget that
starts at `1`.

A second, **admin-only** surface supports monthly reconciliation: an
administrator uploads a partner's revised follow-up CSV with a custom
reconciliation id and receives an exact per-subject diff against the
immutable base table (new, missing and region/outcome changes with before and
after values). Reconciliations never modify the base table or the privacy
budget.

- **Stack:** FastAPI · SQLite · Uvicorn
- **Docs:** interactive Swagger UI at `/docs`, OpenAPI JSON at `/openapi.json`
- **Guarantee:** ε-differential privacy per release (histogram query,
  sensitivity 1), with lifetime budget accounting

---

## Quick start

```bash
docker compose up --build
```

Then open <http://localhost:8000/docs>.

The bundled sample data set is `seed/followup.csv` (100 subjects across
`north`, `south`, `east`). It is imported **once**, on the very first start.
A named volume (`followup-data`) holds the database, so restarts keep the
imported data, the spent budget, every released answer and every
reconciliation report.

Enable the admin reconciliation API by supplying a token (use the Swagger
**Authorize** button or any bearer-capable HTTP client):

```bash
ADMIN_TOKEN=$(openssl rand -hex 32) docker compose up --build
```

With `ADMIN_TOKEN` unset, the privacy endpoints work normally and every
`/v1/reconciliations` call answers `503 reconciliation_disabled`.

Tear down completely, including the volume:

```bash
docker compose down -v
```

### Running without Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
uvicorn app.main:app --port 8000
```

---

## API

### `POST /v1/counts`

```bash
curl -s -X POST http://localhost:8000/v1/counts \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-0001","region":"north","outcome":1,"epsilon":"0.1"}'
```

```json
{
  "request_id": "req-0001",
  "region": "north",
  "outcome": 1,
  "epsilon": "0.1",
  "noisy_count": 27,
  "remaining_budget": "0.9",
  "replayed": false,
  "charged": true
}
```

| Field | Meaning |
| --- | --- |
| `noisy_count` | Integer count after adding Laplace(0, 1/ε) noise, clamped at 0. Never the exact count. |
| `remaining_budget` | Global budget left **after** this call, as an exact decimal string. |
| `replayed` | `true` when a stored answer was returned instead of a new one. |
| `charged` | `true` when this call spent budget. |

`request_id` is a client-generated idempotency key. Retrying it with the same
parameters returns the identical stored answer, `replayed: true`, and no new
charge. Reusing it with any different parameter (region, outcome or epsilon)
is rejected with `409 request_id_conflict`.

### `GET /v1/budget`

```json
{"total_budget": "1", "remaining_budget": "0.9"}
```

### `GET /v1/requests/{request_id}`

Returns the stored answer for a request id (same body as above) or `404`.

---

## Reconciliation API (admin only)

Every endpoint under `/v1/reconciliations` requires the header
`Authorization: Bearer <ADMIN_TOKEN>`. Requests without it, or with a wrong
token, get `401`; when no token is configured the API is disabled (`503`).
These endpoints return **exact** per-subject data and are the only place in
the service that does. They never write to `subjects` and never charge the
privacy budget.

### `POST /v1/reconciliations`

Multipart upload of a CSV with `subject_id`, `region`, `outcome` columns plus
a client-chosen `reconciliation_id` (the idempotency key). A sample input is
`examples/reconciliation.csv`.

```bash
curl -s -X POST http://localhost:8000/v1/reconciliations \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F "reconciliation_id=rec-2026-11" \
  -F "file=@examples/reconciliation.csv"
```

The report compares the upload against the immutable base table, per subject:

| Bucket | Meaning |
| --- | --- |
| `added` | Subject present only in the uploaded file; `after_*` values are populated. |
| `missing` | Subject present only in the base table; `before_*` values are populated. |
| `changed` | Present in both, but `region` and/or `outcome` differ; `changed_fields` names each differing field with its before/after value. |

```json
{
  "reconciliation_id": "rec-2026-11",
  "file_fingerprint": "7b767fd3…d05102",
  "source_filename": "reconciliation.csv",
  "row_count": 4,
  "created_at": "2026-11-15T07:39:24+00:00",
  "summary": {"added": 1, "missing": 97, "changed": 2},
  "added": [
    {"subject_id": "S0101", "change_type": "added",
     "before_region": null, "after_region": "west",
     "before_outcome": null, "after_outcome": 0, "changed_fields": []}
  ],
  "missing": [
    {"subject_id": "S0003", "change_type": "missing",
     "before_region": "north", "after_region": null,
     "before_outcome": 1, "after_outcome": null, "changed_fields": []}
  ],
  "changed": [
    {"subject_id": "S0031", "change_type": "changed",
     "before_region": "north", "after_region": "north",
     "before_outcome": 0, "after_outcome": 1,
     "changed_fields": [{"field": "outcome", "before": 0, "after": 1}]}
  ],
  "replayed": false
}
```

`file_fingerprint` is the SHA-256 of the raw uploaded bytes.

### `GET /v1/reconciliations/{reconciliation_id}`

Returns the same full report, or `404 reconciliation_not_found`. Only
authenticated administrators can read per-subject reports.

### `GET /v1/reconciliations`

Lists stored reports with metadata and counts only (no per-subject rows),
newest first.

### Reconciliation semantics

| Rule | Behaviour |
| --- | --- |
| Same id + same file | The stored report is returned (`replayed: true`); safe to retry after a lost response, even across restarts. |
| Same id + different file | Rejected with `409 reconciliation_conflict`; the original report stays untouched. |
| Different ids concurrently | Each report is keyed by its own id; SQLite's serialised write path means uploads can never interleave rows. |
| Same id concurrently, same file | Exactly one report is published; all callers receive that one. |
| Bad file (duplicate id, bad row, wrong header, bad encoding, empty file) | `422 invalid_csv`; nothing is written, so no half report becomes visible and the id stays reusable. |
| File larger than `MAX_UPLOAD_MB` | `413 file_too_large`; nothing is written. |
| Write/disk failure | Header and detail rows are one transaction: the failed report rolls back completely and older reports are unaffected. |
| Base table / budget | Never modified by reconciliation; `/v1/budget` is unchanged after any upload. |

### `GET /health`

Liveness plus whether the data set has been loaded and its row count.

### Errors

All errors share one shape:

```json
{"error": "invalid_request", "message": "request validation failed", "details": [...]}
```

| Status | `error` | When |
| --- | --- | --- |
| 401 | `unauthorized` | Reconciliation call without a valid `ADMIN_TOKEN` bearer token |
| 409 | `request_id_conflict` | Same `request_id`, different parameters |
| 409 | `reconciliation_conflict` | Same `reconciliation_id`, different file fingerprint |
| 413 | `file_too_large` | Upload exceeds `MAX_UPLOAD_MB` |
| 422 | `invalid_request` | Malformed body, `ε` outside `(0, 1]`, outcome not `0`/`1`, bad reconciliation id, … |
| 422 | `invalid_csv` | Reconciliation CSV: bad header, bad row, duplicate subject id, bad encoding, empty file |
| 429 | `budget_exhausted` | `ε` does not fit in the remaining global budget |
| 503 | `dataset_missing` | No data set imported |
| 503 | `reconciliation_disabled` | `ADMIN_TOKEN` is not configured |
| 404 | `reconciliation_not_found` | Unknown `reconciliation_id` |
| 500 | `internal_error` | Unexpected failure; nothing was charged and no partial report exists |

---

## Privacy model

- **Mechanism.** The count of subjects in one cell has global sensitivity 1
  (adding or removing one person changes it by at most one). Adding a sample
  from `Laplace(0, 1/ε)` therefore gives ε-differential privacy for that
  answer. Rounding to an integer and clamping at zero are post-processing and
  preserve the guarantee.
- **Composition.** Every release is charged `ε` against one global budget of
  1, so a sequence of releases is safe under basic sequential composition
  while the sum of ε stays within the budget. Once the budget is exhausted,
  only replays of already-released answers are served, free of charge.
- **No exact values.** The exact counts are computed inside the process but
  are never returned, logged or included in an error message. Sweep
  `region` × `outcome` and intersect only through noisy releases, each paid
  for by the budget.

---

## Correctness properties and how they are enforced

| Requirement | Enforcement |
| --- | --- |
| CSV imported once, never replaced | The importer short-circuits whenever the singleton `dataset` row exists; the check is repeated under the write lock. A different CSV is ignored. |
| Unique subject id | `UNIQUE` constraint plus a pre-insert duplicate scan; a bad file leaves zero rows and no data-set row behind (whole import is one transaction). |
| 0/1 outcomes only | CSV validation and a `CHECK` constraint on `subjects.outcome`. |
| Laplace noise, scale `1/ε` | `app/privacy.py`; draws uniforms from the OS CSPRNG in the open interval `(0, 1)`. |
| Budget never overspent under concurrency | Budget check and deduction happen in one `BEGIN IMMEDIATE` transaction; SQLite allows exactly one writer, so the read-modify-write is serialised. Exact decimal arithmetic — never floats — so `1 - 10×0.1` is exactly `0`. |
| Same id + same parameters → same answer, no charge | The stored `noisy_count` is returned; the fingerprint of `(region, outcome, canonical ε)` is compared first. |
| Same id + different parameters → reject | Fingerprint mismatch raises `409`. |
| Invalid request never charges | Validation happens before the service is called; no row is written. |
| Save failure never charges, and produces no answer | The noisy draw, the request row and the budget update are one transaction. Any exception rolls back both; `GET /v1/budget` is unchanged. |
| Commit succeeded but response lost | The answer is durable before the response is written. A retry — even after a restart — returns the identical stored value. |
| Reconciliation never replaces the base table | Reconciliation only reads `subjects`; its writes go to separate `reconciliations` / `reconciliation_entries` tables and never touch the budget. |
| A bad reconciliation file leaves no report | The whole CSV is parsed and validated before any write; a trailing duplicate id or a bad row raises before the transaction starts. |
| A report is published completely or not at all | Header row, every per-subject entry and the counts are inserted in one `BEGIN IMMEDIATE` transaction; a disk failure rolls them all back, leaving older reports intact. |
| Only admins see per-subject data | All three reconciliation endpoints require a constant-time match against `ADMIN_TOKEN`; without a configured token they fail closed with `503`. |
| Reconciliation idempotency and conflict detection | The SHA-256 of the raw upload is stored next to the id. Same id + same file replays the stored report; same id + different file gets `409`. |
| Concurrent uploads cannot mix data | Every detail row is keyed by its caller-supplied id and inserted in one serialised write transaction; concurrent same-id same-file calls converge on the single stored report. |

---

## Concurrency test

The suite spins up 20 simultaneous `ε = 0.1` requests against a budget of
`1.0` and asserts exactly ten `200`s, ten `429`s and a final budget of `0`
(`tests/test_api.py::test_concurrent_requests_never_overspend`).

The reconciliation suite (`tests/test_reconciliation.py`) covers the
per-subject diff contents, admin authentication and the disabled-token mode,
idempotent replay and `409` conflicts, malformed/duplicate/oversized uploads
publishing nothing, transaction rollback on a triggered write failure while an
older report survives, and concurrent uploads for same and different ids.

```bash
pytest -q
```

---

## Layout

```
app/
  config.py          env-driven settings (paths, total budget, admin token)
  db.py              SQLite connections, schema, serialised transactions
  importer.py        validated one-shot CSV import
  privacy.py         Laplace mechanism, canonical decimals, request fingerprints
  models.py          dataclasses and the SQL repository (budget critical section)
  reconciliation.py  admin CSV reconciliation: validation, per-subject diff, atomic publish
  service.py         query orchestration and idempotency
  main.py            FastAPI app, endpoints, error mapping
examples/followup.csv          sample base input (same as seed/followup.csv)
examples/reconciliation.csv    sample partner revision for the reconciliation API
seed/followup.csv       data set imported by the container on first start
tests/                  pytest suite (API, privacy, concurrency, durability, reconciliation)
```

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | `data/followup.db` | SQLite file (mounted volume in Docker) |
| `CSV_PATH` | `seed/followup.csv` | CSV imported on first start only |
| `TOTAL_BUDGET` | `1` | Lifetime privacy budget |
| `IMPORT_ON_STARTUP` | `1` | Set `0` to boot without an import attempt |
| `SQLITE_BUSY_TIMEOUT_MS` | `30000` | How long writers wait for the lock |
| `ADMIN_TOKEN` | _(unset)_ | Bearer token enabling the admin reconciliation API; unset disables it (`503`) |
| `MAX_UPLOAD_MB` | `10` | Maximum accepted reconciliation upload size |
| `API_PORT` | `8000` | Host port published by Compose |

> **Operational note:** every successful new answer consumes budget
> permanently. To start a fresh study, remove the volume
> (`docker compose down -v`) and start again — that is the only way the data
> set or the budget is ever reset, by design.
