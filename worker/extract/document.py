"""
Document loading for data extraction: every supported format becomes a list of pages of plain text.

  PDF    text layer read with PDFium; pages without a usable text layer are rendered and OCR'd.
         Bookmarks (the PDF outline) are kept: they are the most reliable table of contents.
  Images OCR, one page per frame.
  DOCX   paragraphs and tables of the body in reading order. Heading styles become markdown headings,
         and the text is cut into page-sized parts at paragraph boundaries.
  Text   decoded (BOM, UTF-8, Windows-1252) and cut into page-sized parts at line boundaries.

For documents without real pages, "page" means one of those parts; `Document.paginated` says which.
"""
import asyncio
import re
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Optional

from shared.config import settings
from shared.formats import FORMAT_DOCX, FORMAT_IMAGE, FORMAT_PDF, FORMAT_TEXT, extension_of
from shared.logger import setup_logger

log = setup_logger("worker.extract.document")

MIN_TEXT_LAYER_WORDS = 5
FLOW_PAGE_CHARS = 3500  # size of a "page" for documents without pages


@dataclass
class Page:
    number: int  # 1-based
    text: str
    source: str  # text_layer | ocr | docx | text

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class OutlineItem:
    title: str
    level: int
    page: Optional[int]  # 1-based physical page


@dataclass
class Document:
    name: str
    format: str
    pages: list[Page]
    paginated: bool = True
    outline: list[OutlineItem] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    extension: str = ""

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def total_chars(self) -> int:
        return sum(p.chars for p in self.pages)

    @property
    def ocr_pages(self) -> int:
        return sum(1 for p in self.pages if p.source == "ocr")

    def page(self, number: int) -> Optional[Page]:
        if 1 <= number <= len(self.pages):
            return self.pages[number - 1]
        return None

    def full_text(self, markers: bool = True) -> str:
        if not markers:
            return "\n\n".join(p.text for p in self.pages)
        label = "Page" if self.paginated else "Part"
        return "\n\n".join(f"[{label} {p.number}]\n{p.text}" for p in self.pages)

    def describe(self) -> dict:
        return {"file": self.name, "format": self.format, "pages": self.page_count, "paginated": self.paginated,
                "ocr_pages": self.ocr_pages, "chars": self.total_chars, "bookmarks": len(self.outline),
                "approx_tokens": approx_tokens(self.full_text(markers=False))}


def approx_tokens(text: str) -> int:
    """Conservative token estimate (about 3.6 characters per token for English prose and numbers)."""
    return int(len(text) / 3.6) + 1


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _lines_from_words(words) -> str:
    lines: list[str] = []
    current = None
    for text, _box, line in words:
        if line != current:
            lines.append(text)
            current = line
        else:
            lines[-1] += " " + text
    return "\n".join(lines)


def _read_pdf(path: str, ocr_mode: str) -> tuple[list[Optional[str]], list[OutlineItem], dict]:
    """Text layer per page (None where OCR is needed), bookmarks and metadata."""
    from worker import pdf_util
    texts: list[Optional[str]] = []
    outline: list[OutlineItem] = []
    with pdf_util.PDFIUM_LOCK:
        pdf = pdf_util.open_pdf(path, forms=False)
        try:
            if len(pdf) == 0:
                raise ValueError("The PDF has no pages.")
            for index in range(len(pdf)):
                page = pdf[index]
                try:
                    words = pdf_util.page_words(page)
                finally:
                    page.close()
                # Reading order matters here, not positions, so rotated pages keep their text layer.
                if ocr_mode == "always" or (ocr_mode == "auto" and len(words) < MIN_TEXT_LAYER_WORDS):
                    texts.append(None)
                else:
                    texts.append(_lines_from_words(words))
            outline = _read_outline(pdf)
            try:
                meta = dict(pdf.get_metadata_dict(skip_empty=True))
            except Exception:
                meta = {}
        finally:
            pdf.close()
    return texts, outline, meta


