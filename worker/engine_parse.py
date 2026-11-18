import asyncio
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# MUST be set before importing paddleocr/paddlex so that PaddleX skips its geo-based model-source check.
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

from PIL import Image

from shared.config import settings
from shared.llm import get_llm_client
from shared.usage import report_from_calls
from shared.logger import setup_logger
from worker import pdf_util

log = setup_logger("worker.engine_parse")

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
with open(os.path.join(PROMPTS_DIR, "image_prompt.txt"), "r", encoding="utf-8") as f:
    VLM_IMAGE_PROMPT = f.read().strip()
with open(os.path.join(PROMPTS_DIR, "chart_prompt.txt"), "r", encoding="utf-8") as f:
    VLM_CHART_PROMPT = f.read().strip()

HEADING_LABELS = {"title", "doc_title", "section_title", "paragraph_title"}
CAPTION_LABELS = {"figure_title"}
FOOTNOTE_LABELS = {"footnote"}
IMAGE_LABELS = {"image", "figure", "photo", "logo", "diagram"}
CHART_LABELS = {"chart", "graph"}
FORMULA_LABELS = {"formula"}
TABLE_LABELS = {"table"}
MARGINALIA_LABELS = {"number", "header", "footer", "header_image", "footer_image"}
VISION_DPI = 200

_PIPELINE = None
_PARSE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parse")
_DONE = object()


_PIPELINE_LOCK = threading.Lock()


def get_pipeline():
    """Singleton PP-StructureV3 pipeline. Loaded on first use so it only takes VRAM when parsing is used."""
    global _PIPELINE
    if _PIPELINE is None:
        with _PIPELINE_LOCK:  # several job slots may ask for it at the same time
            if _PIPELINE is None:
                _PIPELINE = _build_pipeline()
    return _PIPELINE


def _build_pipeline():
    from paddleocr import PPStructureV3
    last_error = None
    for hpi in ([settings.paddle_enable_hpi, False] if settings.paddle_enable_hpi else [False]):
        try:
            log.info("Initialising PP-StructureV3 (ocr det=%s rec=%s hpi=%s)", settings.ocr_det_model,
                     settings.ocr_rec_model, hpi)
            pipeline = PPStructureV3(
                text_detection_model_name=settings.ocr_det_model,
                text_recognition_model_name=settings.ocr_rec_model,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                use_table_recognition=settings.paddle_use_table,
                use_formula_recognition=settings.paddle_use_formula,
                use_chart_recognition=settings.paddle_use_chart,
                use_seal_recognition=False,
                use_region_detection=False,
                enable_hpi=hpi,
            )
            log.info("PP-StructureV3 ready")
            return pipeline
        except Exception as exc:
            last_error = exc
            log.error("PP-StructureV3 initialisation failed (hpi=%s): %s", hpi, exc)
    raise last_error


def render_pdf_page(pdf_path: Path, page_index: int, dpi: int) -> Optional[bytes]:
    try:
        with pdf_util.PDFIUM_LOCK:
            pdf = pdf_util.open_pdf(str(pdf_path))
            try:
                page = pdf[page_index]
                image = pdf_util.render_rgb(page, dpi)
                page.close()
            finally:
                pdf.close()
        buf = BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        log.warning("could not render page %s for vision: %s", page_index + 1, exc)
        return None


def _next_page(iterator):
    # PP-StructureV3 reads PDF pages with PDFium inside next(); hold the lock so it never runs at the
    # same time as another job slot's PDFium work.
    with pdf_util.PDFIUM_LOCK:
        return next(iterator, _DONE)


def crop_png(source, bbox: List[float]) -> Optional[bytes]:
    """`source` is PNG bytes or an image path."""
    try:
        img = Image.open(BytesIO(source)) if isinstance(source, (bytes, bytearray)) else Image.open(source)
        cropped = img.convert("RGB").crop((int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])))
        if cropped.width < 8 or cropped.height < 8:
            return None
        buf = BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _visual_markdown(label: str, content: str, description: str) -> str:
    if description:
        body = f"**[{label} -- AI Summary]**\n\n{description}"
        if content:
            body += f"\n\n*OCR text in region:* {content}"
        return body
    return (f"**[{label}]**\n\n*OCR text in region:* {content}" if content
            else f"**[{label}]** *(no OCR text detected)*")


def block_to_chunk(block: Dict[str, Any], page_index: int) -> Tuple[Dict[str, Any], bool]:
    """Returns (chunk metadata with markdown, needs_vision)."""
    block_id = str(block.get("block_id", str(uuid.uuid4())))
    chunk_id = f"block_p{page_index}_{block_id}"
    label = str(block.get("block_label", "text")).lower()
    content = str(block.get("block_content", "") or "").strip()
    bbox = block.get("block_bbox", [])
    grounding = {"bbox": [float(v) for v in bbox], "page": page_index} if bbox is not None and len(bbox) else {}
    needs_vision = False

    if label in HEADING_LABELS:
        level = {"title": "#", "doc_title": "#", "section_title": "##", "paragraph_title": "###"}.get(label, "###")
        body = f"{level} {content}"
    elif label in CAPTION_LABELS:
        body = f"*Figure: {content}*"
    elif label in FOOTNOTE_LABELS:
        body = f"> *{content}*"
    elif label in MARGINALIA_LABELS:
        body = f"*-- {content} --*" if label == "header" else f"<!-- {label}: {content} -->"
    elif label in IMAGE_LABELS or label in CHART_LABELS:
        body = _visual_markdown(label, content, "")
        needs_vision = bool(grounding)
    elif label in FORMULA_LABELS:
        body = f"$$\n{content}\n$$" if ("\n" in content or len(content) > 60) else f"${content}$"
    elif label in TABLE_LABELS:
        body = f"<!-- table-html -->\n{content}" if content.startswith("<") else content
    else:
        body = content

    meta = {"id": chunk_id, "label": label, "block_id": block_id, "page": page_index,
            "markdown": f"<a id='{chunk_id}'></a>\n\n{body}", "grounding": grounding, "_content": content}
    return meta, needs_vision


