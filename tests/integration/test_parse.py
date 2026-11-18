import os
import time

from tests.integration.conftest import PARSE_DIR, load_json, record


def test_structured_parse_of_a_pdf(parse_user):
    truth = load_json(PARSE_DIR, "discharge_summary.expected.json")
    with open(os.path.join(PARSE_DIR, "discharge_summary.pdf"), "rb") as f:
        started = time.perf_counter()
        res = parse_user.post("/parse/sync", files={"file": ("discharge_summary.pdf", f.read())},
                              data={"mode": truth["mode"], "ai_summaries": "false"})
    assert res.status_code == 200, res.text
    job = res.json()
    assert job["job_type"] == "PARSE" and job["status"] == "COMPLETED"
    md = job["result_markdown"]
    assert all(text in md for text in truth["must_contain"])
    chunks = job["result_data"]["chunks"]
    assert chunks and all("grounding" in c and "label" in c for c in chunks)
    assert job["result_data"]["metadata"]["page_count"] == truth["pages"]
    # ai_summaries=false: no figure descriptions, so no language model calls and no cost
    assert job["usage"]["total"]["calls"] == 0 and job["usage"]["total"]["cost_usd"] == 0
    md_file = parse_user.get(f"/jobs/{job['job_id']}/markdown")
    assert md_file.status_code == 200 and md_file.text == md
    record("parse_structured", {"file": "discharge_summary.pdf", "pages": truth["pages"], "chunks": len(chunks),
                                "job_ms": job["duration_ms"], "wall_ms": int((time.perf_counter() - started) * 1000),
                                "labels": sorted({c["label"] for c in chunks}), "usage": job.get("usage")})


def test_ocr_mode_returns_words_with_boxes(parse_user):
    truth = load_json(PARSE_DIR, "lab_requisition.expected.json")
    with open(os.path.join(PARSE_DIR, "lab_requisition.png"), "rb") as f:
        res = parse_user.post("/parse/sync", files={"file": ("lab_requisition.png", f.read())}, data={"mode": truth["mode"]})
    assert res.status_code == 200, res.text
    job = res.json()
    assert job["job_type"] == "OCR"
    page = job["result_data"]["pages"][0]
    assert all(text in job["result_markdown"] for text in truth["must_contain"])
    assert job["usage"]["total"] == {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                                     "cache_write_tokens": 0, "cost_usd": 0.0, "priced": True}  # OCR uses no LLM
    word = page["lines"][0]["words"][0]
    assert set(word["boundingBox"]) == {"xmin", "ymin", "xmax", "ymax"}
    record("parse_ocr", {"file": "lab_requisition.png", "lines": len(page["lines"]), "job_ms": job["duration_ms"]})


def test_parse_rejects_formats_it_does_not_support(parse_user):
    res = parse_user.post("/parse/jobs", files={"file": ("n.docx", b"PK\x03\x04")})
    assert res.status_code == 415
