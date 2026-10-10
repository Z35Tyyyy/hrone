from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from pymongo.errors import ConnectionFailure

from app import main
from tests.conftest import ms


def punch(api, at, code="EMP0001", **extra):
    return api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(at), **extra})


def close(api, at, code="EMP0001"):
    return api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(at)})


def correction(api, day="2026-07-06", **changes):
    return api.patch(
        f"/attendance/EMP0001/{day}",
        json={"reason": "Biometric correction", "regularized_by": "hr.admin", **changes},
    )


def parallel(count, action):
    gate = Barrier(count)

    def call(index):
        gate.wait(timeout=10)
        return action(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(call, range(count)))


def test_health_checks_mongo(api, monkeypatch):
    assert api.get("/health").json() == {"status": "ok"}

    def unavailable(*args, **kwargs):
        raise ConnectionFailure("offline")

    monkeypatch.setattr(main.db, "command", unavailable)
    result = api.get("/health")
    assert result.status_code == 503
    assert "detail" in result.json()


def test_create_employee_serialization_and_ignored_fields(api, database):
    body = {
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
        "created_at": 123,
        "_id": "fake",
    }
    result = api.post("/employees", json=body)
    assert result.status_code == 201, result.text
    assert result.json()["shift_start"] == "09:30"
    assert isinstance(result.json()["created_at"], int)
    assert result.json()["created_at"] % 1000 == 0
    assert "_id" not in result.json()
    assert isinstance(database.employees.find_one({})["created_at"], datetime)
    assert api.post("/employees", json=body).status_code == 409


@pytest.mark.parametrize(
    "field,value",
    [
        ("emp_code", "EMP001"),
        ("emp_code", "EMP1234567"),
        ("name", ""),
        ("name", "x" * 101),
        ("email", "a@b"),
        ("email", "a b@c.com"),
        ("department", ""),
        ("department", "x" * 51),
        ("shift_start", "24:00"),
        ("shift_start", "9:30"),
        ("shift_end", "09:30"),
        ("joined_on", "2026-02-29"),
        ("joined_on", "2026-1-01"),
        ("joined_on", "0000-01-01"),
    ],
)
def test_employee_validation(api, field, value):
    body = {
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
        field: value,
    }
    assert api.post("/employees", json=body).status_code == 422


def test_employee_pagination_filters_and_order(api, add_employee):
    for code, department in [("EMP0003", "HR"), ("EMP0002", "Engineering"), ("EMP0001", "Engineering")]:
        add_employee(code, department=department)
    first = api.get("/employees", params={"department": "Engineering", "page_size": 1}).json()
    assert first["total"] == 2 and first["items"][0]["emp_code"] == "EMP0001"
    second = api.get("/employees", params={"department": "Engineering", "page_size": 1, "page": 2}).json()
    assert second["items"][0]["emp_code"] == "EMP0002"
    assert api.get("/employees", params={"department": "engineering"}).json()["total"] == 0
    assert api.get("/employees", params={"department": ""}).json()["total"] == 0


@pytest.mark.parametrize("path", ["/employees", "/attendance"])
@pytest.mark.parametrize("params", [{"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101}])
def test_pagination_validation(api, path, params):
    assert api.get(path, params=params).status_code == 422


@pytest.mark.parametrize("path", ["/employees", "/attendance"])
def test_very_large_valid_pages_are_empty(api, add_employee, path):
    add_employee()
    punch(api, "2026-07-06T09:30:00")
    response = api.get(path, params={"page": 10**30, "page_size": 100})
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "total": 1, "page": 10**30, "page_size": 100}


@pytest.mark.parametrize(
    "timestamp",
    [1783312500, 99999999999, 4102444800001, 1783312500000.0, "1783312500000", True, False, None, {}, []],
)
@pytest.mark.parametrize("endpoint", ["punch-in", "punch-out"])
def test_epoch_validation(api, add_employee, timestamp, endpoint):
    add_employee()
    result = api.post(f"/attendance/{endpoint}", json={"emp_code": "EMP0001", "punched_at": timestamp})
    assert result.status_code == 422


@pytest.mark.parametrize(
    "at,late", [("09:29:59", 0), ("09:40:00.999", 0), ("09:40:01", 10), ("10:15:59", 45)]
)
def test_grace_period_seconds_and_truncation(api, add_employee, database, at, late):
    add_employee()
    result = punch(api, f"2026-07-06T{at}", late_minutes=999, half_day=True, work_hours=100)
    assert result.status_code == 201, result.text
    data = result.json()
    assert data["late_minutes"] == late and data["half_day"] is False
    assert data["work_hours"] is None and data["history"] == []
    assert data["punch_in"] % 1000 == 0 and "id" not in data and "_id" not in data
    assert database.attendance_logs.find_one({})["punch_in"].microsecond == 0


@pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY"])
def test_presence_statuses(api, add_employee, status):
    add_employee()
    assert punch(api, "2026-07-06T09:30:00", status=status).json()["status"] == status


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "present", "UNKNOWN", None])
def test_invalid_punch_status(api, add_employee, status):
    add_employee()
    assert punch(api, "2026-07-06T09:30:00", status=status).status_code == 422


