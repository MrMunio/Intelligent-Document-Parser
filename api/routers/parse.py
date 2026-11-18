from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from api.deps import Principal, audit, require_service
from api.job_service import accepted, create_job, job_out, new_job_id, raise_if_failed, save_upload, wait_for_job
from api.schemas import JobAccepted, JobOut
from shared.database import get_db
from shared.formats import FORMAT_IMAGE, FORMAT_PDF, PARSE_ACCEPT_HINT
from shared.models import SERVICE_PARSE

router = APIRouter(prefix="/parse", tags=["Document parsing"])

PARSE_FORMATS = {FORMAT_PDF, FORMAT_IMAGE}
MODES = {"structured": "PARSE", "ocr": "OCR"}
_MODE_DOC = ("'structured' returns layout-aware markdown plus JSON chunks (headings, tables, figures). "
             "'ocr' returns plain text with line and word bounding boxes.")


async def _submit(request: Request, db: Session, principal: Principal, file: UploadFile, mode: str,
                  ai_summaries: bool, priority: bool):
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="mode must be 'structured' or 'ocr'.")
    job_id = new_job_id()
    path, fmt, size = await save_upload(file, job_id, PARSE_FORMATS, PARSE_ACCEPT_HINT)
    options = {"mode": mode, "ai_summaries": bool(ai_summaries)}
    job = create_job(db, principal, job_id=job_id, job_type=MODES[mode], file_name=file.filename or "document",
                     input_path=path, input_format=fmt, file_size=size, options=options, priority=priority)
    audit(db, "job.submitted", actor=principal.user, target_type="job", target_id=job.id,
          detail={"job_type": MODES[mode], "file": job.original_file_name, "format": fmt, "via": principal.via},
          request=request)
    return job


@router.post("/jobs", response_model=JobAccepted, status_code=202,
             summary="Parse a document asynchronously (returns a job to poll)")
async def create_parse_job(
    request: Request,
    file: UploadFile = File(..., description=f"Document to parse. {PARSE_ACCEPT_HINT}."),
    mode: str = Form("structured", description=_MODE_DOC),
    ai_summaries: bool = Form(True, description="Describe figures and charts with the vision model (structured mode)."),
    principal: Principal = Depends(require_service(SERVICE_PARSE)),
    db: Session = Depends(get_db),
):
    job = await _submit(request, db, principal, file, mode, ai_summaries, priority=False)
    return accepted(job)


@router.post("/sync", response_model=JobOut, summary="Parse a document synchronously (waits for the result)")
async def parse_sync(
    request: Request,
    file: UploadFile = File(..., description=f"Document to parse. {PARSE_ACCEPT_HINT}."),
    mode: str = Form("structured", description=_MODE_DOC),
    ai_summaries: bool = Form(True),
    principal: Principal = Depends(require_service(SERVICE_PARSE)),
    db: Session = Depends(get_db),
):
    job = await _submit(request, db, principal, file, mode, ai_summaries, priority=True)
    done = await wait_for_job(job.id)
    raise_if_failed(done)
    return job_out(done)
