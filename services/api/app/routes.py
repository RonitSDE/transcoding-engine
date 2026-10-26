from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.db import Job, LoginFailure, Outbox, Session, User, get_db, session_expiry, utcnow
from app.schemas import JobOut, LoginIn, SessionOut, SignupIn, UserOut, VideoCreated, VideoIn
from app.security import hash_password, new_id, upload_url, valid_upload, verify_password
from app.settings import ALLOWED_TYPES, MAX_BYTES, cookie_secure, storage_dir

router = APIRouter(prefix="/api")
COOKIE = "session"
WINDOW = timedelta(minutes=10)
MAX_FAILURES = 5


def normalize_email(email: str) -> str:
    value = email.strip().lower()
    if "@" not in value or value.startswith("@") or value.endswith("@"):
        raise HTTPException(422, "invalid email")
    domain = value.split("@", 1)[1]
    if "." not in domain:
        raise HTTPException(422, "invalid email")
    return value


def aware(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def current_session(
    request: Request,
    db: DbSession = Depends(get_db),
) -> Session:
    sid = request.cookies.get(COOKIE)
    if not sid:
        raise HTTPException(401, "login required")
    row = db.get(Session, sid)
    if row is None or row.revoked_at is not None or aware(row.expires_at) <= utcnow():
        raise HTTPException(401, "login required")
    return row


def require_csrf(session: Session, x_csrf_token: Optional[str]) -> None:
    if not x_csrf_token or x_csrf_token != session.csrf:
        raise HTTPException(403, "csrf token missing")


def set_session_cookie(response: Response, sid: str) -> None:
    response.set_cookie(
        COOKIE,
        sid,
        httponly=True,
        secure=cookie_secure(),
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/",
    )


def open_session(db: DbSession, user: User) -> Session:
    row = Session(id=new_id(), user_id=user.id, csrf=new_id(), expires_at=session_expiry())
    db.add(row)
    db.commit()
    return row


def job_for_user(db: DbSession, user_id: int, job_id: int) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    if job is None:
        raise HTTPException(404, "job not found")
    return job


def too_many_failures(db: DbSession, email: str) -> bool:
    cutoff = utcnow() - WINDOW
    rows = db.scalars(select(LoginFailure).where(LoginFailure.email == email)).all()
    return sum(1 for row in rows if aware(row.created_at) >= cutoff) >= MAX_FAILURES


@router.get("/health")
def health(db: DbSession = Depends(get_db)):
    db.scalar(select(User.id).limit(1))
    return {"status": "ok"}


@router.post("/signup", status_code=201, response_model=UserOut)
def signup(body: SignupIn, db: DbSession = Depends(get_db)):
    email = normalize_email(body.email)
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "email already registered")
    user = User(email=email, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=SessionOut)
def login(body: LoginIn, response: Response, db: DbSession = Depends(get_db)):
    email = normalize_email(body.email)
    if too_many_failures(db, email):
        raise HTTPException(429, "too many login attempts")
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(body.password, user.password_hash):
        db.add(LoginFailure(email=email))
        db.commit()
        raise HTTPException(401, "wrong email or password")
    for row in db.scalars(select(LoginFailure).where(LoginFailure.email == email)):
        db.delete(row)
    session = open_session(db, user)
    set_session_cookie(response, session.id)
    return SessionOut(user=UserOut.model_validate(user), csrf=session.csrf)


@router.get("/session", response_model=SessionOut)
def get_session(session: Session = Depends(current_session), db: DbSession = Depends(get_db)):
    user = db.get(User, session.user_id)
    if user is None:
        raise HTTPException(401, "login required")
    return SessionOut(user=UserOut.model_validate(user), csrf=session.csrf)


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    x_csrf_token: Optional[str] = Header(default=None),
    session: Session = Depends(current_session),
    db: DbSession = Depends(get_db),
):
    require_csrf(session, x_csrf_token)
    session.revoked_at = utcnow()
    db.commit()
    response.delete_cookie(COOKIE, path="/")


