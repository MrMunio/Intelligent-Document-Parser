import json
import os
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from api.deps import Principal, audit, require_service
from api.job_service import (UPLOAD_DIR, accepted, create_job, job_out, new_job_id, raise_if_failed, save_upload,
                             wait_for_job)
from api.routers.jobs import download_file
from api.schemas import JobAccepted, JobOut, RedactTextRequest
from shared.config import settings
from shared.database import get_db
from shared.formats import FORMAT_DOCX, FORMAT_IMAGE, FORMAT_PDF, FORMAT_TEXT, PHI_ACCEPT_HINT, safe_file_name
from shared.models import SERVICE_PHI
from shared.phi_catalog import (BUILTIN_ENTITIES, MAX_CUSTOM_ENTITIES, OCR_MODES, REDACTION_STYLES, OptionsError,
                                normalize_phi_options)

router = APIRouter(prefix="/phi", tags=["PHI redaction"])

PHI_FORMATS = {FORMAT_PDF, FORMAT_IMAGE, FORMAT_DOCX, FORMAT_TEXT}

_ENTITY_TYPES_DOC = ("JSON array (or comma separated list) of built-in entity labels to detect. "
                     "Omit to detect all built-in types. See GET /api/v1/phi/entity-types.")
_CUSTOM_DOC = ('JSON array of your own PHI / PII definitions for this request, for example '
               '[{"label": "EMPLOYEE_ID", "description": "Internal employee numbers such as EMP-48213"}].')


def _parse_list(value: Optional[str], field: str):
    if value is None or not value.strip():
        return None
    text = value.strip()
    if text.startswith("["):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{field} is not valid JSON.") from exc
    return [part.strip() for part in text.split(",") if part.strip()]


def _options_from_form(entity_types, custom_entities, instructions, redaction_style, ocr_mode,
                       use_pattern_detectors, scrub_metadata) -> dict:
    custom = None
    if custom_entities and custom_entities.strip():
        try:
            custom = json.loads(custom_entities)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="custom_entities is not valid JSON.") from exc
    try:
        return normalize_phi_options({
            "entity_types": _parse_list(entity_types, "entity_types"),
            "custom_entities": custom,
            "instructions": instructions,
            "redaction_style": redaction_style,
            "ocr_mode": ocr_mode,
            "use_pattern_detectors": use_pattern_detectors,
            "scrub_metadata": scrub_metadata,
        })
    except OptionsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/entity-types", summary="Built-in entity types and accepted option values")
def entity_types(_: Principal = Depends(require_service(SERVICE_PHI))):
    return {
        "entity_types": BUILTIN_ENTITIES,
        "redaction_styles": REDACTION_STYLES,
        "ocr_modes": OCR_MODES,
        "max_custom_entities": MAX_CUSTOM_ENTITIES,
        "max_upload_mb": settings.max_upload_mb,
        "accepted_files": PHI_ACCEPT_HINT,
    }


async def _submit(request, db, principal, file, options, priority):
    job_id = new_job_id()
    path, fmt, size = await save_upload(file, job_id, PHI_FORMATS, PHI_ACCEPT_HINT)
    job = create_job(db, principal, job_id=job_id, job_type="PHI", file_name=file.filename or "document",
                     input_path=path, input_format=fmt, file_size=size, options=options, priority=priority)
    audit(db, "job.submitted", actor=principal.user, target_type="job", target_id=job.id,
          detail={"job_type": "PHI", "file": job.original_file_name, "format": fmt, "via": principal.via,
                  "custom_entities": [c["label"] for c in options["custom_entities"]]}, request=request)
    return job


@router.post("/jobs", response_model=JobAccepted, status_code=202,
             summary="Redact a document asynchronously (returns a job to poll)")
