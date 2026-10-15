"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (reads from environment; defaults to localhost)
"""
import calendar
import datetime
import math
import os
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
import pymongo
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Load .env if present, but os.environ takes precedence
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def ensure_indexes():
    # employees collection indexes
    db.employees.create_index([("emp_code", pymongo.ASCENDING)], unique=True)
    db.employees.create_index([("department", pymongo.ASCENDING)])
    db.employees.create_index([("joined_on", pymongo.ASCENDING)])
    db.employees.create_index([("department", pymongo.ASCENDING), ("joined_on", pymongo.ASCENDING)])

    # attendance_logs collection indexes
    db.attendance_logs.create_index(
        [("emp_code", pymongo.ASCENDING), ("date", pymongo.ASCENDING)], unique=True
    )
    db.attendance_logs.create_index(
        [("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("emp_code", pymongo.ASCENDING), ("date", pymongo.DESCENDING)]
    )
    db.attendance_logs.create_index(
        [("emp_code", pymongo.ASCENDING), ("punch_in", pymongo.DESCENDING)]
    )
    db.attendance_logs.create_index(
        [("date", pymongo.ASCENDING), ("late_minutes", pymongo.ASCENDING), ("emp_code", pymongo.ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("status", pymongo.ASCENDING), ("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)]
    )


# --------------------------------------------------------------------------- #
# Lifespan: create all indexes idempotently at startup
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


# Create indexes immediately on import as well
try:
    ensure_indexes()
except Exception:
    pass



app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Mathematical & Date Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float | Decimal], decimals: int) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def to_epoch_ms(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=datetime.timezone.utc)
        return int(val.timestamp() * 1000)
    return None


def from_epoch_ms(ms: int) -> datetime.datetime:
    # Truncate to whole seconds per R1 before storing or computing
    truncated_ms = (ms // 1000) * 1000
    return datetime.datetime.fromtimestamp(truncated_ms / 1000.0, tz=datetime.timezone.utc)


def get_current_epoch_ms() -> int:
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    return (now_ms // 1000) * 1000


def get_attendance_date(punch_in_dt: datetime.datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is the IST calendar date of punch_in.
    Overnight shifts (shift_end <= shift_start): punch-in earlier than shift_end belongs
    to the shift that started the previous day.
    """
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))

    punch_ist = punch_in_dt.astimezone(IST)
    cal_date = punch_ist.date()

    if (h_end, m_end) <= (h_start, m_start):
        # Overnight shift
        if (punch_ist.hour, punch_ist.minute, punch_ist.second) < (h_end, m_end, 0):
            cal_date = cal_date - datetime.timedelta(days=1)

    return cal_date.isoformat()


