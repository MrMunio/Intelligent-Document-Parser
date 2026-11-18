"""PHI redaction entry point: picks the redactor for the input format and builds the metadata document."""
import os
import tempfile
from datetime import datetime, timezone
from typing import Callable

from shared.config import settings
from shared.formats import (FORMAT_DOCX, FORMAT_IMAGE, FORMAT_PDF, FORMAT_TEXT, classify, extension_of,
                            media_type_for, safe_file_name)
from shared import runtime_config
from shared.llm import get_llm_client
from shared.usage import report_from_calls
from shared.logger import setup_logger
from shared.phi_catalog import normalize_phi_options
from worker.phi.detector import Detector
from worker.phi.matching import LabelRegistry

log = setup_logger("worker.phi.engine")


def output_name_for(original_name: str) -> str:
    name = safe_file_name(original_name)
    stem, ext = os.path.splitext(name)
    return f"{stem}.redacted{ext}"


async def run_phi(job_id: str, input_path: str, original_name: str, input_format: str, raw_options: dict,
                  progress: Callable[[str, int, str], None]) -> dict:
    """
    Returns {"metadata": dict, "output_path": str, "output_name": str, "media_type": str}.
    The output file has the same format as the input.
    """
    options = normalize_phi_options(raw_options)
    options["inline_text"] = bool((raw_options or {}).get("inline_text"))
    if not input_format:
        with open(input_path, "rb") as handle:
            input_format = classify(original_name, handle.read(8192)) or FORMAT_TEXT

    output_name = output_name_for(original_name)
    work_dir = tempfile.mkdtemp(prefix=f"phi_{job_id}_")
    output_path = os.path.join(work_dir, output_name)
    registry = LabelRegistry(options["redaction_style"])

    async with get_llm_client("phi") as llm:
        detector = Detector(options, llm)
        if input_format == FORMAT_PDF:
            from worker.phi.pdf import redact_pdf
            result = await redact_pdf(input_path, output_path, options, detector, registry, progress)
        elif input_format == FORMAT_IMAGE:
            from worker.phi.image import redact_image
            result = await redact_image(input_path, output_path, options, detector, registry, progress)
        elif input_format == FORMAT_DOCX:
            from worker.phi.docx import redact_docx
            result = await redact_docx(input_path, output_path, options, detector, registry, progress)
        elif input_format == FORMAT_TEXT:
            from worker.phi.text import redact_text_file
            result = await redact_text_file(input_path, output_path, options, detector, registry, progress)
        else:
            raise ValueError(f"Unsupported input format '{input_format}'.")
        llm_stats = detector.stats()
        usage = report_from_calls(llm.call_log)

    phi_primary = runtime_config.get("phi").get("primary") or {}
    ocr_used = input_format == FORMAT_IMAGE or result["summary"].get("pages_ocr", 0) > 0
    engine = {
        "llm": {
            "configured": f"{phi_primary.get('provider')}:{phi_primary.get('model')}",
            "used": llm_stats.get("engines", {}),
            "calls": llm_stats.get("calls", 0),
            "failovers": llm_stats.get("failovers", 0),
            "input_tokens": llm_stats.get("input_tokens", 0),
            "output_tokens": llm_stats.get("output_tokens", 0),
            "segments": llm_stats.get("segments", 0),
        },
        "ocr": ({"detection_model": settings.ocr_det_model, "recognition_model": settings.ocr_rec_model,
                 "render_dpi": settings.phi_render_dpi} if ocr_used else None),
        "pattern_detectors": options["use_pattern_detectors"],
    }
    engine["llm"]["cost_usd"] = usage["total"]["cost_usd"]
    public_options = {k: v for k, v in options.items() if k != "inline_text"}
    metadata = {
        "schema_version": "2.0",
        "job_id": job_id,
        "source_file": safe_file_name(original_name),
        "input_format": input_format,
        "output_file": output_name,
        "output_media_type": media_type_for(output_name),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "options": public_options,
        "engine": engine,
        "usage": usage,
        **result,
    }
    log.info("[run_phi] job %s: %s occurrence(s), %s unique, verification=%s, %sms", job_id,
             result["summary"]["total_occurrences"], result["summary"]["unique_entities"],
             result["verification"]["status"], result["timings_ms"].get("total_ms"))
    return {"metadata": metadata, "output_path": output_path, "output_name": output_name,
            "media_type": media_type_for(output_name), "work_dir": work_dir,
            "extension": extension_of(output_name)}
