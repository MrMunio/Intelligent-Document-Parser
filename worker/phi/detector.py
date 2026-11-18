"""PHI / PII detection: prompt building, deterministic patterns, and concurrent LLM calls."""
import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Optional

from shared.config import settings
from shared.logger import setup_logger
from shared.phi_catalog import BUILTIN_ENTITIES

log = setup_logger("worker.phi.detector")

PROMPT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts", "phi_prompt.txt")


@dataclass(frozen=True)
class Entity:
    text: str
    type: str
    source: str  # "llm" or "pattern"


class DetectionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Deterministic patterns. They complement the LLM for rigidly structured identifiers.
# ---------------------------------------------------------------------------

_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
          r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")

PATTERNS: dict[str, list[re.Pattern]] = {
    "EMAIL": [re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")],
    "SSN": [re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")],
    "PHONE": [re.compile(r"(?<![\d(+])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)")],
    "URL": [re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]{}|]+[^\s<>\"'()\[\]{}|.,;:!?]", re.IGNORECASE)],
    "IP_ADDRESS": [re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")],
    "DATE": [
        re.compile(r"(?<![\d/-])(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:\d{4}|\d{2})(?![\d/-])"),
        re.compile(r"(?<![\d-])(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?![\d-])"),
        re.compile(rf"\b{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+(?:19|20)\d{{2}}\b"),
        re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}\.?,?\s+(?:19|20)\d{{2}}\b"),
    ],
}


def pattern_entities(text: str, enabled_types: set) -> list[Entity]:
    found: list[Entity] = []
    seen = set()
    for etype, patterns in PATTERNS.items():
        if etype not in enabled_types:
            continue
        for pattern in patterns:
            for match in pattern.finditer(text):
                value = match.group(0).strip()
                if value and (value, etype) not in seen:
                    seen.add((value, etype))
                    found.append(Entity(value, etype, "pattern"))
    return found


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_system_prompt(options: dict) -> str:
    with open(PROMPT_PATH, "r", encoding="utf-8") as handle:
        template = handle.read()
    descriptions = {e["label"]: e["description"] for e in BUILTIN_ENTITIES}
    lines = [f"  {label}: {descriptions[label]}" for label in options.get("entity_types", [])]
    entity_types = "\n".join(lines) if lines else "  (no built-in types selected for this request)"

    custom = options.get("custom_entities") or []
    custom_section = ""
    if custom:
        custom_lines = "\n".join(f"  {c['label']}: {c['description']}" for c in custom)
        custom_section = ("\nCUSTOM ENTITY TYPES defined by the user for this request. Treat them exactly like the "
                          "types above and apply each description literally:\n" + custom_lines + "\n")

    instructions = (options.get("instructions") or "").strip()
    instructions_section = ""
    if instructions:
        instructions_section = ("\nADDITIONAL GUIDANCE FROM THE USER (about what to detect only):\n  "
                                + instructions.replace("\n", "\n  ") + "\n")
    return template.format(entity_types=entity_types, custom_section=custom_section,
                           instructions_section=instructions_section).strip()


# ---------------------------------------------------------------------------
# Chunking and response parsing
# ---------------------------------------------------------------------------

def chunk_text(text: str, max_words: Optional[int] = None) -> list[str]:
    """Splits on line boundaries into chunks of at most max_words words. Oversized lines are split on spaces."""
    max_words = max_words or settings.phi_chunk_words
    chunks: list[str] = []
    current: list[str] = []
    count = 0

    def flush():
        nonlocal current, count
        if current:
            joined = "\n".join(current).strip()
            if joined:
                chunks.append(joined)
        current, count = [], 0

    for line in text.splitlines():
        words = line.split()
        if not words:
            if current:
                current.append("")
            continue
        if len(words) > max_words:
            flush()
            for i in range(0, len(words), max_words):
                chunks.append(" ".join(words[i:i + max_words]))
            continue
        if count + len(words) > max_words:
            flush()
        current.append(line)
        count += len(words)
    flush()
    return chunks


