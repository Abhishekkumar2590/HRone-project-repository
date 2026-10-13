"""Employee attendance API. Run with ``uvicorn app.main:app --port 8000``."""
import calendar
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_day")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_emp_date_status")
    db.attendance_logs.create_index([("punch_in", DESCENDING)], name="attendance_punch_in")


@app.on_event("startup")
def startup():
    ensure_indexes()


def fail422(message):
    raise HTTPException(422, message)


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        fail422("date must be YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        fail422("month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    start = date(year, month, 1)
    return start, date(year, month, calendar.monthrange(year, month)[1])


def epoch_datetime(value: Optional[int]) -> datetime:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)
    if type(value) is not int or not 100_000_000_000 <= value <= 4_102_444_800_000:
        fail422("timestamp must be epoch milliseconds in the supported range")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        fail422("invalid timestamp")


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def ms(value: Optional[datetime]):
    return None if value is None else int(as_utc(value).timestamp() * 1000)


def local_instant(value: datetime) -> datetime:
    return as_utc(value).astimezone(IST)


def attendance_date(punch_in: datetime, emp: dict) -> str:
    local = local_instant(punch_in)
    sh, eh = time.fromisoformat(emp["shift_start"]), time.fromisoformat(emp["shift_end"])
    day = local.date()
    if eh <= sh and local.time().replace(tzinfo=None) < eh:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_for(punch_in: datetime, shift_start: str) -> datetime:
    local = local_instant(punch_in)
    h, m = map(int, shift_start.split(":"))
    return datetime.combine(local.date(), time(h, m), IST)


