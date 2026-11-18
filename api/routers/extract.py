"""
Data extraction service (document to JSON). Every request carries the requester's spec, which is validated
before the job is accepted: a request with an invalid spec gets 422 with every problem listed by JSON path.
"""
import json
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from api.deps import Principal, audit, require_service
from api.job_service import (UPLOAD_DIR, accepted, create_job, job_out, new_job_id, raise_if_failed, save_upload,
                             wait_for_job)
from api.schemas import ExtractTextRequest, JobAccepted, JobOut, ValidateSpecRequest
from shared.config import settings
from shared.database import get_db
from shared.extract_spec import (APPROACHES, ExtractOptionsError, SpecError, example_spec, normalize_options,
                                 order_result, validate_spec, validation_report)
from shared.formats import EXTRACT_ACCEPT_HINT, FORMAT_DOCX, FORMAT_IMAGE, FORMAT_PDF, FORMAT_TEXT, safe_file_name
from shared.llm import available_extract_models
from shared import model_catalog, runtime_config
from shared.models import SERVICE_EXTRACT

router = APIRouter(prefix="/extract", tags=["Data extraction"])

EXTRACT_FORMATS = {FORMAT_PDF, FORMAT_IMAGE, FORMAT_DOCX, FORMAT_TEXT}
_SPEC_DOC = ("The extraction spec as a JSON object: {\"properties\": {\"field\": {\"type\": \"string\", "
             "\"description\": \"...\"}}}. Every field needs a type and a description. See GET /extract/options "
             "for an example and POST /extract/validate-spec to check a spec.")
_APPROACH_DOC = "'agentic' (indexed retrieval with a tool-using agent, best for long documents) or 'batch' (sequential parts)."
_MODEL_DOC = "Engine as 'provider:model' from GET /extract/options. Omit to use the configured engine."


def _spec_or_422(raw: Any):
    try:
        if isinstance(raw, str):
            raw = raw.strip()
        return validate_spec(raw)
    except SpecError as exc:
        raise HTTPException(status_code=422, detail={"message": "The extraction spec is not valid.",
                                                     "errors": exc.errors, "warnings": exc.warnings}) from exc


def _options(spec, approach, model, instructions, context_window, ocr_mode) -> dict:
    try:
        options = normalize_options(approach=approach, model=model, instructions=instructions,
                                    context_window=context_window, ocr_mode=ocr_mode,
                                    available_models=available_extract_models())
    except ExtractOptionsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Stored as text: JSONB re-sorts object keys, and the output must follow the requester's key order.
    options["spec"] = json.dumps(spec.schema, ensure_ascii=False)
    options["field_count"] = len(spec.fields)
    return options


def _audit(db, request, principal, job, fmt: str, options: dict):
    audit(db, "job.submitted", actor=principal.user, target_type="job", target_id=job.id,
          detail={"job_type": "EXTRACT", "file": job.original_file_name, "format": fmt, "via": principal.via,
                  "approach": options["approach"], "model": options["model"], "fields": options["field_count"]},
          request=request)


async def _submit(request, db, principal, file: UploadFile, spec_text: str, approach, model, instructions,
                  context_window, ocr_mode, priority: bool):
    spec = _spec_or_422(spec_text)  # validated before the upload is stored
    options = _options(spec, approach, model, instructions, context_window, ocr_mode)
    job_id = new_job_id()
    path, fmt, size = await save_upload(file, job_id, EXTRACT_FORMATS, EXTRACT_ACCEPT_HINT)
    job = create_job(db, principal, job_id=job_id, job_type="EXTRACT", file_name=file.filename or "document",
                     input_path=path, input_format=fmt, file_size=size, options=options, priority=priority)
    _audit(db, request, principal, job, fmt, options)
    return job


@router.get("/options", summary="Approaches, available engines, limits and an example spec")
def extract_options(_: Principal = Depends(require_service(SERVICE_EXTRACT))):
    available = available_extract_models()
    # The engine administrators chose in the web app (platform_settings), not the .env value it was seeded from.
    primary = runtime_config.get("extract").get("primary") or {}
    default_key = f"{primary.get('provider')}:{primary.get('model')}"
    models = []
    for key, info in sorted(available.items(), key=lambda kv: (kv[1].provider, kv[1].status != "current", kv[0])):
        row = info.public()
        row["default"] = key == default_key
        row["recommended_for_agentic"] = model_catalog.recommended(info.provider, "agentic") == info.id
        row["provider_name"] = model_catalog.provider_display_name(info.provider)
        models.append(row)
    return {
        "approaches": [{"value": k, "description": v} for k, v in APPROACHES.items()],
        "default_model": default_key,
        "models": models,
        "context_threshold": model_catalog.compaction_threshold(),
        "context_cap_tokens": settings.extract_context_cap_tokens or None,
        "limits": {"min_fields": settings.extract_spec_min_fields, "max_fields": settings.extract_spec_max_fields,
                   "max_depth": settings.extract_spec_max_depth, "max_upload_mb": settings.max_upload_mb,
                   "max_text_chars": settings.max_text_chars},
        "supported_types": ["string", "number", "integer", "boolean", "object", "array", "date"],
        "accepted_files": EXTRACT_ACCEPT_HINT,
        "example_spec": example_spec(),
    }


