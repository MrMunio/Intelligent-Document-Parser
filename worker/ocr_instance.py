"""Shared PaddleOCR instance (PP-OCRv6 by default) used by PHI redaction and plain OCR."""
import os
import threading
from typing import Any

# Must be set before paddleocr / paddlex are imported: skips the geo-based model source probe.
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

from shared.config import settings
from shared.logger import setup_logger

log = setup_logger("worker.ocr_instance")

_OCR_INSTANCE = None
_OCR_INFO: dict[str, Any] = {}
_INIT_LOCK = threading.Lock()
_PREDICT_LOCK = threading.Lock()  # one inference at a time on the GPU


def _build(enable_hpi: bool):
    from paddleocr import PaddleOCR
    kwargs: dict[str, Any] = dict(
        text_detection_model_name=settings.ocr_det_model,
        text_recognition_model_name=settings.ocr_rec_model,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_recognition_batch_size=settings.ocr_rec_batch_size,
        enable_hpi=enable_hpi,
    )
    if settings.ocr_device:
        kwargs["device"] = settings.ocr_device
    return PaddleOCR(**kwargs)


def get_ocr_instance():
    """Singleton so the models stay loaded in VRAM between jobs."""
    global _OCR_INSTANCE
    if _OCR_INSTANCE is None:
        with _INIT_LOCK:
            if _OCR_INSTANCE is None:
                hpi = settings.paddle_enable_hpi
                log.info("Initialising PaddleOCR det=%s rec=%s hpi=%s", settings.ocr_det_model,
                         settings.ocr_rec_model, hpi)
                try:
                    instance = _build(hpi)
                except Exception as exc:
                    if not hpi:
                        raise
                    log.warning("High-performance inference could not be enabled (%s). Using standard inference.", exc)
                    hpi = False
                    instance = _build(False)
                _OCR_INSTANCE = instance
                _OCR_INFO.update({"detection_model": settings.ocr_det_model,
                                  "recognition_model": settings.ocr_rec_model, "hpi": hpi})
                log.info("PaddleOCR ready")
    return _OCR_INSTANCE


def ocr_info() -> dict:
    return dict(_OCR_INFO)


def _result_dict(res) -> dict:
    raw = res.json() if callable(getattr(res, "json", None)) else getattr(res, "json", {})
    if isinstance(raw, dict):
        return raw.get("res", raw)
    return {}


def _box4(box) -> tuple:
    """Accepts [x0, y0, x1, y1] or a polygon of points and returns an axis-aligned box."""
    if box is None:
        return (0.0, 0.0, 0.0, 0.0)
    flat = list(box)
    if flat and isinstance(flat[0], (list, tuple)) or (flat and hasattr(flat[0], "__len__")):
        xs = [float(p[0]) for p in flat]
        ys = [float(p[1]) for p in flat]
        return (min(xs), min(ys), max(xs), max(ys))
    if len(flat) >= 8:
        xs, ys = flat[0::2], flat[1::2]
        return (float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)))
    return (float(flat[0]), float(flat[1]), float(flat[2]), float(flat[3]))


def _words_from_line(text: str, box: tuple) -> list:
    """Fallback when the recogniser gives no word boxes: split the line box in proportion to characters."""
    tokens = text.split()
    if not tokens:
        return []
    x0, y0, x1, y1 = box
    total = sum(len(t) for t in tokens) + (len(tokens) - 1)
    width = max(1.0, x1 - x0)
    out = []
    cursor = 0
    for token in tokens:
        start = x0 + width * (cursor / total)
        end = x0 + width * ((cursor + len(token)) / total)
        out.append((token, (start, y0, end, y1)))
        cursor += len(token) + 1
    return out


def parse_ocr_result(res) -> list[dict]:
    """
    Normalises one page result to lines:
        [{"text": str, "bbox": (x0, y0, x1, y1), "score": float, "words": [(text, bbox), ...]}]
    Coordinates are image pixels.
    """
    data = _result_dict(res)
    texts = list(data.get("rec_texts") or [])
    boxes = data.get("rec_boxes")
    if boxes is None or len(boxes) != len(texts):
        boxes = data.get("rec_polys") or data.get("dt_polys") or []
    scores = list(data.get("rec_scores") or [])
    word_texts = data.get("text_word") or []
    word_boxes = data.get("text_word_boxes") or []
    have_words = len(word_texts) == len(texts) and len(word_boxes) == len(texts)

    lines = []
    for i, text in enumerate(texts):
        text = (text or "").strip()
        if not text or i >= len(boxes):
            continue
        line_box = _box4(boxes[i])
        words = []
        if have_words and len(word_texts[i]) == len(word_boxes[i]):
            for token, token_box in zip(word_texts[i], word_boxes[i]):
                token = (token or "").strip()
                if token:
                    words.append((token, _box4(token_box)))
        if not words:
            words = _words_from_line(text, line_box)
        if words:
            lines.append({"text": text, "bbox": line_box, "words": words,
                          "score": float(scores[i]) if i < len(scores) else None})
    return lines


def ocr_images(images: list) -> list[list[dict]]:
    """Runs OCR on a batch of BGR numpy images. Returns one list of lines per image."""
    if not images:
        return []
    ocr = get_ocr_instance()
    with _PREDICT_LOCK:
        results = list(ocr.predict(input=images, return_word_box=True))
    return [parse_ocr_result(res) for res in results]