def compute_late_minutes(punch_in_dt: datetime.datetime, shift_start: str, record_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch_in is strictly more than 10:00 (600s) after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start.
    """
    h_start, m_start = map(int, shift_start.split(":"))
    rec_date = datetime.date.fromisoformat(record_date)
    shift_start_dt = datetime.datetime.combine(
        rec_date, datetime.time(h_start, m_start), tzinfo=IST
    )

    diff_sec = (punch_in_dt - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(math.floor(diff_sec / 60.0))
    return 0


def compute_work_hours(punch_in_dt: datetime.datetime, punch_out_dt: datetime.datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def compute_overtime(
    punch_out_dt: datetime.datetime, shift_end: str, shift_start: str, record_date: str
) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    For overnight shifts, shift_end is on record_date + 1 day.
    """
    h_end, m_end = map(int, shift_end.split(":"))
    h_start, m_start = map(int, shift_start.split(":"))
    rec_date = datetime.date.fromisoformat(record_date)

    if (h_end, m_end) <= (h_start, m_start):
        end_date = rec_date + datetime.timedelta(days=1)
    else:
        end_date = rec_date

    shift_end_dt = datetime.datetime.combine(
        end_date, datetime.time(h_end, m_end), tzinfo=IST
    )

    diff_sec = (punch_out_dt - shift_end_dt).total_seconds()
    if diff_sec >= 1800:  # at least 30 minutes
        return int(math.floor(diff_sec / 60.0))
    return 0


def compute_working_days_in_month(month: str, joined_on_str: str) -> int:
    """
    R7: Working days Monday-Friday, counted from max(joined_on, month_start) to month_end.
    """
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    start_date = max(joined_on_str, month_start)
    if start_date > month_end:
        return 0

    cur = datetime.date.fromisoformat(start_date)
    end = datetime.date.fromisoformat(month_end)
    count = 0
    while cur <= end:
        if cur.weekday() < 5:  # Mon-Fri
            count += 1
        cur += datetime.timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Serialization Helpers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


def serialize_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    hist = []
    for h in doc.get("history") or []:
        changes = {}
        for k, v in (h.get("changes") or {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                f_val = to_epoch_ms(f_val)
                t_val = to_epoch_ms(t_val)
            changes[k] = {"from": f_val, "to": t_val}
        hist.append({
            "at": to_epoch_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": hist,
    }


# --------------------------------------------------------------------------- #
# Request Schemas (Pydantic)
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift_and_date(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end cannot be equal")
        try:
            datetime.date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("Invalid calendar date for joined_on")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database unavailable: {str(e)}")


@app.post("/employees", status_code=http_status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    created_dt = from_epoch_ms(get_current_epoch_ms())
    doc = body.model_dump()
    doc["created_at"] = created_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail="Employee with this emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", pymongo.ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(emp) for emp in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=http_status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Employee not found")

    raw_ms = body.punched_at if body.punched_at is not None else get_current_epoch_ms()
    punch_in_dt = from_epoch_ms(raw_ms)

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
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail="Attendance record already exists for this date")

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=http_status.HTTP_200_OK)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Employee not found")

    raw_ms = body.punched_at if body.punched_at is not None else get_current_epoch_ms()
    punch_out_dt = from_epoch_ms(raw_ms)

    # Find the most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", pymongo.DESCENDING)],
    )

    if not record:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="No matching punch-in found for punch-out")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail="Record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be strictly after punch_in")

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec > 86400:  # 24 hours
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = compute_overtime(punch_out_dt, emp["shift_end"], emp["shift_start"], record["date"])
    half_day = bool(work_hours < 4.50)

    # Race-safe atomic update
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail="Record was concurrently punched out")

    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from cannot be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    body: RegularizeRequest,
    emp_code: str = Path(..., pattern=r"^EMP\d{4,6}$"),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Attendance record not found for this date")

    new_status = body.status if body.status is not None else record["status"]

    # Rules: setting ABSENT or LEAVE clears both punch times, supplying punch_in or punch_out is 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Status {new_status} cannot have punch_in or punch_out times",
            )
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            target_punch_in = from_epoch_ms(body.punch_in)
        else:
            target_punch_in = record.get("punch_in")

        if target_punch_in is None:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Status {new_status} requires a punch_in time",
            )

        # punch_in must stay on the record's attendance date per R1
        attributed_date = get_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if attributed_date != date:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in resolves to date {attributed_date}, which does not match record date {date}",
            )

        # target punch_out
        if body.punch_out is not None:
            target_punch_out = from_epoch_ms(body.punch_out)
        else:
            target_punch_out = record.get("punch_out")

        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)

        if target_punch_out is not None:
            if target_punch_out <= target_punch_in:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in",
                )
            diff_sec = (target_punch_out - target_punch_in).total_seconds()
            if diff_sec > 86400:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime(
                target_punch_out, emp["shift_end"], emp["shift_start"], date
            )
            target_half_day = bool(target_work_hours < 4.50)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    # Compare old values vs new values for changes
    changes_db = {}
    tracked_fields = [
        ("status", record.get("status"), new_status),
        ("punch_in", record.get("punch_in"), target_punch_in),
        ("punch_out", record.get("punch_out"), target_punch_out),
        ("work_hours", record.get("work_hours"), target_work_hours),
        ("late_minutes", record.get("late_minutes", 0), target_late_minutes),
        ("overtime_minutes", record.get("overtime_minutes", 0), target_overtime_minutes),
        ("half_day", record.get("half_day", False), target_half_day),
    ]

    for f_name, old_val, new_val in tracked_fields:
        if old_val != new_val:
            changes_db[f_name] = {"from": old_val, "to": new_val}

    if not changes_db:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Regularization request changes nothing",
        )

    now_dt = from_epoch_ms(get_current_epoch_ms())
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    current_history_len = len(record.get("history") or [])

    # Optimistic locking: ensure history length has not changed to avoid losing concurrent history
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": current_history_len},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Conflict: record was concurrently modified by another request",
        )

    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(..., pattern=r"^EMP\d{4,6}$"),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Employee not found")

    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    working_days = compute_working_days_in_month(month, emp["joined_on"])

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": month_start, "$lte": month_end},
        }},
        {"$addFields": {
            "dt": {"$dateFromString": {"dateString": "$date"}},
            "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
        }},
        {"$addFields": {
            "iso_dow": {"$isoDayOfWeek": "$dt"},
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": ["$is_presence", {"$lte": ["$iso_dow", 5]}]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                        0.0,
                    ]
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
        }},
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        stats = agg_res[0]
        present_days = stats["present_days"]
        leave_days = stats["leave_days"]
        late_count = stats["late_count"]
        total_late = stats["total_late_minutes"]
        total_ot = stats["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    att_pct = None
    if working_days > 0:
        att_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_filter: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_filter["department"] = department

    pipeline = [
        {"$match": match_filter},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$code"]},
                            {"$gte": ["$date", month_start]},
                            {"$lte": ["$date", month_end]},
                        ]
                    }
                }}
            ],
            "as": "logs",
        }},
        {"$unwind": {
            "path": "$logs",
            "preserveNullAndEmptyArrays": True,
        }},
        {"$addFields": {
            "log_dt": {
                "$cond": [
                    {"$ifNull": ["$logs.date", False]},
                    {"$dateFromString": {"dateString": "$logs.date"}},
                    None,
                ]
            }
        }},
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$lte": [{"$isoDayOfWeek": "$log_dt"}, 5]},
                        ]},
                        {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                        0.0,
                    ]
                }
            },
            "work_hours_sum": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$logs.work_hours", None]},
                        ]},
                        "$logs.work_hours",
                        0.0,
                    ]
                }
            },
            "work_hours_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$logs.work_hours", None]},
                        ]},
                        1,
                        0,
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                        1,
                        0,
                    ]
                }
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
            },
            "leave_count": {
                "$sum": {
                    "$cond": [
                        {"$eq": ["$logs.status", "LEAVE"]},
                        1,
                        0,
                    ]
                }
            },
            "on_duty_count": {
                "$sum": {
                    "$cond": [
                        {"$eq": ["$logs.status", "ON_DUTY"]},
                        1,
                        0,
                    ]
                }
            },
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$employees"},
            "present_days": "$present_days",
            "avg_work_hours": {
                "$cond": [
                    {"$gt": ["$work_hours_count", 0]},
                    {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                    None,
                ]
            },
            "late_count": "$late_count",
            "total_late_minutes": "$total_late_minutes",
            "leave_count": "$leave_count",
            "on_duty_count": "$on_duty_count",
        }},
        {"$sort": {"department": pymongo.ASCENDING}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        if doc.get("headcount", 0) > 0:
            doc["present_days"] = round_half_up(doc["present_days"], 2)
            doc["avg_work_hours"] = round_half_up(doc["avg_work_hours"], 2)
            items.append(doc)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {"$match": {
            "date": {"$gte": month_start, "$lte": month_end},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": "$total_late_minutes",
            "late_count": "$late_count",
        }},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    if to_date < from_date:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'to' date cannot be before 'from' date")

    start_d = datetime.date.fromisoformat(from_date)
    end_d = datetime.date.fromisoformat(to_date)
    days_span = (end_d - start_d).days + 1
    if days_span > 92:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range cannot exceed 92 days")

    # 404 for unknown department (no employees in department)
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Department not found")

    from_dt = datetime.datetime.combine(start_d, datetime.time.min)
    to_dt = datetime.datetime.combine(end_d, datetime.time.min)

    pipeline = [
        {"$match": {"emp_code": "__NON_EXISTENT__"}},
        {"$densify": {
            "field": "d",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt + datetime.timedelta(days=1)],
            },
        }},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"cur_date": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$cur_date"]},
                        ]
                    }
                }},
                {"$project": {"_id": 0, "emp_code": 1}},
            ],
            "as": "dept_emps",
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"cur_date": "$date", "emp_codes": "$dept_emps.emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$date", "$$cur_date"]},
                            {"$in": ["$emp_code", "$$emp_codes"]},
                        ]
                    }
                }},
            ],
            "as": "day_logs",
        }},
        {"$addFields": {
            "headcount": {"$size": "$dept_emps"},
            "present_count": {
                "$reduce": {
                    "input": "$day_logs",
                    "initialValue": 0.0,
                    "in": {
                        "$add": [
                            "$$value",
                            {"$cond": [
                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1.0]},
                                0.0,
                            ]},
                        ]
                    },
                }
            },
            "late_count": {
                "$reduce": {
                    "input": "$day_logs",
                    "initialValue": 0,
                    "in": {
                        "$add": [
                            "$$value",
                            {"$cond": [{"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]},
                        ]
                    },
                }
            },
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None,
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"d": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, "current"],
                    },
                }
            },
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]

    items = []
    for doc in db.attendance_logs.aggregate(pipeline):
        doc["present_count"] = round_half_up(doc["present_count"], 2)
        doc["attendance_rate"] = round_half_up(doc["attendance_rate"], 4)
        doc["moving_avg_7d"] = round_half_up(doc["moving_avg_7d"], 4)
        items.append(doc)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