async def create_redaction_job(
    request: Request,
    file: UploadFile = File(..., description=f"Document to redact. {PHI_ACCEPT_HINT}."),
    entity_types: Optional[str] = Form(None, description=_ENTITY_TYPES_DOC),
    custom_entities: Optional[str] = Form(None, description=_CUSTOM_DOC),
    instructions: Optional[str] = Form(None, description="Optional extra guidance for the detector (max 2000 chars)."),
    redaction_style: str = Form("label", description="'label' writes [TYPE_n] placeholders, 'blackout' masks without a label."),
    ocr_mode: str = Form("auto", description="PDF only. 'auto' uses the text layer when present and OCR for scanned pages."),
    use_pattern_detectors: bool = Form(True, description="Also run deterministic patterns (email, phone, SSN, dates, URLs, IPs)."),
    scrub_metadata: bool = Form(True, description="Remove document metadata such as author and title."),
    principal: Principal = Depends(require_service(SERVICE_PHI)),
    db: Session = Depends(get_db),
):
    options = _options_from_form(entity_types, custom_entities, instructions, redaction_style, ocr_mode,
                                 use_pattern_detectors, scrub_metadata)
    job = await _submit(request, db, principal, file, options, priority=False)
    return accepted(job)


@router.post("/redact", summary="Redact a document synchronously (waits for the result)",
             response_model=JobOut,
             responses={200: {"description": "Job result as JSON, or the redacted file when response_format=file."}})
async def redact_sync(
    request: Request,
    file: UploadFile = File(..., description=f"Document to redact. {PHI_ACCEPT_HINT}."),
    entity_types: Optional[str] = Form(None, description=_ENTITY_TYPES_DOC),
    custom_entities: Optional[str] = Form(None, description=_CUSTOM_DOC),
    instructions: Optional[str] = Form(None),
    redaction_style: str = Form("label"),
    ocr_mode: str = Form("auto"),
    use_pattern_detectors: bool = Form(True),
    scrub_metadata: bool = Form(True),
    response_format: str = Form("json", description="'json' returns the job with metadata. 'file' returns the redacted "
                                                    "file itself, with the job id in the X-Job-Id header."),
    principal: Principal = Depends(require_service(SERVICE_PHI)),
    db: Session = Depends(get_db),
):
    if response_format not in ("json", "file"):
        raise HTTPException(status_code=400, detail="response_format must be 'json' or 'file'.")
    options = _options_from_form(entity_types, custom_entities, instructions, redaction_style, ocr_mode,
                                 use_pattern_detectors, scrub_metadata)
    job = await _submit(request, db, principal, file, options, priority=True)
    done = await wait_for_job(job.id)
    raise_if_failed(done)
    if response_format == "file":
        db.expire_all()  # the session still holds the job as it was when it was queued
        return download_file(done.id, request, False, principal, db)
    return job_out(done)


@router.post("/redact-text", summary="Redact a plain text string (JSON in, JSON out)")
async def redact_text(body: RedactTextRequest, request: Request,
                      principal: Principal = Depends(require_service(SERVICE_PHI)), db: Session = Depends(get_db)):
    if len(body.text) > settings.max_text_chars:
        raise HTTPException(status_code=413, detail=f"text is longer than {settings.max_text_chars} characters.")
    try:
        options = normalize_phi_options({
            "entity_types": body.entity_types,
            "custom_entities": [c.model_dump() for c in body.custom_entities],
            "instructions": body.instructions,
            "redaction_style": body.redaction_style,
            "use_pattern_detectors": body.use_pattern_detectors,
        })
    except OptionsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = new_job_id()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, f"{job_id}.txt")
    data = body.text.encode("utf-8")
    with open(path, "wb") as out:
        out.write(data)
    options["inline_text"] = True
    name = safe_file_name(body.file_name or "inline-text.txt")
    job = create_job(db, principal, job_id=job_id, job_type="PHI", file_name=name, input_path=path,
                     input_format=FORMAT_TEXT, file_size=len(data), options=options, priority=True)
    audit(db, "job.submitted", actor=principal.user, target_type="job", target_id=job.id,
          detail={"job_type": "PHI", "format": "inline_text", "via": principal.via}, request=request)
    done = await wait_for_job(job.id)
    raise_if_failed(done)
    result = done.result_data or {}
    return {
        "job_id": done.id,
        "redacted_text": result.get("redacted_text", ""),
        "entities": result.get("entities", []),
        "summary": result.get("summary", {}),
        "duration_ms": done.duration_ms,
    }
