"""AI engine settings per service, edited by administrators and applied to new jobs."""
import json
import os
import time

import pytest

from tests.integration.conftest import EXTRACT_DIR, load_json, make_user, record


@pytest.fixture
def restore(admin):
    """Every test leaves the services as .env configures them."""
    yield
    for key in ("phi", "doc", "extract", "extract_toc", "extract_embed"):
        admin.post(f"/admin/llm-config/{key}/reset")


def test_only_administrators_see_and_change_engines(admin, phi_user, phi_api):
    data = admin.get("/admin/llm-config").json()
    keys = [s["key"] for s in data["services"]]
    assert keys == ["phi", "doc", "extract", "extract_toc", "extract_embed"]
    assert {m["value"] for m in data["toc_modes"]} == {"rules", "llm", "hybrid"}
    openai = next(p for p in data["providers"] if p["name"] == "openai")
    assert [m["id"] for m in openai["models"]] == ["gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"]  # account models only
    assert not any("api_key" in json.dumps(s) for s in data["services"])  # keys are never exposed
    assert phi_user.get("/admin/llm-config").status_code == 403
    assert phi_api.get("/admin/llm-config").status_code in (401, 403)
    assert phi_user.put("/admin/llm-config/phi", json={"primary": {"provider": "ollama", "model": "qwen3.5:4b"}}).status_code == 403
    record("llm_config_initial", {s["key"]: {"value": s["value"], "source": s["source"]} for s in data["services"]})


def test_invalid_engine_settings_are_rejected(admin, restore):
    cases = [
        ("phi", {"primary": {"provider": "openai", "model": "gpt-6.2-astra"}}, "not offered for openai"),
        ("phi", {"primary": {"provider": "gemini", "model": "gemini-4-argon"}}, "no credentials for gemini"),
        ("extract_toc", {"mode": "sometimes"}, "mode must be"),
        ("extract_embed", {"provider": "cohere", "model": "x"}, "provider must be"),
        ("extract", {"primary": {"provider": "anthropic", "model": "claude-sonnet-5-5"}, "reasoning_effort": "max"}, "reasoning_effort"),
    ]
    for key, value, fragment in cases:
        res = admin.put(f"/admin/llm-config/{key}", json=value)
        assert res.status_code == 400 and fragment in res.json()["detail"], (key, res.text)
    assert admin.put("/admin/llm-config/unknown", json={}).status_code == 404


def test_phi_detection_switched_to_the_local_model(admin, phi_api, restore):
    res = admin.put("/admin/llm-config/phi", json={"primary": {"provider": "ollama", "model": "qwen3.5:4b"}, "fallback": None})
    assert res.status_code == 200, res.text
    assert res.json()["source"] == "admin" and res.json()["value"]["primary"]["provider"] == "ollama"
    test = admin.post("/admin/llm-config/phi/test").json()["results"]
    assert test[0]["ok"], test
    time.sleep(6)  # the worker re-reads engine settings every few seconds
    out = phi_api.post("/phi/redact-text", json={"text": "Patient Sarah Mitchell, MRN 4481920, phone (415) 555-0134."})
    assert out.status_code == 200, out.text
    body = out.json()
    assert set(body["usage"]["by_model"]) == {"ollama:qwen3.5:4b"} and body["usage"]["total"]["cost_usd"] == 0
    assert "Sarah Mitchell" not in body["redacted_text"]
    audit = admin.get("/admin/audit-logs", params={"action": "settings."}).json()["items"]
    assert audit and audit[0]["action"] == "settings.engine_updated" and audit[0]["target_id"] == "phi"
    reset = admin.post("/admin/llm-config/phi/reset").json()
    assert reset["source"] == "env" and reset["value"]["primary"]["provider"] == "openai"
    record("llm_config_phi_local", {"redacted_text": body["redacted_text"], "usage": body["usage"]})


def test_index_detection_mode_is_applied_to_extraction(admin, restore):
    res = admin.put("/admin/llm-config/extract_toc", json={"mode": "rules", "engine": None})
    assert res.status_code == 200, res.text
    time.sleep(6)
    user = make_user(admin, ["data_extractor"])
    name = "services_agreement.docx"
    with open(os.path.join(EXTRACT_DIR, name), "rb") as f:
        job = user.post("/extract/sync", files={"file": (name, f.read())},
                        data={"spec": json.dumps(load_json(EXTRACT_DIR, "services_agreement.schema.json")), "approach": "agentic"}).json()
    r = job["result_data"]
    assert r["engine"]["toc"] == {"mode": "rules", "engine": None}
    assert "Contents detection mode: rules." in r["document"]["toc"]["notes"]
    assert not any(k.startswith("openai:gpt-6-luna") for k in r["usage"]["by_model"])
    record("llm_config_toc_rules", {"toc": r["engine"]["toc"], "found": r["summary"]["found"], "usage": r["usage"]["total"]})
