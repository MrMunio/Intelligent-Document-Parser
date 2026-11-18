"""Job creation, queueing, waiting and serialisation shared by the service routers."""
import asyncio
import os
import time
import uuid
from typing import Optional

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from api.deps import Principal
from api.schemas import JobAccepted, JobOut
from shared.config import settings
from shared.database import SessionLocal
from shared.formats import classify, extension_of, safe_file_name
from shared.models import JobRequest, User
from shared.queue import default_queue, priority_queue

API_PREFIX = "/api/v1"
UPLOAD_DIR = os.path.join(settings.data_dir, "uploads")
TERMINAL = ("COMPLETED", "FAILED")
CHUNK = 1024 * 1024


def job_links(job: JobRequest) -> dict:
    base = f"{API_PREFIX}/jobs/{job.id}"
    links = {"self": base}
    if job.status == "COMPLETED":
        if job.result_file_url:
            links["file"] = f"{base}/file"
        if job.job_type == "PHI":
            links["metadata"] = f"{base}/metadata"
        if job.result_markdown is not None:
            links["markdown"] = f"{base}/markdown"
    return links


def job_out(job: JobRequest, *, owner_email: Optional[str] = None, include_result: bool = True) -> JobOut:
    return JobOut(
        job_id=job.id, job_type=job.job_type, status=job.status, file_name=job.original_file_name,
        input_format=job.input_format, file_size=job.file_size, source=job.source, options=job.options,
        progress=job.progress, stats=job.stats,
        error=_public_error(job.error_details) if job.status == "FAILED" else None,
        created_at=job.created_at, started_at=job.started_at, completed_at=job.completed_at,
        duration_ms=job.duration_ms, owner_email=owner_email,
        result_file_name=job.result_file_name, result_media_type=job.result_media_type,
        result_available=bool(job.status == "COMPLETED"),
        links=job_links(job),
        result_data=job.result_data if include_result else None,
        result_markdown=job.result_markdown if include_result else None,
    )


def _public_error(details: Optional[str]) -> Optional[str]:
    """Clients get the final error line. Full tracebacks stay in the worker log and the database."""
    if not details:
        return None
    lines = [line for line in details.strip().splitlines() if line.strip()]
    return lines[-1][:500] if lines else None


async def save_upload(file: UploadFile, job_id: str, allowed_formats: set, accept_hint: str) -> tuple[str, str, int]:
    """Streams the upload to disk with a size cap. Returns (path, format, size)."""
    name = safe_file_name(file.filename or "")
    ext = extension_of(name)
    head = await file.read(8192)
    if not head:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    fmt = classify(name, head)
    if fmt is None or fmt not in allowed_formats:
        raise HTTPException(status_code=415, detail=f"Unsupported file type '{ext or name}'. Supported: {accept_hint}.")

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, f"{job_id}{ext if ext and len(ext) <= 10 else ''}")
    limit = settings.max_upload_mb * 1024 * 1024
    size = 0
    try:
        with open(path, "wb") as out:
            chunk = head
            while chunk:
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413,
                                        detail=f"File is larger than the {settings.max_upload_mb} MB limit.")
                out.write(chunk)
                chunk = await file.read(CHUNK)
    except HTTPException:
        _silent_remove(path)
        raise
    return path, fmt, size


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def create_job(db: Session, principal: Principal, *, job_id: str, job_type: str, file_name: str, input_path: str,
               input_format: str, file_size: int, options: dict, priority: bool) -> JobRequest:
    job = JobRequest(
        id=job_id, job_type=job_type, status="PENDING", original_file_name=safe_file_name(file_name),
        user_id=principal.user.id, api_key_id=principal.api_key.id if principal.api_key else None,
        source=principal.via, input_format=input_format, input_path=input_path, file_size=file_size,
        options=options, progress={"stage": "queued", "percent": 0},
    )
    db.add(job)
    db.commit()
    queue = priority_queue if priority else default_queue
    try:
        queue.enqueue("worker.tasks.process_job", job_id, job_timeout="2h", job_id=job_id,
                      result_ttl=300, failure_ttl=3600)
    except Exception as exc:
        job.status = "FAILED"
        job.error_details = f"Queue unavailable: {exc}"
        db.commit()
        _silent_remove(input_path)
        raise HTTPException(status_code=503, detail="The processing queue is unavailable. Try again shortly.") from exc
    return job


def new_job_id() -> str:
    return str(uuid.uuid4())


def accepted(job: JobRequest) -> JobAccepted:
    return JobAccepted(job_id=job.id, status=job.status,
                       message="Job queued. Poll the 'self' link until status is COMPLETED or FAILED.",
                       links={"self": f"{API_PREFIX}/jobs/{job.id}"})


async def wait_for_job(job_id: str, timeout: Optional[int] = None) -> JobRequest:
    """Polls with a fresh short-lived session so the request does not pin a pooled connection."""
    deadline = time.monotonic() + (timeout or settings.sync_timeout_seconds)
    delay = 0.15
    while time.monotonic() < deadline:
        job = await asyncio.to_thread(_load_job, job_id)
        if job is not None and job.status in TERMINAL:
            return job
        await asyncio.sleep(delay)
        delay = min(delay * 1.3, 1.0)
    raise HTTPException(status_code=504, detail=f"Processing did not finish in time. The job is still running: "
                                                f"poll {API_PREFIX}/jobs/{job_id}.")


def _load_job(job_id: str) -> Optional[JobRequest]:
    db = SessionLocal()
    try:
        return db.query(JobRequest).filter(JobRequest.id == job_id).first()
    finally:
        db.close()


def raise_if_failed(job: JobRequest) -> None:
    if job.status == "FAILED":
        raise HTTPException(status_code=422, detail={"job_id": job.id, "status": "FAILED",
                                                     "error": _public_error(job.error_details)})


def owner_email_for(db: Session, job: JobRequest) -> Optional[str]:
    if not job.user_id:
        return None
    row = db.query(User.email).filter(User.id == job.user_id).first()
    return row[0] if row else None
