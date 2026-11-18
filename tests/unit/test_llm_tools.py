"""Provider-neutral tool conversations are converted to each engine's native format; credentials resolve."""
from shared.config import settings
from shared.llm import (AgentTurn, AnthropicProvider, GeminiProvider, OllamaProvider, OpenAIProvider, ToolCall,
                        build_extract_engines, resolve_api_key)


def history(provider_name: str, model: str, native=None):
    turn = AgentTurn(text="Looking.", tool_calls=[ToolCall(id="c1", name="find_text", arguments={"text": "NPI"},
                                                           native_id="g1")],
                     native=native, provider=provider_name, model=model)
    return [{"role": "user", "content": "Extract."}, {"role": "assistant", "turn": turn},
            {"role": "tool", "results": [{"id": "c1", "name": "find_text", "content": "Page 9: NPI 1234567890",
                                          "is_error": False, "native_id": "g1"}], "text": "Context note."}]


def test_anthropic_messages_replay_native_content_or_rebuild():
    p = AnthropicProvider(None, "claude-sonnet-5-5", "k")
    native = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "tool_use", "id": "c1", "name": "find_text", "input": {}}]
    msgs = p._tool_messages(history("anthropic", "claude-sonnet-5-5", native))
    assert msgs[1]["content"] is native  # thinking blocks replayed unchanged
    rebuilt = p._tool_messages(history("openai", "gpt-6.2-sol", {"role": "assistant"}))[1]["content"]
    assert rebuilt == [{"type": "text", "text": "Looking."},
                       {"type": "tool_use", "id": "c1", "name": "find_text", "input": {"text": "NPI"}}]
    tool_msg = msgs[2]
    assert tool_msg["role"] == "user" and tool_msg["content"][0]["type"] == "tool_result"
    assert tool_msg["content"][-1] == {"type": "text", "text": "Context note."}


def test_openai_chat_and_responses_formats():
    chat = OpenAIProvider(None, "gpt-4.1", "k")
    assert not chat._uses_responses_api()
    msgs = chat._tool_messages("sys", history("anthropic", "x"))
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "find_text", "arguments": '{"text": "NPI"}'}
    assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "content": "Page 9: NPI 1234567890"}
    assert msgs[4] == {"role": "user", "content": "Context note."}
    reasoning = OpenAIProvider(None, "gpt-6.2-sol", "k")
    assert reasoning._uses_responses_api() and OpenAIProvider(None, "gpt-5-mini", "k")._is_reasoning_model()
    native = [{"type": "reasoning", "encrypted_content": "e"}, {"type": "function_call", "call_id": "c1"}]
    items = reasoning._responses_input(history("openai", "gpt-6.2-sol", native))
    assert items[1:3] == native  # encrypted reasoning replayed
    assert {"type": "function_call_output", "call_id": "c1", "output": "Page 9: NPI 1234567890"} in items


def test_gemini_and_ollama_formats():
    # Gemini: rebuilt calls become functionCall parts and results carry the call id.
    g = GeminiProvider(None, "gemini-4-argon", "k")
    assert g._replay(AgentTurn(text="", native={"role": "model"}, provider="gemini", model="gemini-4-argon"))
    assert not g._replay(AgentTurn(text="", native={"role": "model"}, provider="gemini", model="other"))
    o = OllamaProvider(None, "qwen3.5:4b")
    assert o._replay(AgentTurn(text="", native={"role": "assistant"}, provider="ollama", model="qwen3.5:4b"))


def test_credentials_resolve_across_workloads(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "phi_llm_provider", "openai")
    monkeypatch.setattr(settings, "phi_llm_api_key", "phi-key")
    for mode in ("doc", "extract"):
        monkeypatch.setattr(settings, f"{mode}_llm_api_key", None)
        monkeypatch.setattr(settings, f"{mode}_llm_fallback_api_key", None)
    assert resolve_api_key("openai") == "phi-key"  # reused from the PHI workload
    assert resolve_api_key("openai", "explicit") == "explicit"
    assert resolve_api_key("gemini") is None
    monkeypatch.setattr(settings, "anthropic_api_key", "anthropic-key")
    assert resolve_api_key("anthropic") == "anthropic-key"


def test_extract_engines_follow_configuration_and_request_choice(monkeypatch):
    monkeypatch.setattr(settings, "extract_llm_provider", "anthropic")
    monkeypatch.setattr(settings, "extract_llm_model", "claude-sonnet-5-5")
    monkeypatch.setattr(settings, "extract_llm_url", None)
    monkeypatch.setattr(settings, "extract_llm_fallback_provider", "openai")
    monkeypatch.setattr(settings, "extract_llm_fallback_model", "gpt-6.2-sol")
    monkeypatch.setattr(settings, "extract_reasoning_effort", "medium")
    primary, fallback = build_extract_engines()
    assert (primary.name, primary.model, fallback.name, fallback.model) == ("anthropic", "claude-sonnet-5-5", "openai", "gpt-6.2-sol")
    assert primary.effort == "medium"
    primary, fallback = build_extract_engines("openai:gpt-6.2-sol")  # the fallback was chosen: roles swap
    assert (primary.model, fallback.model) == ("gpt-6.2-sol", "claude-sonnet-5-5")
    primary, fallback = build_extract_engines("anthropic:claude-opus-5-5")
    assert primary.model == "claude-opus-5-5" and fallback.model == "gpt-6.2-sol"
