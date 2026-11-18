"""
Token usage and cost reports, the same shape for every service:

    {"total":    {"calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                  "cost_usd", "priced"},
     "by_model": {"provider:model": {...same keys...}}}

`input_tokens` are uncached prompt tokens; cache reads and writes are counted separately because they are priced
separately. Prices come from shared/model_catalog.json. `cost_usd` is null when any call used a model that is not
in the catalog (`priced` is then false); local models are in the catalog at no cost.
"""
from typing import Iterable, Optional

from shared.model_catalog import find_model

COUNTERS = ("calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _empty() -> dict:
    return {**{k: 0 for k in COUNTERS}, "cost_usd": 0.0, "priced": True}


def _finish(row: dict) -> dict:
    row["cost_usd"] = round(row["cost_usd"], 6) if row["priced"] else None
    return row


def report_from_calls(call_log: Iterable) -> dict:
    """Builds a report from LLMResult-like records (provider, model, token counts)."""
    by_model: dict[str, dict] = {}
    for res in call_log:
        key = f"{res.provider}:{res.model}"
        row = by_model.setdefault(key, _empty())
        row["calls"] += 1
        row["input_tokens"] += res.input_tokens
        row["output_tokens"] += res.output_tokens
        row["cache_read_tokens"] += res.cache_read_tokens
        row["cache_write_tokens"] += res.cache_write_tokens
        info = find_model(res.provider, res.model)
        if info.in_catalog:
            row["cost_usd"] += info.call_cost(input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                                              cache_read_tokens=res.cache_read_tokens,
                                              cache_write_tokens=res.cache_write_tokens)
        else:
            row["priced"] = False
    return merge_reports({"by_model": by_model})


def merge_reports(*reports: Optional[dict]) -> dict:
    """Adds reports together (for example the contents scan and the agent of one extraction)."""
    by_model: dict[str, dict] = {}
    for report in reports:
        for key, row in ((report or {}).get("by_model") or {}).items():
            acc = by_model.setdefault(key, _empty())
            for k in COUNTERS:
                acc[k] += row.get(k) or 0
            if row.get("cost_usd") is None or row.get("priced") is False:
                acc["priced"] = False
            else:
                acc["cost_usd"] += row["cost_usd"]
    total = _empty()
    for row in by_model.values():
        for k in COUNTERS:
            total[k] += row[k]
        total["priced"] = total["priced"] and row["priced"]
        total["cost_usd"] += row["cost_usd"] if row["priced"] else 0.0
    return {"total": _finish(total), "by_model": {k: _finish(v) for k, v in by_model.items()}}


def no_usage() -> dict:
    """For jobs that call no language model (plain OCR)."""
    return merge_reports()
