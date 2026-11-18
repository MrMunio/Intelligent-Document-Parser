import os
import time

from tests.integration.conftest import FIXTURES, record


def test_structured_parse_of_a_pdf(parse_user):
    with open(os.path.join(FIXTURES, "discharge_summary.pdf"), "rb") as f:
        started = time.perf_counter()
        res = parse_user.post("/parse/sync", files={"file": ("discharge_summary.pdf", f.read())},
                              data={"mode": "structured", "ai_summaries": "false"})
    assert res.status_code == 200, res.text
    job = res.json()
    assert job["job_type"] == "PARSE" and job["status"] == "COMPLETED"
    md = job["result_markdown"]
    assert "DISCHARGE SUMMARY" in md and "Hospital Course" in md
    chunks = job["result_data"]["chunks"]
    assert chunks and all("grounding" in c and "label" in c for c in chunks)
    assert job["result_data"]["metadata"]["page_count"] == 3
    md_file = parse_user.get(f"/jobs/{job['job_id']}/markdown")
    assert md_file.status_code == 200 and md_file.text == md
    record("parse_structured", {"file": "discharge_summary.pdf", "pages": 3, "chunks": len(chunks),
                                "job_ms": job["duration_ms"], "wall_ms": int((time.perf_counter() - started) * 1000),
                                "labels": sorted({c["label"] for c in chunks})})


def test_ocr_mode_returns_words_with_boxes(parse_user):
    with open(os.path.join(FIXTURES, "lab_requisition.png"), "rb") as f:
        res = parse_user.post("/parse/sync", files={"file": ("lab_requisition.png", f.read())}, data={"mode": "ocr"})
    assert res.status_code == 200, res.text
    job = res.json()
    assert job["job_type"] == "OCR"
    page = job["result_data"]["pages"][0]
    assert "LABORATORY REQUISITION" in job["result_markdown"]
    word = page["lines"][0]["words"][0]
    assert set(word["boundingBox"]) == {"xmin", "ymin", "xmax", "ymax"}
    record("parse_ocr", {"file": "lab_requisition.png", "lines": len(page["lines"]), "job_ms": job["duration_ms"]})


def test_parse_rejects_formats_it_does_not_support(parse_user):
    res = parse_user.post("/parse/jobs", files={"file": ("n.docx", b"PK\x03\x04")})
    assert res.status_code == 415
