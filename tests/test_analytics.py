"""Independent Python oracles compare the database pipelines with awkward seeded records."""

import calendar
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from tests.conftest import employee, log

PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def rounded(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))


def days_between(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]


def weight(record, weekdays=False):
    if record["status"] not in PRESENT or (weekdays and date.fromisoformat(record["date"]).weekday() >= 5):
        return 0
    return 0.5 if record.get("half_day", False) else 1


def monthly_oracle(person, records, month):
    year, number = map(int, month.split("-"))
    working = sum(
        date.fromisoformat(day).weekday() < 5
        for day in days_between(
            max(month + "-01", person["joined_on"]), f"{month}-{calendar.monthrange(year, number)[1]}"
        )
    )
    selected = [r for r in records if r["emp_code"] == person["emp_code"] and r["date"].startswith(month)]
    present = sum(weight(r, weekdays=True) for r in selected)
    return {
        "emp_code": person["emp_code"],
        "month": month,
        "working_days": working,
        "present_days": present,
        "leave_days": sum(r["status"] == "LEAVE" for r in selected),
        "late_count": sum(r.get("late_minutes", 0) > 0 for r in selected),
        "total_late_minutes": sum(r.get("late_minutes", 0) for r in selected),
        "total_overtime_minutes": sum(r.get("overtime_minutes", 0) for r in selected),
        "attendance_pct": rounded(Decimal(str(present)) / working * 100) if working else None,
    }


def summary_oracle(people, records, month, department=None):
    year, number = map(int, month.split("-"))
    end = f"{month}-{calendar.monthrange(year, number)[1]}"
    eligible = [
        p for p in people if p["joined_on"] <= end and (department is None or p["department"] == department)
    ]
    items = []
    for name in sorted({p["department"] for p in eligible}):
        codes = {p["emp_code"] for p in eligible if p["department"] == name}
        selected = [r for r in records if r["emp_code"] in codes and r["date"].startswith(month)]
        hours = [
            Decimal(str(r["work_hours"]))
            for r in selected
            if r["status"] in PRESENT and r.get("work_hours") is not None
        ]
        items.append(
            {
                "department": name,
                "headcount": len(codes),
                "present_days": sum(weight(r, weekdays=True) for r in selected),
                "avg_work_hours": rounded(sum(hours) / len(hours)) if hours else None,
                "late_count": sum(r.get("late_minutes", 0) > 0 for r in selected),
                "total_late_minutes": sum(r.get("late_minutes", 0) for r in selected),
                "leave_count": sum(r["status"] == "LEAVE" for r in selected),
                "on_duty_count": sum(r["status"] == "ON_DUTY" for r in selected),
            }
        )
    return {"month": month, "items": items}


def leaderboard_oracle(people, records, month, limit, department=None):
    items = []
    for person in people:
        if department is not None and person["department"] != department:
            continue
        selected = [r for r in records if r["emp_code"] == person["emp_code"] and r["date"].startswith(month)]
        total = sum(r.get("late_minutes", 0) for r in selected)
        if total:
            items.append(
                {
                    "emp_code": person["emp_code"],
                    "name": person["name"],
                    "department": person["department"],
                    "total_late_minutes": total,
                    "late_count": sum(r.get("late_minutes", 0) > 0 for r in selected),
                }
            )
    items.sort(key=lambda item: (-item["total_late_minutes"], item["emp_code"]))
    totals = [item["total_late_minutes"] for item in items]
    for item in items:
        item["rank"] = 1 + sum(total > item["total_late_minutes"] for total in totals)
    return {"month": month, "items": [item for item in items if item["rank"] <= limit]}


