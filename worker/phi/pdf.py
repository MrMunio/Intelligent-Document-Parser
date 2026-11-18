"""
PDF redaction, built only on permissively licensed components:
PDFium via pypdfium2 (read, render), Pillow (paint) and reportlab (write).

The output is a sanitised PDF: every page is re-rendered with the redaction boxes painted into the
pixels, and a new searchable text layer is written that contains only the words that were not redacted.
Nothing from the source file is carried over by accident: hidden text, form values, annotations,
attachments, scripts and metadata are all left behind.

Speed:
  1. Pages with a usable text layer skip OCR completely (the layer already has exact word boxes).
  2. Pages that need OCR are rasterised in separate processes while the GPU recognises the previous one.
  3. LLM detection for a page starts the moment its text is known, so model calls for early pages run
     while later pages are still being recognised.
"""
import asyncio
import io
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as pdf_canvas

from shared.config import settings
from shared.logger import setup_logger
from worker import pdf_util
from worker.phi.detector import DetectionError, Detector, Entity
from worker.phi.matching import LabelRegistry, Word, WordIndex, match_page, squash, summarise
from worker.render_pool import submit_render, to_bgr_array, to_pil

log = setup_logger("worker.phi.pdf")

OCR_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")
# Pillow releases the GIL while encoding, so output pages are compressed in parallel.
ENCODE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="encode")

MIN_TEXT_LAYER_WORDS = 5
IMAGE_COVERAGE_FOR_OCR = 0.15
FONT = "Helvetica"
LABEL_MIN_SIZE = 3.5
LABEL_MAX_SIZE = 10.0


@dataclass
class PageState:
    number: int  # zero based
    width: float  # points
    height: float
    words: list = field(default_factory=list)
    mode: str = "text"  # "text" (text layer) or "ocr"
    reason: str = ""
    text: str = ""
    task: asyncio.Task | None = None
    entities: list = field(default_factory=list)
    matches: list = field(default_factory=list)


def _lines_text(words: list[Word]) -> str:
    lines: list[str] = []
    current = object()
    for word in words:
        if word.line != current:
            lines.append(word.text)
            current = word.line
        else:
            lines[-1] += " " + word.text
    return "\n".join(lines)


def _analyse(input_path: str, ocr_mode: str) -> list[PageState]:
    """Reads page sizes and text layers and decides, per page, whether OCR is needed."""
    pages = []
    with pdf_util.PDFIUM_LOCK:
        pdf = pdf_util.open_pdf(input_path)
        try:
            if len(pdf) == 0:
                raise ValueError("The PDF has no pages.")
            for number in range(len(pdf)):
                page = pdf[number]
                try:
                    width, height = page.get_size()
                    rotation = page.get_rotation()
                    words = [Word(text, box, ("t", line)) for text, box, line in pdf_util.page_words(page)]
                    state = PageState(number, width, height, words=words)
                    if rotation:
                        # Text layer coordinates are unrotated; reading the rendered page keeps them aligned.
                        state.mode, state.reason = "ocr", "rotated page"
                        state.width, state.height = (height, width) if rotation in (90, 270) else (width, height)
                    elif ocr_mode == "always":
                        state.mode, state.reason = "ocr", "OCR requested"
                    elif ocr_mode == "never":
                        state.mode, state.reason = "text", "text layer requested"
                    elif len(words) < MIN_TEXT_LAYER_WORDS:
                        state.mode, state.reason = "ocr", "no usable text layer"
                    elif pdf_util.has_visible_annotations(page):
                        state.mode, state.reason = "ocr", "form fields or annotations"
                    elif pdf_util.image_coverage(page) > IMAGE_COVERAGE_FOR_OCR:
                        state.mode, state.reason = "ocr", "large images"
                    else:
                        state.reason = "text layer"
                    if state.mode == "ocr":
                        state.words = []
                    pages.append(state)
                finally:
                    page.close()
        finally:
            pdf.close()
    return pages


def _ocr_words(lines: list[dict], scale: float) -> list[Word]:
    words = []
    for line_index, line in enumerate(lines):
        for text, (x0, y0, x1, y1) in line["words"]:
            words.append(Word(text, (x0 * scale, y0 * scale, x1 * scale, y1 * scale), ("o", line_index)))
    return words


