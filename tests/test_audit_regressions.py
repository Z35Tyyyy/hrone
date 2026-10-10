"""Contract edge cases discovered during the independent second review."""

import json
from datetime import datetime, timezone
from urllib.parse import quote

import pytest

from tests.conftest import ms
from tests.test_attendance import close, correction, punch


def test_punch_out_preserves_lateness_recorded_at_punch_in(api, database, add_employee):
    add_employee()
    # The stored lateness was calculated when this shift started. Punch-out is
    # only responsible for work hours, overtime and half-day classification.
    punch(api, "2026-07-06T10:05:00")
    database.employees.update_one({"emp_code": "EMP0001"}, {"$set": {"shift_start": "10:00"}})
    before = database.attendance_logs.find_one({})
    response = close(api, "2026-07-06T19:10:00")
    assert response.status_code == 200, response.text
    assert response.json()["late_minutes"] == 35
    assert response.json()["work_hours"] == 9.08
    assert response.json()["overtime_minutes"] == 40
    after = database.attendance_logs.find_one({})
    for name in ("punch_in", "status", "date", "late_minutes", "history"):
        assert after[name] == before[name]


def test_punch_out_keeps_authoritative_lateness_from_seeded_data(api, database, add_employee):
    add_employee()
    database.attendance_logs.insert_one(
        {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "WFH",
            "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 27,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
    )
    response = api.post(
        "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T18:30:00")}
    )
    assert response.status_code == 200, response.text
    assert response.json()["late_minutes"] == 27
    assert response.json()["work_hours"] == 9


@pytest.mark.parametrize(
    "department",
    ["Engineering/QA", "R&D + Support", "Payroll\nOperations", "研发/工程", "Sales/\nNorth", "/"],
)
def test_trend_can_address_valid_department_names(api, department):
    response = api.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha",
            "email": "asha@example.com",
            "department": department,
            "joined_on": "2026-01-01",
        },
    )
    assert response.status_code == 201, response.text
    response = api.get(
        f"/analytics/departments/{quote(department, safe='')}/trend",
        params={"from": "2026-07-06", "to": "2026-07-06"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["department"] == department
    assert response.json()["items"][0]["headcount"] == 1


@pytest.mark.parametrize("number", ["1e309", "-1e309", "NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("endpoint", ["punch-in", "punch-out"])
def test_nonfinite_timestamp_is_validation_error(api, add_employee, endpoint, number):
    add_employee()
    response = api.post(
        f"/attendance/{endpoint}",
        content='{"emp_code":"EMP0001","punched_at":' + number + "}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)


@pytest.mark.parametrize("name", ["\ud800", "\udfff"])
def test_invalid_unicode_still_has_a_serializable_validation_error(api, name):
    body = {
        "emp_code": "EMP0001",
        "name": name,
        "email": "a@example.com",
        "department": "HR",
        "joined_on": "2026-01-01",
    }
    response = api.post("/employees", content=json.dumps(body), headers={"content-type": "application/json"})
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)


def test_non_json_binary_body_returns_validation_error(api):
    response = api.post("/employees", content=b"\xff", headers={"content-type": "application/octet-stream"})
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)


@pytest.mark.parametrize(
    "path, params",
    [
        ("/attendance", {"date_from": "2026-07-07", "date_to": "2026-07-06"}),
        (
            "/analytics/departments/Engineering/trend",
            {"from": "2026-07-07", "to": "2026-07-06"},
        ),
        (
            "/analytics/departments/Engineering/trend",
            {"from": "2026-01-01", "to": "2026-07-06"},
        ),
        ("/admin/explain/department_summary", {}),
        ("/admin/explain/employee_monthly", {"month": "2026-07"}),
        ("/admin/explain/department_trend", {}),
    ],
)
def test_query_business_errors_match_validation_schema(api, path, params):
    response = api.get(path, params=params)
    assert response.status_code == 422, response.text
    details = response.json()["detail"]
    assert isinstance(details, list) and details
    assert all(isinstance(item, dict) for item in details)


def test_write_business_errors_match_validation_schema(api, add_employee):
    add_employee()
    assert punch(api, "2026-07-06T09:30:00").status_code == 201
    responses = [
        close(api, "2026-07-06T09:30:00"),
        close(api, "2026-07-07T09:30:01"),
        correction(api, status="PRESENT"),
        correction(api, status="ABSENT", punch_in=ms("2026-07-06T09:30:00")),
    ]
    for response in responses:
        assert response.status_code == 422, response.text
        details = response.json()["detail"]
        assert isinstance(details, list) and details
        assert all(isinstance(item, dict) for item in details)


def test_resource_and_conflict_errors_keep_their_contract_shape(api, add_employee):
    missing = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
    assert missing.status_code == 404
    assert isinstance(missing.json()["detail"], str)
    add_employee()
    assert punch(api, "2026-07-06T09:30:00").status_code == 201
    duplicate = punch(api, "2026-07-06T09:30:00")
    assert duplicate.status_code == 409
    assert isinstance(duplicate.json()["detail"], str)
