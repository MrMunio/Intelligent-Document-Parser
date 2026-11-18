"""Approach 2: the extraction agent, its tools, context compaction and engine failover."""
import asyncio
import json

import pytest

from shared.extract_spec import validate_spec
from shared.llm import AgentTurn, LLMError, ToolCall
from worker.extract.agent import AgentRunner, DocumentTools, ExtractionState, _ranges
from worker.extract.chunking import chunk_document
from worker.extract.document import Document, Page
from worker.extract.retrieval import HybridIndex
from worker.extract.toc import detect_toc

SPEC = validate_spec({"properties": {
    "plan": {"type": "string", "description": "Health plan name"},
    "notice_days": {"type": "integer", "description": "Termination notice in days"},
    "fees": {"type": "array", "description": "Fee schedule", "items": {"type": "object", "properties": {
        "code": {"type": "string", "description": "Code"}, "rate": {"type": "number", "description": "Rate"}}}},
    "terms": {"type": "object", "description": "Terms", "properties": {
        "auto_renew": {"type": "boolean", "description": "Renews automatically"}}},
}})
FILLER = "The parties shall comply with the provider manual. " * 30


def document() -> Document:
    pages = [Page(1, "CONTENTS\nARTICLE 1. Parties ..... 1\nARTICLE 2. Term ..... 2\nARTICLE 3. Fees ..... 3", "text_layer"),
             Page(2, "ARTICLE 1. PARTIES\nHealth plan: Blue Harbor Health Plan, Inc.\n" + FILLER, "text_layer"),
             Page(3, "ARTICLE 2. TERM\nEither party may terminate on 120 days notice. The agreement renews "
                     "automatically.\n" + FILLER, "text_layer"),
             Page(4, "ARTICLE 3. FEES\n90791 | $165.00\n90837 | $132.00\n" + FILLER, "text_layer")]
    return Document("contract.pdf", "pdf", pages)


def setup(max_chars=4000):
    doc = document()
    toc = asyncio.run(detect_toc(doc, None))
    chunks = chunk_document(doc, toc, "section", 200, 20)
    index = HybridIndex(chunks, None)
    state = ExtractionState(SPEC, doc.page_count)
    return doc, toc, DocumentTools(doc, toc, chunks, index, state, max_chars=max_chars), state


def call(name, **args):
    return ToolCall(id=f"id-{name}-{len(json.dumps(args))}", name=name, arguments=args)


def run_tool(tools, name, **args):
    return asyncio.run(tools.execute(call(name, **args), 1, 1))


# ---------------------------------------------------------------------------
# Tools and state
# ---------------------------------------------------------------------------

def test_document_map_reflects_the_calibrated_contents():
    _, toc, tools, _ = setup()
    out = run_tool(tools, "get_document_map").content
    assert "Map source: contents page(s) [1]" in out and "printed page numbers + 1 = physical pages" in out
    assert "s2 [p3-4] ARTICLE 2. Term" in out


def test_read_and_search_tools():
    _, _, tools, _ = setup(max_chars=1200)
    pages = run_tool(tools, "read_pages", start_page=2, end_page=4).content
    assert pages.startswith("=== Page 2 ===") and "call read_pages with start_page=3" in pages
    assert run_tool(tools, "read_pages", start_page=99).is_error
    section = run_tool(tools, "read_section", section_id="s3").content
    assert "ARTICLE 3. FEES" in section and "90791" in section and "part 1 of" in section
    assert run_tool(tools, "read_section", section_id="s9").is_error
    hits = run_tool(tools, "find_text", text="120 days").content
    assert "Page 3:" in hits and "1 match" in hits
    assert run_tool(tools, "find_text", text="[", regex=True).is_error
    assert "No results" in run_tool(tools, "search_document", query="zzzz qqqq").content
    found = run_tool(tools, "search_document", query="terminate notice days", top_k=2).content
    assert "p3" in found and "s2" in found
    chunk_id = found.split("[")[1].split("]")[0]
    assert "120 days" in run_tool(tools, "read_chunk", chunk_ids=[chunk_id]).content
    assert tools.pages_read >= {2, 3, 4} and len(tools.searches) == 3


