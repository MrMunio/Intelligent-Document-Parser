"""Plain OCR: text with line and word bounding boxes."""
from shared.logger import setup_logger
from worker.ocr_instance import _PREDICT_LOCK, get_ocr_instance, parse_ocr_result

log = setup_logger("worker.engine_ocr")


def _box(values) -> dict:
    x0, y0, x1, y1 = values
    return {"xmin": int(x0), "ymin": int(y0), "xmax": int(x1), "ymax": int(y1)}


def run_ocr(file_path: str):
    """
    Runs OCR on a PDF or image.
    Returns (text, result_data) where result_data holds pages -> lines -> words with pixel boxes.
    """
    log.info("[run_ocr] Starting OCR for file: %s", file_path)
    ocr = get_ocr_instance()
    pages = []
    all_lines = []
    with _PREDICT_LOCK:
        for page_number, res in enumerate(ocr.predict_iter(input=file_path, return_word_box=True), start=1):
            lines = parse_ocr_result(res)
            max_x = max((line["bbox"][2] for line in lines), default=0)
            max_y = max((line["bbox"][3] for line in lines), default=0)
            pages.append({
                "pageNumber": page_number,
                # Paddle rasterises PDFs internally, so the page size is estimated from the text extent.
                "width": int(max_x * 1.05),
                "height": int(max_y * 1.05),
                "lines": [{
                    "text": line["text"],
                    "confidence": round(line["score"], 4) if line.get("score") is not None else None,
                    "boundingBox": _box(line["bbox"]),
                    "words": [{"text": text, "boundingBox": _box(box)} for text, box in line["words"]],
                } for line in lines],
            })
            all_lines.extend(line["text"] for line in lines)
    log.info("[run_ocr] Completed OCR: %s page(s), %s line(s)", len(pages), len(all_lines))
    return "\n".join(all_lines), {"page_count": len(pages), "pages": pages}