def shift_end_for(day: str, shift_start: str, shift_end: str) -> datetime:
    d = date.fromisoformat(day)
    sh, eh = time.fromisoformat(shift_start), time.fromisoformat(shift_end)
    if eh <= sh:
        d += timedelta(days=1)
    return datetime.combine(d, eh, IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str = "18:30") -> int:
    # Use the attendance day, which may be the previous local date overnight.
    local = local_instant(punch_in)
    start_day = date.fromisoformat(attendance_date(punch_in, {"shift_start": shift_start, "shift_end": shift_end}))
    start = datetime.combine(start_day, time.fromisoformat(shift_start), IST)
    elapsed = int((local - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = Decimal(int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "09:30") -> int:
    end = shift_end_for(date_str, shift_start, shift_end).astimezone(UTC)
    elapsed = int((as_utc(punch_out) - end).total_seconds())
    minutes = elapsed // 60
    return minutes if minutes >= 30 else 0


def round_num(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def clean_history(history):
    result = []
    for entry in history or []:
        copy = dict(entry)
        copy["at"] = ms(copy.get("at"))
        changes = {}
        for key, values in (copy.get("changes") or {}).items():
            changes[key] = {k: ms(v) if key in ("punch_in", "punch_out") else v for k, v in values.items()}
        copy["changes"] = changes
        result.append(copy)
    return result


def attendance_out(doc):
    return {"emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
            "punch_in": ms(doc.get("punch_in")), "punch_out": ms(doc.get("punch_out")),
            "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
            "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False),
            "history": clean_history(doc.get("history", []))}


def employee_out(doc):
    out = {k: doc[k] for k in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")}
    out["created_at"] = ms(doc["created_at"])
    return out


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, value):
        parse_date(value)
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            fail422("shift_start and shift_end must differ")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_time(cls, value):
        if value is not None:
            epoch_datetime(value)
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value not in PRESENCE:
            fail422("status must be PRESENT, WFH, or ON_DUTY")
        return value

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            fail422("punched_at cannot be null")
        return self


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_time(cls, value):
        if value is not None:
            epoch_datetime(value)
        return value

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            fail422("punched_at cannot be null")
        return self


class RegularizeIn(StrictModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value is not None and value not in STATUSES:
            fail422("invalid status")
        return value

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def valid_time(cls, value):
        if value is not None:
            epoch_datetime(value)
        return value


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(x) for x in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = epoch_datetime(body.punched_at)
    day = attendance_date(ts, emp)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from: query["date"]["$gte"] = date_from
        if date_to: query["date"]["$lte"] = date_to
    if status is not None: query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from: parse_date(date_from)
    if date_to: parse_date(date_to)
    if status is not None and status not in STATUSES: fail422("invalid status")
    if date_from and date_to and date_from > date_to: fail422("date_from must not exceed date_to")
    query = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "unknown employee")
    ts = epoch_datetime(body.punched_at)
    old = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not old: raise HTTPException(404, "no punch-in found")
    if old.get("punch_out") is not None: raise HTTPException(409, "already punched out")
    elapsed = (ts - as_utc(old["punch_in"])).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600: fail422("punch-out must be after punch-in and within 24 hours")
    work = compute_work_hours(old["punch_in"], ts)
    update = {"punch_out": ts, "work_hours": work,
              "overtime_minutes": compute_overtime(ts, emp["shift_end"], old["date"], emp["shift_start"]),
              "half_day": work < 4.50}
    result = db.attendance_logs.find_one_and_update({"_id": old["_id"], "punch_out": None}, {"$set": update}, return_document=ReturnDocument.AFTER)
    if result is None: raise HTTPException(409, "already punched out")
    return attendance_out(result)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "unknown employee")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old: raise HTTPException(404, "attendance record not found")
    new = {k: old.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    if "status" in body.model_fields_set: new["status"] = body.status
    if "punch_in" in body.model_fields_set: new["punch_in"] = epoch_datetime(body.punch_in) if body.punch_in is not None else None
    if "punch_out" in body.model_fields_set: new["punch_out"] = epoch_datetime(body.punch_out) if body.punch_out is not None else None
    if "punch_in" in body.model_fields_set and body.punch_in is None: fail422("punch_in cannot be null when supplied")
    if "punch_out" in body.model_fields_set and body.punch_out is None: fail422("punch_out cannot be null when supplied")
    if new["status"] in ("ABSENT", "LEAVE"):
        if "punch_in" in body.model_fields_set and body.punch_in is not None or "punch_out" in body.model_fields_set and body.punch_out is not None:
            fail422("ABSENT and LEAVE cannot have punch times")
        new["punch_in"] = new["punch_out"] = None
    elif new["punch_in"] is None:
        fail422("presence status requires punch_in")
    if new["punch_in"] is not None:
        if attendance_date(new["punch_in"], emp) != date: fail422("punch_in must belong to the record date")
        if new["punch_out"] is not None:
            seconds = (as_utc(new["punch_out"]) - as_utc(new["punch_in"])).total_seconds()
            if seconds <= 0 or seconds > 86400: fail422("punch_out must be after punch_in and within 24 hours")
    elif new["punch_out"] is not None: fail422("punch_out requires punch_in")
    if new["status"] in PRESENCE:
        new["late_minutes"] = compute_late_minutes(new["punch_in"], emp["shift_start"], emp["shift_end"])
        if new["punch_out"] is None:
            new.update(work_hours=None, overtime_minutes=0, half_day=False)
        else:
            new["work_hours"] = compute_work_hours(new["punch_in"], new["punch_out"])
            new["overtime_minutes"] = compute_overtime(new["punch_out"], emp["shift_end"], date, emp["shift_start"])
            new["half_day"] = new["work_hours"] < 4.50
    else:
        new.update(work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for key in fields:
        before = old.get(key, False if key == "half_day" else 0 if key in ("late_minutes", "overtime_minutes") else None)
        after = new[key]
        if before != after:
            changes[key] = {"from": before, "to": after}
    if not changes: fail422("request does not change the record")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    update = dict(new)
    result = db.attendance_logs.find_one_and_update({"_id": old["_id"], "$or": [{"history": old.get("history", [])}, {"history": {"$exists": False}}]},
        {"$set": update, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if result is None: raise HTTPException(409, "record changed concurrently; retry")
    return attendance_out(result)


def month_match(month):
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat(), end


def working_days(start: date, end: date) -> int:
    if start > end: return 0
    return sum(1 for i in range((end-start).days+1) if (start+timedelta(days=i)).weekday() < 5)


def month_pipeline(emp_code, month):
    lo, hi, _ = month_match(month)
    return [{"$match": {"emp_code": emp_code, "date": {"$gte": lo, "$lte": hi}}},
      {"$group": {"_id": None,
       "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
       "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
       "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
       "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
       "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    lo, hi, last = month_match(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp: raise HTTPException(404, "unknown employee")
    joined = max(date.fromisoformat(emp["joined_on"]), date.fromisoformat(lo))
    days = working_days(joined, last)
    result = next(db.attendance_logs.aggregate(month_pipeline(emp_code, month)), {})
    present = round_num(result.get("present_days", 0))
    pct = round_num(present / days * 100, 4) if days else None
    return {"emp_code": emp_code, "month": month, "working_days": days, "present_days": present,
      "leave_days": result.get("leave_days", 0), "late_count": result.get("late_count", 0),
      "total_late_minutes": result.get("total_late_minutes", 0), "total_overtime_minutes": result.get("total_overtime_minutes", 0), "attendance_pct": pct}


def department_summary_pipeline(month, department=None):
    lo, hi, end = month_match(month)
    employee_match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None: employee_match["department"] = department
    return [{"$match": employee_match}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "dept": "$department"}, "pipeline": [
       {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", lo]}, {"$lte": ["$date", hi]}]}}},
       {"$group": {"_id": None,
        "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
        "work_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
        "work_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}], "as": "stats"}},
      {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
      {"$group": {"_id": "$department", "headcount": {"$sum": 1},
        "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}}, "work_sum": {"$sum": {"$ifNull": ["$stats.work_sum", 0]}},
        "work_count": {"$sum": {"$ifNull": ["$stats.work_count", 0]}}, "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}}, "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
        "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}}}},
      {"$sort": {"_id": 1}}, {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
       "present_days": {"$round": ["$present_days", 2]}, "avg_work_hours": {"$cond": [{"$gt": ["$work_count", 0]}, {"$round": [{"$divide": ["$work_sum", "$work_count"]}, 2]}, None]},
       "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    month_match(month)
    return {"month": month, "items": list(db.employees.aggregate(department_summary_pipeline(month, department)))}


def leaderboard_pipeline(month, department=None, limit=10):
    lo, hi, _ = month_match(month)
    pipeline = [{"$match": {"date": {"$gte": lo, "$lte": hi}, "late_minutes": {"$gt": 0}}},
      {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
      {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
      {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
      {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    month_match(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(month, department, limit)))}


def trend_pipeline(department, start: date, end: date):
    # MongoDB creates intervening calendar days; Python supplies only range bounds.
    start_dt = datetime.combine(start, time.min, UTC)
    end_dt = datetime.combine(end, time.min, UTC)
    return [{"$documents": [{"day": start_dt}]},
      # $densify's explicit upper bound is exclusive; extend it by one day so
      # the API's requested `to` date is included in the result.
      {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]}}},
      {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day", "timezone": "UTC"}}}},
      {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}}, {"$count": "n"}], "as": "hc"}},
      {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [{"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
       {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}}, {"$unwind": "$e"}, {"$match": {"e.department": department}},
       {"$group": {"_id": None, "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}], "as": "stats"}},
      {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "present_count": {"$ifNull": [{"$arrayElemAt": ["$stats.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$stats.late_count", 0]}, 0]},
       "is_working_day": {"$lt": [{"$isoDayOfWeek": "$day"}, 6]}}},
      {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
      {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
      {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}}]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query()):
    start, end = parse_date(from_), parse_date(to)
    if end < start: fail422("to must be on or after from")
    if (end-start).days + 1 > 92: fail422("range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}): raise HTTPException(404, "unknown department")
    pipeline = trend_pipeline(department, start, end)
    return {"department": department, "items": list(db.aggregate(pipeline))}


def jsonable(value):
    return json.loads(json_util.dumps(value))


def explain(endpoint, params):
    if endpoint == "attendance_list":
        query = attendance_query(params.get("emp_code"), params.get("date_from"), params.get("date_to"), params.get("status"))
        find_command = {"find": "attendance_logs", "filter": query,
          "sort": {"date": -1, "emp_code": 1},
          "skip": (params.get("page", 1)-1)*params.get("page_size", 20),
          "limit": params.get("page_size", 20)}
        return "attendance_logs", db.command("explain", find_command, verbosity="executionStats")
    if endpoint == "employee_monthly":
        if not params.get("emp_code") or not params.get("month"): fail422("emp_code and month are required")
        return "attendance_logs", db.command("explain", {"aggregate": "attendance_logs", "pipeline": month_pipeline(params["emp_code"], params["month"]), "cursor": {}}, verbosity="executionStats")
    if endpoint == "department_summary":
        if not params.get("month"): fail422("month is required")
        return "employees", db.command("explain", {"aggregate": "employees", "pipeline": department_summary_pipeline(params["month"], params.get("department")), "cursor": {}}, verbosity="executionStats")
    if endpoint == "late_leaderboard":
        if not params.get("month"): fail422("month is required")
        p = leaderboard_pipeline(params["month"], params.get("department"), params.get("limit", 10))
        return "attendance_logs", db.command("explain", {"aggregate": "attendance_logs", "pipeline": p, "cursor": {}}, verbosity="executionStats")
    if endpoint == "department_trend":
        if not all(params.get(k) for k in ("department", "from", "to")): fail422("department, from, and to are required")
        p = trend_pipeline(params["department"], parse_date(params["from"]), parse_date(params["to"]))
        return "database", db.command("explain", {"aggregate": 1, "pipeline": p, "cursor": {}}, verbosity="executionStats")
    fail422("unknown endpoint")


@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                  limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                  status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                  from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):
    params = locals()
    params["from"] = from_
    collection, raw = explain(endpoint, params)
    return {"endpoint": endpoint, "collection": collection, "explain": jsonable(raw)}