def test_record_fields_validates_types_paths_and_pages():
    _, _, tools, state = setup()
    out = run_tool(tools, "record_fields", fields=[
        {"path": "plan", "status": "found", "value_json": '"Blue Harbor Health Plan, Inc."', "pages": [2, 99], "confidence": "high"},
        {"path": "notice_days", "status": "found", "value_json": '"120 days"'},  # not an integer: rejected
        {"path": "terms.auto_renew", "status": "found", "value_json": "true"},
        {"path": "auto_renew", "status": "found", "value_json": "true"},  # wrong path, suggestion offered
        {"path": "fees", "status": "found", "value_json": '[{"code": "90791", "rate": "165.00"}]'},
    ])
    assert not out.is_error  # partly saved
    text = out.content
    assert "does not fit type integer" in text and "Did you mean: terms.auto_renew" in text
    assert state.fields["plan"].pages == [2] and state.fields["fees"].value == [{"code": "90791", "rate": 165.0}]
    assert state.pending() == ["notice_days"]
    assert run_tool(tools, "finish").is_error  # a field is still pending
    premature = run_tool(tools, "record_fields", fields=[{"path": "notice_days", "status": "not_found", "note": "later"}])
    assert "not searched for" in premature.content and state.fields["notice_days"].status == "pending"
    run_tool(tools, "find_text", text="termination notice")  # searching with words from the description counts
    run_tool(tools, "record_fields", fields=[{"path": "notice_days", "status": "not_found", "note": "searched article 2"}])
    assert not run_tool(tools, "finish", summary="done").is_error
    assert state.result() == {"plan": "Blue Harbor Health Plan, Inc.", "notice_days": None,
                              "fees": [{"code": "90791", "rate": 165.0}], "terms": {"auto_renew": True}}


# ---------------------------------------------------------------------------
# Runner with a scripted engine
# ---------------------------------------------------------------------------

class ScriptedEngine:
    """Plays back turns. Each step is a list of ToolCalls (or a text reply) and a reported context size."""

    def __init__(self, name, steps, fail=False):
        self.name, self.model, self.steps, self.fail = name, "scripted", list(steps), fail
        self.ident = f"{name}:scripted"
        self.histories = []
        self.api_key = "x"

    async def tool_chat(self, system, history, tools, *, max_tokens=16000):
        self.histories.append([dict(h) for h in history])
        if self.fail:
            raise LLMError(f"{self.name}: down")
        if not self.steps:
            return AgentTurn(text="(nothing left)", provider=self.name, model=self.model, input_tokens=100)
        calls, context = self.steps.pop(0)
        if isinstance(calls, str):
            return AgentTurn(text=calls, provider=self.name, model=self.model, input_tokens=context)
        return AgentTurn(text="", tool_calls=calls, provider=self.name, model=self.model, input_tokens=context,
                         output_tokens=50)

    async def aclose(self):
        pass


def runner(engine, fallback=None, window=20000, **kw):
    doc, toc, tools, state = setup()
    events = []
    r = AgentRunner(spec=SPEC, doc=doc, toc=toc, tools=tools, state=state, primary=engine, fallback=fallback,
                    window_override=window, progress=lambda *a: events.append(a), **kw)
    return r, state, events


RECORD_ALL = [call("record_fields", fields=[
    {"path": "plan", "status": "found", "value_json": '"Blue Harbor"', "pages": [2]},
    {"path": "notice_days", "status": "found", "value_json": "120", "pages": [3]},
    {"path": "fees", "status": "found", "value_json": '[{"code": "90791", "rate": 165}]', "pages": [4]},
    {"path": "terms.auto_renew", "status": "found", "value_json": "true", "pages": [3]}]), call("finish", summary="ok")]


