"""
Chunking for agentic retrieval. The strategy is chosen from the shape of the document:

  section    a table of contents or a heading outline exists: chunks never cross a section boundary and
             carry the section id and title, so retrieval results say where they come from.
  tabular    CSV / TSV or delimiter-heavy text: groups of rows, each chunk repeating the header row.
  page       short forms (a few pages with many "Label: value" lines) and images: one chunk per page,
             because a form is understood as a whole.
  recursive  everything else: paragraph boundaries first, then lines, then sentences, with overlap.

Every chunk knows its page range. Sizes are in approximate tokens (EXTRACT_RAG_CHUNK_TOKENS,
EXTRACT_RAG_OVERLAP_TOKENS).
"""
import re
from dataclasses import dataclass
from typing import Optional

from shared.formats import FORMAT_IMAGE
from worker.extract.document import Document, approx_tokens
from worker.extract.toc import TocResult

LABEL_VALUE = re.compile(r"^\s*[A-Za-z][A-Za-z /#()&.'-]{1,40}\s*[:]\s*\S")
CHARS_PER_TOKEN = 3.6


@dataclass
class Chunk:
    id: str
    text: str
    page_start: int
    page_end: int
    section_id: Optional[str] = None
    section_title: Optional[str] = None

    @property
    def tokens(self) -> int:
        return approx_tokens(self.text)

    def pages_label(self) -> str:
        return f"p{self.page_start}" if self.page_start == self.page_end else f"p{self.page_start}-{self.page_end}"


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

def _delimiter_share(lines: list[str]) -> float:
    if not lines:
        return 0.0
    best = 0.0
    for delim in (",", "\t", "|", ";"):
        counts = [ln.count(delim) for ln in lines[:200] if ln.strip()]
        if not counts:
            continue
        common = max(set(counts), key=counts.count)
        if common >= 2:
            best = max(best, sum(1 for c in counts if c == common) / len(counts))
    return best


def choose_strategy(doc: Document, toc: TocResult) -> tuple[str, str]:
    lines = [ln for p in doc.pages for ln in p.text.splitlines() if ln.strip()]
    if doc.extension in (".csv", ".tsv") or (_delimiter_share(lines) >= 0.8 and len(lines) >= 10):
        return "tabular", "rows with a consistent delimiter: row groups with the header repeated"
    if doc.format == FORMAT_IMAGE:
        return "page", "image input: one chunk per page"
    label_share = sum(1 for ln in lines if LABEL_VALUE.match(ln)) / max(1, len(lines))
    if doc.page_count <= 4 and label_share >= 0.25:
        return "page", f"short form ({doc.page_count} pages, {label_share:.0%} label/value lines): one chunk per page"
    usable = [e for e in toc.entries if e.page]
    if len(usable) >= 3:
        return "section", f"{len(usable)} sections from the {toc.source.replace('_', ' ')}: chunks follow section boundaries"
    return "recursive", "continuous text: paragraph-aware chunks with overlap"


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------

class _Text:
    """The whole document as one string with a map from character offsets to pages."""

    def __init__(self, doc: Document):
        parts, self.starts = [], []
        offset = 0
        for page in doc.pages:
            self.starts.append((offset, page.number))
            parts.append(page.text)
            offset += len(page.text) + 2
        self.text = "\n\n".join(parts)

    def page_at(self, offset: int) -> int:
        number = self.starts[0][1]
        for start, page in self.starts:
            if start > offset:
                break
            number = page
        return number

    def page_start_offset(self, number: int) -> int:
        for start, page in self.starts:
            if page == number:
                return start
        return len(self.text)


def split_text(text: str, target_chars: int, overlap_chars: int) -> list[tuple[int, int]]:
    """Spans (start, end) of about target_chars, cut at paragraph, line, then sentence boundaries."""
    spans = []
    start, n = 0, len(text)
    while start < n:
        end = min(n, start + target_chars)
        if end < n:
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", "; ", " "):
                cut = window.rfind(sep, int(target_chars * 0.5))
                if cut > 0:
                    end = start + cut + len(sep)
                    break
        if text[start:end].strip():
            spans.append((start, end))
        if end >= n:
            break
        start = max(start + 1, end - overlap_chars)
        # begin the overlap at a word boundary
        space = text.find(" ", start, end)
        if 0 <= space < end and space - start < 40:
            start = space + 1
    return spans


def _chunks_for_span(t: _Text, a: int, b: int, target: int, overlap: int, section=None) -> list[Chunk]:
    out = []
    for s, e in split_text(t.text[a:b], target, overlap):
        body = t.text[a + s:a + e].strip()
        if not body:
            continue
        out.append(Chunk(id="", text=body, page_start=t.page_at(a + s), page_end=t.page_at(a + max(s, e - 1)),
                         section_id=section.id if section else None, section_title=section.title if section else None))
    return out


def _find_title(t: _Text, title: str, start_page: int, not_before: int) -> int:
    """Character offset of a section title on (or just after) its start page."""
    base = max(not_before, t.page_start_offset(start_page))
    words = [w for w in re.findall(r"\w+", title) if len(w) > 1][:6]
    if not words:
        return base
    pattern = r"\W+".join(re.escape(w) for w in words)
    m = re.compile(pattern, re.IGNORECASE).search(t.text, base, base + 12000)
    return m.start() if m else base


def chunk_document(doc: Document, toc: TocResult, strategy: str, target_tokens: int, overlap_tokens: int) -> list[Chunk]:
    target = max(400, int(target_tokens * CHARS_PER_TOKEN))
    overlap = max(0, int(overlap_tokens * CHARS_PER_TOKEN))
    chunks: list[Chunk] = []
    if strategy == "tabular":
        rows = [(page.number, line) for page in doc.pages for line in page.text.splitlines() if line.strip()]
        header = rows[0][1] if rows else ""
        group: list[tuple[int, str]] = []
        size = 0

        def emit():
            chunks.append(Chunk("", header + "\n" + "\n".join(line for _, line in group), group[0][0], group[-1][0]))

        for number, line in rows[1:]:
            if group and size + len(line) > target:
                emit()
                group, size = [], 0
            group.append((number, line))
            size += len(line) + 1
        if group:
            emit()
        elif rows:  # header only
            chunks.append(Chunk("", header, rows[0][0], rows[0][0]))
    elif strategy == "page":
        t = _Text(doc)
        for page in doc.pages:
            a = t.page_start_offset(page.number)
            chunks.extend(_chunks_for_span(t, a, a + len(page.text), target, overlap))
    elif strategy == "section":
        t = _Text(doc)
        entries = [e for e in toc.entries if e.page]
        bounds = []
        cursor = 0
        for e in entries:
            pos = _find_title(t, e.title, e.page, cursor)
            bounds.append((pos, e))
            cursor = pos
        if bounds and bounds[0][0] > 0:  # front matter before the first section
            chunks.extend(_chunks_for_span(t, 0, bounds[0][0], target, overlap))
        for i, (pos, e) in enumerate(bounds):
            end = bounds[i + 1][0] if i + 1 < len(bounds) else len(t.text)
            if end > pos:
                chunks.extend(_chunks_for_span(t, pos, end, target, overlap, section=e))
    else:
        t = _Text(doc)
        chunks = _chunks_for_span(t, 0, len(t.text), target, overlap)
    for i, chunk in enumerate(chunks, start=1):
        chunk.id = f"c{i:04d}"
    return chunks
