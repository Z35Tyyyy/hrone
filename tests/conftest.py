"""Integration tests use real MongoDB, exclusively in a fresh disposable database."""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main

IST = timezone(timedelta(hours=5, minutes=30))


def ms(local_time):
    return int(datetime.fromisoformat(local_time).replace(tzinfo=IST).timestamp() * 1000)


def employee(code="EMP0001", **fields):
    return {
        "emp_code": code,
        "name": f"Employee {code}",
        "email": f"{code}@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        **fields,
    }


def log(code, day, status="PRESENT", **fields):
    # Deliberately independent stored values: analytics must not reconstruct punches.
    return {
        "emp_code": code,
        "date": day,
        "status": status,
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        **fields,
    }


@pytest.fixture(scope="session")
def database():
    connection = MongoClient(
        os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=5000
    )
    assert connection.server_info()["versionArray"][0] >= 6
    database_name = "hrone_test_" + uuid4().hex
    test_database = connection[database_name]
    yield test_database
    # Never erase a configured application database, even if the test configuration is wrong.
    assert database_name.startswith("hrone_test_") and len(database_name) == 43
    connection.drop_database(database_name)
    connection.close()


@pytest.fixture(scope="session")
def api(database):
    original_db, original_client = main.db, main.client
    app_connection = (
        MongoClient(database.client.address[0], database.client.address[1], tz_aware=True)
        if not os.getenv("TEST_MONGO_URI")
        else MongoClient(os.environ["TEST_MONGO_URI"], tz_aware=True)
    )
    main.client, main.db = app_connection, app_connection[database.name]
    with TestClient(main.app) as test_client:
        yield test_client
    main.db, main.client = original_db, original_client
    original_client.close()


@pytest.fixture(autouse=True)
def clean_database(api, database):
    database.employees.delete_many({})
    database.attendance_logs.delete_many({})


@pytest.fixture
def add_employee(database):
    def add(code="EMP0001", **fields):
        document = employee(code, **fields)
        database.employees.insert_one(document)
        return document

    return add
