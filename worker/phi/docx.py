"""
DOCX redaction. The output is a DOCX with the original formatting.

The package is edited at the XML level so that everything Word can show is covered: body, tables,
headers, footers, footnotes, endnotes, comments, text boxes, tracked deletions, field codes and
hyperlink targets. A value split across several formatting runs is still found, because matching
runs on the concatenated text and edits are mapped back to the individual run nodes.
"""
import bisect
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Optional

from lxml import etree

from shared.logger import setup_logger
from worker.phi.detector import Detector, Entity
from worker.phi.matching import LabelRegistry, expand_entities, find_spans, squash, summarise
from worker.phi.text import detect_in_text

log = setup_logger("worker.phi.docx")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
T, P, TAB, BR, CR = f"{{{W}}}t", f"{{{W}}}p", f"{{{W}}}tab", f"{{{W}}}br", f"{{{W}}}cr"
NOBREAK, DELTEXT, INSTR = f"{{{W}}}noBreakHyphen", f"{{{W}}}delText", f"{{{W}}}instrText"
AUTHOR, INITIALS = f"{{{W}}}author", f"{{{W}}}initials"
TABS = f"{{{W}}}tabs"
TC = f"{{{W}}}tc"

TEXT_PARTS = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$")
RELS_PARTS = re.compile(r"^word/_rels/.+\.rels$")
PLACEHOLDER = re.compile(r"\[[A-Z0-9_]+_\d+\]")
CORE_FIELDS = {"creator", "lastModifiedBy", "title", "subject", "keywords", "description", "category"}
APP_FIELDS = {"Company", "Manager"}


@dataclass
class Node:
    element: Optional[etree._Element]  # None for separators (tab, line break, paragraph break)
    start: int
    text: str
    part: str = ""
    paragraph: int = 0
    edits: list = field(default_factory=list)  # [(local_start, local_end, replacement)]


def _part_label(name: str) -> str:
    return name.split("/")[-1].replace(".xml", "")


def _paragraph_separator(paragraph) -> str:
    """
    Table cells are joined with a tab and rows end with a newline, so a row reads as one line
    ("Case manager<TAB>Linda Harper<TAB>EMP-48213"). The detector needs that row context to tell
    what a bare name or number in a cell refers to.
    """
    cell = paragraph.getparent()
    if cell is None or cell.tag != TC:
        return "\n"
    last_paragraph = [child for child in cell if child.tag == P][-1]
    if paragraph is not last_paragraph:
        return " "
    row = cell.getparent()
    cells = [child for child in row if child.tag == TC] if row is not None else []
    return "\n" if not cells or cell is cells[-1] else "\t"


def _owning_paragraph(element):
    parent = element.getparent()
    while parent is not None and parent.tag != P:
        parent = parent.getparent()
    return parent


class DocxText:
    """Linear view of all visible text in the package, with a map back to the XML text nodes."""

    def __init__(self, parts: dict[str, etree._Element]):
        self.nodes: list[Node] = []
        self.side_nodes: list[tuple[str, etree._Element]] = []  # deleted text and field codes
        self.paragraph_count = 0
        buffer: list[str] = []
        offset = 0

        def add(element, text, part, paragraph):
            nonlocal offset
            self.nodes.append(Node(element, offset, text, part, paragraph))
            buffer.append(text)
            offset += len(text)

        for name, root in parts.items():
            label = _part_label(name)
            paragraph_index = 0
            for paragraph in root.iter(P):
                paragraph_index += 1
                self.paragraph_count += 1
                wrote = False
                for element in paragraph.iter(T, TAB, BR, CR, NOBREAK, DELTEXT, INSTR):
                    if _owning_paragraph(element) is not paragraph:
                        continue  # belongs to a nested paragraph (text box), handled on its own
                    if element.tag == T:
                        if element.text:
                            add(element, element.text, label, paragraph_index)
                            wrote = True
                    elif element.tag in (DELTEXT, INSTR):
                        if element.text:
                            self.side_nodes.append((label, element))
                    elif element.tag == TAB and element.getparent().tag == TABS:
                        continue  # tab stop definition, not text
                    elif element.tag == TAB:
                        add(None, "\t", label, paragraph_index)
                    elif element.tag == NOBREAK:
                        add(None, "-", label, paragraph_index)
                    else:
                        add(None, "\n", label, paragraph_index)
                if wrote:
                    add(None, _paragraph_separator(paragraph), label, paragraph_index)
            add(None, "\n", label, paragraph_index)
        self.text = "".join(buffer)
        self._starts = [n.start for n in self.nodes]

    def nodes_between(self, start: int, end: int) -> list[Node]:
        index = max(0, bisect.bisect_right(self._starts, start) - 1)
        out = []
        while index < len(self.nodes) and self.nodes[index].start < end:
            node = self.nodes[index]
            if node.start + len(node.text) > start:
                out.append(node)
            index += 1
        return out


def _apply_edits(node: Node) -> None:
    text = node.text
    for local_start, local_end, replacement in sorted(node.edits, key=lambda e: -e[0]):
        text = text[:local_start] + replacement + text[local_end:]
    node.element.text = text
    node.element.set(XML_SPACE, "preserve")


def _load_parts(archive: zipfile.ZipFile) -> dict[str, etree._Element]:
    names = [n for n in archive.namelist() if TEXT_PARTS.match(n)]
    names.sort(key=lambda n: (0 if n == "word/document.xml" else 1, n))
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    return {name: etree.fromstring(archive.read(name), parser) for name in names}


