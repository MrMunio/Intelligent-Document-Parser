"""Locating detected entities in source text and in word boxes, and assigning placeholders."""
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from rapidfuzz import fuzz

from worker.phi.detector import Entity

FUZZY_THRESHOLD = 86
FUZZY_MIN_CHARS = 6
MIN_ENTITY_CHARS = 2
MIN_PROPAGATE_CHARS = 4

_WS = re.compile(r"\s+")


def squash(value: str) -> str:
    return _WS.sub("", value)


class LabelRegistry:
    """Same value and type always gets the same placeholder, e.g. every 'Sarah Mitchell' is [PATIENT_NAME_1]."""

    def __init__(self, style: str = "label"):
        self.style = style
        self._counters: dict[str, int] = {}
        self._labels: dict[tuple, str] = {}

    def label_for(self, etype: str, text: str) -> str:
        key = (etype, squash(text).lower())
        if key not in self._labels:
            self._counters[etype] = self._counters.get(etype, 0) + 1
            self._labels[key] = f"[{etype}_{self._counters[etype]}]"
        return self._labels[key]

    def replacement(self, etype: str, text: str) -> str:
        """Text written into text-based outputs."""
        label = self.label_for(etype, text)
        if self.style == "blackout":
            return "█" * max(3, min(len(text), 40))
        return label


# ---------------------------------------------------------------------------
# Plain text matching (TXT, MD, CSV, DOCX paragraphs ...)
# ---------------------------------------------------------------------------

@dataclass
class Span:
    start: int
    end: int
    entity: Entity
    match: str = "exact"


def _entity_regex(text: str, flags: int = 0) -> Optional[re.Pattern]:
    tokens = text.split()
    if not tokens:
        return None
    body = r"\s+".join(re.escape(token) for token in tokens)
    prefix = r"(?<![A-Za-z0-9])" if tokens[0][0].isalnum() else ""
    suffix = r"(?![A-Za-z0-9])" if tokens[-1][-1].isalnum() else ""
    return re.compile(prefix + body + suffix, flags)


def expand_entities(entities: list[Entity]) -> list[Entity]:
    """Adds per-line parts of multi-line entities so an address split over lines can still be located."""
    out = list(entities)
    seen = {(e.text, e.type) for e in entities}
    for entity in entities:
        if "\n" in entity.text:
            for part in entity.text.splitlines():
                part = part.strip(" ,;")
                if len(squash(part)) >= MIN_PROPAGATE_CHARS and (part, entity.type) not in seen:
                    seen.add((part, entity.type))
                    out.append(Entity(part, entity.type, entity.source))
    return out


def find_spans(text: str, entities: list[Entity]) -> tuple[list[Span], list[Entity]]:
    """Returns non-overlapping spans (longest match wins) and the entities that were not found."""
    candidates: list[Span] = []
    unmatched: list[Entity] = []
    for entity in entities:
        if len(squash(entity.text)) < MIN_ENTITY_CHARS:
            continue
        pattern = _entity_regex(entity.text)
        if pattern is None:
            continue
        hits = [Span(m.start(), m.end(), entity, "exact") for m in pattern.finditer(text)]
        if not hits:
            relaxed = _entity_regex(entity.text, re.IGNORECASE)
            hits = [Span(m.start(), m.end(), entity, "case_insensitive") for m in relaxed.finditer(text)]
        if hits:
            candidates.extend(hits)
        else:
            unmatched.append(entity)

    candidates.sort(key=lambda s: (-(s.end - s.start), s.start))
    taken = bytearray(len(text))
    accepted: list[Span] = []
    for span in candidates:
        if span.end <= span.start or any(taken[span.start:span.end]):
            continue
        taken[span.start:span.end] = b"\x01" * (span.end - span.start)
        accepted.append(span)
    accepted.sort(key=lambda s: s.start)
    return accepted, unmatched