async def _describe(meta: Dict[str, Any], image_bytes: bytes, llm, semaphore: asyncio.Semaphore) -> None:
    prompt = VLM_CHART_PROMPT if meta["label"] in CHART_LABELS else VLM_IMAGE_PROMPT
    async with semaphore:
        try:
            result = await llm.vision(prompt, image_bytes)
            description = result.text
        except Exception as exc:
            log.warning("vision description failed for %s: %s", meta["id"], exc)
            description = ""
    if description:
        body = _visual_markdown(meta["label"], meta["_content"], description)
        meta["markdown"] = f"<a id='{meta['id']}'></a>\n\n{body}"
        meta["ai_summary"] = True


async def run_parse(file_path: str, original_name: Optional[str] = None, *, ai_summaries: bool = True,
                    progress: Optional[Callable[[str, int, str], None]] = None) -> Tuple[str, Dict[str, Any]]:
    """
    Layout-aware parsing. Page inference runs on the GPU in a worker thread while figure and chart
    descriptions for pages that are already done are requested from the vision model concurrently.
    """
    progress = progress or (lambda *_: None)
    input_path = Path(file_path)
    display_name = original_name or input_path.name
    is_pdf = input_path.suffix.lower() == ".pdf"
    use_vision = bool(ai_summaries and settings.doc_llm_enable_summary)
    loop = asyncio.get_running_loop()
    t0 = time.perf_counter()

    total_pages = 1
    if is_pdf:
        try:
            total_pages = max(1, pdf_util.page_count(str(input_path)))
        except pdf_util.PdfOpenError as exc:
            raise ValueError(str(exc)) from exc

    progress("loading", 3, "Loading the parsing models")
    pipeline = await loop.run_in_executor(_PARSE_EXECUTOR, get_pipeline)
    iterator = pipeline.predict_iter(input=[str(input_path)])

    metas: List[Dict[str, Any]] = []
    vision_tasks: List[asyncio.Task] = []
    semaphore = asyncio.Semaphore(max(1, settings.doc_llm_concurrency))

    async with get_llm_client("doc") as llm:
        page_index = 0
        while True:
            result = await loop.run_in_executor(_PARSE_EXECUTOR, _next_page, iterator)
            if result is _DONE:
                break
            raw = result.json.get("res", result.json)
            blocks = raw.get("parsing_res_list", [])

            def order(block: Dict) -> int:
                try:
                    return int(block.get("block_id", 0))
                except (ValueError, TypeError):
                    return 0

            page_png: Optional[bytes] = None
            for block in sorted(blocks, key=order):
                if str(block.get("block_label", "")).lower() == "number":
                    continue
                meta, needs_vision = block_to_chunk(block, page_index)
                metas.append(meta)
                if needs_vision and use_vision:
                    if is_pdf and page_png is None:  # render only pages that actually contain a figure
                        page_png = await loop.run_in_executor(None, render_pdf_page, input_path, page_index, VISION_DPI)
                    source = page_png if is_pdf else str(input_path)
                    crop = crop_png(source, meta["grounding"]["bbox"]) if source else None
                    if crop:
                        vision_tasks.append(asyncio.create_task(_describe(meta, crop, llm, semaphore)))
            page_index += 1
            progress("parsing", int(5 + 75 * min(1.0, page_index / total_pages)),
                     f"Parsed page {page_index} of {total_pages}")

        if vision_tasks:
            progress("describing", 85, f"Describing {len(vision_tasks)} figure(s) with the vision model")
            await asyncio.gather(*vision_tasks)
        llm_usage = dict(llm.usage)
        usage = report_from_calls(llm.call_log)

    elapsed = time.perf_counter() - t0
    page_count = max((m["page"] for m in metas), default=0) + 1
    log.info("[run_parse] %s: %s page(s), %s chunk(s), %s figure description(s) in %.2fs", display_name,
             page_count, len(metas), len(vision_tasks), elapsed)

    lines = ["<!-- Generated by the IDP Platform -->",
             f"<!-- Source: {display_name} | Pages: {page_count} | Time: {elapsed:.2f}s -->\n"]
    current_page = -1
    for meta in metas:
        meta.pop("_content", None)
        if meta["page"] != current_page:
            if current_page >= 0:
                lines.append("\n<!-- PAGE BREAK -->\n")
            lines.append(f"<!-- page {meta['page']} -->\n")
            current_page = meta["page"]
        lines.append(meta["markdown"])
        lines.append("")

    out_json = {
        "metadata": {
            "filename": display_name,
            "page_count": page_count,
            "duration_s": round(elapsed, 3),
            "generator": f"PP-StructureV3 ({settings.ocr_det_model}, {settings.ocr_rec_model})",
            "figures_described": sum(1 for m in metas if m.get("ai_summary")),
            "llm": llm_usage,
            "usage": usage,
        },
        "chunks": metas,
    }
    return "\n".join(lines), out_json
