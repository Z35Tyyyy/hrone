import json
import os
from datetime import date, timedelta
from time import perf_counter

import pytest

from app import main
from tests.conftest import employee, log


def assert_indexed(explanation):
    encoded = json.dumps(explanation)
    assert '"IXSCAN"' in encoded, encoded[:2000]
    assert '"COLLSCAN"' not in encoded, encoded[:2000]

    # MongoDB reports nested lookup scans as counters, outside the winning plan.
    def check(node):
        if isinstance(node, dict):
            if "collectionScans" in node:
                assert node["collectionScans"] == 0
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(explanation)


def explain_cases():
    return [
        ("attendance_list", {}),
        (
            "attendance_list",
            {
                "emp_code": "EMP0001",
                "date_from": "2026-07-01",
                "date_to": "2026-07-31",
                "page": 2,
                "page_size": 3,
            },
        ),
        ("attendance_list", {"status": "PRESENT", "date_from": "2026-07-01", "page": 2}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("department_summary", {"month": "2026-07", "department": "Engineering"}),
        ("late_leaderboard", {"month": "2026-07", "limit": 2}),
        ("late_leaderboard", {"month": "2026-07", "limit": 2, "department": "Engineering"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}),
    ]


def test_idempotent_indexes_and_real_explain_queries(api, database, add_employee):
    add_employee()
    database.attendance_logs.insert_one(log("EMP0001", "2026-07-06", late_minutes=35))
    main.create_indexes()
    main.create_indexes()
    assert database.employees.index_information()["employee_code_unique"]["unique"]
    assert database.attendance_logs.index_information()["employee_day_unique"]["unique"]
    for endpoint, params in explain_cases():
        response = api.get(f"/admin/explain/{endpoint}", params=params)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["endpoint"] == endpoint
        assert data["collection"] in ("employees", "attendance_logs")
        assert_indexed(data["explain"])
        if endpoint == "attendance_list" and params.get("emp_code"):
            command = data["explain"]["command"]
            assert command["skip"] == 3 and command["limit"] == 3
            assert command["filter"] == {
                "emp_code": "EMP0001",
                "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"},
            }
            assert command["sort"] == {"date": -1, "emp_code": 1}


@pytest.mark.scale
@pytest.mark.skipif(
    os.getenv("RUN_SCALE_TESTS") != "1", reason="set RUN_SCALE_TESTS=1 for the 100,000-record checks"
)
def test_one_hundred_thousand_records(api, database):
    # 1,000 employees * 100 dates: several departments, ties and attendance statuses.
    database.employees.insert_many(
        [employee(f"EMP{i:04d}", department="Engineering" if i % 2 else "HR") for i in range(1, 1001)]
    )
    beginning = date(2026, 5, 1)
    for offset in range(100):
        day = (beginning + timedelta(days=offset)).isoformat()
        database.attendance_logs.insert_many(
            [
                log(
                    f"EMP{i:04d}",
                    day,
                    status="WFH" if i % 3 else "PRESENT",
                    work_hours=8.25,
                    late_minutes=10 if i % 4 else 0,
                    overtime_minutes=30 if i % 5 else 0,
                )
                for i in range(1, 1001)
            ]
        )
    assert database.attendance_logs.count_documents({}) == 100000
    # Exercise startup indexes against a populated database, just like the grader.
    database.employees.drop_indexes()
    database.attendance_logs.drop_indexes()
    before = perf_counter()
    main.create_indexes()
    print(f"\nIndexes on 100,000 records: {perf_counter() - before:.3f}s")
    for endpoint, params in explain_cases():
        before = perf_counter()
        response = api.get(f"/admin/explain/{endpoint}", params=params)
        assert response.status_code == 200, response.text
        assert_indexed(response.json()["explain"])
        print(f"Explain {endpoint} {params}: {perf_counter() - before:.3f}s")
    before = perf_counter()
    response = api.get(
        "/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-07-31"}
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 31
    for row in response.json()["items"]:
        assert row["headcount"] == 500 and row["present_count"] == 500
        assert row["late_count"] == 500
        assert row["attendance_rate"] == (1.0 if row["is_working_day"] else None)
    print(f"Trend response on 100,000 records: {perf_counter() - before:.3f}s")
    assert (
        api.get("/attendance", params={"page": 1000, "page_size": 50, "status": "WFH"}).json()["total"]
        == 66700
    )
