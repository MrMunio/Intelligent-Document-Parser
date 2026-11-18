"""
PDF reading and rendering on PDFium (pypdfium2: Apache-2.0 / BSD-3, PDFium: BSD-3).

Coordinates returned here are PDF points with a top-left origin (y grows downwards), matching the
convention used by OCR boxes after scaling. Kept free of paddle imports: the render pool imports it.
"""
import threading
from typing import Optional

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

WHITESPACE = {0x20, 0x09, 0xA0, 0x2002, 0x2003, 0x2009, 0x200B, 0x3000}
LINE_BREAKS = {0x0A, 0x0D}
IGNORED_ANNOTATIONS = {pdfium_c.FPDF_ANNOT_LINK, pdfium_c.FPDF_ANNOT_POPUP}


# PDFium is not thread safe. Every in-process use (several job slots run as threads) holds this lock.
# Rendering for OCR and output happens in separate processes, so it still runs in parallel.
PDFIUM_LOCK = threading.RLock()


class PdfOpenError(ValueError):
    pass


def open_pdf(path_or_bytes, forms: bool = True) -> pdfium.PdfDocument:
    """Opens a PDF. With forms=True, form field values are drawn when pages are rendered."""
    try:
        pdf = pdfium.PdfDocument(path_or_bytes)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise PdfOpenError("The PDF is password protected. Remove the password and submit it again.") from exc
        raise PdfOpenError(f"The PDF could not be opened: {exc}") from exc
    if forms:
        try:
            pdf.init_forms()
        except Exception:  # documents without an AcroForm are fine
            pass
    return pdf


def page_words(page) -> list[tuple[str, tuple, int]]:
    """
    Words of the page's text layer: [(text, (x0, y0, x1, y1), line_index)].
    Built from per-character boxes so positions are exact.
    """
    _, height = page.get_size()
    textpage = page.get_textpage()
    try:
        count = textpage.count_chars()
        words: list[tuple[str, tuple, int]] = []
        chars: list[str] = []
        box: Optional[list] = None
        line = 0

        def flush():
            nonlocal chars, box
            if chars and box:
                words.append(("".join(chars), (box[0], height - box[3], box[2], height - box[1]), line))
            chars, box = [], None

        for index in range(count):
            code = pdfium_c.FPDFText_GetUnicode(textpage.raw, index)
            if code in LINE_BREAKS:
                flush()
                if code == 0x0A:
                    line += 1
                continue
            if code in WHITESPACE or code == 0:
                flush()
                continue
            left, bottom, right, top = textpage.get_charbox(index)
            if right <= left or top <= bottom:
                continue
            if box and (left > box[2] + max(1.5, (box[3] - box[1]) * 0.6) or abs(bottom - box[1]) > (box[3] - box[1]) * 0.7):
                flush()  # a visual gap or a jump to another baseline without an explicit space
            try:
                chars.append(chr(code))
            except ValueError:
                continue
            box = [left, bottom, right, top] if box is None else [min(box[0], left), min(box[1], bottom),
                                                                    max(box[2], right), max(box[3], top)]
        flush()
        return words
    finally:
        textpage.close()


def image_coverage(page) -> float:
    """Share of the page area covered by image objects."""
    width, height = page.get_size()
    area = max(1.0, width * height)
    covered = 0.0
    try:
        for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=4):
            left, bottom, right, top = obj.get_pos()
            left, right = max(0.0, left), min(width, right)
            bottom, top = max(0.0, bottom), min(height, top)
            if right > left and top > bottom:
                covered += (right - left) * (top - bottom)
    except Exception:
        return 0.0
    return min(1.0, covered / area)


def has_visible_annotations(page) -> bool:
    """Form fields, comments and stamps are drawn on the page but are not part of the text layer."""
    count = pdfium_c.FPDFPage_GetAnnotCount(page.raw)
    for index in range(count):
        annot = pdfium_c.FPDFPage_GetAnnot(page.raw, index)
        if not annot:
            continue
        try:
            if pdfium_c.FPDFAnnot_GetSubtype(annot) not in IGNORED_ANNOTATIONS:
                return True
        finally:
            pdfium_c.FPDFPage_CloseAnnot(annot)
    return False


def render_rgb(page, dpi: float):
    """Renders a page (including form fields and annotations) to an RGB PIL image."""
    bitmap = page.render(scale=dpi / 72.0, may_draw_forms=True, draw_annots=True)
    try:
        return bitmap.to_pil().convert("RGB")
    finally:
        bitmap.close()


def document_text(path_or_bytes) -> list[str]:
    """Plain text per page, used for verification and in tests."""
    with PDFIUM_LOCK:
        return _document_text(path_or_bytes)


def _document_text(path_or_bytes) -> list[str]:
    pdf = open_pdf(path_or_bytes, forms=False)
    try:
        out = []
        for index in range(len(pdf)):
            page = pdf[index]
            textpage = page.get_textpage()
            out.append(textpage.get_text_range())
            textpage.close()
            page.close()
        return out
    finally:
        pdf.close()


def metadata(path_or_bytes) -> dict:
    with PDFIUM_LOCK:
        pdf = open_pdf(path_or_bytes, forms=False)
        try:
            return dict(pdf.get_metadata_dict(skip_empty=True))
        finally:
            pdf.close()


def page_count(path_or_bytes) -> int:
    with PDFIUM_LOCK:
        pdf = open_pdf(path_or_bytes, forms=False)
        try:
            return len(pdf)
        finally:
            pdf.close()
