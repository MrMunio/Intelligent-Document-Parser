"""Image redaction. The output keeps the input format (PNG stays PNG, JPEG stays JPEG, TIFF keeps its frames)."""
import asyncio
import time
from typing import Callable

from PIL import Image, ImageDraw, ImageFont, ImageSequence

from shared.logger import setup_logger
from worker.phi.detector import Detector, Entity
from worker.phi.matching import LabelRegistry, Word, match_page, summarise
from worker.phi.pdf import OCR_EXECUTOR

log = setup_logger("worker.phi.image")

PAD_X, PAD_Y = 3, 2
MAX_PIXELS = 60_000_000


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no scalable default font
        return None


def _draw_box(draw: ImageDraw.ImageDraw, rect, label, black, white) -> None:
    x0, y0, x1, y1 = rect
    draw.rectangle([x0, y0, x1, y1], fill=black)
    if not label:
        return
    height, width = y1 - y0, x1 - x0
    size = int(min(height * 0.8, 28))
    while size >= 7:
        font = _font(size)
        if font is None:
            return
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        if right - left <= width - 4 and bottom - top <= height:
            draw.text((x0 + (width - (right - left)) / 2 - left, y0 + (height - (bottom - top)) / 2 - top),
                      label, fill=white, font=font)
            return
        size -= 1


async def redact_image(input_path: str, output_path: str, options: dict, detector: Detector,
                       registry: LabelRegistry, progress: Callable[[str, int, str], None]) -> dict:
    import numpy as np
    from worker.ocr_instance import ocr_images

    t0 = time.perf_counter()
    timings: dict[str, int] = {}
    style = options.get("redaction_style", "label")
    loop = asyncio.get_running_loop()

    source = Image.open(input_path)
    fmt = source.format or "PNG"
    frames = []
    for frame in ImageSequence.Iterator(source):
        if frame.width * frame.height > MAX_PIXELS:
            raise ValueError("The image is too large to process (more than 60 megapixels).")
        mode = frame.mode if frame.mode in ("RGB", "RGBA", "L") else "RGB"
        frames.append(frame.convert(mode))
    dpi = source.info.get("dpi")

    progress("ocr", 10, f"Recognising text in {len(frames)} image frame(s)")
    t_ocr = time.perf_counter()
    arrays = [np.ascontiguousarray(np.asarray(f.convert("RGB"))[:, :, ::-1]) for f in frames]
    results = await loop.run_in_executor(OCR_EXECUTOR, ocr_images, arrays)
    timings["ocr_ms"] = int((time.perf_counter() - t_ocr) * 1000)

    detector.on_chunk = lambda done, total: progress(
        "detecting", int(40 + 45 * done / max(1, total)), f"Detecting entities: {done} of {total} segments")
    t_detect = time.perf_counter()
    frame_words = []
    tasks = []
    for lines in results:
        words = [Word(text, box, ("o", i)) for i, line in enumerate(lines) for text, box in line["words"]]
        frame_words.append(words)
        tasks.append(detector.detect("\n".join(line["text"] for line in lines)))
    frame_entities = await asyncio.gather(*tasks)
    detector.raise_if_incomplete()
    timings["detect_ms"] = int((time.perf_counter() - t_detect) * 1000)

    progress("redacting", 88, "Applying redactions")
    t_apply = time.perf_counter()
    union: dict[tuple, Entity] = {}
    for entities in frame_entities:
        for entity in entities:
            union.setdefault((entity.text, entity.type), entity)
    all_entities = list(union.values())

    occurrences, unmatched = [], []
    for index, (frame, words, entities) in enumerate(zip(frames, frame_words, frame_entities)):
        matches, missing = match_page(words, entities, all_entities, allow_fuzzy=True)
        for entity in missing:
            unmatched.append({"page": index + 1, "type": entity.type, "text": entity.text, "source": entity.source})
        draw = ImageDraw.Draw(frame)
        black = 0 if frame.mode == "L" else ((0, 0, 0, 255) if frame.mode == "RGBA" else (0, 0, 0))
        white = 255 if frame.mode == "L" else ((255, 255, 255, 255) if frame.mode == "RGBA" else (255, 255, 255))
        for match in matches:
            label = registry.label_for(match.entity.type, match.entity.text)
            boxes = []
            for i, (x0, y0, x1, y1) in enumerate(match.rects):
                # OCR word boxes can cut a glyph in half, so padding scales with the text height.
                pad_x = max(PAD_X, int((y1 - y0) * 0.35))
                pad_y = max(PAD_Y, int((y1 - y0) * 0.18))
                rect = (max(0, int(x0) - pad_x), max(0, int(y0) - pad_y),
                        min(frame.width, int(x1) + pad_x), min(frame.height, int(y1) + pad_y))
                _draw_box(draw, rect, label if (style == "label" and i == 0) else None, black, white)
                boxes.append(list(rect))
            occurrences.append({
                "id": len(occurrences) + 1, "label": label, "type": match.entity.type, "text": match.entity.text,
                "matched_text": match.matched_text, "source": match.entity.source, "match": match.match,
                "score": match.score, "page": index + 1, "method": "ocr", "boxes": boxes,
            })

    save_kwargs: dict = {}
    if dpi and not options.get("scrub_metadata", True):
        save_kwargs["dpi"] = dpi
    if fmt.upper() in ("JPEG", "MPO"):
        fmt = "JPEG"
        frames = [f.convert("RGB") for f in frames]
        save_kwargs.update(quality=92, optimize=True)
    if len(frames) > 1 and fmt.upper() == "TIFF":
        frames[0].save(output_path, format=fmt, save_all=True, append_images=frames[1:], **save_kwargs)
    else:
        frames[0].save(output_path, format=fmt, **save_kwargs)  # metadata (EXIF) is not carried over
    timings["apply_ms"] = int((time.perf_counter() - t_apply) * 1000)
    timings["total_ms"] = int((time.perf_counter() - t0) * 1000)

    summary = summarise(occurrences)
    summary.update({"pages": len(frames), "pages_text_layer": 0, "pages_ocr": len(frames)})
    return {
        "summary": summary,
        "entities": summary.pop("entities"),
        "occurrences": occurrences,
        "unmatched": unmatched,
        "pages": [{"page": i + 1, "method": "ocr", "width_px": f.width, "height_px": f.height,
                   "words": len(w)} for i, (f, w) in enumerate(zip(frames, frame_words))],
        "verification": {"status": "not_applicable",
                         "note": "Redacted regions are painted over in the pixels. There is no text layer to re-read."},
        "timings_ms": timings,
    }