@router.post("/videos", status_code=201, response_model=VideoCreated)
def create_video(
    body: VideoIn,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    x_csrf_token: Optional[str] = Header(default=None),
    session: Session = Depends(current_session),
    db: DbSession = Depends(get_db),
):
    require_csrf(session, x_csrf_token)
    if not idempotency_key:
        raise HTTPException(422, "idempotency key required")
    if body.content_type not in ALLOWED_TYPES or body.size_bytes <= 0 or body.size_bytes > MAX_BYTES:
        raise HTTPException(422, "file type or size is not allowed")
    existing = db.scalar(
        select(Job).where(Job.user_id == session.user_id, Job.idempotency_key == idempotency_key)
    )
    if existing:
        same = (
            existing.filename == body.filename
            and existing.content_type == body.content_type
            and existing.size_bytes == body.size_bytes
        )
        if not same:
            raise HTTPException(409, "idempotency key already used")
        url = upload_url(existing.id, existing.size_bytes, existing.content_type) if existing.status == "awaiting_upload" else ""
        return VideoCreated(job=JobOut.model_validate(existing), upload_url=url)

    job = Job(
        user_id=session.user_id,
        filename=body.filename,
        content_type=body.content_type,
        size_bytes=body.size_bytes,
        idempotency_key=idempotency_key,
        status="awaiting_upload",
        source_path="",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    job.source_path = str(storage_dir() / "uploads" / str(job.id) / "source")
    db.commit()
    return VideoCreated(
        job=JobOut.model_validate(job),
        upload_url=upload_url(job.id, job.size_bytes, job.content_type),
    )


@router.post("/videos/{job_id}/upload-url")
def refresh_upload_url(
    job_id: int,
    x_csrf_token: Optional[str] = Header(default=None),
    session: Session = Depends(current_session),
    db: DbSession = Depends(get_db),
):
    require_csrf(session, x_csrf_token)
    job = job_for_user(db, session.user_id, job_id)
    if job.status != "awaiting_upload":
        raise HTTPException(409, "upload already finished")
    return {"upload_url": upload_url(job.id, job.size_bytes, job.content_type)}


@router.put("/storage/{job_id}", status_code=204)
async def put_object(
    job_id: int,
    request: Request,
    exp: int,
    size: int,
    type: str,
    sig: str,
    db: DbSession = Depends(get_db),
):
    if not valid_upload(job_id, exp, size, type, sig):
        raise HTTPException(403, "upload url expired or invalid")
    job = db.get(Job, job_id)
    if job is None or job.status != "awaiting_upload":
        raise HTTPException(409, "upload already finished")
    if size != job.size_bytes or type != job.content_type:
        raise HTTPException(422, "upload does not match the job")
    dest = Path(job.source_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with dest.open("wb") as handle:
            async for chunk in request.stream():
                written += len(chunk)
                if written > size:
                    raise HTTPException(422, "upload is larger than declared")
                handle.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    if written != size:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, "upload size does not match")


@router.post("/videos/{job_id}/complete", status_code=202, response_model=JobOut)
def complete_video(
    job_id: int,
    x_csrf_token: Optional[str] = Header(default=None),
    session: Session = Depends(current_session),
    db: DbSession = Depends(get_db),
):
    require_csrf(session, x_csrf_token)
    job = job_for_user(db, session.user_id, job_id)
    if job.status != "awaiting_upload":
        return job
    path = Path(job.source_path)
    if not path.is_file() or path.stat().st_size != job.size_bytes or path.stat().st_size == 0:
        raise HTTPException(422, "object missing or empty")
    job.status = "uploaded"
    db.add(Outbox(job_id=job.id, event="video.uploaded"))
    db.commit()
    db.refresh(job)
    return job


@router.get("/videos", response_model=list[JobOut])
def list_videos(session: Session = Depends(current_session), db: DbSession = Depends(get_db)):
    rows = db.scalars(select(Job).where(Job.user_id == session.user_id).order_by(Job.id.desc())).all()
    return rows


@router.get("/videos/{job_id}", response_model=JobOut)
def get_video(job_id: int, session: Session = Depends(current_session), db: DbSession = Depends(get_db)):
    return job_for_user(db, session.user_id, job_id)


@router.get("/videos/{job_id}/media/{path:path}")
def media(job_id: int, path: str, session: Session = Depends(current_session), db: DbSession = Depends(get_db)):
    job = job_for_user(db, session.user_id, job_id)
    if job.status != "ready":
        raise HTTPException(409, "not ready")
    root = (storage_dir() / "outputs" / str(job.id)).resolve()
    target = (root / path).resolve()
    if root not in target.parents and target != root:
        raise HTTPException(404, "not found")
    if not target.is_file():
        raise HTTPException(404, "not found")
    media_type = "application/vnd.apple.mpegurl" if target.suffix == ".m3u8" else "video/mp2t"
    return FileResponse(target, media_type=media_type)