def parse_llm_entities(raw: str) -> Optional[list[dict]]:
    """Returns the entity dicts from a model response, or None when the response is not usable JSON."""
    if raw is None:
        return None
    text = raw.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    if not text:
        return []
    candidates = [text]
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            for key in ("entities", "phi", "results", "items"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
            else:
                lists = [v for v in data.values() if isinstance(v, list)]
                data = lists[0] if lists else ([data] if "text" in data else [])
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    # Last resort: salvage individual objects from a truncated or malformed response.
    salvaged = []
    for match in re.finditer(r"\{[^{}]*\}", text):
        try:
            item = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(item, dict) and "text" in item:
            salvaged.append(item)
    return salvaged or None


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class Detector:
    """One instance per job. `llm` is a shared.llm.FailoverLLM."""

    def __init__(self, options: dict, llm):
        self.options = options
        self.llm = llm
        self.builtin = set(options.get("entity_types") or [])
        self.custom = {c["label"] for c in options.get("custom_entities") or []}
        self.allowed = self.builtin | self.custom
        self.system_prompt = build_system_prompt(options)
        self.use_patterns = bool(options.get("use_pattern_detectors", True))
        self._sem: Optional[asyncio.Semaphore] = None
        self.chunks_total = 0
        self.chunks_done = 0
        self.chunks_failed = 0
        self.errors: list[str] = []
        self.parse_failures = 0
        self.on_chunk = None  # optional callback(done, total)

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(max(1, settings.phi_llm_concurrency))
        return self._sem

    def _normalise_type(self, raw_type: str) -> Optional[str]:
        label = re.sub(r"[^A-Za-z0-9]+", "_", str(raw_type or "")).strip("_").upper()
        if label in self.allowed:
            return label
        if "OTHER_ID" in self.allowed:
            return "OTHER_ID"
        return None

    async def _llm_chunk(self, chunk: str) -> list[Entity]:
        async with self._semaphore():
            user = f"<document>\n{chunk}\n</document>"
            items = None
            try:
                for attempt in range(2):
                    result = await self.llm.chat(self.system_prompt, user, json_mode=True, max_tokens=4096)
                    items = parse_llm_entities(result.text)
                    if items is not None:
                        break
                    self.parse_failures += 1
                    log.warning("[detector] model returned unparseable output (attempt %s)", attempt + 1)
                if items is None:
                    raise DetectionError("the model did not return valid JSON")
            except Exception as exc:
                self.chunks_failed += 1
                self.errors.append(f"{type(exc).__name__}: {exc}"[:300])
                log.error("[detector] chunk failed: %s", exc)
                return []
            finally:
                self.chunks_done += 1
                if self.on_chunk:
                    try:
                        self.on_chunk(self.chunks_done, self.chunks_total)
                    except Exception:
                        pass
        entities = []
        for item in items:
            # Models sometimes include the sentence punctuation after a value ("555-0134.").
            text = str(item.get("text", "")).strip().rstrip(".,;:")
            if text.count("(") > text.count(")"):
                text = text.rstrip("(").strip()
            etype = self._normalise_type(item.get("type", ""))
            if text and etype and len(text) <= 300:
                entities.append(Entity(text, etype, "llm"))
        return entities

    async def detect(self, text: str) -> list[Entity]:
        """Detects entities in one unit of text (a page, or a block of paragraphs)."""
        if not text or not text.strip():
            return []
        chunks = chunk_text(text)
        self.chunks_total += len(chunks)
        results = await asyncio.gather(*(self._llm_chunk(chunk) for chunk in chunks))
        merged: dict[str, Entity] = {}
        if self.use_patterns:
            for entity in pattern_entities(text, self.builtin):
                merged[entity.text] = entity
        for group in results:
            for entity in group:
                existing = merged.get(entity.text)
                # A user-defined type always wins. Otherwise a pattern hit keeps its precise type.
                if existing is None or entity.type in self.custom:
                    merged[entity.text] = entity
        return list(merged.values())

    def raise_if_incomplete(self) -> None:
        """A redaction that silently skipped part of the document is worse than a failed job."""
        if self.chunks_failed:
            first = self.errors[0] if self.errors else "unknown error"
            raise DetectionError(f"PHI detection failed for {self.chunks_failed} of {self.chunks_total} text "
                                 f"segments, so the document was not redacted. First error: {first}")

    def stats(self) -> dict:
        return {"segments": self.chunks_total, "parse_retries": self.parse_failures, **self.llm.usage}