def _padded(rect: tuple, native: bool, width: float, height: float) -> tuple:
    x0, y0, x1, y1 = rect
    if native:
        # Character boxes are exact; a little room covers anti-aliased glyph edges.
        pad_x, pad_y = 0.8, 0.8
    else:
        # OCR word boxes are estimated from character positions and can cut a glyph in half. Padding
        # scales with the text height so a full character width is always covered (verified visually).
        text_h = y1 - y0
        pad_x, pad_y = max(2.5, text_h * 0.35), max(1.5, text_h * 0.18)
    return (max(0.0, x0 - pad_x), max(0.0, y0 - pad_y), min(width, x1 + pad_x), min(height, y1 + pad_y))


def _safe_text(text: str) -> str:
    """The standard PDF font covers Windows-1252. Other characters become '?' in the invisible layer."""
    return text.encode("cp1252", errors="replace").decode("cp1252")


def _visible_segments(word: Word, covered: set) -> list[tuple[str, tuple]]:
    """Parts of a word that were not redacted, each with a proportional box."""
    token = squash(word.text)
    if not covered:
        return [(token, word.bbox)]
    x0, y0, x1, y1 = word.bbox
    length = max(1, len(token))
    out, start = [], None
    for index in range(length + 1):
        keep = index < length and index not in covered
        if keep and start is None:
            start = index
        elif not keep and start is not None:
            out.append((token[start:index], (x0 + (x1 - x0) * start / length, y0, x0 + (x1 - x0) * index / length, y1)))
            start = None
    return out


def _is_grayscale(image) -> bool:
    """True when a page has no meaningful colour, so it can be stored with one channel instead of three."""
    sample = image.resize((96, 96))
    return all(max(px) - min(px) <= 12 for px in sample.getdata())


def _encode_page(image, from_text_layer: bool) -> io.BytesIO:
    """
    Measured on typical pages: digitally created grayscale pages are about 2.7 times smaller, and sharper,
    as 16-level PNG than as JPEG. Scans carry noise that PNG compresses poorly, so they stay JPEG.
    """
    buffer = io.BytesIO()
    grayscale = _is_grayscale(image)
    if from_text_layer and grayscale:
        image.convert("L").quantize(16, method=Image.Quantize.FASTOCTREE).save(buffer, format="PNG", compress_level=6)
    elif grayscale:
        image.convert("L").save(buffer, format="JPEG", quality=settings.pdf_output_jpeg_quality)
    else:
        image.save(buffer, format="JPEG", quality=settings.pdf_output_jpeg_quality, subsampling=0)
    buffer.seek(0)
    return buffer


def _write_page(c, buffer: io.BytesIO, state: PageState, segments: list, labels: list) -> None:
    buffer.seek(0)
    w, h = state.width, state.height
    c.setPageSize((w, h))
    c.drawImage(ImageReader(buffer), 0, 0, width=w, height=h)

    # Invisible text layer: keeps the unredacted content searchable and selectable.
    text = c.beginText()
    text.setTextRenderMode(3)
    for value, (x0, y0, x1, y1) in segments:
        value = _safe_text(value)
        box_h = max(1.0, y1 - y0)
        size = max(1.0, box_h * 0.9)
        natural = stringWidth(value, FONT, size)
        text.setFont(FONT, size)
        text.setHorizScale(max(10.0, min(400.0, 100.0 * (x1 - x0) / natural)) if natural > 0 else 100.0)
        text.setTextOrigin(x0, h - y1 + box_h * 0.2)
        text.textOut(value)
    # The text render mode is part of the graphics state and outlives the text object, so the invisible
    # layer is drawn in its own state. Otherwise the placeholder labels below would be invisible too.
    c.saveState()
    c.drawText(text)
    c.restoreState()

    # Visible placeholder labels on the black boxes.
    c.setFillColorRGB(1, 1, 1)
    for label, (x0, y0, x1, y1) in labels:
        box_w, box_h = x1 - x0, y1 - y0
        size = min(LABEL_MAX_SIZE, box_h * 0.78)
        while size >= LABEL_MIN_SIZE and stringWidth(label, FONT, size) > box_w - 1.0:
            size -= 0.5
        if size >= LABEL_MIN_SIZE:
            c.setFont(FONT, size)
            c.drawCentredString((x0 + x1) / 2, h - y1 + (box_h - size * 0.72) / 2, label)
    c.showPage()


def _verify(path: str, expected: dict[int, list[Entity]]) -> tuple[list[dict], int]:
    """Re-reads the written file and reports any redacted value still present in its text layer."""
    residual: list[dict] = []
    with pdf_util.PDFIUM_LOCK:
        pdf = pdf_util.open_pdf(path, forms=False)
        try:
            for number, entities in expected.items():
                page = pdf[number]
                words = [Word(t, b, ("t", line)) for t, b, line in pdf_util.page_words(page)]
                page.close()
                index = WordIndex(words)
                for entity in entities:
                    if index.find(entity):
                        residual.append({"page": number + 1, "type": entity.type})
        finally:
            pdf.close()
    return residual, len(expected)