def trend_oracle(people, records, department, start, end):
    members = [p for p in people if p["department"] == department]
    codes = {p["emp_code"] for p in members}
    items = []
    for day in days_between(start, end):
        selected = [r for r in records if r["emp_code"] in codes and r["date"] == day]
        headcount = sum(p["joined_on"] <= day for p in members)
        working = date.fromisoformat(day).weekday() < 5
        present = sum(weight(r) for r in selected)
        rate = rounded(Decimal(str(present)) / headcount, 4) if working and headcount else None
        item = {
            "date": day,
            "headcount": headcount,
            "is_working_day": working,
            "present_count": present,
            "late_count": sum(r.get("late_minutes", 0) > 0 for r in selected),
            "attendance_rate": rate,
        }
        window = [row["attendance_rate"] for row in items[-6:]] + [rate]
        rates = [Decimal(str(value)) for value in window if value is not None]
        item["moving_avg_7d"] = rounded(sum(rates) / len(rates), 4) if rates else None
        items.append(item)
    return {"department": department, "items": items}


def test_awkward_seeded_analytics(api, database):
    people = [
        employee("EMP0001"),
        employee("EMP0002"),
        employee("EMP0003", joined_on="2026-07-15"),
        employee("EMP0004", department="HR"),
        employee("EMP0005", department="Empty"),
        employee("EMP0006", department="Future", joined_on="2026-08-01"),
    ]
    records = [
        log("EMP0001", "2026-07-06", work_hours=8.12, late_minutes=35, overtime_minutes=31),
        log("EMP0001", "2026-07-07", "WFH", work_hours=3.5, half_day=True),
        log("EMP0001", "2026-07-11", work_hours=6.0, late_minutes=45, overtime_minutes=60),
        log("EMP0001", "2026-07-12", "LEAVE"),
        log("EMP0001", "2026-07-13", "ABSENT"),
        log("EMP0002", "2026-07-06", "ON_DUTY", work_hours=7.0, late_minutes=80),
        log("EMP0003", "2026-07-15", "WFH", late_minutes=100),
        log("EMP0004", "2026-07-06", late_minutes=60),
        log("EMP9999", "2026-07-06", late_minutes=1000),
        log("EMP0001", "2026-06-30", late_minutes=900),
    ]
    database.employees.insert_many(people)
    database.attendance_logs.insert_many(records)
    for person in people:
        response = api.get(f"/analytics/employees/{person['emp_code']}/monthly", params={"month": "2026-07"})
        assert response.status_code == 200, response.text
        assert response.json() == monthly_oracle(person, records, "2026-07")
    assert api.get("/analytics/departments/summary", params={"month": "2026-07"}).json() == summary_oracle(
        people, records, "2026-07"
    )
    for department in (None, "Engineering", "Empty", "Unknown", ""):
        params = {"month": "2026-07", "limit": 2}
        if department is not None:
            params["department"] = department
        assert api.get("/analytics/departments/summary", params=params).json() == summary_oracle(
            people, records, "2026-07", department
        )
        assert api.get("/analytics/leaderboard/late", params=params).json() == leaderboard_oracle(
            people, records, "2026-07", 2, department
        )
    for department in ("Engineering", "HR", "Empty", "Future"):
        response = api.get(
            f"/analytics/departments/{department}/trend", params={"from": "2026-07-04", "to": "2026-07-17"}
        )
        assert response.status_code == 200, response.text
        assert response.json() == trend_oracle(people, records, department, "2026-07-04", "2026-07-17")


def test_competition_ranks_skip_and_include_all_ties(api, database):
    database.employees.insert_many([employee(f"EMP{i:04d}") for i in range(1, 5)])
    database.attendance_logs.insert_many(
        [
            log(f"EMP{i:04d}", "2026-07-06", late_minutes=minutes)
            for i, minutes in enumerate([100, 80, 80, 60], 1)
        ]
    )
    rows = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP0001"), (2, "EMP0002"), (2, "EMP0003")]
    assert (
        len(api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3}).json()["items"])
        == 3
    )
    assert (
        api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 4}).json()["items"][-1][
            "rank"
        ]
        == 4
    )