@router.post("/validate-spec", summary="Check an extraction spec without submitting a document")
def validate(body: ValidateSpecRequest, _: Principal = Depends(require_service(SERVICE_EXTRACT))):
    return validation_report(body.spec.strip() if isinstance(body.spec, str) else body.spec)


@router.post("/jobs", response_model=JobAccepted, status_code=202,
             summary="Extract JSON from a document asynchronously (returns a job to poll)",
             responses={422: {"description": "The spec is invalid; detail.errors lists each problem by JSON path."}})
async def create_extract_job(
    request: Request,
    file: UploadFile = File(..., description=f"Document. {EXTRACT_ACCEPT_HINT}."),
    spec: str = Form(..., description=_SPEC_DOC),
    approach: str = Form("agentic", description=_APPROACH_DOC),
    model: Optional[str] = Form(None, description=_MODEL_DOC),
    instructions: Optional[str] = Form(None, description="Optional guidance for the extractor (max 4000 chars)."),
    context_window: Optional[int] = Form(None, description="Optional cap on the usable context window in tokens (min 16000)."),
    ocr_mode: str = Form("auto", description="PDF only: auto, always or never."),
    principal: Principal = Depends(require_service(SERVICE_EXTRACT)),
    db: Session = Depends(get_db),
):
    job = await _submit(request, db, principal, file, spec, approach, model, instructions, context_window, ocr_mode,
                        priority=False)
    return accepted(job)


@router.post("/sync", response_model=JobOut, summary="Extract JSON from a document synchronously (waits for the result)",
             responses={422: {"description": "The spec is invalid; detail.errors lists each problem by JSON path."}})
async def extract_sync(
    request: Request,
    file: UploadFile = File(..., description=f"Document. {EXTRACT_ACCEPT_HINT}."),
    spec: str = Form(..., description=_SPEC_DOC),
    approach: str = Form("agentic", description=_APPROACH_DOC),
    model: Optional[str] = Form(None, description=_MODEL_DOC),
    instructions: Optional[str] = Form(None),
    context_window: Optional[int] = Form(None),
    ocr_mode: str = Form("auto"),
    principal: Principal = Depends(require_service(SERVICE_EXTRACT)),
    db: Session = Depends(get_db),
):
    job = await _submit(request, db, principal, file, spec, approach, model, instructions, context_window, ocr_mode,
                        priority=True)
    done = await wait_for_job(job.id)
    raise_if_failed(done)
    return job_out(done)


@router.post("/text", summary="Extract JSON from a text string (JSON in, JSON out)",
             responses={422: {"description": "The spec is invalid; detail.errors lists each problem by JSON path."}})
async def extract_text(body: ExtractTextRequest, request: Request,
                       principal: Principal = Depends(require_service(SERVICE_EXTRACT)), db: Session = Depends(get_db)):
    if len(body.text) > settings.max_text_chars:
        raise HTTPException(status_code=413, detail=f"text is longer than {settings.max_text_chars} characters.")
    spec = _spec_or_422(body.spec)
    options = _options(spec, body.approach, body.model, body.instructions, body.context_window, "auto")
    options["inline_text"] = True
    job_id = new_job_id()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, f"{job_id}.txt")
    data = body.text.encode("utf-8")
    with open(path, "wb") as out:
        out.write(data)
    name = safe_file_name(body.file_name or "inline-text.txt")
    job = create_job(db, principal, job_id=job_id, job_type="EXTRACT", file_name=name, input_path=path,
                     input_format=FORMAT_TEXT, file_size=len(data), options=options, priority=True)
    _audit(db, request, principal, job, "inline_text", options)
    done = await wait_for_job(job.id)
    raise_if_failed(done)
    result = order_result(done.result_data or {}, options["spec"])
    return {"job_id": done.id, "data": result.get("data"), "fields": result.get("fields", []),
            "summary": result.get("summary", {}), "engine": result.get("engine", {}),
            "usage": (result.get("usage") or {}).get("total"), "warnings": result.get("warnings", []),
            "duration_ms": done.duration_ms}