def test_agent_completes_in_one_session(monkeypatch):
    monkeypatch.setattr("worker.extract.agent.settings.extract_context_threshold", 0.7)
    engine = ScriptedEngine("anthropic", [([call("read_section", section_id="s2")], 3000), (RECORD_ALL, 4000)])
    r, state, events = runner(engine)
    out = asyncio.run(r.run())
    assert out["complete"] and out["agent"]["compactions"] == 0 and len(out["agent"]["sessions"]) == 1
    assert out["data"]["notice_days"] == 120 and out["agent"]["sessions"][0]["ended"] == "finished"
    assert out["agent"]["trace"][0]["tool"] == "read_section"
    assert events[-1][0] == "agent" and "4 of 4" in events[-1][2]


def test_compaction_restarts_the_session_from_a_checkpoint(monkeypatch):
    monkeypatch.setattr("worker.extract.agent.settings.extract_context_threshold", 0.7)
    # window 20000 x 0.7 = 14000: the second turn reports 13900 tokens, so the next send would exceed it.
    engine = ScriptedEngine("anthropic", [
        ([call("search_document", query="health plan name"),
          call("record_fields", fields=[{"path": "plan", "status": "found", "value_json": '"Blue Harbor"', "pages": [2]}])], 5000),
        ([call("read_pages", start_page=3, end_page=4)], 13900),
        ("Handoff: notice period is on page 3; fees table on page 4.", 14500),  # the handoff turn
        (RECORD_ALL[0:1] + RECORD_ALL[1:], 2500),
    ])
    r, state, _ = runner(engine)
    out = asyncio.run(r.run())
    sessions = out["agent"]["sessions"]
    assert out["complete"] and out["agent"]["compactions"] == 1 and [s["ended"] for s in sessions] == ["compacted", "finished"]
    assert sessions[0]["compact_at_tokens"] == 14000 and sessions[0]["compacted_at_tokens"] >= 14000
    handoff_request = engine.histories[2][-1]
    assert handoff_request["role"] == "tool" and "CONTEXT CHECKPOINT" in handoff_request["text"]
    restart = engine.histories[3]
    assert len(restart) == 1, "a new session starts with a single checkpoint message"
    checkpoint = restart[0]["content"]
    assert "Checkpoint: session 1 ended" in checkpoint and '- plan = "Blue Harbor" (pages 2)' in checkpoint
    assert '"health plan name"' in checkpoint and "Pages read: 3-4" in checkpoint
    assert "notice period is on page 3" in checkpoint and "notice_days (integer)" in checkpoint
    assert out["agent"]["handoff_notes"] == ["Handoff: notice period is on page 3; fees table on page 4."]


def test_agent_is_told_to_save_findings_before_compaction(monkeypatch):
    monkeypatch.setattr("worker.extract.agent.settings.extract_context_threshold", 0.7)
    engine = ScriptedEngine("anthropic", [([call("read_pages", start_page=2)], 11500), (RECORD_ALL, 12000)])
    r, _, _ = runner(engine)
    asyncio.run(r.run())
    warned = engine.histories[1][-1]
    assert warned["role"] == "tool" and "will be compacted soon" in warned["text"]


def test_primary_failure_continues_on_the_fallback_engine():
    primary = ScriptedEngine("anthropic", [], fail=True)
    fallback = ScriptedEngine("openai", [(RECORD_ALL, 3000)])
    r, _, _ = runner(primary, fallback=fallback)
    out = asyncio.run(r.run())
    assert out["complete"] and [s["ended"] for s in out["agent"]["sessions"]] == ["engine_failover", "finished"]
    assert out["agent"]["sessions"][1]["provider"] == "openai" and "continued on openai" in out["warnings"][0]
    assert set(out["usage"]["by_model"]) == {"openai:scripted"}


def test_failure_without_fallback_raises():
    r, _, _ = runner(ScriptedEngine("anthropic", [], fail=True))
    with pytest.raises(LLMError):
        asyncio.run(r.run())