def _serialise(root) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _scrub_properties(name: str, data: bytes) -> bytes:
    wanted = CORE_FIELDS if name == "docProps/core.xml" else APP_FIELDS
    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError:
        return data
    for element in root.iter():
        if isinstance(element.tag, str) and etree.QName(element).localname in wanted:
            element.text = ""
    return _serialise(root)


def _scrub_relationships(data: bytes, needles: list[str]) -> tuple[bytes, int]:
    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError:
        return data, 0
    changed = 0
    for rel in root:
        if rel.get("TargetMode") == "External":
            target = squash(rel.get("Target", "")).lower()
            if any(n in target for n in needles):
                rel.set("Target", "about:blank")
                changed += 1
    return (_serialise(root), changed) if changed else (data, 0)


async def redact_docx(input_path: str, output_path: str, options: dict, detector: Detector,
                      registry: LabelRegistry, progress: Callable[[str, int, str], None]) -> dict:
    t0 = time.perf_counter()
    timings: dict[str, int] = {}
    try:
        archive = zipfile.ZipFile(input_path)
    except zipfile.BadZipFile as exc:
        raise ValueError("The file is not a valid DOCX document.") from exc
    with archive:
        if "word/document.xml" not in archive.namelist():
            raise ValueError("The file is not a valid DOCX document (word/document.xml is missing).")
        parts = _load_parts(archive)
        view = DocxText(parts)

        detector.on_chunk = lambda done, total: progress(
            "detecting", int(10 + 75 * done / max(1, total)), f"Detecting entities: {done} of {total} segments")
        progress("detecting", 8, "Detecting entities")
        t_detect = time.perf_counter()
        side_text = "\n".join(element.text for _, element in view.side_nodes)
        entities = await detect_in_text(view.text + ("\n" + side_text if side_text else ""), detector)
        timings["detect_ms"] = int((time.perf_counter() - t_detect) * 1000)

        progress("redacting", 90, "Applying redactions")
        t_apply = time.perf_counter()
        expanded = expand_entities(entities)
        spans, unmatched = find_spans(view.text, expanded)

        occurrences: list[dict] = []
        for span in spans:
            replacement = registry.replacement(span.entity.type, span.entity.text)
            touched = [n for n in view.nodes_between(span.start, span.end) if n.element is not None]
            if not touched:
                continue
            for i, node in enumerate(touched):
                local_start = max(0, span.start - node.start)
                local_end = min(len(node.text), span.end - node.start)
                node.edits.append((local_start, local_end, replacement if i == 0 else ""))
            first = touched[0]
            occurrences.append({
                "id": len(occurrences) + 1,
                "label": registry.label_for(span.entity.type, span.entity.text),
                "type": span.entity.type, "text": span.entity.text,
                "matched_text": view.text[span.start:span.end], "source": span.entity.source,
                "match": span.match, "part": first.part, "paragraph": first.paragraph,
            })
        for node in view.nodes:
            if node.edits:
                _apply_edits(node)

        # Tracked deletions and field codes are not visible text but still hold the original values.
        hidden_redactions = 0
        for label, element in view.side_nodes:
            side_spans, _ = find_spans(element.text, expanded)
            if side_spans:
                text = element.text
                for span in reversed(side_spans):
                    text = text[:span.start] + registry.replacement(span.entity.type, span.entity.text) + text[span.end:]
                element.text = text
                element.set(XML_SPACE, "preserve")
                hidden_redactions += len(side_spans)

        scrub = options.get("scrub_metadata", True)
        if scrub:
            for root in parts.values():
                for element in root.iter():
                    if element.get(AUTHOR) is not None:
                        element.set(AUTHOR, "Author")
                    if element.get(INITIALS) is not None:
                        element.set(INITIALS, "A")

        needles = [squash(e.text).lower() for e in entities if len(squash(e.text)) >= 4]
        links_scrubbed = 0
        images = 0
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as out:
            for info in archive.infolist():
                data = archive.read(info.filename)
                if info.filename in parts:
                    data = _serialise(parts[info.filename])
                elif RELS_PARTS.match(info.filename) and needles:
                    data, changed = _scrub_relationships(data, needles)
                    links_scrubbed += changed
                elif scrub and info.filename in ("docProps/core.xml", "docProps/app.xml"):
                    data = _scrub_properties(info.filename, data)
                elif info.filename.startswith("word/media/"):
                    images += 1
                out.writestr(info, data)
    timings["apply_ms"] = int((time.perf_counter() - t_apply) * 1000)

    # Verify against the file that was actually written.
    progress("verifying", 96, "Verifying the output")
    with zipfile.ZipFile(output_path) as written:
        check = DocxText(_load_parts(written))
    masked = PLACEHOLDER.sub(lambda m: " " * len(m.group(0)), check.text)
    residual_spans, _ = find_spans(masked, [s.entity for s in spans])
    residual = [{"type": s.entity.type} for s in residual_spans]
    timings["total_ms"] = int((time.perf_counter() - t0) * 1000)

    summary = summarise(occurrences)
    summary.update({
        "paragraphs": view.paragraph_count,
        "characters": len(view.text),
        "hidden_text_redactions": hidden_redactions,
        "hyperlinks_scrubbed": links_scrubbed,
        "embedded_images_not_scanned": images,
    })
    return {
        "summary": summary,
        "entities": summary.pop("entities"),
        "occurrences": occurrences,
        "unmatched": [{"type": e.type, "text": e.text, "source": e.source} for e in unmatched
                      if "\n" not in e.text],
        "verification": {"status": "passed" if not residual else "failed", "residual": residual},
        "warnings": ([f"{images} embedded image(s) were kept as they are. Text inside pictures is not redacted in "
                      f"DOCX files: export to PDF to have images scanned."] if images else []),
        "timings_ms": timings,
    }
