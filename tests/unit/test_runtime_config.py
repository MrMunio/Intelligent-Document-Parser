"""Engine settings per service: .env defaults, validation of administrator changes, contents detection modes."""
import asyncio
import json

import pytest

from shared import runtime_config as rc
from shared.config import settings
from shared.llm import LLMResult
from worker.extract.document import Document, Page
from worker.extract.toc import detect_toc

SELECTABLE = {"openai": {"gpt-6.1-sol", "gpt-6-luna"}, "anthropic": {"claude-sonnet-5-5"}, "gemini": set(),
              "ollama": None, "azure_openai": None, "openai_compatible": None}


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "k1")
    monkeypatch.setattr(settings, "anthropic_api_key", "k2")
    monkeypatch.setattr(settings, "gemini_api_key", None)
    for mode in ("phi", "doc", "extract"):
        monkeypatch.setattr(settings, f"{mode}_llm_api_key", None)
        monkeypatch.setattr(settings, f"{mode}_llm_fallback_api_key", None)


def test_env_defaults_cover_every_service(monkeypatch):
    monkeypatch.setattr(settings, "phi_llm_provider", "ollama")
    monkeypatch.setattr(settings, "phi_llm_model", "qwen3.5:4b")
    monkeypatch.setattr(settings, "phi_llm_fallback_provider", None)
    assert rc.env_defaults("phi") == {"primary": {"provider": "ollama", "model": "qwen3.5:4b", "url": settings.phi_llm_url},
                                      "fallback": None}
    monkeypatch.setattr(settings, "extract_toc_use_llm", False)
    assert rc.env_defaults("extract_toc")["mode"] == "rules"  # the legacy switch still works
    monkeypatch.setattr(settings, "extract_toc_use_llm", True)
    monkeypatch.setattr(settings, "extract_toc_mode", "llm")
    assert rc.env_defaults("extract_toc")["mode"] == "llm"
    assert set(rc.env_defaults("extract")) == {"primary", "fallback", "reasoning_effort"}
    assert rc.get("extract_embed") == rc.env_defaults("extract_embed")  # RUNTIME_CONFIG_SOURCE=env in unit tests
    with pytest.raises(rc.ConfigError):
        rc.env_defaults("nope")


def test_validation_of_engine_changes():
    ok = rc.validate("phi", {"primary": {"provider": "ollama", "model": "qwen3.5:4b"}, "fallback": None}, SELECTABLE)
    assert ok == {"primary": {"provider": "ollama", "model": "qwen3.5:4b", "url": None}, "fallback": None}
    ext = rc.validate("extract", {"primary": {"provider": "anthropic", "model": "claude-sonnet-5-5"},
                                  "fallback": {"provider": "openai", "model": "gpt-6.1-sol"}, "reasoning_effort": "high"}, SELECTABLE)
    assert ext["reasoning_effort"] == "high" and ext["fallback"]["model"] == "gpt-6.1-sol"
    for value, fragment in [
        ({"primary": None}, "choose a provider"),
        ({"primary": {"provider": "openai", "model": "gpt-6.2-astra"}}, "not offered for openai"),
        ({"primary": {"provider": "gemini", "model": "x"}}, "not offered"),
        ({"primary": {"provider": "skynet", "model": "x"}}, "provider must be one of"),
        ({"primary": {"provider": "openai_compatible", "model": "llama"}}, "URL is required"),
        ({"primary": {"provider": "ollama", "model": "q", "url": "ftp://x"}}, "must start with http"),
        ({"primary": {"provider": "ollama", "model": "q"}, "fallback": {"provider": "ollama", "model": "q"}}, "must differ"),
    ]:
        with pytest.raises(rc.ConfigError, match=fragment):
            rc.validate("phi", value, SELECTABLE)
    with pytest.raises(rc.ConfigError, match="no credentials for gemini"):
        rc.validate("phi", {"primary": {"provider": "gemini", "model": "g"}}, {**SELECTABLE, "gemini": None})
    with pytest.raises(rc.ConfigError, match="reasoning_effort"):
        rc.validate("extract", {"primary": {"provider": "ollama", "model": "q"}, "reasoning_effort": "max"}, SELECTABLE)
    toc = rc.validate("extract_toc", {"mode": "hybrid", "engine": {"provider": "openai", "model": "gpt-6-luna"}}, SELECTABLE)
    assert toc == {"mode": "hybrid", "engine": {"provider": "openai", "model": "gpt-6-luna", "url": None}}
    assert rc.validate("extract_toc", {"mode": "rules", "engine": None}, SELECTABLE)["engine"] is None
    with pytest.raises(rc.ConfigError, match="mode must be"):
        rc.validate("extract_toc", {"mode": "magic"}, SELECTABLE)
    emb = rc.validate("extract_embed", {"provider": "ollama", "model": "all-minilm:33m"}, SELECTABLE)
    assert emb["model"] == "all-minilm:33m"
    assert rc.validate("extract_embed", {"provider": "none"}, SELECTABLE)["provider"] == "none"


def contract_with_unlabelled_contents():
    """A contents page without a heading or dot leaders: the rules miss it, a language model finds it."""
    filler = "The parties shall comply with the provider manual. " * 30
    pages = [Page(1, "Cover page", "text_layer"),
             Page(2, "Overview\nARTICLE 1. Parties 3\nARTICLE 2. Term 4\nARTICLE 3. Fees 5", "text_layer"),
             Page(3, "ARTICLE 1. PARTIES\n" + filler, "text_layer"), Page(4, "ARTICLE 2. TERM\n" + filler, "text_layer"),
             Page(5, "ARTICLE 3. FEES\n" + filler, "text_layer")]
    return Document("c.pdf", "pdf", pages)


class StubLLM:
    def __init__(self):
        self.calls = 0

    async def chat(self, system, user, *, json_mode=False, max_tokens=4096, json_schema=None):
        self.calls += 1
        if "decide which" in system:
            return LLMResult(text=json.dumps({"toc_pages": [2]}))
        return LLMResult(text=json.dumps({"document_type": "contract", "summary": "s", "entries": [
            {"title": "ARTICLE 1. Parties", "level": 1, "page": "3"}, {"title": "ARTICLE 2. Term", "level": 1, "page": "4"},
            {"title": "ARTICLE 3. Fees", "level": 1, "page": "5"}]}))


def test_contents_detection_modes():
    doc = contract_with_unlabelled_contents()
    rules = asyncio.run(detect_toc(doc, StubLLM(), mode="rules"))
    assert rules.source != "toc_page_llm"  # no language model in rules mode
    llm = StubLLM()
    found = asyncio.run(detect_toc(doc, llm, mode="llm"))
    assert found.source == "toc_page_llm" and [e.page for e in found.entries] == [3, 4, 5] and llm.calls == 2
    hybrid_llm = StubLLM()
    hybrid = asyncio.run(detect_toc(doc, hybrid_llm, mode="hybrid"))
    assert hybrid.source == "toc_page_llm" and hybrid_llm.calls == 2  # rules found nothing, so the model was asked
    assert "Contents detection mode: hybrid." in hybrid.notes