def test_unknown_employee_and_missing_punch(api, add_employee):
    assert punch(api, "2026-07-06T09:30:00").status_code == 404
    assert close(api, "2026-07-06T18:30:00").status_code == 404
    add_employee()
    assert close(api, "2026-07-06T18:30:00").status_code == 404


def test_ist_date_not_host_timezone(api, add_employee):
    add_employee()
    result = punch(api, "2026-07-07T00:01:00")
    assert result.json()["date"] == "2026-07-07"


@pytest.mark.parametrize(
    "at,day,late",
    [
        ("2026-07-06T21:55:00", "2026-07-06", 0),
        ("2026-07-07T01:00:00", "2026-07-06", 180),
        ("2026-07-07T05:59:59", "2026-07-06", 479),
        ("2026-07-07T06:00:00", "2026-07-07", 0),
    ],
)
def test_overnight_attendance_day(api, add_employee, at, day, late):
    add_employee(shift_start="22:00", shift_end="06:00")
    result = punch(api, at).json()
    assert result["date"] == day and result["late_minutes"] == late


def test_overnight_punch_out(api, add_employee):
    add_employee(shift_start="22:00", shift_end="06:00")
    punch(api, "2026-07-06T21:55:00")
    result = close(api, "2026-07-07T06:40:00")
    assert result.status_code == 200, result.text
    assert result.json()["work_hours"] == 8.75
    assert result.json()["overtime_minutes"] == 40 and result.json()["history"] == []


@pytest.mark.parametrize(
    "out,work,half,ot",
    [
        ("13:59:41", 4.49, True, 0),
        ("13:59:42", 4.50, False, 0),
        ("10:00:18", 0.51, True, 0),
        ("18:59:59", 9.5, False, 0),
        ("19:00:00", 9.5, False, 30),
        ("19:10:59", 9.68, False, 40),
    ],
)
def test_work_rounding_half_day_and_overtime(api, add_employee, out, work, half, ot):
    add_employee()
    punch(api, "2026-07-06T09:30:00")
    result = close(api, f"2026-07-06T{out}")
    assert result.status_code == 200, result.text
    assert (result.json()["work_hours"], result.json()["half_day"], result.json()["overtime_minutes"]) == (
        work,
        half,
        ot,
    )


@pytest.mark.parametrize("out", ["2026-07-06T09:29:59", "2026-07-06T09:30:00.900", "2026-07-07T09:30:01"])
def test_invalid_punch_out_order_and_duration(api, add_employee, out):
    add_employee()
    punch(api, "2026-07-06T09:30:00")
    assert close(api, out).status_code == 422


def test_24_hours_allowed_and_latest_closed_record_wins(api, add_employee):
    add_employee()
    punch(api, "2026-07-06T09:30:00")
    punch(api, "2026-07-07T09:30:00")
    assert close(api, "2026-07-08T09:30:00").json()["work_hours"] == 24
    assert close(api, "2026-07-08T09:30:00").status_code == 409