def test_half_up_averages_record_weighting_and_zero_hours(api, database):
    database.employees.insert_many(
        [employee("EMP0001"), employee("EMP0002"), employee("EMP0003", department="Tie")]
    )
    database.attendance_logs.insert_many(
        [
            log("EMP0001", "2026-07-06", work_hours=8),
            log("EMP0001", "2026-07-07", work_hours=8),
            log("EMP0002", "2026-07-06", work_hours=0),
            log("EMP0002", "2026-07-07", "ABSENT", work_hours=99),
            log("EMP0002", "2026-07-08", work_hours=None),
            log("EMP0003", "2026-07-06", work_hours=2.675),
        ]
    )
    rows = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert rows[0]["avg_work_hours"] == 5.33
    assert rows[1]["avg_work_hours"] == 2.68


def test_rates_round_half_up_and_average_the_reported_rates(api, database):
    people = [employee(f"EMP{i:04d}") for i in range(1, 33)]
    database.employees.insert_many(people)
    records = [log("EMP0001", "2026-07-06"), log("EMP0001", "2026-07-07", half_day=True)]
    database.attendance_logs.insert_many(records)
    response = api.get(
        "/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-07"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["attendance_rate"] == 0.0313
    assert response.json() == trend_oracle(people, records, "Engineering", "2026-07-06", "2026-07-07")


@pytest.mark.parametrize(
    "start,end,count",
    [
        ("2026-07-06", "2026-07-06", 1),
        ("2026-07-01", "2026-09-30", 92),
        ("2028-02-28", "2028-03-01", 3),
        ("9999-12-31", "9999-12-31", 1),
    ],
)
def test_trend_empty_range_edges(api, add_employee, start, end, count):
    add_employee(joined_on="2027-01-01")
    response = api.get("/analytics/departments/Engineering/trend", params={"from": start, "to": end})
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert len(rows) == count and rows[0]["date"] == start and rows[-1]["date"] == end
    assert all(r["present_count"] == 0 for r in rows)
    if start < "2027-01-01":
        assert all(
            r["headcount"] == 0 and r["attendance_rate"] is None and r["moving_avg_7d"] is None for r in rows
        )


@pytest.mark.parametrize("seed", [11, 47, 89])
def test_randomized_analytics_against_independent_oracle(api, database, seed):
    rng = random.Random(seed)
    people = [
        employee(
            f"EMP{i:04d}",
            department=rng.choice(["Engineering", "HR", "Sales"]),
            joined_on=rng.choice(["2026-01-01", "2026-07-15", "2026-08-01"]),
        )
        for i in range(1, 16)
    ]
    records = []
    for person in people:
        if person["emp_code"] == "EMP0001":
            continue
        for day in days_between("2026-07-01", "2026-07-31"):
            if day < person["joined_on"] or rng.random() < 0.45:
                continue
            status = rng.choice(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"])
            fields = {}
            if status in PRESENT:
                fields.update(
                    work_hours=rng.choice([None, 0, 3.5, 4.49, 4.5, 8.01, 8.12, 10.99]),
                    late_minutes=rng.choice([0, 10, 11, 35, 60]),
                    overtime_minutes=rng.choice([0, 30, 59]),
                )
                if rng.random() < 0.7:
                    fields["half_day"] = fields["work_hours"] is not None and fields["work_hours"] < 4.5
            records.append(log(person["emp_code"], day, status, **fields))
    database.employees.insert_many(people)
    database.attendance_logs.insert_many(records)
    for person in people:
        assert api.get(
            f"/analytics/employees/{person['emp_code']}/monthly", params={"month": "2026-07"}
        ).json() == monthly_oracle(person, records, "2026-07")
    for department in sorted({p["department"] for p in people}):
        params = {"month": "2026-07", "department": department, "limit": 3}
        assert api.get("/analytics/departments/summary", params=params).json() == summary_oracle(
            people, records, "2026-07", department
        )
        assert api.get("/analytics/leaderboard/late", params=params).json() == leaderboard_oracle(
            people, records, "2026-07", 3, department
        )
        assert api.get(
            f"/analytics/departments/{department}/trend", params={"from": "2026-07-01", "to": "2026-07-31"}
        ).json() == trend_oracle(people, records, department, "2026-07-01", "2026-07-31")


@pytest.mark.parametrize("month", ["2026-00", "2026-13", "2026-7", "0000-01", "bad", "2026-07-01"])
def test_month_validation(api, month):
    for endpoint in (
        "/analytics/employees/EMP0001/monthly",
        "/analytics/departments/summary",
        "/analytics/leaderboard/late",
    ):
        assert api.get(endpoint, params={"month": month}).status_code == 422


def test_missing_resources_empty_results_and_invalid_ranges(api):
    assert api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).status_code == 404
    assert (
        api.get(
            "/analytics/departments/Unknown/trend", params={"from": "2026-07-01", "to": "2026-07-31"}
        ).status_code
        == 404
    )
    assert api.get("/analytics/departments/summary", params={"month": "2026-07"}).json() == {
        "month": "2026-07",
        "items": [],
    }
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json() == {
        "month": "2026-07",
        "items": [],
    }
    for start, end in [
        ("2026-07-01", "2026-10-01"),
        ("2026-07-02", "2026-07-01"),
        ("2026-02-30", "2026-03-01"),
    ]:
        assert (
            api.get("/analytics/departments/Unknown/trend", params={"from": start, "to": end}).status_code
            == 422
        )
    for limit in [0, 51]:
        assert (
            api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": limit}).status_code
            == 422
        )


@pytest.mark.parametrize(
    "endpoint,params",
    [
        ("unknown", {}),
        ("employee_monthly", {"month": "2026-07"}),
        ("employee_monthly", {"emp_code": "EMP0001"}),
        ("department_summary", {}),
        ("late_leaderboard", {}),
        ("department_trend", {"department": "Engineering"}),
        ("attendance_list", {"date_from": "2026-07-02", "date_to": "2026-07-01"}),
    ],
)
def test_explain_validation(api, endpoint, params):
    assert api.get(f"/admin/explain/{endpoint}", params=params).status_code == 422


@pytest.mark.parametrize(
    "month", ["0001-01", "1974-02", "2000-02", "2024-02", "2026-01", "2026-12", "2099-12", "9999-12"]
)
def test_calendar_boundaries_against_independent_oracle(api, database, month):
    year, number = map(int, month.split("-"))
    start = month + "-01"
    end = f"{month}-{calendar.monthrange(year, number)[1]}"
    people = [
        employee("EMP0001", joined_on=start),
        employee("EMP0002", joined_on=month + "-15"),
        employee("EMP0003", joined_on=end),
    ]
    records = [
        log("EMP0001", start, work_hours=8, late_minutes=10),
        log("EMP0002", end, "WFH", work_hours=3.5, half_day=True),
    ]
    # Legacy documents omit counters; their defaults must participate in aggregates.
    records[1].pop("late_minutes")
    records[1].pop("overtime_minutes")
    database.employees.insert_many(people)
    database.attendance_logs.insert_many(records)
    for person in people:
        result = api.get(f"/analytics/employees/{person['emp_code']}/monthly", params={"month": month})
        assert result.status_code == 200, result.text
        assert result.json() == monthly_oracle(person, records, month)
    assert api.get("/analytics/departments/summary", params={"month": month}).json() == summary_oracle(
        people, records, month
    )
    assert api.get("/analytics/leaderboard/late", params={"month": month}).json() == leaderboard_oracle(
        people, records, month, 10
    )
    result = api.get("/analytics/departments/Engineering/trend", params={"from": start, "to": end})
    assert result.status_code == 200, result.text
    assert result.json() == trend_oracle(people, records, "Engineering", start, end)