def _read_outline(pdf) -> list[OutlineItem]:
    """PDF bookmarks. Supports both pypdfium2 4.x (named tuples) and 5.x (bookmark objects)."""
    outline: list[OutlineItem] = []
    try:
        for item in pdf.get_toc(max_depth=6):
            if hasattr(item, "get_title"):
                title = item.get_title() or ""
                dest = item.get_dest()
                page_index = dest.get_index() if dest is not None else None
            else:
                title = getattr(item, "title", "") or ""
                page_index = getattr(item, "page_index", None)
            if title.strip():
                outline.append(OutlineItem(title=title.strip(), level=int(getattr(item, "level", 0)) + 1,
                                           page=(page_index + 1) if page_index is not None else None))
    except Exception as exc:  # malformed outlines are common and never fatal
        log.debug("outline unreadable: %s", exc)
    return outline


async def _ocr_pdf_pages(path: str, numbers: list[int], progress: Callable) -> dict[int, str]:
    """OCR for the given zero-based page numbers: rendering in parallel processes, recognition on the GPU."""
    from worker.ocr_instance import ocr_images
    from worker.phi.pdf import OCR_EXECUTOR
    from worker.render_pool import submit_render, to_bgr_array
    loop = asyncio.get_running_loop()
    dpi = settings.phi_render_dpi
    out: dict[int, str] = {}
    pending: list = []
    cursor = 0
    done = 0
    while cursor < len(numbers) or pending:
        while cursor < len(numbers) and len(pending) < 3:
            pending.append((numbers[cursor], submit_render(path, numbers[cursor], dpi, settings.render_workers)))
            cursor += 1
        number, future = pending.pop(0)
        _, width, height, samples = await asyncio.wrap_future(future)
        image = to_bgr_array(width, height, samples)
        lines = (await loop.run_in_executor(OCR_EXECUTOR, ocr_images, [image]))[0]
        out[number] = "\n".join(line["text"] for line in lines)
        done += 1
        progress("ocr", 5 + int(20 * done / max(1, len(numbers))), f"Recognising text: page {done} of {len(numbers)}")
    return out


async def _load_pdf(path: str, name: str, ocr_mode: str, progress: Callable) -> Document:
    from worker import pdf_util
    loop = asyncio.get_running_loop()
    try:
        texts, outline, meta = await loop.run_in_executor(None, _read_pdf, path, ocr_mode)
    except pdf_util.PdfOpenError as exc:
        raise ValueError(str(exc)) from exc
    need_ocr = [i for i, t in enumerate(texts) if t is None]
    ocr_text = await _ocr_pdf_pages(path, need_ocr, progress) if need_ocr else {}
    pages = []
    for i, text in enumerate(texts):
        if text is None:
            pages.append(Page(i + 1, ocr_text.get(i, ""), "ocr"))
        else:
            pages.append(Page(i + 1, text, "text_layer"))
    return Document(name=name, format=FORMAT_PDF, pages=pages, paginated=True, outline=outline, metadata=meta,
                    extension=".pdf")


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

async def _load_image(path: str, name: str, progress: Callable) -> Document:
    import numpy as np
    from PIL import Image, ImageSequence
    from worker.ocr_instance import ocr_images
    from worker.phi.pdf import OCR_EXECUTOR
    loop = asyncio.get_running_loop()
    pages = []
    with Image.open(path) as source:
        frames = [frame.convert("RGB") for frame in ImageSequence.Iterator(source)]
    for index, frame in enumerate(frames, start=1):
        progress("ocr", 5 + int(20 * index / len(frames)), f"Recognising text: page {index} of {len(frames)}")
        array = np.ascontiguousarray(np.asarray(frame)[:, :, ::-1])
        lines = (await loop.run_in_executor(OCR_EXECUTOR, ocr_images, [array]))[0]
        pages.append(Page(index, "\n".join(line["text"] for line in lines), "ocr"))
    return Document(name=name, format=FORMAT_IMAGE, pages=pages, paginated=True, extension=extension_of(name))


# ---------------------------------------------------------------------------
# DOCX and text
# ---------------------------------------------------------------------------

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_HEADING_STYLE = re.compile(r"^(?:heading|titre|berschrift|ttulo)\s*(\d)$|^title$", re.IGNORECASE)


