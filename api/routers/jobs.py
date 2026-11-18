import json
import os
import shutil
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from api.deps import Principal, audit, get_principal
from api.job_service import job_out, owner_email_for
from api.schemas import JobList, JobOut
from shared.config import settings
from shared.database import get_db
from shared.models import JOB_SERVICE, JobRequest, User

router = APIRouter(prefix="/jobs", tags=["Jobs"])

OUTPUT_DIR = os.path.realpath(os.path.join(settings.data_dir, "output"))


def _content_disposition(file_name: str, inline: bool) -> str:
    kind = "inline" if inline else "attachment"
    ascii_name = file_name.encode("ascii", "ignore").decode("ascii").replace('"', "") or "download"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(file_name)}"


def get_job_for(principal: Principal, job_id: str, db: Session) -> JobRequest:
    """A job is visible to its owner and to administrators, and only with access to its service."""
    job = db.query(JobRequest).filter(JobRequest.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.user_id != principal.user.id and not principal.is_admin:
        raise HTTPException(status_code=404, detail="Job not found.")
    if JOB_SERVICE.get(job.job_type) not in principal.services:
        raise HTTPException(status_code=403, detail="Your account does not have access to this service.")
    return job


@router.get("", response_model=JobList, summary="List jobs (your own, or everyone's for administrators)")
def list_jobs(
    job_type: Optional[str] = Query(None, description="PHI, PARSE or OCR"),
    status: Optional[str] = Query(None, description="PENDING, PROCESSING, COMPLETED or FAILED"),
    scope: str = Query("mine", description="'mine' or 'all' (administrators only)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    allowed_types = [t for t, svc in JOB_SERVICE.items() if svc in principal.services]
    query = db.query(JobRequest, User.email).outerjoin(User, User.id == JobRequest.user_id)
    query = query.filter(JobRequest.job_type.in_(allowed_types))
    if not (scope == "all" and principal.is_admin):
        query = query.filter(JobRequest.user_id == principal.user.id)
    if job_type:
        query = query.filter(JobRequest.job_type == job_type.upper())
    if status:
        query = query.filter(JobRequest.status == status.upper())
    total = query.count()
    rows = query.order_by(JobRequest.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return JobList(items=[job_out(job, owner_email=email, include_result=False) for job, email in rows],
                   total=total, page=page, page_size=page_size)


@router.get("/{job_id}", response_model=JobOut, summary="Job status, progress and result")
def get_job(job_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    job = get_job_for(principal, job_id, db)
    return job_out(job, owner_email=owner_email_for(db, job) if principal.is_admin else None)


@router.get("/{job_id}/file", summary="Download the output file (redacted document)")
def download_file(job_id: str, request: Request, inline: bool = Query(False),
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    job = get_job_for(principal, job_id, db)
    if job.status != "COMPLETED" or not job.result_file_url:
        raise HTTPException(status_code=409, detail="This job has no output file (not completed, or not a file job).")
    file_name = job.result_file_name or "output"
    media_type = job.result_media_type or "application/octet-stream"
    headers = {"Content-Disposition": _content_disposition(file_name, inline), "X-Job-Id": job.id,
               "Cache-Control": "no-store"}
    audit(db, "job.file_downloaded", actor=principal.user, target_type="job", target_id=job.id, request=request)

    if job.result_file_url.startswith("s3://"):
        import boto3
        bucket, _, key = job.result_file_url[5:].partition("/")
        s3 = boto3.client("s3", aws_access_key_id=settings.s3_access_key,
                          aws_secret_access_key=settings.s3_secret_key)
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
        except Exception as exc:
            raise HTTPException(status_code=410, detail="The output file is no longer available.") from exc
        return StreamingResponse(obj["Body"].iter_chunks(64 * 1024), media_type=media_type, headers=headers)

    path = os.path.realpath(job.result_file_url)
    if not path.startswith(OUTPUT_DIR + os.sep) or not os.path.isfile(path):
        raise HTTPException(status_code=410, detail="The output file is no longer available (retention expired).")
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/{job_id}/metadata", summary="Download the redaction metadata JSON (all detected entities)")
def download_metadata(job_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    job = get_job_for(principal, job_id, db)
    if job.status != "COMPLETED" or job.result_data is None:
        raise HTTPException(status_code=409, detail="Metadata is available once the job has completed.")
    stem = os.path.splitext(job.original_file_name or "document")[0]
    body = json.dumps(job.result_data, indent=2, ensure_ascii=False)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": _content_disposition(f"{stem}.redaction-metadata.json", False),
                             "Cache-Control": "no-store"})


@router.get("/{job_id}/markdown", summary="Download the parsed markdown")
def download_markdown(job_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    job = get_job_for(principal, job_id, db)
    if job.status != "COMPLETED" or job.result_markdown is None:
        raise HTTPException(status_code=409, detail="Markdown is available once a parsing job has completed.")
    stem = os.path.splitext(job.original_file_name or "document")[0]
    return Response(job.result_markdown, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": _content_disposition(f"{stem}.md", False)})


@router.delete("/{job_id}", status_code=204, summary="Delete a job and its output")
def delete_job(job_id: str, request: Request, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    job = get_job_for(principal, job_id, db)
    if job.status == "PROCESSING":
        raise HTTPException(status_code=409, detail="The job is processing. Delete it once it has finished.")
    if job.result_file_url and not job.result_file_url.startswith("s3://"):
        folder = os.path.realpath(os.path.dirname(job.result_file_url))
        if folder.startswith(OUTPUT_DIR + os.sep):
            shutil.rmtree(folder, ignore_errors=True)
    if job.input_path and os.path.isfile(job.input_path):
        try:
            os.remove(job.input_path)
        except OSError:
            pass
    audit(db, "job.deleted", actor=principal.user, target_type="job", target_id=job.id,
          detail={"job_type": job.job_type, "file": job.original_file_name}, request=request, commit=False)
    db.delete(job)
    db.commit()
