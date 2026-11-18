"""The model catalog JSON: completeness, lookups, usable window and cost."""
from datetime import date

import pytest

from shared import model_catalog as mc
from shared.config import settings


def test_catalog_entries_are_complete():
    cat = mc.catalog()
    assert {"anthropic", "openai", "gemini", "ollama"} <= set(cat["providers"])
    for name, provider in cat["providers"].items():
        ids = [m["id"] for m in provider["models"]]
        assert len(ids) == len(set(ids)), name
        for purpose, model_id in provider["recommended"].items():
            assert model_id in ids, (name, purpose, model_id)
        for m in provider["models"]:
            assert m["context_window"] > 0 and m["max_output_tokens"] > 0, m["id"]
            assert {"input", "output"} <= set(m["pricing"]), m["id"]
            assert m["status"] in {"current", "previous", "legacy", "preview", "deprecated"}, m["id"]


def test_recommended_reasoning_models_for_the_agent():
    assert mc.recommended("anthropic", "agentic") == "claude-sonnet-5-5"
    assert mc.recommended("openai", "agentic") == "gpt-6.1-sol"
    sonnet = mc.find_model("anthropic", "claude-sonnet-5-5")
    assert sonnet.context_window == 1_000_000 and sonnet.reasoning and sonnet.pricing["input"] == 2.0


def test_lookup_exact_prefix_alias_and_unknown():
    assert mc.find_model("openai", "gpt-6.2-sol").max_input_tokens == 922_000
    dated = mc.find_model("anthropic", "claude-haiku-4-5-20251001")
    assert dated.in_catalog and dated.context_window == 200_000 and dated.id == "claude-haiku-4-5-20251001"
    azure = mc.find_model("azure_openai", "gpt-6.2-sol")
    assert azure.in_catalog and azure.provider == "azure_openai"
    unknown = mc.find_model("openai_compatible", "my-local-llm")
    assert not unknown.in_catalog and unknown.context_window == mc.UNKNOWN_CONTEXT_WINDOW


def test_usable_window_respects_cap_override_and_local_context(monkeypatch):
    sonnet = mc.find_model("anthropic", "claude-sonnet-5-5")
    monkeypatch.setattr(settings, "extract_context_cap_tokens", 200_000)
    assert mc.usable_window(sonnet) == 200_000
    assert mc.usable_window(sonnet, 30_000) == 30_000
    monkeypatch.setattr(settings, "extract_context_cap_tokens", 0)
    assert mc.usable_window(sonnet) == sonnet.input_limit() == 1_000_000 - 128_000
    monkeypatch.setattr(settings, "ollama_num_ctx", 8192)
    assert mc.usable_window(mc.find_model("ollama", "qwen3.5:4b")) == 8192
    assert mc.usable_window(mc.find_model("openai", "gpt-6.2-sol")) == 922_000


@pytest.mark.parametrize("value, expected", [(0.7, 0.7), (0.05, 0.30), (1.5, 0.95)])
def test_threshold_is_kept_in_range(monkeypatch, value, expected):
    monkeypatch.setattr(settings, "extract_context_threshold", value)
    assert mc.compaction_threshold() == expected


def test_call_cost_with_cache_long_context_and_scheduled_prices():
    sonnet = mc.find_model("anthropic", "claude-sonnet-5-5")
    cost = sonnet.call_cost(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000,
                            cache_write_tokens=1_000_000)
    assert cost == pytest.approx(2.00 + 1.00 + 0.10 + 2.50)
    sol = mc.find_model("openai", "gpt-6.2-sol")
    assert sol.call_cost(input_tokens=100_000, output_tokens=0) == pytest.approx(0.2)
    assert sol.call_cost(input_tokens=300_000, output_tokens=0) == pytest.approx(1.2)  # > 272K: long-context price
    argon = mc.find_model("gemini", "gemini-4-argon")
    assert argon.prices_on(date(2026, 11, 30))["input"] == 2.0
    assert argon.prices_on(date(2026, 12, 1))["input"] == 4.0


def test_embedding_models_have_prefixes():
    qwen = mc.embedding_model("ollama", "qwen3-embedding:0.6b")
    assert qwen["dimensions"] == 1024 and qwen["query_prefix"].startswith("Instruct:")
    assert mc.embedding_model("ollama", "qwen3-embedding:4b")["id"] == "qwen3-embedding:0.6b"  # same family
    assert mc.embedding_model("ollama", "other")["query_prefix"] == ""
