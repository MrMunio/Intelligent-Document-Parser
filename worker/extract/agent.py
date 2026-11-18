"""
Approach 2, agentic retrieval: a tool-using agent navigates the indexed document and fills the fields.

  ExtractionState   the result lives here, outside the conversation. The agent writes to it with
                    record_fields, so nothing found is lost when a session is compacted.
  DocumentTools     curated retrieval tools: document map, hybrid search, exact text search, read pages,
                    read a section, record fields, list pending fields, finish.
  AgentRunner       the loop. After every turn it measures the size of the conversation from the token
                    counts the provider reports (prompt, cache reads and writes, reply) plus the tool results
                    about to be sent. When that reaches EXTRACT_CONTEXT_THRESHOLD of the usable window (the
                    model's window from the catalog, capped by EXTRACT_CONTEXT_CAP_TOKENS), the agent writes
                    short handoff notes, the conversation is dropped, and a new session starts from a compact
                    checkpoint: fields recorded, fields not found, fields pending, searches and pages already
                    covered, and the notes. Raw tool output is never carried over.
                    A failure of the primary engine also starts a new session, on the fallback engine.
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from shared.config import settings
from shared.extract_spec import FieldDef, Spec, conform
from shared.llm import AgentTurn, BaseProvider, LLMError, ToolCall
from shared.logger import setup_logger
from shared.model_catalog import ModelInfo, compaction_threshold, find_model, usable_window
from worker.extract.chunking import Chunk
from worker.extract.document import Document, approx_tokens
from worker.extract.retrieval import HybridIndex
from worker.extract.toc import TocResult

log = setup_logger("worker.extract.agent")

PROMPT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts",
                           "extract_agent_prompt.txt")
MAX_READ_PAGES = 6
MAX_MAP_CHARS_IN_TASK = 9000
TRACE_LIMIT = 2000
PRE_COMPACTION_SHARE = 0.8  # share of the compaction limit at which the agent is told to save its findings
TOOL_OUTPUT_SHARE = 0.15  # one tool result may use about this share of the usable window
MIN_TOOL_CHARS = 2000
READ_TOOLS = ("read_pages", "read_section", "read_chunk")
_STOP = set("this that with from into what which when where whose have been were their there these those than then "
            "they them each must shall will would about under over such other within days date name".split())


def tool_chars_for(window: int) -> int:
    """Largest tool result for a usable window: small windows get short reads so a session can still think and record."""
    return max(MIN_TOOL_CHARS, min(settings.extract_agent_max_tool_chars, int(window * TOOL_OUTPUT_SHARE * 3.6)))


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 4 and w not in _STOP}
HANDOFF_REQUEST = (
    "CONTEXT CHECKPOINT. This conversation has reached its size limit and will restart from a checkpoint that "
    "already lists every recorded field, the pending fields, your searches and the pages you read. Do not call "
    "any tools now. Write handoff notes for your next session in at most 200 words: where the pending fields are "
    "likely to be (sections, pages, terms to search), leads not yet followed, and anything uncertain about the "
    "values you recorded.")


def _system_prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read().strip()


def _ranges(numbers) -> str:
    """[1, 2, 3, 7, 9, 10] -> '1-3, 7, 9-10'"""
    nums = sorted(set(numbers))
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(out)


def _preview(value: Any, limit: int = 180) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class FieldState:
    path: str
    status: str = "pending"  # pending | found | not_found
    value: Any = None
    pages: list = field(default_factory=list)
    confidence: Optional[str] = None
    note: str = ""
    session: Optional[int] = None


class ExtractionState:
    def __init__(self, spec: Spec, page_count: int):
        self.spec = spec
        self.page_count = page_count
        self.fields: dict[str, FieldState] = {f.path: FieldState(f.path) for f in spec.fields}
        self.defs: dict[str, FieldDef] = {f.path: f for f in spec.fields}

    @staticmethod
    def _schema(d: FieldDef) -> dict:
        schema: dict[str, Any] = {"type": d.type}
        if d.enum:
            schema["enum"] = d.enum
        if d.items:
            schema["items"] = d.items
        return schema

    def pending(self) -> list[str]:
        return [p for p, s in self.fields.items() if s.status == "pending"]

    @property
    def done(self) -> bool:
        return not self.pending()

    def counts(self) -> dict:
        out = {"total": len(self.fields), "found": 0, "not_found": 0, "pending": 0}
        for s in self.fields.values():
            out[s.status] += 1
        return out

    def _suggest(self, path: str) -> str:
        tail = path.split(".")[-1].lower()
        close = [p for p in self.fields if p.split(".")[-1].lower() == tail or tail in p.lower()]
        if not close:
            prefix = path + "."
            close = [p for p in self.fields if p.startswith(prefix)]
        return f" Did you mean: {', '.join(close[:6])}?" if close else ""

    def record(self, entry: dict, session: int) -> tuple[bool, str]:
        path = str(entry.get("path", "")).strip().lstrip("$.")
        if path not in self.fields:
            return False, f"'{path}' is not a target field.{self._suggest(path)}"
        status = str(entry.get("status") or "found").strip().lower()
        if status not in ("found", "not_found"):
            return False, f"{path}: status must be 'found' or 'not_found'."
        d = self.defs[path]
        state = self.fields[path]
        pages = []
        for p in entry.get("pages") or []:
            try:
                n = int(p)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= self.page_count and n not in pages:
                pages.append(n)
        confidence = str(entry.get("confidence") or "").strip().lower() or None
        if confidence not in (None, "high", "medium", "low"):
            confidence = None
        note = str(entry.get("note") or "")[:500]
        if status == "not_found":
            state.status, state.value, state.pages, state.confidence, state.note, state.session = (
                "not_found", None, pages, confidence, note, session)
            return True, f"{path}: marked not found"
        raw = entry.get("value_json", entry.get("value"))
        if isinstance(raw, str) and "value_json" in entry:
            try:
                raw = json.loads(raw)
            except ValueError:
                pass  # a plain string value written without JSON quotes
        value = conform(raw, self._schema(d))
        if value is None or value == []:
            return False, (f"{path}: the value {_preview(raw, 80)} does not fit type {d.type}"
                           f"{' with values ' + str(d.enum) if d.enum else ''}. Send it again in that type, "
                           "or mark the field not_found.")
        state.status, state.value, state.pages, state.confidence, state.note, state.session = (
            "found", value, pages, confidence, note, session)
        return True, f"{path} = {_preview(value, 100)}"

    def result(self) -> dict:
        return self.spec.assemble({p: s.value for p, s in self.fields.items() if s.status == "found"})

    def field_rows(self, source: str = "agent") -> list[dict]:
        rows = []
        for path, s in self.fields.items():
            d = self.defs[path]
            rows.append({"path": path, "type": d.type, "status": s.status, "value": s.value, "pages": s.pages,
                         "confidence": s.confidence, "note": s.note, "session": s.session, "source": source})
        return rows


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _obj(properties: dict, required: list) -> dict:
    return {"type": "object", "properties": properties, "required": required}


TOOL_SPECS = [
    {"name": "get_document_map",
     "description": "Returns the document overview and its map: the table of contents (or heading outline) with "
                    "section ids, levels and page ranges. Use it to decide where each field should be.",
     "parameters": _obj({"max_level": {"type": "integer", "description": "Deepest level to list (default: all)."}}, [])},
    {"name": "search_document",
     "description": "Ranked search over the document chunks, combining keyword and semantic matching. Returns chunk "
                    "ids, pages, section and a snippet. Good for finding where a topic is discussed.",
     "parameters": _obj({"query": {"type": "string", "description": "What to look for, in words likely to appear in the document."},
                         "top_k": {"type": "integer", "description": "Number of results (1-12, default 6)."},
                         "page_from": {"type": "integer", "description": "Only results on or after this page."},
                         "page_to": {"type": "integer", "description": "Only results on or before this page."},
                         "section_id": {"type": "string", "description": "Only results inside this section id."}},
                        ["query"])},
    {"name": "find_text",
     "description": "Exact text search (case-insensitive), or a regular expression. Returns every matching line with "
                    "its page and the lines around it. Good for identifiers, defined terms, amounts and dates.",
     "parameters": _obj({"text": {"type": "string", "description": "Text or regular expression to find."},
                         "regex": {"type": "boolean", "description": "Treat text as a regular expression (default false)."},
                         "page_from": {"type": "integer"}, "page_to": {"type": "integer"},
                         "max_hits": {"type": "integer", "description": "Maximum matches (default 20, at most 50)."}},
                        ["text"])},
    {"name": "read_pages",
     "description": f"Returns the full text of pages start_page to end_page (at most {MAX_READ_PAGES} pages per call). "
                    "Long output is cut; the reply says where to continue.",
     "parameters": _obj({"start_page": {"type": "integer"}, "end_page": {"type": "integer"}}, ["start_page"])},
    {"name": "read_section",
     "description": "Returns the text of a section from the document map by its id (for example s12). Long sections "
                    "are returned in parts; ask for part 2, 3 and so on.",
     "parameters": _obj({"section_id": {"type": "string"},
                         "part": {"type": "integer", "description": "Part number for long sections (default 1)."}},
                        ["section_id"])},
    {"name": "read_chunk",
     "description": "Returns the full text of chunks returned by search_document, by id.",
     "parameters": _obj({"chunk_ids": {"type": "array", "items": {"type": "string"}}}, ["chunk_ids"])},
    {"name": "record_fields",
     "description": "Saves field values (or marks fields not found). Recorded values are kept outside the "
                    "conversation and survive context compaction. Send several fields per call. A field can be marked "
                    "not_found only after you looked for it: a search_document or find_text with words from its "
                    "description, or a read of the pages where it should be (cite them in pages). Returns what was "
                    "saved, any field that was rejected and why, and the fields still pending.",
     "parameters": _obj({"fields": {"type": "array", "items": _obj({
         "path": {"type": "string", "description": "Field path exactly as listed in the task, for example contract.effective_date."},
         "status": {"type": "string", "enum": ["found", "not_found"]},
         "value_json": {"type": "string", "description": "The value encoded as JSON, matching the field type: "
                                                         "\"text\", 42, 3.5, true, \"2026-01-31\", or a JSON array for "
                                                         "array fields. Omit when status is not_found."},
         "pages": {"type": "array", "items": {"type": "integer"}, "description": "Pages where the value was found."},
         "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
         "note": {"type": "string", "description": "Short evidence or reason (for not_found: where you looked)."}},
         ["path", "status"])}}, ["fields"])},
    {"name": "get_pending_fields",
     "description": "Lists the fields that are still pending, with their types and descriptions.",
     "parameters": _obj({}, [])},
    {"name": "finish",
     "description": "Ends the extraction. Accepted only when every field is recorded or marked not_found.",
     "parameters": _obj({"summary": {"type": "string", "description": "One or two sentences on the outcome."}}, [])},
]


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False


class DocumentTools:
    def __init__(self, doc: Document, toc: TocResult, chunks: list[Chunk], index: HybridIndex, state: ExtractionState,
                 max_chars: Optional[int] = None):
        self.doc, self.toc, self.chunks, self.index, self.state = doc, toc, chunks, index, state
        self.chunk_by_id = {c.id: c for c in chunks}
        self.max_chars = max_chars or settings.extract_agent_max_tool_chars
        self.trace: list[dict] = []
        self.pages_read: set[int] = set()
        self.sections_read: set[str] = set()
        self.searches: list[str] = []
        self.search_words: set[str] = set()  # words of every search, to check not_found claims
        self.examined_pages: set[int] = set()  # pages read or returned by a search
        self.read_log: dict[str, list[int]] = {}  # read key -> sessions in which it was served
        self.finished_summary: Optional[str] = None
        self.calls = 0

    # -- helpers ------------------------------------------------------------------------------
    def _unit(self) -> str:
        return "Page" if self.doc.paginated else "Part"

    def overview(self) -> str:
        d = self.doc
        lines = [f"Document: {d.name} ({d.format.upper()}, {d.page_count} {'pages' if d.paginated else 'parts of about 3500 characters'}"
                 f"{f', {d.ocr_pages} read by OCR' if d.ocr_pages else ''})"]
        if self.toc.document_type:
            lines.append(f"Type: {self.toc.document_type}")
        if self.toc.summary:
            lines.append(f"Summary: {self.toc.summary}")
        src = {"bookmarks": "PDF bookmarks", "toc_page_rules": f"contents page(s) {self.toc.toc_pages}",
               "toc_page_llm": f"contents page(s) {self.toc.toc_pages} (found by the language model)",
               "headings": "headings in the text", "none": "none"}[self.toc.source]
        lines.append(f"Map source: {src}" + (f"; printed page numbers + {self.toc.page_offset} = physical pages"
                                             if self.toc.page_offset else ""))
        lines.append(f"Index: {len(self.chunks)} chunks, {self.index.mode} search")
        return "\n".join(lines)

    def map_text(self, max_level: Optional[int] = None, limit: Optional[int] = None) -> str:
        entries = [e for e in self.toc.entries if max_level is None or e.level <= max_level]
        if not entries:
            return "(no table of contents or headings were found: use search_document and find_text)"
        rows = []
        for e in entries:
            pages = f"p{e.page}" + (f"-{e.end_page}" if e.end_page and e.end_page != e.page else "") if e.page else "p?"
            rows.append(f"{'  ' * (e.level - 1)}{e.id} [{pages}] {e.title}")
        text = "\n".join(rows)
        limit = limit or self.max_chars
        if len(text) > limit:
            shallow = max(1, min(e.level for e in entries))
            if max_level is None and any(e.level > shallow for e in entries):
                return self.map_text(max_level=shallow, limit=limit) + (
                    f"\n(map shortened to level {shallow}; call get_document_map with max_level for more)")
            text = text[:limit] + "\n(map cut; use search_document to locate other sections)"
        return text

    def _clip(self, text: str, hint: str = "") -> str:
        if len(text) <= self.max_chars:
            return text
        return text[: self.max_chars] + f"\n[output cut at {self.max_chars} characters{'; ' + hint if hint else ''}]"

    def pending_text(self) -> str:
        pending = self.state.pending()
        if not pending:
            return "No pending fields."
        lines = []
        for path in pending:
            d = self.state.defs[path]
            extra = f" one of {d.enum}" if d.enum else ""
            if d.type == "array" and d.items:
                from shared.extract_spec import describe_type
                extra += f" ({describe_type({'type': 'array', 'items': d.items})})"
            ctx = f" [in: {d.context}]" if d.context else ""
            lines.append(f"- {path} ({d.type}{extra}): {d.description}{ctx}")
        return f"{len(pending)} pending:\n" + "\n".join(lines)

    # -- tools ---------------------------------------------------------------------------------
    def t_get_document_map(self, max_level=None) -> ToolOutput:
        level = int(max_level) if max_level else None
        return ToolOutput(self.overview() + "\n\nMap:\n" + self.map_text(level))

    async def t_search_document(self, query, top_k=6, page_from=None, page_to=None, section_id=None) -> ToolOutput:
        query = str(query or "").strip()
        if not query:
            return ToolOutput("query is required.", True)
        top_k = max(1, min(12, int(top_k or 6)))
        hits = await self.index.search(query, top_k, int(page_from) if page_from else None,
                                       int(page_to) if page_to else None, section_id or None)
        self.searches.append(query)
        self.search_words |= _words(query)
        for h in hits:
            self.examined_pages.update(range(h.chunk.page_start, h.chunk.page_end + 1))
        if not hits:
            return ToolOutput(f'No results for "{query}".')
        snippet = max(250, min(700, self.max_chars // top_k - 120))
        rows = []
        for n, h in enumerate(hits, start=1):
            c = h.chunk
            sec = f' section {c.section_id} "{c.section_title[:60]}"' if c.section_id else ""
            body = re.sub(r"\s+", " ", c.text)[:snippet]
            rows.append(f"{n}. [{c.id}] {c.pages_label()}{sec}\n   {body}")
        return ToolOutput(self._clip(f'Results for "{query}" ({self.index.mode} ranking):\n' + "\n".join(rows)))

    def t_find_text(self, text, regex=False, page_from=None, page_to=None, max_hits=20) -> ToolOutput:
        text = str(text or "")
        if not text.strip():
            return ToolOutput("text is required.", True)
        try:
            pattern = re.compile(text if regex else re.escape(text), re.IGNORECASE)
        except re.error as exc:
            return ToolOutput(f"Invalid regular expression: {exc}", True)
        max_hits = max(1, min(50, int(max_hits or 20)))
        lo, hi = int(page_from or 1), int(page_to or self.doc.page_count)
        hits, total = [], 0
        for page in self.doc.pages:
            if not lo <= page.number <= hi:
                continue
            lines = page.text.splitlines()
            for i, line in enumerate(lines):
                if pattern.search(line):
                    total += 1
                    if len(hits) < max_hits:
                        context = " / ".join(x.strip() for x in lines[max(0, i - 1):i + 2] if x.strip())
                        hits.append(f"{self._unit()} {page.number}: {context[:400]}")
                    self.examined_pages.add(page.number)
        self.searches.append(f"find:{text}")
        self.search_words |= _words(text)
        if not hits:
            return ToolOutput(f'"{text}" was not found{f" on pages {lo}-{hi}" if (page_from or page_to) else ""}.')
        more = f"\n({total - len(hits)} more matches not shown; narrow the page range)" if total > len(hits) else ""
        return ToolOutput(self._clip(f'{total} match(es) for "{text}":\n' + "\n".join(hits) + more))

    def t_read_pages(self, start_page, end_page=None) -> ToolOutput:
        try:
            start = int(start_page)
            end = int(end_page) if end_page else start
        except (TypeError, ValueError):
            return ToolOutput("start_page and end_page must be page numbers.", True)
        if start < 1 or start > self.doc.page_count:
            return ToolOutput(f"start_page must be between 1 and {self.doc.page_count}.", True)
        end = max(start, min(end, self.doc.page_count, start + MAX_READ_PAGES - 1))
        out, used = [], 0
        for n in range(start, end + 1):
            block = f"=== {self._unit()} {n} ===\n{self.doc.page(n).text}"
            if used and used + len(block) > self.max_chars:
                out.append(f"[stopped before {self._unit().lower()} {n} to keep the output short; call read_pages "
                           f"with start_page={n}]")
                break
            if len(block) > self.max_chars:
                block = block[: self.max_chars] + f"\n[{self._unit().lower()} {n} cut at {self.max_chars} characters]"
            out.append(block)
            used += len(block)
            self.pages_read.add(n)
        return ToolOutput("\n\n".join(out))

    def t_read_section(self, section_id, part=1) -> ToolOutput:
        entry = self.toc.section(str(section_id or "").strip())
        if entry is None or entry.page is None:
            return ToolOutput(f"Unknown section '{section_id}'. Section ids are listed by get_document_map.", True)
        part = max(1, int(part or 1))
        end = entry.end_page or entry.page
        text = "\n\n".join(f"=== {self._unit()} {n} ===\n{self.doc.page(n).text}" for n in range(entry.page, end + 1))
        # start at the section title when it can be located on the first page
        words = [w for w in re.findall(r"\w+", entry.title) if len(w) > 1][:6]
        if words:
            m = re.search(r"\W+".join(map(re.escape, words)), text, re.IGNORECASE)
            if m and m.start() > 0:
                header_end = text.find("\n") + 1
                text = text[:header_end] + text[m.start():]
        size = self.max_chars - 200
        parts = max(1, -(-len(text) // size))
        if part > parts:
            return ToolOutput(f"Section {entry.id} has {parts} part(s).", True)
        body = text[(part - 1) * size: part * size]
        self.sections_read.add(entry.id)
        self.pages_read.update(range(entry.page, end + 1))
        more = f"\n[part {part} of {parts}; call read_section with part={part + 1} for more]" if part < parts else ""
        return ToolOutput(f'Section {entry.id} "{entry.title}" ({self._unit().lower()}s {entry.page}-{end}), part {part} of {parts}:\n{body}{more}')

    def t_read_chunk(self, chunk_ids) -> ToolOutput:
        ids = chunk_ids if isinstance(chunk_ids, list) else [chunk_ids]
        out, missing = [], []
        for cid in ids[:8]:
            c = self.chunk_by_id.get(str(cid).strip())
            if c is None:
                missing.append(str(cid))
                continue
            out.append(f"=== [{c.id}] {c.pages_label()}{' ' + c.section_title if c.section_title else ''} ===\n{c.text}")
            self.pages_read.update(range(c.page_start, c.page_end + 1))
        if missing and not out:
            return ToolOutput(f"Unknown chunk id(s): {', '.join(missing)}", True)
        note = f"\n(unknown ids: {', '.join(missing)})" if missing else ""
        return ToolOutput(self._clip("\n\n".join(out) + note))

    @staticmethod
    def _field_entries(fields) -> Optional[list]:
        """
        The `fields` argument as a list. Smaller models sometimes send the array JSON-encoded as a string, or a
        single object; both are accepted. Returns None when nothing usable was sent.
        """
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except ValueError:
                return None
        if isinstance(fields, dict):
            fields = [fields]
        return fields if isinstance(fields, list) and fields else None

    def t_record_fields(self, fields, session: int) -> ToolOutput:
        entries = self._field_entries(fields)
        if entries is None:
            received = "nothing" if fields in (None, "", []) else f"a {type(fields).__name__}: {str(fields)[:120]}"
            return ToolOutput("fields must be a JSON array of objects such as "
                              '[{"path": "effective_date", "status": "found", "value_json": "\"2026-03-01\"", "pages": [8]}]; '
                              f"received {received}.", True)
        fields = entries
        saved, rejected = [], []
        for entry in fields:
            if not isinstance(entry, dict):
                rejected.append("each entry must be an object with path and status")
                continue
            path = str(entry.get("path", "")).strip().lstrip("$.")
            if (str(entry.get("status") or "").lower() == "not_found" and path in self.state.defs
                    and not self._looked_for(path, entry.get("pages"))):
                d = self.state.defs[path]
                hint = ", ".join(sorted(_words(path.replace(".", " ").replace("_", " ")) | _words(d.description))[:6])
                rejected.append(f"{path}: not marked not_found yet, because it was not searched for. Use search_document "
                                f"or find_text with words such as {hint}, or read the section where it should be, then "
                                "mark it not_found citing the pages you checked.")
                continue
            ok, message = self.state.record(entry, session)
            (saved if ok else rejected).append(message)
        c = self.state.counts()
        text = []
        if saved:
            text.append("Saved:\n- " + "\n- ".join(saved))
        if rejected:
            text.append("Rejected:\n- " + "\n- ".join(rejected))
        text.append(f"Progress: {c['found']} found, {c['not_found']} not found, {c['pending']} pending of {c['total']}.")
        if c["pending"]:
            pending = self.state.pending()
            text.append("Pending: " + ", ".join(pending[:40]) + (" ..." if len(pending) > 40 else ""))
        else:
            text.append("All fields are resolved. Call finish.")
        return ToolOutput("\n".join(text), is_error=bool(rejected) and not saved)

    def _looked_for(self, path: str, pages) -> bool:
        """Evidence that a field was searched for before it is declared not found."""
        cited = set()
        for p in pages or []:
            try:
                cited.add(int(p))
            except (TypeError, ValueError):
                continue
        if cited and cited <= (self.examined_pages | self.pages_read):
            return True
        d = self.state.defs[path]
        terms = _words(path.replace(".", " ").replace("_", " ")) | _words(d.description) | _words(d.context)
        return bool(terms & self.search_words)

    @staticmethod
    def _read_key(name: str, args: dict) -> Optional[str]:
        if name == "read_section":
            try:
                part = int(args.get("part") or 1)
            except (TypeError, ValueError):
                part = 1
            return f"section {str(args.get('section_id') or '').strip()} part {part}"
        if name == "read_pages":
            start = args.get("start_page")
            return f"pages {start}-{args.get('end_page') or start}"
        if name == "read_chunk":
            ids = args.get("chunk_ids") or []
            return "chunks " + ",".join(sorted(map(str, ids if isinstance(ids, list) else [ids])))
        return None

    def _repeat_guard(self, name: str, args: dict, session: int) -> tuple:
        """
        Stops reading loops. A read already done in this session is pointed to; a read from an earlier session is
        served once more with a reminder to record from it; after that it is refused.
        """
        key = self._read_key(name, args)
        if not key:
            return None, ""
        earlier = self.read_log.get(key, [])
        if session in earlier:
            return ToolOutput(f"You already read {key} in this session: its text is above in this conversation. Use "
                              "it, record the values with record_fields, or read something else."), ""
        prior = sorted(set(earlier))
        if len(prior) >= 2:
            return ToolOutput(f"{key} was already read in sessions {', '.join(map(str, prior))}. Its full text is not "
                              "returned again, to avoid a loop. Use find_text with page_from and page_to for a specific "
                              f"value, or read another section.\n{self.pending_text()}", True), ""
        note = (f"Note: you already read {key} in session {prior[0]}. Record every value you need from it with "
                "record_fields now: it will not be shown in full again.\n") if prior else ""
        return None, note

    def t_finish(self, summary="") -> ToolOutput:
        pending = self.state.pending()
        if pending:
            return ToolOutput(f"Cannot finish: {len(pending)} field(s) are still pending: {', '.join(pending[:30])}. "
                              "Record them or mark them not_found first.", True)
        self.finished_summary = str(summary or "")[:1000]
        return ToolOutput("Extraction finished.")

    async def execute(self, call: ToolCall, session: int, turn: int) -> ToolOutput:
        started = time.perf_counter()
        args = call.arguments or {}
        self.calls += 1
        try:
            guard, note = self._repeat_guard(call.name, args, session) if call.name in READ_TOOLS else (None, "")
            if "__unparsed__" in args:
                out = ToolOutput("The tool arguments were not valid JSON. Send them again.", True)
            elif guard is not None:
                out = guard
            elif call.name == "get_document_map":
                out = self.t_get_document_map(args.get("max_level"))
            elif call.name == "search_document":
                out = await self.t_search_document(args.get("query"), args.get("top_k", 6), args.get("page_from"),
                                                   args.get("page_to"), args.get("section_id"))
            elif call.name == "find_text":
                out = self.t_find_text(args.get("text"), bool(args.get("regex")), args.get("page_from"),
                                       args.get("page_to"), args.get("max_hits", 20))
            elif call.name == "read_pages":
                out = self.t_read_pages(args.get("start_page"), args.get("end_page"))
            elif call.name == "read_section":
                out = self.t_read_section(args.get("section_id"), args.get("part", 1))
            elif call.name == "read_chunk":
                out = self.t_read_chunk(args.get("chunk_ids") or [])
            elif call.name == "record_fields":
                out = self.t_record_fields(args.get("fields"), session)
            elif call.name == "get_pending_fields":
                out = ToolOutput(self.pending_text())
            elif call.name == "finish":
                out = self.t_finish(args.get("summary", ""))
            else:
                out = ToolOutput(f"Unknown tool '{call.name}'.", True)
            if call.name in READ_TOOLS and guard is None and not out.is_error:
                self.read_log.setdefault(self._read_key(call.name, args), []).append(session)
                if note:
                    out = ToolOutput(note + out.content)
        except Exception as exc:  # a tool bug must not end the job; the agent sees the error and continues
            log.warning("[agent] tool %s failed: %s", call.name, exc)
            out = ToolOutput(f"The tool failed: {type(exc).__name__}: {str(exc)[:200]}", True)
        if len(self.trace) < TRACE_LIMIT:
            shown = {k: v for k, v in args.items() if k != "fields"}
            if call.name == "record_fields":
                entries = self._field_entries(args.get("fields"))
                if entries is not None:
                    shown["fields"] = [f"{f.get('path')}:{f.get('status')}" for f in entries if isinstance(f, dict)]
                elif "fields" in args:  # keep what the model sent, so failures are visible in the trace
                    shown["fields_raw"] = str(args.get("fields"))[:200]
            self.trace.append({"session": session, "turn": turn, "tool": call.name, "args": shown,
                               "result": out.content[:240], "result_chars": len(out.content), "is_error": out.is_error,
                               "ms": int((time.perf_counter() - started) * 1000)})
        return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    priced: bool = True

    def add(self, turn: AgentTurn, info: ModelInfo):
        self.calls += 1
        self.input_tokens += turn.input_tokens
        self.output_tokens += turn.output_tokens
        self.cache_read_tokens += turn.cache_read_tokens
        self.cache_write_tokens += turn.cache_write_tokens
        if info.in_catalog:
            self.cost_usd += info.call_cost(input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
                                            cache_read_tokens=turn.cache_read_tokens,
                                            cache_write_tokens=turn.cache_write_tokens)
        else:
            self.priced = False

    def public(self) -> dict:
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens, "cache_write_tokens": self.cache_write_tokens,
                "cost_usd": round(self.cost_usd, 6) if self.priced else None, "priced": self.priced}


class AgentRunner:
    def __init__(self, *, spec: Spec, doc: Document, toc: TocResult, tools: DocumentTools, state: ExtractionState,
                 primary: BaseProvider, fallback: Optional[BaseProvider], instructions: str = "",
                 window_override: Optional[int] = None, progress: Callable = lambda *a, **k: None,
                 max_turns: Optional[int] = None, max_sessions: Optional[int] = None,
                 max_output_tokens: Optional[int] = None):
        self.spec, self.doc, self.toc, self.tools, self.state = spec, doc, toc, tools, state
        self.primary, self.fallback = primary, fallback
        self.instructions = instructions
        self.window_override = window_override
        self.progress = progress
        self.max_turns = max_turns or settings.extract_agent_max_turns
        self.max_sessions = max_sessions or settings.extract_agent_max_sessions
        self.max_output = max_output_tokens or settings.extract_agent_max_output_tokens
        self.system = _system_prompt()
        self.usage: dict[str, Usage] = {}
        self.sessions: list[dict] = []
        self.handoff_notes: list[str] = []
        self.warnings: list[str] = []

    # -- messages ------------------------------------------------------------------------------
    def _fields_text(self) -> str:
        lines = []
        for d in self.spec.fields:
            extra = f", one of {d.enum}" if d.enum else ""
            if d.format:
                extra += f", format {d.format}"
            if d.type == "array" and d.items:
                from shared.extract_spec import describe_type
                extra += f", {describe_type({'type': 'array', 'items': d.items})}"
            ctx = f" [in: {d.context}]" if d.context else ""
            lines.append(f"- {d.path} ({d.type}{extra}){' REQUIRED' if d.required else ''}: {d.description}{ctx}")
        return "\n".join(lines)

    def task_message(self, checkpoint: Optional[str]) -> str:
        spec_head = ""
        if self.spec.title or self.spec.description:
            spec_head = f"Extraction target: {self.spec.title}{' - ' if self.spec.title and self.spec.description else ''}{self.spec.description}\n"
        parts = [
            f"Extract the fields below from the document.\n{spec_head}",
            "## Document\n" + self.tools.overview(),
            "## Document map\n" + self.tools.map_text(limit=MAX_MAP_CHARS_IN_TASK),
        ]
        if checkpoint:
            parts.append(checkpoint)
        else:
            parts.append(f"## Fields to extract ({len(self.spec.fields)})\n{self._fields_text()}")
        if self.instructions:
            parts.append("## Additional instructions from the requester\n" + self.instructions)
        parts.append("Use the tools to locate and read the relevant passages, record values with record_fields as "
                     "you confirm them, and call finish when every field is resolved.")
        return "\n\n".join(parts)

    def checkpoint(self, session_no: int, reason: str, notes: str) -> str:
        found = [s for s in self.state.fields.values() if s.status == "found"]
        missing = [s for s in self.state.fields.values() if s.status == "not_found"]
        lines = [f"## Checkpoint: session {session_no} ended ({reason}). Resume from here.",
                 f"### Recorded fields ({len(found)}): already saved, do not redo"]
        lines += [f"- {s.path} = {_preview(s.value)}" + (f" (pages {_ranges(s.pages)})" if s.pages else "") for s in found] or ["- none"]
        lines.append(f"### Marked not found ({len(missing)})")
        lines += [f"- {s.path}: {s.note or 'no note'}" for s in missing] or ["- none"]
        lines.append("### Fields still to extract")
        lines.append(self.tools.pending_text())
        searches = list(dict.fromkeys(self.tools.searches))[-40:]
        lines.append("### Already done")
        lines.append("Searches: " + ("; ".join(f'"{q}"' for q in searches) if searches else "none"))
        lines.append("Pages read: " + (_ranges(self.tools.pages_read) or "none"))
        if self.tools.sections_read:
            lines.append("Sections read: " + ", ".join(sorted(self.tools.sections_read, key=lambda s: int(s[1:]) if s[1:].isdigit() else 0)))
        if notes:
            lines.append("### Notes from the previous session\n" + notes.strip())
        return "\n".join(lines)

    # -- loop ------------------------------------------------------------------------------------
    def _usage_for(self, engine: BaseProvider) -> tuple[Usage, ModelInfo]:
        info = find_model(engine.name, engine.model)
        return self.usage.setdefault(f"{engine.name}:{engine.model}", Usage()), info

    def _report(self, session_no: int, turn_no: int):
        c = self.state.counts()
        resolved = c["found"] + c["not_found"]
        self.progress("agent", 35 + int(60 * resolved / max(1, c["total"])),
                      f"Session {session_no}, turn {turn_no}: {resolved} of {c['total']} fields resolved")

    async def _handoff(self, engine: BaseProvider, history: list, results: list) -> str:
        history.append({"role": "tool", "results": results, "text": HANDOFF_REQUEST})
        try:
            turn = await engine.tool_chat(self.system, history, TOOL_SPECS, max_tokens=4000)
        except LLMError as exc:
            self.warnings.append(f"Handoff notes could not be written: {str(exc)[:160]}")
            return ""
        usage, info = self._usage_for(engine)
        usage.add(turn, info)
        return (turn.text or "").strip()[:3000]

    async def run(self) -> dict:
        engine = self.primary
        info = find_model(engine.name, engine.model)
        window = usable_window(info, self.window_override)
        threshold = compaction_threshold()
        checkpoint: Optional[str] = None
        turns_total = 0
        finished = stopped = False
        while not finished and not stopped and len(self.sessions) < self.max_sessions and turns_total < self.max_turns:
            session_no = len(self.sessions) + 1
            limit = int(window * threshold)
            session = {"session": session_no, "provider": engine.name, "model": engine.model, "usable_window": window,
                       "compact_at_tokens": limit, "turns": 0, "tool_calls": 0, "peak_context_tokens": 0,
                       "start_context_tokens": 0, "ended": "", "fields_resolved_at_start": len(self.state.fields) - len(self.state.pending())}
            history: list = [{"role": "user", "content": self.task_message(checkpoint)}]
            session["start_context_tokens"] = approx_tokens(self.system + history[0]["content"])
            nudges = 0
            while True:
                if turns_total >= self.max_turns:
                    session["ended"] = "turn_limit"
                    break
                try:
                    turn = await engine.tool_chat(self.system, history, TOOL_SPECS, max_tokens=self.max_output)
                except LLMError as exc:
                    if self.fallback is not None and engine is self.primary:
                        self.warnings.append(f"Primary engine {engine.name}:{engine.model} failed ({str(exc)[:160]}); "
                                             f"continued on {self.fallback.name}:{self.fallback.model}.")
                        engine = self.fallback
                        info = find_model(engine.name, engine.model)
                        window = usable_window(info, self.window_override)
                        session["ended"] = "engine_failover"
                        checkpoint = self.checkpoint(session_no, "engine failover", "")
                        break
                    raise
                turns_total += 1
                session["turns"] += 1
                usage, info = self._usage_for(engine)
                usage.add(turn, info)
                session["peak_context_tokens"] = max(session["peak_context_tokens"], turn.context_tokens)
                history.append({"role": "assistant", "turn": turn})
                if turn.text and len(self.tools.trace) < TRACE_LIMIT:
                    self.tools.trace.append({"session": session_no, "turn": turns_total, "tool": "(message)",
                                             "args": {}, "result": turn.text[:240], "result_chars": len(turn.text),
                                             "is_error": False, "ms": 0})
                if not turn.tool_calls:
                    if self.state.done:
                        finished, session["ended"] = True, "completed"
                        break
                    nudges += 1
                    if nudges > 2:
                        session["ended"] = "agent_stopped"
                        stopped = True
                        self.warnings.append("The agent stopped before resolving every field.")
                        break
                    cut = " Your last reply was cut off by the output limit." if "max" in (turn.stop_reason or "").lower() or turn.stop_reason == "length" else ""
                    history.append({"role": "user", "content": f"{cut} Continue with the tools. {self.tools.pending_text()}\n"
                                                               "Record these fields or mark them not_found, then call finish."})
                    continue
                nudges = 0
                results = []
                finish_called = False
                for call in turn.tool_calls:
                    out = await self.tools.execute(call, session_no, turns_total)
                    session["tool_calls"] += 1
                    results.append({"id": call.id, "name": call.name, "content": out.content, "is_error": out.is_error,
                                    "native_id": call.native_id})
                    if call.name == "finish" and not out.is_error:
                        finish_called = True
                self._report(session_no, turns_total)
                if finish_called:
                    history.append({"role": "tool", "results": results})
                    finished, session["ended"] = True, "finished"
                    break
                projected = turn.context_tokens + sum(approx_tokens(r["content"]) for r in results) + 200
                if projected >= limit:
                    session["ended"] = "compacted"
                    session["compacted_at_tokens"] = projected
                    notes = await self._handoff(engine, history, results)
                    if notes:
                        self.handoff_notes.append(notes)
                    checkpoint = self.checkpoint(session_no, f"context reached {projected} of {window} usable tokens", notes)
                    break
                message = {"role": "tool", "results": results}
                recorded_now = any(c.name == "record_fields" for c in turn.tool_calls)
                if projected >= PRE_COMPACTION_SHARE * limit and not recorded_now and not session.get("warned"):
                    # Values that are only in the conversation are lost at compaction: ask for them first.
                    session["warned"] = True
                    message["text"] = (f"Context note: this session is at {int(100 * projected / limit)}% of its budget "
                                       "and will be compacted soon. Save every value you have already confirmed now "
                                       "with record_fields, then continue.")
                history.append(message)
            session["fields_resolved_at_end"] = len(self.state.fields) - len(self.state.pending())
            self.sessions.append(session)
            log.info("[agent] session %s ended: %s after %s turns (peak %s tokens)", session_no, session["ended"],
                     session["turns"], session["peak_context_tokens"])
        if not finished and not stopped:
            reason = "turn limit" if turns_total >= self.max_turns else "session limit"
            self.warnings.append(f"Stopped at the {reason} with {len(self.state.pending())} field(s) unresolved.")
        for path in self.state.pending():
            s = self.state.fields[path]
            s.status, s.note = "not_found", "not resolved before the agent stopped"
        total = Usage()
        for u in self.usage.values():
            for k in ("calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                setattr(total, k, getattr(total, k) + getattr(u, k))
            total.cost_usd += u.cost_usd
            total.priced = total.priced and u.priced
        return {
            "data": self.state.result(),
            "fields": self.state.field_rows(),
            "warnings": self.warnings,
            "complete": finished,
            "agent": {
                "finished": finished, "summary": self.tools.finished_summary, "turns": turns_total,
                "sessions": self.sessions, "compactions": sum(1 for s in self.sessions if s["ended"] == "compacted"),
                "context_threshold": threshold, "tool_calls": self.tools.calls,
                "pages_read": len(self.tools.pages_read), "handoff_notes": self.handoff_notes,
                "trace": self.tools.trace,
            },
            "usage": {"total": total.public(), "by_model": {k: u.public() for k, u in self.usage.items()}},
        }