def test_manual_corrections_preserve_audit_and_bson_dates(api, database, add_employee):
    add_employee()
    punch(api, "2026-07-06T10:05:00")
    close(api, "2026-07-06T18:30:00")
    result = correction(api, punch_in=ms("2026-07-06T09:28:00"))
    assert result.status_code == 200, result.text
    first = result.json()["history"][0]
    assert first["changes"] == {
        "punch_in": {"from": ms("2026-07-06T10:05:00"), "to": ms("2026-07-06T09:28:00")},
        "work_hours": {"from": 8.42, "to": 9.03},
        "late_minutes": {"from": 35, "to": 0},
    }
    result = correction(api, status="LEAVE")
    assert result.status_code == 200
    data = result.json()
    assert len(data["history"]) == 2 and data["history"][0] == first
    assert data["punch_in"] is None and data["punch_out"] is None and data["work_hours"] is None
    assert (data["late_minutes"], data["overtime_minutes"], data["half_day"]) == (0, 0, False)
    assert isinstance(
        database.attendance_logs.find_one({})["history"][0]["changes"]["punch_in"]["to"], datetime
    )
    assert correction(api, status="WFH").status_code == 422
    restored = correction(api, status="WFH", punch_in=ms("2026-07-06T09:30:00"))
    assert restored.status_code == 200 and len(restored.json()["history"]) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"status": "PRESENT"},
        {"punch_in": None},
        {"status": None},
        {"status": "ABSENT", "punch_out": 1783335600000},
        {"punch_in": ms("2026-07-07T09:30:00")},
        {"punch_out": ms("2026-07-06T09:30:00")},
        {"work_hours": 999},
        {"reason": "tiny", "status": "WFH"},
        {"regularized_by": "", "status": "WFH"},
    ],
)
def test_invalid_corrections_do_not_write_history(api, database, add_employee, changes):
    add_employee()
    punch(api, "2026-07-06T09:30:00")
    assert correction(api, **changes).status_code == 422
    assert database.attendance_logs.find_one({})["history"] == []


def test_legacy_defaults_and_missing_patch_records(api, database, add_employee):
    assert correction(api, status="WFH").status_code == 404
    add_employee()
    assert correction(api, status="WFH").status_code == 404
    database.attendance_logs.insert_one(
        {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
        }
    )
    item = api.get("/attendance").json()["items"][0]
    assert item["history"] == [] and item["half_day"] is False and item["punch_out"] is None
    result = correction(api, status="WFH")
    assert result.status_code == 200 and len(result.json()["history"]) == 1
    assert result.json()["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_attendance_list_filters_sort_and_total(api, database, add_employee):
    for code in ("EMP0003", "EMP0001", "EMP0002"):
        add_employee(code)
        punch(api, "2026-07-06T09:30:00", code=code)
        punch(api, "2026-07-07T09:30:00", code=code, status="WFH")
    result = api.get(
        "/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-07", "page_size": 2, "page": 2}
    ).json()
    assert result["total"] == 6
    assert [(row["date"], row["emp_code"]) for row in result["items"]] == [
        ("2026-07-07", "EMP0003"),
        ("2026-07-06", "EMP0001"),
    ]
    assert api.get("/attendance", params={"status": "WFH", "emp_code": "EMP0002"}).json()["total"] == 1
    assert (
        api.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-06"}).status_code == 422
    )
    assert api.get("/attendance", params={"status": "bad"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "2026-02-30"}).status_code == 422


def test_concurrent_employee_and_punch_creation(api, database):
    body = {
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
    }
    responses = parallel(12, lambda _: api.post("/employees", json=body))
    assert sorted(r.status_code for r in responses) == [201] + [409] * 11
    responses = parallel(12, lambda _: punch(api, "2026-07-06T09:30:00"))
    assert sorted(r.status_code for r in responses) == [201] + [409] * 11
    assert database.attendance_logs.count_documents({}) == 1


def test_concurrent_punch_out(api, database, add_employee):
    add_employee()
    punch(api, "2026-07-06T09:30:00")
    responses = parallel(12, lambda _: close(api, "2026-07-06T18:30:00"))
    assert sorted(r.status_code for r in responses) == [200] + [409] * 11
    assert database.attendance_logs.find_one({})["history"] == []


def test_concurrent_corrections_never_lose_history(api, database, add_employee):
    add_employee()
    punch(api, "2026-07-06T09:30:00")
    responses = parallel(
        12,
        lambda index: correction(
            api, punch_in=ms("2026-07-06T09:30:00") + (index + 1) * 60000, reason=f"Correction number {index}"
        ),
    )
    assert all(r.status_code in (200, 409) for r in responses)
    successes = sum(r.status_code == 200 for r in responses)
    history = database.attendance_logs.find_one({})["history"]
    assert successes >= 1 and len(history) == successes
    for before, after in zip(history, history[1:]):
        assert before["changes"]["punch_in"]["to"] == after["changes"]["punch_in"]["from"]
