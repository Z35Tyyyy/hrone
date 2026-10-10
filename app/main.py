"""Employee attendance API. Run with: uvicorn app.main:app --port 8000."""

import calendar
import json
import math
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date as CalendarDate
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from starlette.convertors import PathConvertor, register_url_convertor

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
ExplainEndpoint = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def create_indexes():
    """Unique indexes arbitrate races; the remaining indexes match query prefixes."""
    db.employees.create_indexes(
        [
            IndexModel([("emp_code", ASCENDING)], unique=True, name="employee_code_unique"),
            IndexModel([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_code"),
            IndexModel([("joined_on", ASCENDING), ("department", ASCENDING)], name="joined_department"),
            IndexModel([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined"),
        ]
    )
    db.attendance_logs.create_indexes(
        [
            IndexModel(
                [("emp_code", ASCENDING), ("date", DESCENDING)], unique=True, name="employee_day_unique"
            ),
            IndexModel([("date", DESCENDING), ("emp_code", ASCENDING)], name="day_employee"),
            IndexModel(
                [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
                name="status_day_employee",
            ),
            IndexModel([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="employee_punch"),
        ]
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_indexes()
    try:
        yield
    finally:
        client.close()


class DepartmentPath(PathConvertor):
    # EmployeeCreate accepts any department string of the allowed length. Encoded slashes
    # are already decoded by ASGI, so this route must accept them, too.
    regex = r"[\s\S]+"


register_url_convertor("department_name", DepartmentPath())
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error_response(_request, error: RequestValidationError):
    # Preserve FastAPI's error shape even when the rejected input itself cannot
    # be encoded as JSON (overflowing floats, invalid Unicode, or binary bodies).
    details = jsonable_encoder(
        error.errors(),
        custom_encoder={
            float: lambda value: value if math.isfinite(value) else str(value),
            str: lambda value: value.encode("utf-8", errors="backslashreplace").decode("utf-8"),
            bytes: lambda value: value.decode("utf-8", errors="backslashreplace"),
        },
    )
    return JSONResponse(status_code=422, content={"detail": details})


# Validation and conversions. Calendar strings stay strings in the database.
def valid_date(value: str) -> str:
    CalendarDate.fromisoformat(value)
    return value


def valid_month(value: str) -> str:
    CalendarDate.fromisoformat(value + "-01")
    return value


DateString = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(valid_date)]
MonthString = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(valid_month)]
ShiftTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
LeaderboardLimit = Annotated[int, Query(ge=1, le=50)]


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def epoch_ms(value: datetime) -> int:
    # PyMongo is configured for aware UTC dates; handle legacy naive UTC dates too.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def instant(value: int) -> datetime:
    return EPOCH + timedelta(seconds=value // 1000)


def whole_seconds(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def api_value(value):
    """Convert instants recursively, including before/after values in history."""
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, dict):
        return {key: api_value(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, list):
        return [api_value(item) for item in value]
    return value


DEFAULTS = {
    "punch_in": None,
    "punch_out": None,
    "work_hours": None,
    "late_minutes": 0,
    "overtime_minutes": 0,
    "half_day": False,
}
EDITABLE = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def attendance_response(record):
    return api_value({**DEFAULTS, "history": [], **record})


class InputModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(InputModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateString

    @model_validator(mode="after")
    def distinct_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(InputModel):
    emp_code: str
    punched_at: EpochMillis = Field(default_factory=lambda: epoch_ms(now()))


class PunchInIn(PunchOutIn):
    status: PresenceStatus = "PRESENT"


class RegularizeIn(InputModel):
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def supplied_fields_are_not_null(cls, value):
        # The contract permits omission, but does not mark PATCH inputs nullable.
        if value is None:
            raise ValueError("omit an unchanged field; explicit null is not accepted")
        return value


def employee_or_404(emp_code):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    return employee


# Business rules R1-R5. All comparisons use truncated instants and the shift's day.
def attendance_day(punch_in: datetime, employee) -> str:
    local = whole_seconds(punch_in).astimezone(IST)
    day = local.date()
    if employee["shift_end"] <= employee["shift_start"] and local.time() < time.fromisoformat(
        employee["shift_end"]
    ):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(employee, day: str):
    start = datetime.combine(
        CalendarDate.fromisoformat(day), time.fromisoformat(employee["shift_start"]), IST
    )
    end = datetime.combine(CalendarDate.fromisoformat(day), time.fromisoformat(employee["shift_end"]), IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    seconds = int((whole_seconds(punch_in) - shift_start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((whole_seconds(punch_out) - whole_seconds(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    minutes = int((whole_seconds(punch_out) - shift_end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def closing_fields(punch_in: datetime, punch_out: datetime, shift_end: datetime):
    punch_in, punch_out = whole_seconds(punch_in), whole_seconds(punch_out)
    elapsed = (punch_out - punch_in).total_seconds()
    if not 0 < elapsed <= 86400:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "punch_out": punch_out,
        "work_hours": hours,
        "overtime_minutes": compute_overtime(punch_out, shift_end),
        "half_day": hours < 4.50,
    }


def recompute(record, employee):
    final = {key: record.get(key, DEFAULTS.get(key)) for key in EDITABLE}
    if final["status"] not in PRESENCE:
        return {**final, **DEFAULTS}
    if final["punch_in"] is None:
        raise HTTPException(422, "a presence status requires punch_in")
    final["punch_in"] = whole_seconds(final["punch_in"])
    if attendance_day(final["punch_in"], employee) != record["date"]:
        raise HTTPException(422, "punch_in must stay on the record's attendance date")
    start, end = shift_bounds(employee, record["date"])
    final.update(
        work_hours=None,
        late_minutes=compute_late_minutes(final["punch_in"], start),
        overtime_minutes=0,
        half_day=False,
    )
    if final["punch_out"] is not None:
        final.update(closing_fields(final["punch_in"], final["punch_out"], end))
    return final


def unchanged_filter(record):
    """Compare the snapshot atomically so corrections and punch-outs cannot overwrite a newer edit."""
    return {
        "_id": record["_id"],
        **{key: record[key] if key in record else {"$exists": False} for key in (*EDITABLE, "history")},
    }


@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    document = {**body.model_dump(), "created_at": now()}
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return api_value(document)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department is not None else {}
    hint = "department_code" if department is not None else "employee_code_unique"
    items = (
        db.employees.find(query, {"_id": 0})
        .hint(hint)
        .sort("emp_code", ASCENDING)
        .skip(page_offset(page, page_size))
        .limit(page_size)
    )
    return {
        "items": api_value(list(items)),
        "total": db.employees.count_documents(query, hint=hint),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = employee_or_404(body.emp_code)
    timestamp = instant(body.punched_at)
    document = {
        "emp_code": body.emp_code,
        "date": attendance_day(timestamp, employee),
        "status": body.status,
        "punch_in": timestamp,
        "history": [],
    }
    document.update(recompute(document, employee))
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return attendance_response(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = employee_or_404(body.emp_code)
    timestamp = instant(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": timestamp, "$type": "date"}},
        sort=[("punch_in", DESCENDING)],
        hint="employee_punch",
    )
    if record is None:
        # Distinguish an invalid earlier punch-out from an employee with no punches.
        if db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$type": "date"}}, hint="employee_punch"
        ):
            raise HTTPException(422, "punch_out must be after punch_in")
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    _, shift_end = shift_bounds(employee, record["date"])
    # Lateness belongs to the original punch-in (or a manual correction).
    # Closing the record must not overwrite that stored value.
    final = closing_fields(record["punch_in"], timestamp, shift_end)
    updated = db.attendance_logs.find_one_and_update(
        unchanged_filter(record), {"$set": final}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "attendance changed concurrently; reload and retry")
    return attendance_response(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateString, body: RegularizeIn):
    employee = employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")
    supplied = body.model_fields_set & {"status", "punch_in", "punch_out"}
    if not supplied:
        raise HTTPException(422, "no attendance changes supplied")
    final = dict(record)
    for key in supplied:
        value = getattr(body, key)
        final[key] = instant(value) if key in ("punch_in", "punch_out") else value
    if final["status"] not in PRESENCE and supplied & {"punch_in", "punch_out"}:
        raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")
    final = recompute(final, employee)
    changes = {
        key: {"from": record.get(key, DEFAULTS.get(key)), "to": final[key]}
        for key in EDITABLE
        if record.get(key, DEFAULTS.get(key)) != final[key]
    }
    if not changes:
        raise HTTPException(422, "the request changes nothing")
    entry = {"at": now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        unchanged_filter(record),
        {"$set": final, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance changed concurrently; reload and retry")
    return attendance_response(updated)


# Query builders are shared with /admin/explain so its plans describe the real work.
def check_date_range(start, end, max_days=None):
    if start is not None and end is not None:
        days = (CalendarDate.fromisoformat(end) - CalendarDate.fromisoformat(start)).days + 1
        if days < 1 or (max_days is not None and days > max_days):
            raise HTTPException(422, "invalid date range")


def page_offset(page, page_size):
    # Larger pages are necessarily empty; keep the skip representable as BSON int64.
    return min((page - 1) * page_size, 2**63 - 1)


def attendance_query(emp_code, date_from, date_to, status, page, page_size):
    check_date_range(date_from, date_to)
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    hint = (
        "employee_day_unique"
        if emp_code is not None
        else ("status_day_employee" if status is not None else "day_employee")
    )
    return {
        "find": "attendance_logs",
        "filter": query,
        "sort": {"date": -1, "emp_code": 1},
        "skip": page_offset(page, page_size),
        "limit": page_size,
        "hint": hint,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: DateString | None = None,
    date_to: DateString | None = None,
    status: Status | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    records = (
        db.attendance_logs.find(query["filter"])
        .hint(query["hint"])
        .sort(list(query["sort"].items()))
        .skip(query["skip"])
        .limit(query["limit"])
    )
    return {
        "items": [attendance_response(record) for record in records],
        "total": db.attendance_logs.count_documents(query["filter"], hint=query["hint"]),
        "page": page,
        "page_size": page_size,
    }


@dataclass
class Aggregation:
    collection: str
    pipeline: list
    hint: str

    def command(self):
        return {"aggregate": self.collection, "pipeline": self.pipeline, "cursor": {}, "hint": self.hint}

    def run(self):
        return api_value(list(db[self.collection].aggregate(self.pipeline, hint=self.hint)))


def month_bounds(month):
    year, number = map(int, month.split("-"))
    return month + "-01", f"{month}-{calendar.monthrange(year, number)[1]:02d}"


def mongo_date(expression):
    return {"$dateFromString": {"dateString": expression, "format": "%Y-%m-%d", "timezone": "UTC"}}


def weekday(expression):
    return {"$lte": [{"$isoDayOfWeek": mongo_date(expression)}, 5]}


def present_weight(weekdays_only=False):
    conditions = [{"$in": ["$status", list(PRESENCE)]}]
    if weekdays_only:
        conditions.append(weekday("$date"))
    return {"$cond": [{"$and": conditions}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def half_up(expression, places=2):
    """Nonnegative analytics values: Decimal128 math avoids binary ties and $round's half-even rule."""
    scale = Decimal128(str(10**places))
    return {
        "$let": {
            "vars": {"value": {"$toDecimal": expression}},
            "in": {
                "$cond": [
                    {"$eq": ["$$value", None]},
                    None,
                    {
                        "$divide": [
                            {"$floor": {"$add": [{"$multiply": ["$$value", scale]}, Decimal128("0.5")]}},
                            scale,
                        ]
                    },
                ]
            },
        }
    }


def stats_group():
    worked = {
        "$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]
    }
    late = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
    return {
        "$group": {
            "_id": None,
            "present_days": {"$sum": present_weight(weekdays_only=True)},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [late, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [late, "$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "hours_sum": {"$sum": {"$cond": [worked, {"$toDecimal": "$work_hours"}, 0]}},
            "hours_count": {"$sum": {"$cond": [worked, 1, 0]}},
        }
    }


def monthly_lookup(month):
    start, end = month_bounds(month)
    return {
        "$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}, stats_group()],
            "as": "stats",
        }
    }


def working_days_expression(month):
    start, end = month_bounds(month)
    # At most 31 generated dates, all inside MongoDB. Future joiners yield zero.
    first = mongo_date({"$max": ["$joined_on", start]})
    last = mongo_date(end)
    return {
        "$let": {
            "vars": {"first": first},
            "in": {
                "$size": {
                    "$filter": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$max": [
                                        0,
                                        {
                                            "$add": [
                                                {
                                                    "$dateDiff": {
                                                        "startDate": "$$first",
                                                        "endDate": last,
                                                        "unit": "day",
                                                    }
                                                },
                                                1,
                                            ]
                                        },
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "cond": {
                            "$lte": [
                                {
                                    "$isoDayOfWeek": {
                                        "$dateAdd": {
                                            "startDate": "$$first",
                                            "unit": "day",
                                            "amount": "$$offset",
                                        }
                                    }
                                },
                                5,
                            ]
                        },
                    }
                }
            },
        }
    }


def employee_monthly_query(emp_code, month):
    fields = {
        name: {"$ifNull": [f"$stats.{name}", 0]}
        for name in ("present_days", "late_count", "total_late_minutes", "total_overtime_minutes")
    }
    return Aggregation(
        "employees",
        [
            {"$match": {"emp_code": emp_code}},
            monthly_lookup(month),
            {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": 1,
                    "month": {"$literal": month},
                    "working_days": working_days_expression(month),
                    "leave_days": {"$ifNull": ["$stats.leave_count", 0]},
                    **fields,
                }
            },
            {
                "$set": {
                    "attendance_pct": {
                        "$cond": [
                            {"$gt": ["$working_days", 0]},
                            half_up(
                                {
                                    "$divide": [
                                        {"$multiply": [{"$toDecimal": "$present_days"}, 100]},
                                        "$working_days",
                                    ]
                                }
                            ),
                            None,
                        ]
                    }
                }
            },
        ],
        "employee_code_unique",
    )


def department_summary_query(month, department):
    _, end = month_bounds(month)
    match = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    sums = {
        name: {"$sum": {"$ifNull": [f"$stats.{name}", 0]}}
        for name in (
            "present_days",
            "late_count",
            "total_late_minutes",
            "leave_count",
            "on_duty_count",
            "hours_sum",
            "hours_count",
        )
    }
    return Aggregation(
        "employees",
        [
            {"$match": match},
            monthly_lookup(month),
            {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}, **sums}},
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": ["$hours_count", 0]},
                            half_up({"$divide": ["$hours_sum", "$hours_count"]}),
                            None,
                        ]
                    },
                }
            },
            {"$sort": {"department": 1}},
        ],
        "department_joined" if department is not None else "joined_department",
    )


def late_leaderboard_query(month, limit, department):
    start, end = month_bounds(month)
    employee_pipeline = [{"$project": {"_id": 0, "name": 1, "department": 1}}]
    if department is not None:
        employee_pipeline.insert(0, {"$match": {"department": department}})
    return Aggregation(
        "attendance_logs",
        [
            {"$match": {"date": {"$gte": start, "$lte": end}}},
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                    },
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "pipeline": employee_pipeline,
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "rank": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ],
        "day_employee",
    )


def department_trend_query(department, start, end):
    check_date_range(start, end, max_days=92)
    days = (CalendarDate.fromisoformat(end) - CalendarDate.fromisoformat(start)).days + 1
    # Read this department's employees first, then only their logs in the requested period.
    # Empty lookups contribute a zero anchor so an entirely unrecorded period still has a calendar.
    # Numeric densify bounds are exclusive above, including for a one-day range.
    return Aggregation(
        "employees",
        [
            {"$match": {"department": department}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "pipeline": [
                        {"$match": {"date": {"$gte": start, "$lte": end}}},
                        {
                            "$group": {
                                "_id": "$date",
                                "present_count": {"$sum": present_weight()},
                                "late_count": {
                                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                                },
                            }
                        },
                    ],
                    "as": "daily",
                }
            },
            {"$unwind": {"path": "$daily", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": {"$ifNull": ["$daily._id", start]},
                    "present_count": {"$sum": {"$ifNull": ["$daily.present_count", 0]}},
                    "late_count": {"$sum": {"$ifNull": ["$daily.late_count", 0]}},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "present_count": 1,
                    "late_count": 1,
                    "offset": {
                        "$dateDiff": {
                            "startDate": mongo_date(start),
                            "endDate": mongo_date("$_id"),
                            "unit": "day",
                        }
                    },
                }
            },
            {"$densify": {"field": "offset", "range": {"step": 1, "bounds": [0, days]}}},
            {
                "$set": {
                    "date": {
                        "$dateToString": {
                            "date": {
                                "$dateAdd": {
                                    "startDate": mongo_date(start),
                                    "unit": "day",
                                    "amount": "$offset",
                                }
                            },
                            "format": "%Y-%m-%d",
                            "timezone": "UTC",
                        }
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"day": "$date"},
                    "pipeline": [
                        {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                        {"$count": "count"},
                    ],
                    "as": "people",
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": weekday("$date"),
                    "headcount": {"$ifNull": [{"$arrayElemAt": ["$people.count", 0]}, 0]},
                    "present_count": {"$ifNull": ["$present_count", 0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                            half_up({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
                    },
                }
            },
            {"$set": {"moving_avg_7d": half_up("$moving_avg_7d", 4)}},
            {"$sort": {"date": 1}},
        ],
        "department_joined",
    )


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthString):
    items = employee_monthly_query(emp_code, month).run()
    if not items:
        raise HTTPException(404, "employee not found")
    return items[0]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthString, department: str | None = None):
    return {"month": month, "items": department_summary_query(month, department).run()}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthString, limit: LeaderboardLimit = 10, department: str | None = None):
    return {"month": month, "items": late_leaderboard_query(month, limit, department).run()}


@app.get("/analytics/departments/{department:department_name}/trend")
def department_trend(
    department: str,
    start: Annotated[DateString, Query(alias="from")],
    end: Annotated[DateString, Query(alias="to")],
):
    query = department_trend_query(department, start, end)
    if db.employees.find_one({"department": department}, {"_id": 1}, hint="department_joined") is None:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": query.run()}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: str | None = None,
    month: MonthString | None = None,
    department: str | None = None,
    limit: LeaderboardLimit = 10,
    date_from: DateString | None = None,
    date_to: DateString | None = None,
    status: Status | None = None,
    start: Annotated[DateString | None, Query(alias="from")] = None,
    end: Annotated[DateString | None, Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint in ("employee_monthly", "department_summary", "late_leaderboard") and month is None:
        raise HTTPException(422, "month is required")
    if endpoint == "attendance_list":
        command = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
    else:
        if endpoint == "employee_monthly":
            if emp_code is None:
                raise HTTPException(422, "emp_code is required")
            query = employee_monthly_query(emp_code, month)
        elif endpoint == "department_summary":
            query = department_summary_query(month, department)
        elif endpoint == "late_leaderboard":
            query = late_leaderboard_query(month, limit, department)
        else:
            if department is None or start is None or end is None:
                raise HTTPException(422, "department, from and to are required")
            query = department_trend_query(department, start, end)
        command, collection = query.command(), query.collection
    explanation = db.command("explain", command, verbosity="executionStats")
    # Raw plans contain BSON types; Extended JSON preserves them without altering the plan.
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(explanation)),
    }
