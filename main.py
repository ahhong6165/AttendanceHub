import base64
import hashlib
import hmac
import json
import math
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    DateTime, Float, ForeignKey, Integer, String, UniqueConstraint,
    create_engine, select
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

if Path("/var/data").exists():
    default_db = "sqlite:////var/data/attendance.db"
else:
    default_db = "sqlite:///./attendance.db"
DATABASE_URL = os.getenv("DATABASE_URL", default_db)
# DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./attendance.db")
QR_SECRET = os.getenv("QR_SECRET", "CHANGE_THIS_TO_A_LONG_RANDOM_SECRET")
if len(QR_SECRET) < 24:
    raise RuntimeError("QR_SECRET must be at least 24 characters long.")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Student(Base):
    __tablename__ = "students"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(150))


class Course(Base):
    __tablename__ = "courses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150))


class AttendanceSession(Base):
    __tablename__ = "attendance_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    radius_m: Mapped[float] = mapped_column(Float, default=100)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    qr_nonce: Mapped[str] = mapped_column(String(64), unique=True)


class Attendance(Base):
    __tablename__ = "attendance"
    __table_args__ = (
        UniqueConstraint("session_id", "student_id", name="uq_attendance_session_student"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("attendance_sessions.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    student_latitude: Mapped[float] = mapped_column(Float)
    student_longitude: Mapped[float] = mapped_column(Float)
    distance_m: Mapped[float] = mapped_column(Float)


Base.metadata.create_all(bind=engine)
app = FastAPI(title="Student Attendance API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    # SQLite may return naive datetimes even for timezone-aware columns.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two GPS coordinates, in metres."""
    earth_radius = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * earth_radius * math.asin(math.sqrt(a))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_qr_token(attendance_session: AttendanceSession) -> str:
    payload = {
        "sid": attendance_session.id,
        "exp": int(as_utc(attendance_session.ends_at).timestamp()),
        "nonce": attendance_session.qr_nonce,
    }
    encoded = b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(QR_SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{b64url(signature)}"


def verify_qr_token(token: str) -> dict:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected = b64url(
            hmac.new(QR_SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected):
            raise ValueError("Invalid signature")
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        if not all(key in payload for key in ("sid", "exp", "nonce")):
            raise ValueError("Missing claims")
        if int(payload["exp"]) < int(utcnow().timestamp()):
            raise HTTPException(status_code=400, detail="QR code has expired")
        return payload
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid QR code")


class StudentCreate(BaseModel):
    student_code: str = Field(min_length=1, max_length=50)
    full_name: str = Field(min_length=1, max_length=150)


class CourseCreate(BaseModel):
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=150)


class SessionCreate(BaseModel):
    course_id: int
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_m: float = Field(default=100, gt=0, le=2000)
    duration_minutes: int = Field(default=15, ge=1, le=240)


class AttendanceScan(BaseModel):
    student_id: int
    qr_token: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class StudentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    student_code: str
    full_name: str


class CourseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str


@app.get("/")
def root():
    return {"message": "Student Attendance API", "docs": "/docs"}


@app.post("/students", response_model=StudentOut, status_code=201)
def create_student(body: StudentCreate, db: Session = Depends(get_db)):
    student = Student(**body.model_dump())
    db.add(student)
    try:
        db.commit()
        db.refresh(student)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Student code already exists")
    return student


@app.get("/students", response_model=list[StudentOut])
def list_students(db: Session = Depends(get_db)):
    return db.scalars(select(Student).order_by(Student.id)).all()


@app.post("/courses", response_model=CourseOut, status_code=201)
def create_course(body: CourseCreate, db: Session = Depends(get_db)):
    course = Course(**body.model_dump())
    db.add(course)
    try:
        db.commit()
        db.refresh(course)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Course code already exists")
    return course


@app.get("/courses", response_model=list[CourseOut])
def list_courses(db: Session = Depends(get_db)):
    return db.scalars(select(Course).order_by(Course.id)).all()


@app.post("/sessions", status_code=201)
def create_attendance_session(body: SessionCreate, db: Session = Depends(get_db)):
    if db.get(Course, body.course_id) is None:
        raise HTTPException(status_code=404, detail="Course not found")

    now = utcnow()
    attendance_session = AttendanceSession(
        course_id=body.course_id,
        latitude=body.latitude,
        longitude=body.longitude,
        radius_m=body.radius_m,
        starts_at=now,
        ends_at=now + timedelta(minutes=body.duration_minutes),
        qr_nonce=secrets.token_urlsafe(24),
    )
    db.add(attendance_session)
    db.commit()
    db.refresh(attendance_session)
    return {
        "id": attendance_session.id,
        "course_id": attendance_session.course_id,
        "starts_at": as_utc(attendance_session.starts_at).isoformat(),
        "ends_at": as_utc(attendance_session.ends_at).isoformat(),
        "radius_m": attendance_session.radius_m,
    }


@app.get("/sessions/{session_id}/qr")
def get_session_qr(session_id: int, db: Session = Depends(get_db)):
    attendance_session = db.get(AttendanceSession, session_id)
    if attendance_session is None:
        raise HTTPException(status_code=404, detail="Attendance session not found")
    now = utcnow()
    if now < as_utc(attendance_session.starts_at) or now > as_utc(attendance_session.ends_at):
        raise HTTPException(status_code=400, detail="Attendance session is not active")
    token = make_qr_token(attendance_session)
    return {
        "session_id": session_id,
        "qr_token": token,
        "expires_at": as_utc(attendance_session.ends_at).isoformat(),
        "note": "Encode qr_token as the QR code content in your frontend.",
    }


@app.post("/attendance/scan", status_code=201)
def scan_attendance(body: AttendanceScan, db: Session = Depends(get_db)):
    payload = verify_qr_token(body.qr_token)
    attendance_session = db.get(AttendanceSession, int(payload["sid"]))
    if attendance_session is None:
        raise HTTPException(status_code=404, detail="Attendance session not found")

    if not hmac.compare_digest(str(payload["nonce"]), attendance_session.qr_nonce):
        raise HTTPException(status_code=400, detail="QR code is no longer valid")

    now = utcnow()
    if now < as_utc(attendance_session.starts_at) or now > as_utc(attendance_session.ends_at):
        raise HTTPException(status_code=400, detail="Attendance session is not active")

    student = db.get(Student, body.student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")

    existing = db.scalar(
        select(Attendance).where(
            Attendance.session_id == attendance_session.id,
            Attendance.student_id == student.id,
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail="Attendance already marked")

    distance = distance_m(
        body.latitude, body.longitude,
        attendance_session.latitude, attendance_session.longitude,
    )
    if distance > attendance_session.radius_m:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "You are outside the allowed attendance location",
                "distance_m": round(distance, 2),
                "allowed_radius_m": attendance_session.radius_m,
            },
        )

    record = Attendance(
        session_id=attendance_session.id,
        student_id=student.id,
        marked_at=now,
        student_latitude=body.latitude,
        student_longitude=body.longitude,
        distance_m=distance,
    )
    db.add(record)
    try:
        db.commit()
        db.refresh(record)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Attendance already marked")

    return {
        "message": "Attendance marked successfully",
        "attendance_id": record.id,
        "student_id": student.id,
        "session_id": attendance_session.id,
        "marked_at": now.isoformat(),
        "distance_m": round(distance, 2),
    }


@app.get("/sessions/{session_id}/attendance")
def session_attendance(session_id: int, db: Session = Depends(get_db)):
    if db.get(AttendanceSession, session_id) is None:
        raise HTTPException(status_code=404, detail="Attendance session not found")

    rows = db.execute(
        select(Attendance, Student)
        .join(Student, Attendance.student_id == Student.id)
        .where(Attendance.session_id == session_id)
        .order_by(Attendance.marked_at)
    ).all()

    return [
        {
            "attendance_id": attendance.id,
            "student_id": student.id,
            "student_code": student.student_code,
            "full_name": student.full_name,
            "marked_at": as_utc(attendance.marked_at).isoformat(),
            "distance_m": round(attendance.distance_m, 2),
        }
        for attendance, student in rows
    ]


@app.get("/students/{student_id}/attendance")
def student_attendance(student_id: int, db: Session = Depends(get_db)):
    if db.get(Student, student_id) is None:
        raise HTTPException(status_code=404, detail="Student not found")

    rows = db.execute(
        select(Attendance, AttendanceSession, Course)
        .join(AttendanceSession, Attendance.session_id == AttendanceSession.id)
        .join(Course, AttendanceSession.course_id == Course.id)
        .where(Attendance.student_id == student_id)
        .order_by(Attendance.marked_at.desc())
    ).all()

    return [
        {
            "attendance_id": attendance.id,
            "course_id": course.id,
            "course_code": course.code,
            "course_name": course.name,
            "session_id": attendance_session.id,
            "marked_at": as_utc(attendance.marked_at).isoformat(),
            "distance_m": round(attendance.distance_m, 2),
        }
        for attendance, attendance_session, course in rows
    ]
