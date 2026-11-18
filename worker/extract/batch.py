"""
Approach 1, document batch processing.

The document is cut into parts that fit the model's usable window, and the parts are processed in order.
Each call receives the target schema, the JSON extracted so far (the state) and the next part, and returns
the updated JSON. The state is merged defensively (a null never erases a value found earlier; arrays are
merged without duplicates) and carried to the next part, so the final state covers the whole document even
when it is many times larger than the context window.

Part size: min(EXTRACT_BATCH_CHUNK_TOKENS, usable window x EXTRACT_CONTEXT_THRESHOLD - prompt - state - output).
"""
import json
import os
import re
import time
from typing import Callable, Optional

from shared.config import settings
from shared.extract_spec import Spec, conform, get_path, merge_results, strict_json_schema
from shared.logger import setup_logger
from shared.model_catalog import ModelInfo, compaction_threshold
from worker.extract.chunking import CHARS_PER_TOKEN, split_text
from worker.extract.document import Document, approx_tokens

log = setup_logger("worker.extract.batch")

PROMPT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts",
                           "extract_batch_prompt.txt")
MARKER = re.compile(r"^\[(?:Page|Part) (\d+)\]$", re.MULTILINE)


def _prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read().strip()


def parse_json_object(text: str) -> Optional[dict]:
    """Model output to a dict, tolerating code fences and text around the object."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            return value if isinstance(value, dict) else None
        except ValueError:
            return None
    return None


def plan_parts(doc: Document, part_tokens: int, overlap_tokens: int) -> list[dict]:
    """Splits the page-marked text into parts and records the pages each part covers."""
    text = doc.full_text(markers=True)
    markers = [(m.start(), int(m.group(1))) for m in MARKER.finditer(text)]

    def page_at(offset: int) -> int:
        page = markers[0][1] if markers else 1
        for start, number in markers:
            if start > offset:
                break
            page = number
        return page

    parts = []
    for start, end in split_text(text, int(part_tokens * CHARS_PER_TOKEN), int(overlap_tokens * CHARS_PER_TOKEN)):
        body = text[start:end].strip()
        if body:
            parts.append({"text": body, "page_start": page_at(start), "page_end": page_at(max(start, end - 1))})
    return parts


def part_budget(spec: Spec, model: ModelInfo, window: int, instructions: str) -> tuple[int, int]:
    """(part size in tokens, output tokens reserved per call)."""
    schema_tokens = approx_tokens(_prompt() + instructions + json.dumps(spec.schema))
    state_tokens = max(1500, approx_tokens(json.dumps(spec.empty_result())) * 4)  # values grow as fields fill
    output_reserve = min(model.max_output_tokens, max(4000, state_tokens * 2), 16000)
    usable = int(window * compaction_threshold()) - schema_tokens - state_tokens - output_reserve
    return max(1500, min(settings.extract_batch_chunk_tokens, usable)), output_reserve


async def run_batch(doc: Document, spec: Spec, llm, model: ModelInfo, window: int, instructions: str,
                    progress: Callable) -> dict:
    part_tokens, output_reserve = part_budget(spec, model, window, instructions)
    parts = plan_parts(doc, part_tokens, settings.extract_batch_overlap_tokens)
    system = _prompt() + (f"\n\nAdditional instructions from the requester:\n{instructions}" if instructions else "")
    schema_json = json.dumps(spec.schema, ensure_ascii=False)
    strict = strict_json_schema(spec.schema)
    state = spec.empty_result()
    evidence: dict[str, list[int]] = {}
    found_in_part: dict[str, int] = {}
    log_rows, warnings = [], []
    succeeded = 0
    for index, part in enumerate(parts, start=1):
        pages = f"{part['page_start']}-{part['page_end']}" if part["page_start"] != part["page_end"] else str(part["page_start"])
        progress("extracting", 30 + int(65 * (index - 1) / max(1, len(parts))),
                 f"Reading part {index} of {len(parts)} (pages {pages})")
        user = (f"Target schema:\n{schema_json}\n\n"
                f"Current state (JSON extracted from the earlier parts):\n{json.dumps(state, ensure_ascii=False)}\n\n"
                f"Document part {index} of {len(parts)} (pages {pages}):\n<document_part>\n{part['text']}\n</document_part>")
        started = time.perf_counter()
        data, error = None, None
        for attempt in range(2):
            try:
                res = await llm.chat(system, user, json_mode=True, json_schema=strict, max_tokens=output_reserve)
                data = parse_json_object(res.text)
                if data is not None:
                    break
                error = "the model did not return a JSON object"
            except Exception as exc:  # recorded per part; the state from earlier parts is kept
                error = f"{type(exc).__name__}: {str(exc)[:300]}"
        row = {"part": index, "pages": [part["page_start"], part["page_end"]], "approx_tokens": approx_tokens(part["text"]),
               "ms": int((time.perf_counter() - started) * 1000), "ok": data is not None}
        if data is None:
            row["error"] = error
            warnings.append(f"Part {index} (pages {pages}) could not be processed: {error}")
            log_rows.append(row)
            continue
        succeeded += 1
        merged = merge_results(state, conform(data, spec.schema), spec.schema)
        changed = []
        for f in spec.fields:
            before, after = get_path(state, f.path), get_path(merged, f.path)
            if after not in (None, []) and after != before:
                changed.append(f.path)
                evidence.setdefault(f.path, [])
                for p in (part["page_start"], part["page_end"]):
                    if p not in evidence[f.path]:
                        evidence[f.path].append(p)
                found_in_part.setdefault(f.path, index)
        row["fields_updated"] = changed
        log_rows.append(row)
        state = merged
    if succeeded == 0:
        raise RuntimeError(f"No part of the document could be processed. Last error: {log_rows[-1].get('error') if log_rows else 'none'}")
    fields = []
    for f in spec.fields:
        value = get_path(state, f.path)
        status = "found" if value not in (None, []) else "not_found"
        fields.append({"path": f.path, "type": f.type, "status": status, "value": value,
                       "pages": sorted(evidence.get(f.path, [])), "confidence": None,
                       "note": f"first set in part {found_in_part[f.path]}" if f.path in found_in_part else ""})
    return {"data": state, "fields": fields, "warnings": warnings, "complete": succeeded == len(parts),
            "batch": {"parts": len(parts), "part_tokens": part_tokens, "overlap_tokens": settings.extract_batch_overlap_tokens,
                      "output_reserve_tokens": output_reserve, "log": log_rows}}
