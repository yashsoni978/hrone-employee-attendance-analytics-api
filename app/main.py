"""
Employee Attendance & Analytics API
FastAPI + MongoDB (PyMongo)
"""
import calendar
import os
import pathlib
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

# Real environment variables take precedence over .env
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at application startup."""
    # employees collection indexes
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])
    db.employees.create_index([("department", ASCENDING)])

    # attendance_logs collection indexes
    # Natural composite unique key preventing race conditions on punch-in
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # List attendance sort & filter index: sorted by date DESC, emp_code ASC
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    # Analytics late leaderboard & department summary filter index
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    # Employee monthly summary index
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)])
    # Attendance status filter index
    db.attendance_logs.create_index([("date", DESCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    lifespan=lifespan,
)


def custom_openapi():
    """Load contract from openapi.yaml if present, or generate dynamically."""
    if app.openapi_schema:
        return app.openapi_schema
    yaml_file = pathlib.Path(__file__).parent.parent / "openapi.yaml"
    if yaml_file.exists():
        try:
            schema = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            app.openapi_schema = schema
            return app.openapi_schema
        except Exception:
            pass
    app.openapi_schema = get_openapi(
        title=app.title,
        version="2.0.0",
        description="Employee Attendance & Analytics API",
        routes=app.routes,
    )
    return app.openapi_schema


app.openapi = custom_openapi


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #

def round_half_up(val: Optional[float], decimals: int) -> Optional[float]:
    """R8 Rounding: standard arithmetic half-up rounding (not banker's rounding)."""
    if val is None:
        return None
    d = Decimal(str(val))
    pattern = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(pattern), rounding=ROUND_HALF_UP))


def to_epoch_millis(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC BSON datetime to epoch milliseconds."""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_millis(ms: Optional[int]) -> Optional[datetime]:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds (R1)."""
    if ms is None:
        return None
    sec = ms // 1000  # Truncate fractional seconds
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def truncate_seconds(dt: Optional[datetime]) -> Optional[datetime]:
    """R1: Before any calculation, instants are truncated to whole seconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date:
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if punch_in_ist.time() < time(end_h, end_m):
            return (punch_in_ist - timedelta(days=1)).strftime("%Y-%m-%d")
    return punch_in_ist.strftime("%Y-%m-%d")


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, att_date_str: str) -> int:
    """
    R2 Late:
    Grace period is 10 minutes. A punch-in is late only if it is strictly more than 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start - not since the end of grace period.
    """
    h, m = map(int, shift_start.split(":"))
    att_date = date.fromisoformat(att_date_str)
    shift_start_dt = datetime.combine(att_date, time(h, m), tzinfo=IST)
    diff_sec = (punch_in_dt - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    """
    R3 Overtime:
    overtime_minutes = whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    For overnight shifts, shift_end is on the calendar day following the attendance date.
    """
    h, m = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(att_date_str)
    if shift_end <= shift_start:
        att_date = att_date + timedelta(days=1)
    shift_end_dt = datetime.combine(att_date, time(h, m), tzinfo=IST)
    diff_sec = (punch_out_dt - shift_end_dt).total_seconds()
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4 Work hours:
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    hours = Decimal(str(diff_sec)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 Half day:
    After punch-out, half_day = true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format employee document for API response (omits MongoDB internal _id)."""
    created_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_millis(created_at) if isinstance(created_at, datetime) else created_at,
    }


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Format history entry for API response, serializing nested punch dates to epoch millis."""
    at_val = entry.get("at")
    raw_changes = entry.get("changes", {})
    changes = {}
    for k, v in raw_changes.items():
        if k in ("punch_in", "punch_out"):
            changes[k] = {
                "from": to_epoch_millis(v.get("from")) if isinstance(v.get("from"), datetime) else v.get("from"),
                "to": to_epoch_millis(v.get("to")) if isinstance(v.get("to"), datetime) else v.get("to"),
            }
        else:
            changes[k] = {"from": v.get("from"), "to": v.get("to")}
    return {
        "at": to_epoch_millis(at_val) if isinstance(at_val, datetime) else at_val,
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def serialize_attendance_log(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format attendance log document for API response (omits MongoDB internal _id)."""
    history = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_millis(doc.get("punch_in")),
        "punch_out": to_epoch_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas matching OpenAPI components
# --------------------------------------------------------------------------- #

