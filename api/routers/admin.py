import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.deps import Principal, require_admin
from shared.config import settings
from shared.database import get_db
from shared.llm import describe_llm
from shared.models import AuditLog, JobRequest, User
from shared.queue import default_queue, priority_queue, redis_conn

router = APIRouter(prefix="/admin", tags=["Administration"])

WORKER_HEARTBEAT_KEY = "idp:worker:heartbeat"


def _worker_status() -> dict:
    try:
        raw = redis_conn.get(WORKER_HEARTBEAT_KEY)
    except Exception as exc:
        return {"online": False, "detail": f"Redis unavailable: {exc}"}
    if not raw:
        return {"online": False, "detail": "No heartbeat from a worker in the last 60 seconds"}
    try:
        info = json.loads(raw)
    except ValueError:
        info = {}
    return {"online": True, **info}


@router.get("/system", summary="Platform health: worker, queues, OCR models and LLM engines")
async def system_status(_: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    doc_llm, phi_llm, extract_llm = await asyncio.gather(describe_llm("doc"), describe_llm("phi"),
                                                         describe_llm("extract"))
    try:
        queues = {"priority": priority_queue.count, "default": default_queue.count}
        redis_ok = True
    except Exception:
        queues = {"priority": None, "default": None}
        redis_ok = False

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    by_status = dict(db.query(JobRequest.status, func.count()).filter(JobRequest.created_at >= since)
                     .group_by(JobRequest.status).all())
    avg_rows = (db.query(JobRequest.job_type, func.avg(JobRequest.duration_ms), func.count())
                .filter(JobRequest.status == "COMPLETED", JobRequest.created_at >= since)
                .group_by(JobRequest.job_type).all())
    return {
        "api": {"ok": True},
        "database": {"ok": True},
        "redis": {"ok": redis_ok},
        "worker": _worker_status(),
        "queues": queues,
        "ocr": {
            "detection_model": settings.ocr_det_model,
            "recognition_model": settings.ocr_rec_model,
            "high_performance_inference": settings.paddle_enable_hpi,
            "render_dpi": settings.phi_render_dpi,
            "render_workers": settings.render_workers,
        },
        "llm": {"phi": phi_llm, "doc": doc_llm, "extract": extract_llm},
        "extraction": {
            "context_threshold": settings.extract_context_threshold,
            "context_cap_tokens": settings.extract_context_cap_tokens,
            "embedding": f"{settings.extract_embed_provider}:{settings.extract_embed_model}",
            "reasoning_effort": settings.extract_reasoning_effort,
        },
        "limits": {"max_upload_mb": settings.max_upload_mb, "file_retention_days": settings.file_retention_days,
                   "storage_mode": settings.storage_mode},
        "jobs_last_24h": {
            "by_status": by_status,
            "average_duration_ms": {t: int(avg or 0) for t, avg, _ in avg_rows},
            "completed": {t: n for t, _, n in avg_rows},
        },
        "users": {"total": db.query(func.count(User.id)).scalar(),
                  "active": db.query(func.count(User.id)).filter(User.is_active.is_(True)).scalar()},
    }


@router.get("/audit-logs", summary="Audit trail of sign-ins, account changes and job activity")
def audit_logs(action: Optional[str] = Query(None, description="Filter by action prefix, e.g. 'user.' or 'job.'"),
               page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
               _: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action.like(f"{action}%"))
    total = query.count()
    rows = query.order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "items": [{"id": r.id, "created_at": r.created_at, "actor_email": r.actor_email, "action": r.action,
                   "target_type": r.target_type, "target_id": r.target_id, "detail": r.detail,
                   "ip_address": r.ip_address} for r in rows],
        "total": total, "page": page, "page_size": page_size,
    }