def apply_spans(text: str, spans: list[Span], registry: LabelRegistry) -> tuple[str, list[dict]]:
    """Replaces spans in text. Returns the new text and one record per replacement."""
    out: list[str] = []
    records: list[dict] = []
    cursor = 0
    for span in spans:
        original = text[span.start:span.end]
        out.append(text[cursor:span.start])
        out.append(registry.replacement(span.entity.type, span.entity.text))
        records.append({
            "label": registry.label_for(span.entity.type, span.entity.text),
            "type": span.entity.type, "text": span.entity.text, "matched_text": original,
            "source": span.entity.source, "match": span.match, "start": span.start, "end": span.end,
        })
        cursor = span.end
    out.append(text[cursor:])
    return "".join(out), records


# ---------------------------------------------------------------------------
# Word box matching (PDF text layer, OCR output)
# ---------------------------------------------------------------------------

@dataclass
class Word:
    text: str
    bbox: tuple  # (x0, y0, x1, y1) in the page coordinate space
    line: Any = 0


@dataclass
class WordMatch:
    entity: Entity
    pieces: list  # [(word_index, char_start, char_end)]
    match: str = "exact"
    score: Optional[float] = None
    rects: list = field(default_factory=list)
    matched_text: str = ""


class WordIndex:
    """
    Concatenates the words of a page without whitespace and remembers which word each character came from.
    Matching on the squashed text makes the lookup independent of how OCR or the PDF tokenised the line.
    """

    def __init__(self, words: list[Word]):
        self.words = words
        parts: list[str] = []
        self.char_word: list[int] = []
        self.word_start: list[int] = []
        offset = 0
        for index, word in enumerate(words):
            token = squash(word.text)
            self.word_start.append(offset)
            parts.append(token)
            self.char_word.extend([index] * len(token))
            offset += len(token)
        self.text = "".join(parts)
        self.text_lower = self.text.lower()

    @staticmethod
    def _joined(a: str, b: str) -> bool:
        """True when two adjacent characters belong to the same token. OCR often drops the space between
        fields ("11/02/1979MRN:", "WhitfieldCase"), so a digit next to a letter, or a lowercase letter
        followed by a capital, counts as a boundary; "Ann" inside "Annabel" does not."""
        if a.isalpha() and b.isalpha():
            return not (a.islower() and b.isupper())
        return a.isdigit() and b.isdigit()

    def _boundary_ok(self, start: int, end: int, needle: str) -> bool:
        if start > 0 and self.word_start[self.char_word[start]] != start:
            if self._joined(self.text[start - 1], needle[0]):
                return False
        if end < len(self.text) and self.word_start[self.char_word[end]] != end:
            if self._joined(needle[-1], self.text[end]):
                return False
        return True

    def _pieces(self, start: int, end: int) -> list:
        pieces = []
        index = start
        while index < end:
            word_index = self.char_word[index]
            word_begin = self.word_start[word_index]
            word_len = len(squash(self.words[word_index].text))
            stop = min(end, word_begin + word_len)
            pieces.append((word_index, index - word_begin, stop - word_begin))
            index = stop
        return pieces

    def find(self, entity: Entity, allow_fuzzy: bool = False) -> list[WordMatch]:
        needle = squash(entity.text)
        if len(needle) < MIN_ENTITY_CHARS or not self.text:
            return []
        matches: list[WordMatch] = []
        for haystack, probe, kind in ((self.text, needle, "exact"),
                                      (self.text_lower, needle.lower(), "case_insensitive")):
            position = haystack.find(probe)
            while position != -1:
                end = position + len(probe)
                if self._boundary_ok(position, end, needle):
                    matches.append(WordMatch(entity, self._pieces(position, end), kind))
                    position = haystack.find(probe, end)
                else:
                    position = haystack.find(probe, position + 1)
            if matches:
                break
        if not matches and allow_fuzzy and len(needle) >= FUZZY_MIN_CHARS:
            matches = self._fuzzy(entity, needle)
        for match in matches:
            match.rects = rects_for(self.words, match.pieces)
            match.matched_text = " ".join(
                squash(self.words[w].text)[a:b] for w, a, b in match.pieces)
        return matches

    def _fuzzy(self, entity: Entity, needle: str) -> list[WordMatch]:
        """OCR may misread a character, so compare word windows of the entity's own length."""
        size = max(1, len(entity.text.split()))
        target = needle.lower()
        scored = []
        for width in {size, size + 1, max(1, size - 1)}:
            for start in range(0, len(self.words) - width + 1):
                window = "".join(squash(w.text) for w in self.words[start:start + width]).lower()
                if abs(len(window) - len(target)) > max(2, len(target) // 4):
                    continue
                score = fuzz.ratio(target, window)
                if score >= FUZZY_THRESHOLD:
                    scored.append((score, start, width))
        scored.sort(reverse=True)
        used: set = set()
        out: list[WordMatch] = []
        for score, start, width in scored:
            indices = set(range(start, start + width))
            if indices & used:
                continue
            used |= indices
            pieces = [(i, 0, len(squash(self.words[i].text))) for i in range(start, start + width)]
            out.append(WordMatch(entity, pieces, "fuzzy", round(float(score), 1)))
        return out


def rects_for(words: list[Word], pieces: list) -> list[tuple]:
    """One rectangle per text line covered by the match. Partial words get a proportional box."""
    by_line: dict[Any, list] = {}
    order: list[Any] = []
    for word_index, start, end in pieces:
        word = words[word_index]
        x0, y0, x1, y1 = word.bbox
        length = max(1, len(squash(word.text)))
        if start > 0 or end < length:
            width = x1 - x0
            x0, x1 = x0 + width * (start / length), x0 + width * (end / length)
        if word.line not in by_line:
            by_line[word.line] = [x0, y0, x1, y1]
            order.append(word.line)
        else:
            rect = by_line[word.line]
            rect[0], rect[1] = min(rect[0], x0), min(rect[1], y0)
            rect[2], rect[3] = max(rect[2], x1), max(rect[3], y1)
    return [tuple(by_line[line]) for line in order]


def resolve_overlaps(matches: list[WordMatch]) -> list[WordMatch]:
    """When two matches share a word character, the longer one wins."""
    ordered = sorted(matches, key=lambda m: -sum(end - start for _, start, end in m.pieces))
    taken: set = set()
    kept: list[WordMatch] = []
    for match in ordered:
        cells = {(w, c) for w, start, end in match.pieces for c in range(start, end)}
        if cells & taken:
            continue
        taken |= cells
        kept.append(match)
    kept.sort(key=lambda m: m.pieces[0][:2])
    return kept


def match_page(words: list[Word], page_entities: list[Entity], document_entities: list[Entity],
               allow_fuzzy: bool) -> tuple[list[WordMatch], list[Entity]]:
    """
    Finds every occurrence on a page. Entities detected on this page may be matched fuzzily; entities found
    elsewhere in the document are propagated here with exact matching only.
    """
    index = WordIndex(words)
    matches: list[WordMatch] = []
    unmatched: list[Entity] = []
    local = {(e.text, e.type) for e in page_entities}
    for entity in expand_entities(page_entities):
        found = index.find(entity, allow_fuzzy=allow_fuzzy)
        if found:
            matches.extend(found)
        elif (entity.text, entity.type) in local and "\n" not in entity.text:
            unmatched.append(entity)
    for entity in document_entities:
        if (entity.text, entity.type) in local or len(squash(entity.text)) < MIN_PROPAGATE_CHARS:
            continue
        matches.extend(index.find(entity, allow_fuzzy=False))
    return resolve_overlaps(matches), unmatched


def summarise(occurrences: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    unique: dict[str, dict] = {}
    for occ in occurrences:
        by_type[occ["type"]] = by_type.get(occ["type"], 0) + 1
        entry = unique.setdefault(occ["label"], {
            "label": occ["label"], "type": occ["type"], "text": occ["text"], "source": occ["source"],
            "occurrences": 0, "pages": [],
        })
        entry["occurrences"] += 1
        page = occ.get("page")
        if page is not None and page not in entry["pages"]:
            entry["pages"].append(page)
    entities = sorted(unique.values(), key=lambda e: (e["type"], e["label"]))
    for entry in entities:
        if not entry["pages"]:
            entry.pop("pages")
    return {
        "total_occurrences": len(occurrences),
        "unique_entities": len(entities),
        "by_type": dict(sorted(by_type.items())),
        "entities": entities,
    }