class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("Invalid date format YYYY-MM-DD")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    """Readiness probe: returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database ping failed")


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforced by unique index on emp_code."""
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    doc = body.model_dump()
    # Server sets created_at as BSON datetime in UTC truncated to seconds
    doc["created_at"] = truncate_seconds(datetime.now(timezone.utc))

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee {body.emp_code} already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(emp) for emp in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    """
    Record a punch-in. Creates the day's record.
    Concurrency: enforced by unique composite index on (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = from_epoch_millis(body.punched_at)
    else:
        punch_in_dt = truncate_seconds(datetime.now(timezone.utc))

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return serialize_attendance_log(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out. Closes employee's most recent record whose punch_in is <= punched_at.
    Two simultaneous punch-outs yield exactly one 200 and one 409 via atomic CAS update.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = from_epoch_millis(body.punched_at)
    else:
        punch_out_dt = truncate_seconds(datetime.now(timezone.utc))

    # Find candidate record whose punch_in <= punched_at, sorted by punch_in desc
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )

    if not candidate:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    punch_in_dt = candidate["punch_in"]
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    att_date = candidate["date"]
    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_min = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], att_date)
    is_half_day = compute_half_day(work_hrs)

    # Atomic CAS update to prevent concurrent punch-out race
    res = db.attendance_logs.find_one_and_update(
        {
            "_id": candidate["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": is_half_day,
            }
        },
        return_document=True,
    )

    if not res:
        # Concurrent request punched out first
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return serialize_attendance_log(res)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records sorted by date DESC, emp_code ASC.
    date_from / date_to inclusive. date_from > date_to -> 422.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(status_code=422, detail="Invalid status")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_attendance_log(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recompute late_minutes, work_hours, overtime_minutes and half_day.
    Append audit trail entry to history atomically.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Current stored fields
    cur_status = record.get("status")
    cur_punch_in = record.get("punch_in")
    cur_punch_out = record.get("punch_out")
    cur_work_hours = record.get("work_hours")
    cur_late_minutes = record.get("late_minutes", 0)
    cur_overtime_minutes = record.get("overtime_minutes", 0)
    cur_half_day = record.get("half_day", False)
    cur_history = record.get("history") or []

    # Target values from request
    target_status = body.status if body.status is not None else cur_status
    target_punch_in = from_epoch_millis(body.punch_in) if body.punch_in is not None else cur_punch_in
    target_punch_out = from_epoch_millis(body.punch_out) if body.punch_out is not None else cur_punch_out

    # Contract rules validation
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Setting status to ABSENT or LEAVE clears punches; cannot supply punch times")
        target_punch_in = None
        target_punch_out = None
    else:
        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

    if target_punch_in is not None:
        att_date_calc = get_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if att_date_calc != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

    if target_punch_out is not None:
        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        if target_punch_out <= target_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
        if (target_punch_out - target_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived values
    if target_status in ("ABSENT", "LEAVE"):
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out is not None:
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime(target_punch_out, emp["shift_start"], emp["shift_end"], date)
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    # Check which fields actually changed
    changes: Dict[str, Any] = {}
    field_pairs = [
        ("status", cur_status, target_status),
        ("punch_in", cur_punch_in, target_punch_in),
        ("punch_out", cur_punch_out, target_punch_out),
        ("work_hours", cur_work_hours, target_work_hours),
        ("late_minutes", cur_late_minutes, target_late_minutes),
        ("overtime_minutes", cur_overtime_minutes, target_overtime_minutes),
        ("half_day", cur_half_day, target_half_day),
    ]

    for fname, old_val, new_val in field_pairs:
        if old_val != new_val:
            changes[fname] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    # Audit history entry
    hist_entry = {
        "at": truncate_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    update_doc = {
        "$set": {
            "status": target_status,
            "punch_in": target_punch_in,
            "punch_out": target_punch_out,
            "work_hours": target_work_hours,
            "late_minutes": target_late_minutes,
            "overtime_minutes": target_overtime_minutes,
            "half_day": target_half_day,
        },
        "$push": {
            "history": hist_entry,
        },
    }

    # Atomic CAS condition: check history length or non-existence to prevent lost updates
    if len(cur_history) == 0:
        query_filter = {
            "_id": record["_id"],
            "$or": [
                {"history": {"$size": 0}},
                {"history": {"$exists": False}},
            ],
        }
    else:
        query_filter = {
            "_id": record["_id"],
            "history": {"$size": len(cur_history)},
        }

    res = db.attendance_logs.find_one_and_update(
        query_filter,
        update_doc,
        return_document=True,
    )

    if not res:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return serialize_attendance_log(res)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregation Pipelines)
# --------------------------------------------------------------------------- #

def get_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    start_date = f"{month}-01"
    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)
    end_date = f"{month}-{num_days:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$let": {
                            "vars": {
                                "dow": {
                                    "$dayOfWeek": {
                                        "date": {"$dateFromString": {"dateString": "$date"}},
                                        "timezone": "UTC",
                                    }
                                }
                            },
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$gte": ["$$dow", 2]},
                                            {"$lte": ["$$dow", 6]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)
    month_start = date(year, m, 1)
    month_end = date(year, m, num_days)

    joined_d = date.fromisoformat(emp["joined_on"])
    if joined_d > month_end:
        working_days = 0
    else:
        eff_start = max(month_start, joined_d)
        cur = eff_start
        working_days = 0
        while cur <= month_end:
            if cur.weekday() < 5:  # Mon-Fri
                working_days += 1
            cur += timedelta(days=1)

    pipeline = get_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        rec = agg_res[0]
        present_days = float(rec.get("present_days", 0.0))
        leave_days = int(rec.get("leave_days", 0))
        late_count = int(rec.get("late_count", 0))
        total_late = int(rec.get("total_late_minutes", 0))
        total_ot = int(rec.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        raw_pct = (present_days / working_days) * 100
        attendance_pct = round_half_up(raw_pct, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def get_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_filter: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_filter["department"] = department

    return [
        {"$match": match_filter},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"dept_emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": start_date, "$lte": end_date},
                            "$expr": {"$in": ["$emp_code", "$$dept_emps"]},
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$let": {
                                    "vars": {
                                        "dow": {
                                            "$dayOfWeek": {
                                                "date": {"$dateFromString": {"dateString": "$$l.date"}},
                                                "timezone": "UTC",
                                            }
                                        }
                                    },
                                    "in": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$gte": ["$$dow", 2]},
                                                    {"$lte": ["$$dow", 6]},
                                                ]
                                            },
                                            {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                            0.0,
                                        ]
                                    },
                                }
                            },
                        }
                    }
                },
                "present_work_hours": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                                    "$$l.late_minutes",
                                    0,
                                ]
                            },
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$present_work_hours"}, 0]},
                        {"$avg": "$present_work_hours.work_hours"},
                        None,
                    ]
                }
            }
        },
        {"$project": {"present_work_hours": 0}},
        {"$sort": {"department": ASCENDING}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month."""
    pipeline = get_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))

    for item in items:
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)

    return {"month": month, "items": items}