def sanitize_mongo_dict(obj: Any) -> Any:
    """Ensure explain output is JSON serializable."""
    if isinstance(obj, dict):
        return {k: sanitize_mongo_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_mongo_dict(v) for v in obj]
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    return obj


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        cursor = db.attendance_logs.find(q).sort([("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)]).skip(skip).limit(page_size)
        raw_explain = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_mongo_dict(raw_explain)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        y, m = map(int, month.split("-"))
        num_days = calendar.monthrange(y, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"
        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }},
            {"$addFields": {
                "dt": {"$dateFromString": {"dateString": "$date"}},
                "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            }},
            {"$addFields": {
                "iso_dow": {"$isoDayOfWeek": "$dt"},
            }},
            {"$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_presence", {"$lte": ["$iso_dow", 5]}]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }},
        ]
        raw_explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_mongo_dict(raw_explain)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        y, m = map(int, month.split("-"))
        num_days = calendar.monthrange(y, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"
        match_filter: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department is not None:
            match_filter["department"] = department
        pipeline = [
            {"$match": match_filter},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$emp_code", "$$code"]},
                                {"$gte": ["$date", month_start]},
                                {"$lte": ["$date", month_end]},
                            ]
                        }
                    }}
                ],
                "as": "logs",
            }},
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            }},
        ]
        raw_explain = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": sanitize_mongo_dict(raw_explain)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        y, m = map(int, month.split("-"))
        num_days = calendar.monthrange(y, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"
        pipeline = [
            {"$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }},
            {"$unwind": "$emp"},
        ]
        if department is not None:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend([
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])
        raw_explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_mongo_dict(raw_explain)}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="'to' date cannot be before 'from' date")
        start_d = datetime.date.fromisoformat(from_date)
        end_d = datetime.date.fromisoformat(to_date)
        if (end_d - start_d).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        from_dt = datetime.datetime.combine(start_d, datetime.time.min)
        to_dt = datetime.datetime.combine(end_d, datetime.time.min)

        pipeline = [
            {"$match": {"emp_code": "__NON_EXISTENT__"}},
            {"$densify": {
                "field": "d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt + datetime.timedelta(days=1)],
                },
            }},
            {"$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
            }},
            {"$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$department", department]},
                                {"$lte": ["$joined_on", "$$cur_date"]},
                            ]
                        }
                    }},
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "dept_emps",
            }},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date", "emp_codes": "$dept_emps.emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$date", "$$cur_date"]},
                                {"$in": ["$emp_code", "$$emp_codes"]},
                            ]
                        }
                    }},
                ],
                "as": "day_logs",
            }},
        ]
        raw_explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_mongo_dict(raw_explain)}

    raise HTTPException(status_code=422, detail=f"Unsupported endpoint: {endpoint}")
