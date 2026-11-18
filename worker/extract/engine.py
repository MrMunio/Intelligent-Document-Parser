"""
Data extraction service entry point: document in, JSON that follows the requester's spec out.

    load document (text layer, OCR, DOCX, text)
        |
        +-- approach "batch":   parts sized to the model window, processed in order, state carried forward
        |
        +-- approach "agentic": table of contents -> chunking strategy -> chunks -> embeddings (hybrid index)
                                -> tool-using agent with context compaction
        |
    result JSON (data, per-field status and evidence pages, document map, engine, usage and cost, trace)
"""
import json
import os
import time
from typing import Callable

from shared.config import settings
from shared.extract_spec import validate_spec
from shared import runtime_config
from shared.llm import FailoverLLM, build_extract_engines, build_provider
from shared.logger import setup_logger
from shared.usage import merge_reports, report_from_calls
from shared.model_catalog import compaction_threshold, find_model, usable_window
from worker.extract import batch as batch_approach
from worker.extract.agent import AgentRunner, DocumentTools, ExtractionState, tool_chars_for
from worker.extract.chunking import choose_strategy, chunk_document
from worker.extract.document import Document, load_document, load_text
from worker.extract.retrieval import HybridIndex, build_embedder
from worker.extract.toc import detect_toc

log = setup_logger("worker.extract.engine")

SCHEMA_VERSION = "1.0"


