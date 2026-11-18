"""Token and cost reports shared by every service."""
import pytest

from shared.llm import FailoverLLM, LLMResult
from shared.usage import merge_reports, no_usage, report_from_calls


def call(provider, model, i=0, o=0, r=0, w=0):
    return LLMResult(text="", input_tokens=i, output_tokens=o, provider=provider, model=model,
                     cache_read_tokens=r, cache_write_tokens=w)


def test_report_prices_each_call_from_the_catalog():
    rep = report_from_calls([call("openai", "gpt-5-mini", i=1_000_000, o=100_000, r=1_000_000),
                             call("openai", "gpt-5-mini", i=0, o=0),
                             call("ollama", "qwen3.5:4b", i=5000, o=500)])
    mini = rep["by_model"]["openai:gpt-5-mini"]
    assert mini["calls"] == 2 and mini["cost_usd"] == pytest.approx(0.25 + 0.20 + 0.025)
    assert rep["by_model"]["ollama:qwen3.5:4b"]["cost_usd"] == 0.0  # local engine: no token cost
    assert rep["total"]["calls"] == 3 and rep["total"]["priced"] and rep["total"]["input_tokens"] == 1_005_000
    assert rep["total"]["cost_usd"] == pytest.approx(0.475)


def test_unknown_models_are_counted_but_not_priced():
    rep = report_from_calls([call("openai_compatible", "my-model", i=10, o=5), call("openai", "gpt-5-mini", i=10)])
    assert rep["total"]["calls"] == 2 and rep["total"]["cost_usd"] is None and not rep["total"]["priced"]
    assert rep["by_model"]["openai:gpt-5-mini"]["cost_usd"] is not None


def test_merge_and_empty_reports():
    a = report_from_calls([call("anthropic", "claude-sonnet-5-5", i=1000, o=100)])
    b = report_from_calls([call("anthropic", "claude-sonnet-5-5", i=2000, o=200), call("anthropic", "claude-haiku-5-5", i=10)])
    merged = merge_reports(a, b, None)
    assert merged["by_model"]["anthropic:claude-sonnet-5-5"]["calls"] == 2
    assert merged["total"]["cost_usd"] == pytest.approx(a["total"]["cost_usd"] + b["total"]["cost_usd"])
    assert no_usage()["total"] == {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                                   "cache_write_tokens": 0, "cost_usd": 0.0, "priced": True}


def test_failover_client_keeps_a_call_log():
    llm = FailoverLLM.__new__(FailoverLLM)
    llm.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                 "engines": {}, "failovers": 0}
    llm.call_log = []
    llm._record(call("openai", "gpt-5-mini", i=7, o=3, r=2))
    assert report_from_calls(llm.call_log)["total"]["cache_read_tokens"] == 2
