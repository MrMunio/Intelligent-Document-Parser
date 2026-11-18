"""Approach 1: document batch processing carries the JSON state across parts that fit the window."""
import asyncio
import json
import re

import pytest

from shared.extract_spec import validate_spec
from shared.llm import LLMResult
from shared.model_catalog import find_model
from worker.extract.batch import parse_json_object, plan_parts, run_batch
from worker.extract.document import load_text

SPEC = validate_spec({"properties": {
    "plan": {"type": "string", "description": "Health plan name"},
    "notice_days": {"type": "integer", "description": "Termination notice in days"},
    "codes": {"type": "array", "description": "Procedure codes", "items": {"type": "string"}},
}})
FILLER = "General terms apply to both parties in every case. " * 150


def document():
    parts = ["Health plan: Blue Harbor.", FILLER, "Termination requires 120 days notice.", FILLER,
             "Codes billed: 90791.", FILLER, "Codes billed: 90837.", FILLER]
    return load_text("\n".join(parts), "contract.txt")


class StubBatchLLM:
    """Answers like a careful model: keeps the state, adds what the part shows. Can fail on chosen parts."""

    def __init__(self, fail_parts=(), garbage_parts=()):
        self.calls, self.fail_parts, self.garbage_parts, self.prompts = 0, set(fail_parts), set(garbage_parts), []

    async def chat(self, system, user, *, json_mode=False, max_tokens=4096, json_schema=None):
        self.calls += 1
        self.prompts.append(user)
        assert json_schema and json_schema["additionalProperties"] is False
        part = int(re.search(r"Document part (\d+)", user).group(1))
        if part in self.fail_parts:
            raise RuntimeError("engine unavailable")
        if part in self.garbage_parts:
            return LLMResult(text="Sorry, no JSON here.")
        state = json.loads(re.search(r"earlier parts\):\n(.*?)\n\nDocument part", user, re.S).group(1))
        body = user.split("<document_part>")[1]
        out = dict(state, plan=None)  # a careless null for a known field must not erase it
        if "Blue Harbor" in body:
            out["plan"] = "Blue Harbor"
        if "120 days" in body:
            out["notice_days"] = "120"
        codes = list(state.get("codes") or [])
        codes += [c for c in re.findall(r"Codes billed: (\d+)", body) if c not in codes]
        out["codes"] = codes or None
        return LLMResult(text=f"```json\n{json.dumps(out)}\n```", provider="stub", model="test")


def run(llm, window=16000):
    events = []
    model = find_model("anthropic", "claude-sonnet-5-5")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("worker.extract.batch.settings.extract_batch_chunk_tokens", 400)
        mp.setattr("worker.extract.batch.settings.extract_batch_overlap_tokens", 20)
        return asyncio.run(run_batch(document(), SPEC, llm, model, window, "", lambda *a: events.append(a))), events


def test_state_is_carried_across_parts():
    llm = StubBatchLLM()
    result, events = run(llm)
    parts = result["batch"]["parts"]
    assert parts > 3 and llm.calls == parts
    assert result["data"] == {"plan": "Blue Harbor", "notice_days": 120, "codes": ["90791", "90837"]}
    assert result["complete"] and not result["warnings"]
    fields = {f["path"]: f for f in result["fields"]}
    assert fields["plan"]["status"] == "found" and fields["plan"]["pages"]
    assert '"plan": "Blue Harbor"' in llm.prompts[-1]  # later parts see the earlier findings
    assert any(e[0] == "extracting" for e in events)


def test_a_failed_part_keeps_the_state_and_is_reported():
    result, _ = run(StubBatchLLM(fail_parts={2}, garbage_parts={3}))
    assert result["data"]["plan"] == "Blue Harbor" and not result["complete"]
    assert len(result["warnings"]) == 2 and "Part 2" in result["warnings"][0]
    log = {row["part"]: row for row in result["batch"]["log"]}
    assert not log[2]["ok"] and "not return a JSON object" in log[3]["error"]


def test_all_parts_failing_fails_the_job():
    with pytest.raises(RuntimeError, match="No part of the document could be processed"):
        run(StubBatchLLM(fail_parts=set(range(1, 50))))


def test_parts_cover_the_document_with_page_ranges():
    parts = plan_parts(document(), 300, 20)
    assert parts[0]["page_start"] == 1 and parts[-1]["page_end"] == document().page_count
    assert all("[Part" in p["text"] or p is not parts[0] for p in parts[:1])


@pytest.mark.parametrize("text, expected", [
    ('{"a": 1}', {"a": 1}), ('```json\n{"a": 1}\n```', {"a": 1}), ('Here you go: {"a": {"b": 2}} done', {"a": {"b": 2}}),
    ("[1, 2]", None), ("nothing", None),
])
def test_parse_json_object(text, expected):
    assert parse_json_object(text) == expected