async def extract_from_document(doc: Document, spec_raw, options: dict, progress: Callable) -> dict:
    """Runs one extraction on an already loaded document. Shared by jobs and tests."""
    timings: dict[str, int] = {}
    t0 = time.perf_counter()
    spec = validate_spec(spec_raw)
    approach = options.get("approach", "agentic")
    instructions = options.get("instructions") or ""
    primary, fallback = build_extract_engines(options.get("model"))
    info = find_model(primary.name, primary.model)
    window = usable_window(info, options.get("context_window"))
    engine_info = {
        "provider": primary.name, "model": primary.model, "display_name": info.display_name,
        "model_context_window": info.context_window, "model_max_output_tokens": info.max_output_tokens,
        "usable_window": window, "context_threshold": compaction_threshold(),
        "context_cap_tokens": settings.extract_context_cap_tokens or None,
        "window_override": options.get("context_window"), "in_catalog": info.in_catalog,
        "fallback": f"{fallback.name}:{fallback.model}" if fallback else None,
        "reasoning_effort": primary.effort,
    }
    document = doc.describe()
    warnings = list(spec.warnings)
    requested = options.get("context_window")
    if requested and int(requested) > window:
        limit = ("OLLAMA_NUM_CTX" if primary.name == "ollama" and window == settings.ollama_num_ctx
                 else "EXTRACT_CONTEXT_CAP_TOKENS" if window == settings.extract_context_cap_tokens else "the model's input limit")
        warnings.append(f"The requested context window of {int(requested):,} tokens is larger than this engine can use: "
                        f"it ran with {window:,} tokens ({limit}).")
    toc_usage: dict = {}
    extra: dict = {}
    try:
        if approach == "batch":
            llm = FailoverLLM(primary, fallback)
            try:
                outcome = await batch_approach.run_batch(doc, spec, llm, info, window, instructions, progress)
            finally:
                await llm.aclose()
            usage = report_from_calls(llm.call_log)
            extra["batch"] = outcome["batch"]
            if llm.usage.get("failovers"):
                warnings.append(f"{llm.usage['failovers']} call(s) were served by the fallback engine.")
        else:
            # Document map: table of contents by bookmarks, rules or the language model
            progress("indexing", 26, "Looking for a table of contents")
            t = time.perf_counter()
            toc_cfg = runtime_config.get("extract_toc")
            toc_engine = toc_cfg.get("engine") or {}
            if toc_cfg["mode"] != "rules" and toc_engine.get("provider") and toc_engine.get("model"):
                toc_llm = FailoverLLM(build_provider(toc_engine["provider"], toc_engine.get("url"), toc_engine["model"],
                                                     effort="low"), None)
            else:
                toc_llm = FailoverLLM(primary, fallback)
            try:
                toc = await detect_toc(doc, toc_llm if toc_cfg["mode"] != "rules" else None, mode=toc_cfg["mode"])
            finally:
                if toc_llm.primary is not primary:
                    await toc_llm.aclose()
            engine_info["toc"] = {"mode": toc_cfg["mode"],
                                  "engine": (f"{toc_engine['provider']}:{toc_engine['model']}" if toc_engine.get("provider")
                                             else "extraction engine") if toc_cfg["mode"] != "rules" else None}
            toc_usage = report_from_calls(toc_llm.call_log)
            timings["toc_ms"] = int((time.perf_counter() - t) * 1000)

            # Chunks and hybrid index
            progress("indexing", 30, "Chunking and embedding the document")
            t = time.perf_counter()
            strategy, reason = choose_strategy(doc, toc)
            chunks = chunk_document(doc, toc, strategy, settings.extract_rag_chunk_tokens, settings.extract_rag_overlap_tokens)
            index = HybridIndex(chunks, build_embedder())
            await index.build()
            timings["index_ms"] = int((time.perf_counter() - t) * 1000)
            if index.note:
                warnings.append(index.note)
            document["toc"] = toc.public()
            document["chunking"] = {"strategy": strategy, "reason": reason, "chunks": len(chunks),
                                    "target_tokens": settings.extract_rag_chunk_tokens,
                                    "overlap_tokens": settings.extract_rag_overlap_tokens}
            document["tool_output_chars"] = tool_chars_for(window)
            document["retrieval"] = {"mode": index.mode,
                                     "embedding_model": index.embedder.label if index.embedder else None,
                                     "embedding_tokens": index.embedder.tokens_used if index.embedder else 0}

            state = ExtractionState(spec, doc.page_count)
            tools = DocumentTools(doc, toc, chunks, index, state, max_chars=tool_chars_for(window))
            runner = AgentRunner(spec=spec, doc=doc, toc=toc, tools=tools, state=state, primary=primary,
                                 fallback=fallback, instructions=instructions, window_override=options.get("context_window"),
                                 progress=progress)
            progress("agent", 35, "Agent started")
            outcome = await runner.run()
            usage = merge_reports(outcome["usage"], toc_usage)
            extra["agent"] = outcome["agent"]
        timings["extract_ms"] = int((time.perf_counter() - t0) * 1000)
    finally:
        await primary.aclose()
        if fallback is not None:
            await fallback.aclose()

    warnings.extend(outcome.get("warnings", []))
    fields = outcome["fields"]
    counts = {"total": len(fields), "found": sum(1 for f in fields if f["status"] == "found"),
              "not_found": sum(1 for f in fields if f["status"] == "not_found")}
    counts["coverage"] = round(counts["found"] / counts["total"], 4) if counts["total"] else 0.0
    return {
        "schema_version": SCHEMA_VERSION,
        "approach": approach,
        "complete": outcome.get("complete", True),
        "data": outcome["data"],
        "fields": fields,
        "summary": counts,
        "spec": {"title": spec.title, "field_count": len(spec.fields)},
        "document": document,
        "engine": engine_info,
        "usage": usage,
        "warnings": warnings,
        "timings_ms": timings,
        **extra,
    }


async def run_extract(job_id: str, input_path: str, file_name: str, fmt: str, options: dict,
                      progress: Callable) -> dict:
    t0 = time.perf_counter()
    if options.get("inline_text"):
        with open(input_path, encoding="utf-8") as f:
            doc = load_text(f.read(), file_name)
    else:
        doc = await load_document(input_path, file_name, fmt, options.get("ocr_mode", "auto"), progress)
    load_ms = int((time.perf_counter() - t0) * 1000)
    result = await extract_from_document(doc, options["spec"], options, progress)
    result["timings_ms"]["load_ms"] = load_ms
    result["timings_ms"]["total_ms"] = int((time.perf_counter() - t0) * 1000)
    result["job_id"] = job_id
    progress("finishing", 98, "Writing the result")
    stem = os.path.splitext(file_name or "document")[0] or "document"
    return {"result": result, "output_name": f"{stem}.extracted.json",
            "output_bytes": json.dumps(result["data"], indent=2, ensure_ascii=False).encode("utf-8")}