async def redact_pdf(input_path: str, output_path: str, options: dict, detector: Detector,
                     registry: LabelRegistry, progress: Callable[[str, int, str], None]) -> dict:
    timings: dict[str, int] = {}
    t0 = time.perf_counter()
    loop = asyncio.get_running_loop()
    style = options.get("redaction_style", "label")
    workers = settings.render_workers

    # ---- 1. Classify pages --------------------------------------------------------------------
    progress("analyzing", 3, "Reading the document")
    try:
        pages = await loop.run_in_executor(None, _analyse, input_path, options.get("ocr_mode", "auto"))
    except pdf_util.PdfOpenError as exc:
        raise ValueError(str(exc)) from exc
    total_pages = len(pages)
    ocr_pages = [p for p in pages if p.mode == "ocr"]
    timings["analyze_ms"] = int((time.perf_counter() - t0) * 1000)

    ocr_done = 0

    def report():
        ocr_fraction = (ocr_done / len(ocr_pages)) if ocr_pages else 1.0
        llm_fraction = (detector.chunks_done / detector.chunks_total) if detector.chunks_total else 0.0
        if ocr_pages and ocr_done < len(ocr_pages):
            stage, detail = "ocr", f"Recognising text: page {ocr_done} of {len(ocr_pages)}"
        else:
            stage, detail = "detecting", f"Detecting entities: {detector.chunks_done} of {detector.chunks_total} segments"
        progress(stage, int(5 + 30 * ocr_fraction + 45 * llm_fraction), detail)

    detector.on_chunk = lambda done, total: report()

    # ---- 2. Text layer pages: detection starts immediately --------------------------------------
    t_detect = time.perf_counter()
    for state in pages:
        if state.mode == "text":
            state.text = _lines_text(state.words)
            state.task = asyncio.create_task(detector.detect(state.text))

    # ---- 3. OCR pages: render in parallel processes -> GPU OCR -> detection -----------------------
    t_ocr = time.perf_counter()
    if ocr_pages:
        from worker.ocr_instance import ocr_images
        dpi = settings.phi_render_dpi
        scale = 72.0 / dpi
        batch_size = max(1, settings.ocr_batch_pages)
        pending: deque = deque()
        cursor = 0
        while cursor < len(ocr_pages) or pending:
            while cursor < len(ocr_pages) and len(pending) < max(2, batch_size * 2):
                state = ocr_pages[cursor]
                pending.append((state, submit_render(input_path, state.number, dpi, workers)))
                cursor += 1
            group = [pending.popleft() for _ in range(min(batch_size, len(pending)))]
            images = []
            for _, future in group:
                _, width, height, samples = await asyncio.wrap_future(future)
                images.append(to_bgr_array(width, height, samples))
            results = await loop.run_in_executor(OCR_EXECUTOR, ocr_images, images)
            for (state, _), lines in zip(group, results):
                state.words = _ocr_words(lines, scale)
                state.text = "\n".join(line["text"] for line in lines)
                state.task = asyncio.create_task(detector.detect(state.text))
                ocr_done += 1
            del images
            report()
    timings["ocr_ms"] = int((time.perf_counter() - t_ocr) * 1000) if ocr_pages else 0

    # ---- 4. Wait for detection -------------------------------------------------------------------
    for state in pages:
        state.entities = await state.task if state.task else []
    detector.raise_if_incomplete()
    timings["detect_ms"] = int((time.perf_counter() - t_detect) * 1000)

    # ---- 5. Locate every occurrence --------------------------------------------------------------
    document_entities: dict[tuple, Entity] = {}
    for state in pages:
        for entity in state.entities:
            document_entities.setdefault((entity.text, entity.type), entity)
    all_entities = list(document_entities.values())

    occurrences: list[dict] = []
    unmatched: list[dict] = []
    expected_gone: dict[int, list[Entity]] = {}
    for state in pages:
        state.matches, missing = match_page(state.words, state.entities, all_entities, allow_fuzzy=state.mode == "ocr")
        for entity in missing:
            unmatched.append({"page": state.number + 1, "type": entity.type, "text": entity.text, "source": entity.source})
        for match in state.matches:
            expected_gone.setdefault(state.number, []).append(match.entity)

    # ---- 6. Write the sanitised PDF: render, paint, rebuild the text layer --------------------------
    progress("redacting", 85, "Writing the redacted document")
    t_apply = time.perf_counter()
    out_dpi = settings.pdf_output_dpi
    px = out_dpi / 72.0
    c = pdf_canvas.Canvas(output_path, pageCompression=1)
    c.setAuthor("")
    c.setTitle("")
    c.setSubject("")
    c.setKeywords("")
    c.setCreator("IDP Platform")
    c.setProducer("IDP Platform")
    renders: deque = deque()
    cursor = 0
    prepared: list = []  # (state, encode future, segments, labels), written to the PDF in page order
    for state in pages:
        while cursor < total_pages and len(renders) < max(2, workers * 2):
            renders.append(submit_render(input_path, pages[cursor].number, out_dpi, workers))
            cursor += 1
        _, width, height, samples = await asyncio.wrap_future(renders.popleft())
        image = to_pil(width, height, samples)
        # Rendered size is authoritative (it includes any page rotation).
        state.width, state.height = width / px, height / px
        draw = ImageDraw.Draw(image)
        covered: dict[int, set] = {}
        labels: list[tuple[str, tuple]] = []
        native = state.mode == "text"
        for match in state.matches:
            label = registry.label_for(match.entity.type, match.entity.text)
            boxes = []
            for i, rect in enumerate(match.rects):
                x0, y0, x1, y1 = _padded(rect, native, state.width, state.height)
                draw.rectangle([x0 * px, y0 * px, x1 * px, y1 * px], fill=(0, 0, 0))
                if style == "label" and i == 0:
                    labels.append((label, (x0, y0, x1, y1)))
                boxes.append([round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)])
            for word_index, start, end in match.pieces:
                covered.setdefault(word_index, set()).update(range(start, end))
            occurrences.append({
                "id": len(occurrences) + 1, "label": label, "type": match.entity.type,
                "text": match.entity.text, "matched_text": match.matched_text, "source": match.entity.source,
                "match": match.match, "score": match.score, "page": state.number + 1,
                "method": "ocr" if state.mode == "ocr" else "text_layer", "boxes": boxes,
            })
        segments = [segment for index, word in enumerate(state.words)
                    for segment in _visible_segments(word, covered.get(index, set()))]
        encoded = loop.run_in_executor(ENCODE_EXECUTOR, _encode_page, image, native)
        prepared.append((state, encoded, segments, labels))
        del image, draw
        # Write finished pages in order while later pages are still encoding.
        while prepared and prepared[0][1].done():
            done_state, future, done_segments, done_labels = prepared.pop(0)
            await loop.run_in_executor(None, _write_page, c, future.result(), done_state, done_segments, done_labels)
        progress("redacting", 85 + int(10 * (state.number + 1) / total_pages),
                 f"Writing page {state.number + 1} of {total_pages}")
    for done_state, future, done_segments, done_labels in prepared:
        await loop.run_in_executor(None, _write_page, c, await future, done_state, done_segments, done_labels)
    await loop.run_in_executor(None, c.save)
    timings["apply_ms"] = int((time.perf_counter() - t_apply) * 1000)

    # ---- 7. Verify against the written file --------------------------------------------------------
    progress("verifying", 97, "Verifying the output")
    t_verify = time.perf_counter()
    residual, checked = await loop.run_in_executor(None, _verify, output_path, expected_gone)
    timings["verify_ms"] = int((time.perf_counter() - t_verify) * 1000)
    timings["total_ms"] = int((time.perf_counter() - t0) * 1000)
    if residual:
        # The text layer is written from scratch, so this would be a defect. Fail closed.
        raise DetectionError(f"Verification found {len(residual)} redacted value(s) in the output text layer; "
                             "the document was not released.")

    summary = summarise(occurrences)
    summary.update({"pages": total_pages, "pages_text_layer": total_pages - len(ocr_pages), "pages_ocr": len(ocr_pages)})
    return {
        "summary": summary,
        "entities": summary.pop("entities"),
        "occurrences": occurrences,
        "unmatched": unmatched,
        "pages": [{"page": p.number + 1, "method": "ocr" if p.mode == "ocr" else "text_layer", "reason": p.reason,
                   "width_pt": round(p.width, 2), "height_pt": round(p.height, 2), "words": len(p.words)}
                  for p in pages],
        "verification": {
            "status": "passed",
            "pages_checked": checked,
            "note": ("The output is a sanitised PDF: pages are re-rendered with redactions painted into the "
                     "pixels and a new text layer that holds only unredacted words. Hidden text, form data, "
                     "annotations, attachments and metadata from the source are not carried over."),
        },
        "output": {"render_dpi": out_dpi, "jpeg_quality": settings.pdf_output_jpeg_quality},
        "timings_ms": timings,
    }