def get_late_leaderboard_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_filter = {
        "date": {"$gte": start_date, "$lte": end_date},
        "late_minutes": {"$gt": 0},
    }

    pipeline = [
        {"$match": match_filter},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month.
    Rank is standard competition ranking (1, 2, 2, 4).
    limit is applied after ranking (returns rows where rank <= limit).
    """
    pipeline = get_late_leaderboard_pipeline(month, department)
    all_ranked = list(db.attendance_logs.aggregate(pipeline))

    filtered_items = [item for item in all_ranked if item["rank"] <= limit]
    return {"month": month, "items": filtered_items}


def get_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    start_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
    bounds_end = end_dt + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"date_obj": {"$literal": start_dt}}},
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, bounds_end],
                },
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_obj",
                        "timezone": "UTC",
                    }
                },
                "dayOfWeek": {
                    "$dayOfWeek": {
                        "date": "$date_obj",
                        "timezone": "UTC",
                    }
                },
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$and": [
                        {"$gte": ["$dayOfWeek", 2]},
                        {"$lte": ["$dayOfWeek", 6]},
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$cur_date"]},
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_info",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc_info.count"}, 0]}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$cur_date"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {
                            "emp.department": department
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "log_stats",
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": [{"$first": "$log_stats.present_count"}, 0.0]},
                "late_count": {"$ifNull": [{"$first": "$log_stats.late_count"}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {"$toDouble": "$present_count"},
                                {"$toDouble": "$headcount"},
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department."""
    emp = db.employees.find_one({"department": department})
    if not emp:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' must be >= 'from'")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    pipeline = get_department_trend_pipeline(department, from_date, to_date)
    items = list(db.employees.aggregate(pipeline))

    for item in items:
        if item.get("attendance_rate") is not None:
            item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item.get("moving_avg_7d") is not None:
            item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    MongoDB explain with executionStats verbosity for the named endpoint's main query or aggregation.
    Returns response conforming to OpenAPI schema: {endpoint, collection, explain}.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        plan = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": plan,
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code required")
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        pipeline = get_employee_monthly_pipeline(emp_code, month)
        plan = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": plan,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        pipeline = get_department_summary_pipeline(month, department)
        plan = db.command(
            {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": plan,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        pipeline = get_late_leaderboard_pipeline(month, department)
        plan = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": plan,
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department required")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="from and to dates required")
        pipeline = get_department_trend_pipeline(department, from_date, to_date)
        plan = db.command(
            {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": plan,
        }

    raise HTTPException(status_code=422, detail="Unknown endpoint")