def test_turn_limit_and_nudges_leave_unresolved_fields_not_found():
    engine = ScriptedEngine("anthropic", [("I think I am done.", 1000), ("Still done.", 1000), ("Done.", 1000)])
    r, state, _ = runner(engine)
    out = asyncio.run(r.run())
    assert out["agent"]["sessions"][0]["ended"] == "agent_stopped" and not out["complete"]
    assert len(engine.histories[1]) == 3 and "pending" in engine.histories[1][-1]["content"]
    assert all(f["status"] == "not_found" for f in out["fields"])
    engine2 = ScriptedEngine("anthropic", [([call("get_pending_fields")], 1000)] * 10)
    r2, _, _ = runner(engine2, max_turns=3)
    out2 = asyncio.run(r2.run())
    assert out2["agent"]["turns"] == 3 and "turn limit" in out2["warnings"][-1]


def test_ranges():
    assert _ranges([3, 1, 2, 7, 9, 10]) == "1-3, 7, 9-10" and _ranges([]) == ""


def test_record_fields_accepts_a_json_encoded_array_and_explains_bad_input():
    """Small local models sometimes send the array as a JSON string once their context fills up."""
    _, _, tools, state = setup()
    encoded = json.dumps([{"path": "plan", "status": "found", "value_json": '"Blue Harbor"', "pages": [2]}])
    out = asyncio.run(tools.execute(call("record_fields", fields=encoded), 1, 1))
    assert not out.is_error and state.fields["plan"].value == "Blue Harbor"
    single = asyncio.run(tools.execute(call("record_fields", fields={"path": "notice_days", "status": "found", "value_json": "120"}), 1, 2))
    assert not single.is_error and state.fields["notice_days"].value == 120
    bad = asyncio.run(tools.execute(call("record_fields", fields="plan = Blue Harbor"), 1, 3))
    assert bad.is_error and "received a str: plan = Blue Harbor" in bad.content
    assert tools.trace[-1]["args"]["fields_raw"] == "plan = Blue Harbor"  # visible in the trace
    assert tools.trace[0]["args"]["fields"] == ["plan:found"]



def test_not_found_is_accepted_with_cited_pages_that_were_read():
    _, _, tools, state = setup()
    rejected = run_tool(tools, "record_fields", fields=[{"path": "plan", "status": "not_found", "pages": [2]}])
    assert "not searched for" in rejected.content  # page 2 was never examined
    run_tool(tools, "read_pages", start_page=2)
    run_tool(tools, "record_fields", fields=[{"path": "plan", "status": "not_found", "pages": [2]}])
    assert state.fields["plan"].status == "not_found"


def test_repeated_reads_are_pointed_to_then_refused():
    _, _, tools, _ = setup()
    first = asyncio.run(tools.execute(call("read_section", section_id="s2"), 1, 1)).content
    assert "ARTICLE 2" in first
    same_session = asyncio.run(tools.execute(call("read_section", section_id="s2"), 1, 2)).content
    assert "already read section s2 part 1 in this session" in same_session and "ARTICLE 2" not in same_session
    second_session = asyncio.run(tools.execute(call("read_section", section_id="s2"), 2, 3)).content
    assert second_session.startswith("Note: you already read section s2 part 1 in session 1") and "ARTICLE 2" in second_session
    third = asyncio.run(tools.execute(call("read_section", section_id="s2"), 3, 4))
    assert third.is_error and "already read in sessions 1, 2" in third.content and "find_text" in third.content


def test_tool_output_is_sized_to_the_usable_window(monkeypatch):
    from worker.extract.agent import tool_chars_for
    monkeypatch.setattr("worker.extract.agent.settings.extract_agent_max_tool_chars", 12000)
    assert tool_chars_for(8192) == int(8192 * 0.15 * 3.6)  # about 1,200 tokens at an 8K window
    assert tool_chars_for(200_000) == 12000 and tool_chars_for(4000) == 2160