def _docx_blocks(path: str) -> list[str]:
    """Body paragraphs and table rows in reading order; heading styles become markdown headings."""
    from lxml import etree
    ns = {"w": _W}
    with zipfile.ZipFile(path) as archive:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
        root = etree.fromstring(archive.read("word/document.xml"), parser)
        styles = {}
        if "word/styles.xml" in archive.namelist():
            sroot = etree.fromstring(archive.read("word/styles.xml"), parser)
            for style in sroot.iterfind("w:style", ns):
                sid = style.get(f"{{{_W}}}styleId") or ""
                name_el = style.find("w:name", ns)
                styles[sid] = name_el.get(f"{{{_W}}}val") if name_el is not None else sid
    body = root.find("w:body", ns)
    blocks: list[str] = []

    def para_text(p) -> str:
        parts = []
        for el in p.iter(f"{{{_W}}}t", f"{{{_W}}}tab", f"{{{_W}}}br"):
            local = etree.QName(el).localname
            if local == "t":
                parts.append(el.text or "")
            elif local == "tab":
                parts.append("\t")
            else:
                parts.append("\n")
        return "".join(parts).strip()

    for child in (body if body is not None else []):
        tag = etree.QName(child).localname
        if tag == "p":
            text = para_text(child)
            if not text:
                continue
            style_el = child.find("w:pPr/w:pStyle", ns)
            style = styles.get(style_el.get(f"{{{_W}}}val"), style_el.get(f"{{{_W}}}val")) if style_el is not None else ""
            m = _HEADING_STYLE.match(style or "")
            if m:
                level = int(m.group(1)) if m.group(1) else 1
                blocks.append("#" * min(6, level) + " " + text)
            else:
                blocks.append(text)
        elif tag == "tbl":
            for row in child.iterfind(".//w:tr", ns):
                cells = [" ".join(para_text(p) for p in cell.iterfind(".//w:p", ns)).strip()
                         for cell in row.iterfind("w:tc", ns)]
                if any(cells):
                    blocks.append(" | ".join(cells))
    return blocks


def _paginate(blocks: list[str], size: int = FLOW_PAGE_CHARS) -> list[str]:
    pages, current, length = [], [], 0
    for block in blocks:
        if current and length + len(block) > size:
            pages.append("\n".join(current))
            current, length = [], 0
        while len(block) > size * 2:  # a single enormous paragraph
            pages.append(block[:size])
            block = block[size:]
        current.append(block)
        length += len(block) + 1
    if current:
        pages.append("\n".join(current))
    return pages or [""]


def _load_docx(path: str, name: str) -> Document:
    parts = _paginate(_docx_blocks(path))
    return Document(name=name, format=FORMAT_DOCX, pages=[Page(i + 1, t, "docx") for i, t in enumerate(parts)],
                    paginated=False, extension=".docx")


def load_text(text: str, name: str) -> Document:
    parts = _paginate(text.replace("\r\n", "\n").split("\n"))
    return Document(name=name, format=FORMAT_TEXT, pages=[Page(i + 1, t, "text") for i, t in enumerate(parts)],
                    paginated=False, extension=extension_of(name) or ".txt")


def _load_text_file(path: str, name: str) -> Document:
    from worker.phi.text import decode_bytes
    with open(path, "rb") as f:
        text, _, _ = decode_bytes(f.read())
    return load_text(text, name)


async def load_document(path: str, name: str, fmt: str, ocr_mode: str, progress: Callable) -> Document:
    progress("reading", 3, "Reading the document")
    if fmt == FORMAT_PDF:
        doc = await _load_pdf(path, name, ocr_mode, progress)
    elif fmt == FORMAT_IMAGE:
        doc = await _load_image(path, name, progress)
    elif fmt == FORMAT_DOCX:
        doc = await asyncio.get_running_loop().run_in_executor(None, _load_docx, path, name)
    elif fmt == FORMAT_TEXT:
        doc = _load_text_file(path, name)
    else:
        raise ValueError(f"Unsupported format '{fmt}'.")
    if not any(p.text.strip() for p in doc.pages):
        raise ValueError("No text could be read from the document.")
    log.info("Loaded %s: %s pages (%s OCR), %s chars", name, doc.page_count, doc.ocr_pages, doc.total_chars)
    return doc
