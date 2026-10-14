import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import pymongo
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH_MIN = 100000000000
EPOCH_MAX = 4102444800000


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure indexes idempotently at startup
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": exc.errors()})


# --------------------------------------------------------------------------- #
# Helpers & Business Logic (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int) -> float:
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    d = Decimal(str(val)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP)
    return float(d)


def epoch_ms_to_datetime_utc(ms: int) -> datetime:
    # Truncate instant to whole seconds before calculation and storage (R1)
    secs = ms // 1000
    return datetime.fromtimestamp(secs, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (end_h, end_m, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    # R2: Grace period 10 minutes strictly. Floor minutes since shift_start.
    punch_in_ist = punch_in_utc.astimezone(IST)
    att_date = date.fromisoformat(att_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, start_h, start_m, 0, tzinfo=IST)

    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_overtime_minutes(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    # R3: Whole minutes floor >= 30, otherwise 0
    punch_out_ist = punch_out_utc.astimezone(IST)
    att_date = date.fromisoformat(att_date_str)
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    if is_overnight:
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date

    shift_end_dt = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, end_h, end_m, 0, tzinfo=IST)
    diff_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    if diff_seconds >= 1800:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    # R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def is_half_day(work_hours: Optional[float]) -> bool:
    # R5: rounded work_hours < 4.50
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    serialized = {
        "at": datetime_to_epoch_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": {},
    }
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            from_val = datetime_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
            to_val = datetime_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
        serialized["changes"][k] = {"from": from_val, "to": to_val}
    return serialized


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: Optional[int] = Field(default=None, strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database not ready")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start and shift_end must differ")

    doc = body.model_dump()
    created_at = datetime.fromtimestamp(int(datetime.now(timezone.utc).timestamp()), tz=timezone.utc)
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee {body.emp_code} already exists")

    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        ts_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)

    att_date = get_attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(ts_utc, emp["shift_start"], emp["shift_end"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        out_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        out_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code},
        sort=[("punch_in", DESCENDING)],
    )

    if not rec or rec.get("punch_in") is None:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    punch_in_utc = rec["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_sec = (out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if diff_sec > 86400:
        raise HTTPException(status_code=422, detail="punch_out exceeds 24 hours after punch_in")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    att_date = rec["date"]
    work_h = compute_work_hours(punch_in_utc, out_utc)
    ot_m = compute_overtime_minutes(out_utc, emp["shift_start"], emp["shift_end"], att_date)
    hd = is_half_day(work_h)

    res = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": out_utc,
                "work_hours": work_h,
                "overtime_minutes": ot_m,
                "half_day": hd,
            }
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )

    if not res:
        raise HTTPException(status_code=409, detail="Record was punched out concurrently")

    return format_attendance_doc(res)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    for _ in range(5):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not rec:
            raise HTTPException(status_code=404, detail="Attendance record not found for this date")

        curr_status = rec["status"]
        curr_in = rec.get("punch_in")
        if curr_in and curr_in.tzinfo is None:
            curr_in = curr_in.replace(tzinfo=timezone.utc)
        curr_out = rec.get("punch_out")
        if curr_out and curr_out.tzinfo is None:
            curr_out = curr_out.replace(tzinfo=timezone.utc)

        target_status = body.status if body.status is not None else curr_status

        if target_status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(status_code=422, detail="Punch times cannot be set for ABSENT/LEAVE")
            target_in = None
            target_out = None
        else:
            target_in = epoch_ms_to_datetime_utc(body.punch_in) if body.punch_in is not None else curr_in
            if target_in is None:
                raise HTTPException(status_code=422, detail="Presence status requires punch_in")

            target_in_date = get_attendance_date(target_in, emp["shift_start"], emp["shift_end"])
            if target_in_date != date:
                raise HTTPException(status_code=422, detail="punch_in does not match the attendance date")

            target_out = epoch_ms_to_datetime_utc(body.punch_out) if body.punch_out is not None else curr_out
            if target_out is not None:
                diff_sec = (target_out - target_in).total_seconds()
                if diff_sec <= 0:
                    raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
                if diff_sec > 86400:
                    raise HTTPException(status_code=422, detail="punch_out exceeds 24 hours after punch_in")

        if target_status in ("ABSENT", "LEAVE"):
            target_late = 0
            target_ot = 0
            target_work = None
            target_hd = False
        else:
            target_late = compute_late_minutes(target_in, emp["shift_start"], emp["shift_end"], date)
            if target_out is not None:
                target_work = compute_work_hours(target_in, target_out)
                target_ot = compute_overtime_minutes(target_out, emp["shift_start"], emp["shift_end"], date)
                target_hd = is_half_day(target_work)
            else:
                target_work = None
                target_ot = 0
                target_hd = False

        changes = {}
        if curr_status != target_status:
            changes["status"] = {"from": curr_status, "to": target_status}
        if curr_in != target_in:
            changes["punch_in"] = {"from": curr_in, "to": target_in}
        if curr_out != target_out:
            changes["punch_out"] = {"from": curr_out, "to": target_out}
        if rec.get("work_hours") != target_work:
            changes["work_hours"] = {"from": rec.get("work_hours"), "to": target_work}
        if rec.get("late_minutes", 0) != target_late:
            changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": target_late}
        if rec.get("overtime_minutes", 0) != target_ot:
            changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": target_ot}
        if rec.get("half_day", False) != target_hd:
            changes["half_day"] = {"from": rec.get("half_day", False), "to": target_hd}

        if not changes:
            raise HTTPException(status_code=422, detail="No fields were modified")

        now_ms_dt = datetime.fromtimestamp(int(datetime.now(timezone.utc).timestamp()), tz=timezone.utc)
        history_entry = {
            "at": now_ms_dt,
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }

        history_len = len(rec.get("history", []))
        update_res = db.attendance_logs.update_one(
            {"_id": rec["_id"], f"history.{history_len}": {"$exists": False}},
            {
                "$set": {
                    "status": target_status,
                    "punch_in": target_in,
                    "punch_out": target_out,
                    "work_hours": target_work,
                    "late_minutes": target_late,
                    "overtime_minutes": target_ot,
                    "half_day": target_hd,
                },
                "$push": {"history": history_entry},
            },
        )
        if update_res.modified_count == 1:
            updated_rec = db.attendance_logs.find_one({"_id": rec["_id"]})
            return format_attendance_doc(updated_rec)

    raise HTTPException(status_code=409, detail="Concurrent update conflict")


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def get_working_days(joined_on_str: str, month_str: str) -> int:
    y, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    joined_date = date.fromisoformat(joined_on_str)
    start_date = max(date(y, m, 1), joined_date)
    end_date = date(y, m, num_days)

    if start_date > end_date:
        return 0

    working_days = 0
    cur = start_date
    while cur <= end_date:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def build_employee_monthly_pipeline(emp_code: str, month: str):
    date_regex = f"^{month}"
    return [
        {"$match": {"emp_code": emp_code, "date": {"$regex": date_regex}}},
        {
            "$addFields": {
                "dayOfWeek": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = get_working_days(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        agg = res[0]
        present_days = round_half_up(agg["present_days"], 2)
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late = int(agg["total_late_minutes"])
        total_ot = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)

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


def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_end_str = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end_str}}
    if department:
        match_stage["department"] = department

    date_regex = f"^{month}"

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ecode"]},
                                    {"$regexMatch": {"input": "$date", "regex": date_regex}},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "dayOfWeek": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date", "format": "%Y-%m-%d"}}},
                        0,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                    {"$ne": ["$logs.work_hours", "$$REMOVE"]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for doc in results:
        headcount = len(doc["employees_set"])
        if headcount == 0:
            continue

        work_hours_arr = doc.get("work_hours_list", [])
        if work_hours_arr:
            avg_work = round_half_up(sum(work_hours_arr) / len(work_hours_arr), 2)
        else:
            avg_work = None

        items.append({
            "department": doc["_id"],
            "headcount": headcount,
            "present_days": round_half_up(doc["present_days"], 2),
            "avg_work_hours": avg_work,
            "late_count": int(doc["late_count"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "leave_count": int(doc["leave_count"]),
            "on_duty_count": int(doc["on_duty_count"]),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, department: Optional[str] = None, limit: int = 10):
    date_regex = f"^{month}"
    match_logs = {"date": {"$regex": date_regex}, "late_minutes": {"$gt": 0}}

    pipeline: List[Dict[str, Any]] = [
        {"$match": match_logs},
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
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    pipeline = build_late_leaderboard_pipeline(month, department, limit)
    raw_items = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for item in raw_items:
        items.append({
            "rank": item["rank"],
            "emp_code": item["_id"],
            "name": item["emp"]["name"],
            "department": item["emp"]["department"],
            "total_late_minutes": int(item["total_late_minutes"]),
            "late_count": int(item["late_count"]),
        })

    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str):
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)

    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ecode"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "date": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dateFromString": {"dateString": "$logs.date", "format": "%Y-%m-%d", "timezone": "UTC"}},
                        from_dt,
                    ]
                },
                "status": "$logs.status",
                "half_day": "$logs.half_day",
                "late_minutes": "$logs.late_minutes",
                "has_log": {"$cond": [{"$ifNull": ["$logs.date", False]}, 1, 0]},
            }
        },
        {
            "$group": {
                "_id": "$date",
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
                            {"$and": [{"$eq": ["$has_log", 1]}, {"$gt": ["$late_minutes", 0]}]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt + timedelta(days=1)],
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}},
                ],
                "as": "dept_employees",
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id", "timezone": "UTC"}},
                "dayOfWeek": {"$dayOfWeek": {"date": "$_id", "timezone": "UTC"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dept_employees": 1,
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
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
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
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
        {"$sort": {"date": 1}},
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


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' cannot be earlier than 'from'")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    daily_records = list(db.employees.aggregate(pipeline))

    items = []
    for row in daily_records:
        rate = round_half_up(row["attendance_rate"], 4) if row.get("attendance_rate") is not None else None
        m_avg = round_half_up(row["moving_avg_7d"], 4) if row.get("moving_avg_7d") is not None else None
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": round_half_up(row["present_count"], 2),
            "late_count": int(row["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from > date_to")
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
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month required")
        pipe = build_employee_monthly_pipeline(emp_code, month)
        plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        pipe = build_department_summary_pipeline(month, department)
        plan = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        pipe = build_late_leaderboard_pipeline(month, department, limit)
        plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to required")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="'to' cannot be earlier than 'from'")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
        pipe = build_department_trend_pipeline(department, from_date, to_date)
        plan = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}