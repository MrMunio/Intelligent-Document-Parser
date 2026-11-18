"""Redaction of text-based files (TXT, MD, CSV, JSON, XML, HTML, YAML, logs ...). Output keeps the format."""
import asyncio
import codecs
import re
import time
from typing import Callable

from shared.logger import setup_logger
from worker.phi.detector import Detector, Entity
from worker.phi.matching import LabelRegistry, apply_spans, expand_entities, find_spans, summarise

log = setup_logger("worker.phi.text")

PLACEHOLDER = re.compile(r"\[[A-Z0-9_]+_\d+\]")
DETECTION_UNIT_LINES = 60  # lines handed to the detector as one unit (it chunks further by word count)


def decode_bytes(data: bytes) -> tuple[str, str, bytes]:
    """Returns (text, encoding, bom). Tries BOMs, then UTF-8, then Windows-1252."""
    for bom, encoding in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF32_LE, "utf-32-le"),
                          (codecs.BOM_UTF32_BE, "utf-32-be"), (codecs.BOM_UTF16_LE, "utf-16-le"),
                          (codecs.BOM_UTF16_BE, "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(encoding, errors="replace"), encoding, bom
    try:
        return data.decode("utf-8"), "utf-8", b""
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "cp1252", b""
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1", b""


def line_col(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    last_newline = text.rfind("\n", 0, offset)
    return line, offset - last_newline


async def detect_in_text(text: str, detector: Detector) -> list[Entity]:
    """Runs detection over the whole text in parallel units and returns the de-duplicated entities."""
    lines = text.splitlines()
    units = ["\n".join(lines[i:i + DETECTION_UNIT_LINES]) for i in range(0, len(lines), DETECTION_UNIT_LINES)]
    results = await asyncio.gather(*(detector.detect(unit) for unit in units if unit.strip()))
    detector.raise_if_incomplete()
    merged: dict[tuple, Entity] = {}
    for group in results:
        for entity in group:
            merged.setdefault((entity.text, entity.type), entity)
    return list(merged.values())


async def redact_text_file(input_path: str, output_path: str, options: dict, detector: Detector,
                           registry: LabelRegistry, progress: Callable[[str, int, str], None]) -> dict:
    t0 = time.perf_counter()
    timings: dict[str, int] = {}
    with open(input_path, "rb") as handle:
        raw = handle.read()
    text, encoding, bom = decode_bytes(raw)

    detector.on_chunk = lambda done, total: progress(
        "detecting", int(10 + 75 * done / max(1, total)), f"Detecting entities: {done} of {total} segments")
    progress("detecting", 8, "Detecting entities")
    t_detect = time.perf_counter()
    entities = await detect_in_text(text, detector)
    timings["detect_ms"] = int((time.perf_counter() - t_detect) * 1000)

    progress("redacting", 90, "Applying redactions")
    t_apply = time.perf_counter()
    spans, unmatched = find_spans(text, expand_entities(entities))
    redacted, records = apply_spans(text, spans, registry)
    occurrences = []
    for i, record in enumerate(records, start=1):
        line, column = line_col(text, record["start"])
        occurrences.append({"id": i, **record, "line": line, "column": column})

    with open(output_path, "wb") as handle:
        handle.write(bom + redacted.encode(encoding, errors="replace"))
    timings["apply_ms"] = int((time.perf_counter() - t_apply) * 1000)

    # Verify: no located value may survive in the output.
    progress("verifying", 96, "Verifying the output")
    masked = PLACEHOLDER.sub(lambda m: " " * len(m.group(0)), redacted)
    residual_spans, _ = find_spans(masked, [s.entity for s in spans])
    residual = [{"type": s.entity.type, "line": line_col(redacted, s.start)[0]} for s in residual_spans]
    timings["total_ms"] = int((time.perf_counter() - t0) * 1000)

    summary = summarise(occurrences)
    summary.update({"lines": text.count("\n") + 1, "characters": len(text), "encoding": encoding})
    result = {
        "summary": summary,
        "entities": summary.pop("entities"),
        "occurrences": occurrences,
        "unmatched": [{"type": e.type, "text": e.text, "source": e.source} for e in unmatched
                      if "\n" not in e.text],
        "verification": {"status": "passed" if not residual else "failed", "residual": residual},
        "timings_ms": timings,
    }
    if options.get("inline_text"):
        result["redacted_text"] = redacted
    return result
