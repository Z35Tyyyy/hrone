"""Randomized write checks use integer arithmetic independent of the app helpers."""

import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest

from app import main
from tests.conftest import IST
from tests.test_attendance import close, correction, punch


def oracle(start, finish, shift_start, shift_end):
    day = start.date()
    seconds_of_day = start.hour * 3600 + start.minute * 60 + start.second
    overnight = shift_end <= shift_start
    if overnight and seconds_of_day < shift_end * 60:
        day -= timedelta(days=1)
    beginning = datetime.combine(day, datetime.min.time())
    shift_beginning = beginning + timedelta(minutes=shift_start)
    shift_finish = beginning + timedelta(days=int(overnight), minutes=shift_end)
    late_seconds = int((start - shift_beginning).total_seconds())
    result = {
        "date": day.isoformat(),
        "late_minutes": late_seconds // 60 if late_seconds > 600 else 0,
        "punch_in": int(start.replace(tzinfo=IST).timestamp()) * 1000,
        "punch_out": None,
        "work_hours": None,
        "half_day": False,
        "overtime_minutes": 0,
    }
    if finish is not None:
        elapsed = int((finish - start).total_seconds())
        hundredths = (elapsed * 100 + 1800) // 3600
        overtime = int((finish - shift_finish).total_seconds()) // 60
        result.update(
            punch_out=int(finish.replace(tzinfo=IST).timestamp()) * 1000,
            work_hours=hundredths / 100,
            half_day=hundredths < 450,
            overtime_minutes=overtime if overtime >= 30 else 0,
        )
    return result


def assert_values(response, expected):
    assert response.status_code in (200, 201), response.text
    actual = response.json()
    assert {key: actual[key] for key in expected} == expected
    assert "id" not in actual and "_id" not in actual


@pytest.mark.parametrize("seed", [19, 211, 919])
def test_randomized_punches_and_corrections(api, add_employee, seed):
    rng = random.Random(seed)
    for number in range(1, 31):
        code = f"EMP{number:04d}"
        shift_start = rng.randrange(1440)
        shift_end = (shift_start + rng.randrange(1, 1440)) % 1440
        add_employee(
            code,
            shift_start=f"{shift_start // 60:02d}:{shift_start % 60:02d}",
            shift_end=f"{shift_end // 60:02d}:{shift_end % 60:02d}",
        )
        beginning = datetime(
            rng.choice([1974, 1999, 2000, 2024, 2026, 2099]), rng.randrange(1, 13), rng.randrange(1, 27)
        )
        start = beginning + timedelta(seconds=rng.randrange(86400))
        finish = start + timedelta(
            seconds=rng.choice([1, 18, 601, 16181, 16182, 16200, 86400, rng.randrange(1, 86401)])
        )
        initial = oracle(start, None, shift_start, shift_end)
        response = api.post(
            "/attendance/punch-in",
            json={
                "emp_code": code,
                "punched_at": initial["punch_in"] + rng.randrange(1000),
                "status": rng.choice(["PRESENT", "WFH", "ON_DUTY"]),
                "late_minutes": 999,
            },
        )
        assert_values(response, initial)
        completed = oracle(start, finish, shift_start, shift_end)
        response = api.post(
            "/attendance/punch-out",
            json={"emp_code": code, "punched_at": completed["punch_out"] + rng.randrange(1000)},
        )
        assert_values(response, completed)
        assert response.json()["history"] == []
        before = response.json()

        corrected_in = datetime.fromisoformat(completed["date"]) + timedelta(minutes=shift_start)
        corrected_out = corrected_in + timedelta(seconds=rng.randrange(1, 86401))
        corrected = oracle(corrected_in, corrected_out, shift_start, shift_end)
        response = api.patch(
            f"/attendance/{code}/{completed['date']}",
            json={
                "punch_in": corrected["punch_in"] + 999,
                "punch_out": corrected["punch_out"] + 999,
                "reason": "Corrected biometric timestamps",
                "regularized_by": "hr.admin",
                "work_hours": 999,
            },
        )
        assert_values(response, corrected)
        assert len(response.json()["history"]) == 1
        expected_changes = {
            key: {"from": before[key], "to": value}
            for key, value in corrected.items()
            if key != "date" and value != before[key]
        }
        assert response.json()["history"][0]["changes"] == expected_changes


def test_punch_out_racing_with_correction_does_not_lose_either_write(
    api, database, add_employee, monkeypatch
):
    add_employee()
    punch(api, "2026-07-06T10:05:00")
    original_filter = main.unchanged_filter
    barrier = Barrier(2)

    def synchronize_snapshot(record):
        # Both operations must use the same earlier state, not just overlap HTTP calls.
        result = original_filter(record)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(main, "unchanged_filter", synchronize_snapshot)
    operations = [lambda: close(api, "2026-07-06T19:10:00"), lambda: correction(api, punch_in=1783310280000)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda action: action(), operations))
    assert sorted(response.status_code for response in results) == [200, 409]
    monkeypatch.setattr(main, "unchanged_filter", original_filter)
    retry = operations[next(index for index, response in enumerate(results) if response.status_code == 409)]()
    assert retry.status_code == 200, retry.text
    final = retry.json()
    assert final["work_hours"] == 9.7 and final["late_minutes"] == 0 and final["overtime_minutes"] == 40
    assert len(final["history"]) == 1
    assert len(database.attendance_logs.find_one({})["history"]) == 1
