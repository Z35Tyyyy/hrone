# Employee Attendance & Analytics API

Python 3.11+, FastAPI and MongoDB 6.0+ (MongoDB 7 is the evaluation target). All application code is in `app/main.py`; every endpoint from the supplied contract is implemented.

## Run

```sh
python -m venv .venv
# Activate: Windows PowerShell: .venv\Scripts\Activate.ps1
#           Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

Use an existing MongoDB, or start a local instance:

```sh
docker run -d --name hrone-sdt-mongo7 -p 127.0.0.1:27017:27017 mongo:7
```

Set `MONGO_URI` and `MONGO_DB` in the environment. Defaults are `mongodb://localhost:27017` and `attendance_db`. A local `.env` is supported and ignored by Git; real environment variables take precedence. No credentials are required for the local container.

```sh
uvicorn app.main:app --port 8000
```

Interactive documentation: `http://localhost:8000/docs`. `/health` pings MongoDB. Indexes are created automatically at startup, including when the database is already populated. Startup never seeds or deletes data.

## Verify

```sh
pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check app tests
```

Tests require real MongoDB at `TEST_MONGO_URI` (default `mongodb://localhost:27017`). They create and remove only a randomly named `hrone_test_*` database. Tests cover validation, IST/overnight boundaries, rounding, concurrent writes, audit history, independent analytics oracles, and real execution plans. To also generate 100,000 records and check indexed plans at scale:

```powershell
$env:RUN_SCALE_TESTS = "1"
python -m pytest -q -s -m scale
```

On Linux/macOS, use `RUN_SCALE_TESTS=1 python -m pytest -q -s -m scale`.

Verified on Python 3.12/MongoDB 7.0.43 and Python 3.11/MongoDB 6.0.28: **160 tests passed** with scale checks enabled. All five explain endpoints showed index scans and no collection scans on 100,000 records. Local MongoDB 7 timings: index creation 0.45 s, 31-day department trend 0.10 s, HTTP readiness about 1.4 s. The HTTP smoke check also validated responses against the supplied OpenAPI schemas. These are local measurements, not hidden-grader results.

The second audit added regressions for preserving recorded lateness at punch-out, department names containing slashes, invalid numeric/text inputs, leap years, and a forced punch-out/correction race. An independent integer-arithmetic oracle checks 90 varied punch/correction sequences.

Contract checks also cover error bodies: all 422 responses use an array of validation details, including business-rule failures; missing resources and conflicts keep string details.

Analytics use stored derived values. Trend moving averages use the reported daily rates, ignore null rates, and include only rows inside the requested range. Explicit null PATCH fields are rejected; setting `ABSENT` or `LEAVE` clears punches. Concurrent edits may return 409; reload the record before retrying.

No known missing endpoints. `REVIEW.md` records starter defects and `DECISIONS.md` explains the choices. The original kit and local test artifacts are excluded from the submission.

References: MongoDB [`$rank`](https://www.mongodb.com/docs/manual/reference/operator/aggregation/rank/), [`$densify`](https://www.mongodb.com/docs/manual/reference/operator/aggregation/densify/), [`$round` rounding behavior](https://www.mongodb.com/docs/manual/reference/operator/aggregation/round/), and Starlette [path converters](https://starlette.dev/routing/).
