"""RQ task: runs one job (PHI redaction, structured parsing or plain OCR) and records the outcome."""
import asyncio
import os
import shutil
import time
import traceback
from datetime import datetime, timezone

from shared.database import SessionLocal
from shared.logger import setup_logger
from shared.models import JobRequest
from shared.usage import no_usage
from worker.storage import cleanup_old_files, save_file

log = setup_logger("worker.tasks")

PROGRESS_MIN_INTERVAL = 0.4  # seconds between progress writes for the same stage
_last_cleanup = 0.0


class ProgressWriter:
    """Persists progress so the UI can show it and it survives a page refresh."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._last_write = 0.0
        self._last_stage = None

    def __call__(self, stage: str, percent: int, detail: str = "") -> None:
        now = time.monotonic()
        if stage == self._last_stage and now - self._last_write < PROGRESS_MIN_INTERVAL:
            return
        self._last_write, self._last_stage = now, stage
        db = SessionLocal()
        try:
            db.query(JobRequest).filter(JobRequest.id == self.job_id).update(
                {"progress": {"stage": stage, "percent": max(0, min(100, int(percent))), "detail": detail}})
            db.commit()
        except Exception as exc:  # progress is best effort and must never fail the job
            log.debug("progress write failed: %s", exc)
            db.rollback()
        finally:
            db.close()


def _run_phi(job: JobRequest, progress) -> dict:
    from worker.phi.engine import run_phi
    outcome = asyncio.run(run_phi(job.id, job.input_path, job.original_file_name or "document",
                                  job.input_format, job.options or {}, progress))
    try:
        url = save_file(outcome["output_path"], job.id, outcome["output_name"])
    finally:
        shutil.rmtree(outcome["work_dir"], ignore_errors=True)
    meta = outcome["metadata"]
    summary = meta["summary"]
    return {
        "result_data": meta,
        "result_file_url": url,
        "result_file_name": outcome["output_name"],
        "result_media_type": outcome["media_type"],
        "stats": {
            "entities": summary["total_occurrences"], "unique_entities": summary["unique_entities"],
            "by_type": summary["by_type"], "pages": summary.get("pages"),
            "pages_ocr": summary.get("pages_ocr"), "pages_text_layer": summary.get("pages_text_layer"),
            "verification": meta["verification"]["status"], "timings_ms": meta["timings_ms"],
            "llm": meta["engine"]["llm"],
            "usage": meta.get("usage"),
        },
    }


def _run_parse(job: JobRequest, progress) -> dict:
    from worker.engine_parse import run_parse
    options = job.options or {}
    markdown, data = asyncio.run(run_parse(job.input_path, job.original_file_name or "document",
                                           ai_summaries=options.get("ai_summaries", True), progress=progress))
    meta = data.get("metadata", {})
    return {"result_markdown": markdown, "result_data": data,
            "stats": {"pages": meta.get("page_count"), "chunks": len(data.get("chunks", [])),
                      "timings_ms": {"total_ms": int(meta.get("duration_s", 0) * 1000)},
                      "llm": meta.get("llm"), "usage": meta.get("usage")}}


def _run_ocr(job: JobRequest, progress) -> dict:
    from worker.engine_ocr import run_ocr
    progress("ocr", 20, "Recognising text")
    markdown, data = run_ocr(job.input_path)
    lines = sum(len(p.get("lines", [])) for p in data.get("pages", []))
    return {"result_markdown": markdown, "result_data": data,
            "stats": {"pages": data.get("page_count"), "lines": lines, "usage": no_usage()}}


def _run_extract(job: JobRequest, progress) -> dict:
    import tempfile
    from worker.extract.engine import run_extract
    outcome = asyncio.run(run_extract(job.id, job.input_path, job.original_file_name or "document",
                                      job.input_format, job.options or {}, progress))
    result = outcome["result"]
    with tempfile.TemporaryDirectory(prefix="extract-") as tmp:
        path = os.path.join(tmp, outcome["output_name"])
        with open(path, "wb") as f:
            f.write(outcome["output_bytes"])
        url = save_file(path, job.id, outcome["output_name"])
    usage = result.get("usage", {}).get("total", {})
    agent = result.get("agent") or {}
    return {
        "result_data": result,
        "result_file_url": url,
        "result_file_name": outcome["output_name"],
        "result_media_type": "application/json",
        "stats": {
            "approach": result["approach"], "fields": result["summary"]["total"],
            "found": result["summary"]["found"], "not_found": result["summary"]["not_found"],
            "coverage": result["summary"]["coverage"], "complete": result["complete"],
            "pages": result["document"]["pages"], "model": f"{result['engine']['provider']}:{result['engine']['model']}",
            "llm_calls": usage.get("calls"), "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"), "cost_usd": usage.get("cost_usd"),
            "sessions": len(agent.get("sessions") or []) or None, "compactions": agent.get("compactions"),
            "timings_ms": result.get("timings_ms"), "usage": result.get("usage"),
        },
    }


RUNNERS = {"PHI": _run_phi, "PARSE": _run_parse, "OCR": _run_ocr, "EXTRACT": _run_extract}


def process_job(job_id: str) -> None:
    global _last_cleanup
    db = SessionLocal()
    started = time.perf_counter()
    input_path = None
    try:
        job = db.query(JobRequest).filter(JobRequest.id == job_id).first()
        if job is None:
            log.error("Job %s not found. It may have been deleted before it started.", job_id)
            return
        input_path = job.input_path
        job.status = "PROCESSING"
        job.started_at = datetime.now(timezone.utc)
        job.progress = {"stage": "starting", "percent": 1, "detail": "Starting"}
        db.commit()
        log.info("Starting %s job %s (%s, %s bytes)", job.job_type, job_id, job.input_format, job.file_size)

        runner = RUNNERS.get(job.job_type)
        if runner is None:
            raise ValueError(f"Unknown job type '{job.job_type}'.")
        if not input_path or not os.path.isfile(input_path):
            raise FileNotFoundError("The uploaded file is no longer available.")

        outcome = runner(job, ProgressWriter(job_id))

        db.expire_all()
        job = db.query(JobRequest).filter(JobRequest.id == job_id).first()
        if job is None:
            log.warning("Job %s was deleted while it was processing; discarding the result.", job_id)
            return
        for key, value in outcome.items():
            setattr(job, key, value)
        job.status = "COMPLETED"
        job.error_details = None
        job.progress = {"stage": "completed", "percent": 100, "detail": "Completed"}
        job.completed_at = datetime.now(timezone.utc)
        job.duration_ms = int((time.perf_counter() - started) * 1000)
        db.commit()
        log.info("Completed %s job %s in %sms", job.job_type, job_id, job.duration_ms)
    except Exception as exc:
        log.error("Job %s failed: %s\n%s", job_id, exc, traceback.format_exc())
        db.rollback()
        try:
            job = db.query(JobRequest).filter(JobRequest.id == job_id).first()
            if job is not None:
                job.status = "FAILED"
                job.error_details = f"{traceback.format_exc()}\n{type(exc).__name__}: {exc}"
                job.progress = {"stage": "failed", "percent": 100, "detail": "Failed"}
                job.completed_at = datetime.now(timezone.utc)
                job.duration_ms = int((time.perf_counter() - started) * 1000)
                db.commit()
        except Exception as inner:
            log.error("Could not record failure for job %s: %s", job_id, inner)
    finally:
        db.close()
        # The uploaded original is never kept after processing.
        if input_path:
            try:
                if os.path.exists(input_path):
                    os.remove(input_path)
            except OSError as exc:
                log.error("Failed to delete uploaded file %s: %s", input_path, exc)
        if time.time() - _last_cleanup > 3600:
            _last_cleanup = time.time()
            cleanup_old_files()
