import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DATA_DIR", "/tmp/idp-unit")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-unit-test-secret-0123456789")
os.environ.setdefault("RUNTIME_CONFIG_SOURCE", "env")  # engine settings from .env values only: no database in unit tests


class StubLLM:
    """
    Deterministic stand-in for shared.llm.FailoverLLM. It "detects" every known value that appears in the
    prompt, so the tests exercise matching and redaction rather than model quality.
    """

    def __init__(self, known: dict[str, str], fail: bool = False, garbage: bool = False):
        self.known = known
        self.fail = fail
        self.garbage = garbage
        self.calls = 0
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "engines": {"stub:test": 0}, "failovers": 0}

    async def chat(self, system, user, *, json_mode=False, max_tokens=4096):
        from shared.llm import LLMError, LLMResult
        self.calls += 1
        self.usage["calls"] += 1
        if self.fail:
            raise LLMError("stub: engine unavailable")
        if self.garbage:
            return LLMResult(text="I could not find anything useful here, sorry.", provider="stub", model="test")
        found = [{"text": value, "type": etype} for value, etype in self.known.items() if value in user]
        return LLMResult(text=json.dumps({"entities": found}), provider="stub", model="test")


@pytest.fixture
def progress_log():
    events = []
    return events, (lambda stage, pct, detail="": events.append((stage, pct, detail)))
