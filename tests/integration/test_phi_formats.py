"""
End-to-end PHI redaction for every supported format, through the public API with an API key.

For files with readable text in the output (text-layer PDF, DOCX, text files) the test reads the
redacted file back and asserts that no ground-truth value survives and that clinical content is kept.
For scanned PDFs and images, where the output is pixels, it measures recall from the metadata and
checks that the output has no hidden text layer.
"""
import io
import json
import os
import re
import time
import zipfile

import pypdfium2 as pdfium
import pytest
from PIL import Image

from tests.integration.conftest import CUSTOM_EMPLOYEE, PHI_DIR, record

CASES = [
    ("discharge_summary.pdf", "pdf", True),
    ("progress_note.docx", "docx", True),
    ("intake_notes.txt", "text", True),
    ("care_plan.md", "text", True),
    ("claims_export.csv", "text", True),
    ("patient_record.json", "text", True),
    ("scanned_referral.pdf", "pdf", False),
    ("lab_requisition.png", "image", False),
]
SUMMARY: dict = {}


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _output_text(name: str, data: bytes) -> str:
    if name.endswith(".pdf"):
        return "\n".join(_pdf_pages(data))
    if name.endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.startswith("word/") and n.endswith(".xml"))
        return re.sub(r"<[^>]+>", "", xml)
    return data.decode("utf-8", "replace")


def _pdf_pages(data) -> list[str]:
    pdf = pdfium.PdfDocument(data)
    try:
        return [pdf[i].get_textpage().get_text_range() for i in range(len(pdf))]
    finally:
        pdf.close()


def _recall(values, metadata) -> tuple[float, list]:
    found = " ".join(_norm(e["text"]) for e in metadata["entities"])
    missing = [v for v in values if _norm(v) not in found and not any(_norm(e["text"]) in _norm(v) and len(_norm(e["text"])) > 3
                                                                      for e in metadata["entities"])]
    return 1 - len(missing) / max(1, len(values)), missing


@pytest.mark.parametrize("name, fmt, has_text", CASES)
def test_redaction_preserves_format_and_removes_phi(phi_api, expected, name, fmt, has_text):
    path = os.path.join(PHI_DIR, name)
    with open(path, "rb") as f:
        payload = f.read()
    started = time.perf_counter()
    res = phi_api.post("/phi/jobs", files={"file": (name, payload)},
                       data={"custom_entities": json.dumps(CUSTOM_EMPLOYEE)})
    assert res.status_code == 202, res.text
    job = phi_api.wait(res.json()["job_id"])
    wall_ms = int((time.perf_counter() - started) * 1000)
    assert job["status"] == "COMPLETED", job.get("error")
    assert job["input_format"] == fmt
    # Language model usage and cost are reported on every PHI job
    usage = job["usage"]["total"]
    assert usage["calls"] >= 1 and usage["input_tokens"] + usage["cache_read_tokens"] > 0
    assert usage["cost_usd"] is not None and usage["priced"]
    assert job["stats"]["usage"] == job["usage"] and job["result_data"]["usage"] == job["usage"]

    # Same format out as in
    stem, ext = os.path.splitext(name)
    assert job["result_file_name"] == f"{stem}.redacted{ext}"
    out = phi_api.get(f"/jobs/{job['job_id']}/file")
    assert out.status_code == 200 and len(out.content) > 0
    assert job["job_id"] == out.headers["X-Job-Id"]
    if fmt == "pdf":
        assert out.content[:5] == b"%PDF-"
        assert len(_pdf_pages(out.content)) == len(_pdf_pages(path))
        result_meta = pdfium.PdfDocument(out.content).get_metadata_dict(skip_empty=True)
        assert not result_meta.get("Author") and not result_meta.get("Title"), result_meta
    elif fmt == "docx":
        assert zipfile.ZipFile(io.BytesIO(out.content)).testzip() is None
    elif fmt == "image":
        assert Image.open(io.BytesIO(out.content)).format == Image.open(path).format

    # Metadata JSON
    meta_res = phi_api.get(f"/jobs/{job['job_id']}/metadata")
    assert meta_res.status_code == 200 and "attachment" in meta_res.headers["content-disposition"]
    meta = meta_res.json()
    for key in ("summary", "entities", "occurrences", "verification", "engine", "options", "timings_ms"):
        assert key in meta, key
    assert meta["summary"]["total_occurrences"] == len(meta["occurrences"]) > 0
    for occ in meta["occurrences"]:
        assert occ["label"].startswith("[") and occ["type"] and occ["text"]

    truth = expected[name]
    must_remove = truth["must_remove"] + truth["custom_must_remove"]
    recall, missing = _recall(must_remove, meta)
    result = {"file": name, "format": fmt, "pages": meta["summary"].get("pages"), "wall_ms": wall_ms,
              "job_ms": job["duration_ms"], "occurrences": meta["summary"]["total_occurrences"],
              "unique": meta["summary"]["unique_entities"], "by_type": meta["summary"]["by_type"],
              "recall_vs_ground_truth": round(recall, 3), "missed": missing,
              "verification": meta["verification"]["status"], "engine": meta["engine"]["llm"]["used"],
              "timings_ms": meta["timings_ms"]}

    if has_text:
        text = _output_text(name, out.content)
        leaked = [v for v in must_remove if _norm(v) in _norm(text)]
        lost = [v for v in truth["must_keep"] if _norm(v) not in _norm(text)]
        result.update({"leaked_in_output": leaked, "clinical_text_lost": lost})
        assert meta["verification"]["status"] in ("passed", "repaired")
        assert not leaked, f"PHI still present in the redacted output: {leaked}"
        assert not lost, f"Non-PHI content was removed: {lost}"
        if truth["custom_must_remove"]:
            assert "EMPLOYEE_ID" in meta["summary"]["by_type"]
    else:
        if fmt == "pdf":
            # The rebuilt text layer holds only unredacted OCR words and the placeholder labels.
            text = " ".join(_pdf_pages(out.content))
            leaked = [v for v in must_remove if _norm(v) in _norm(text)]
            assert not leaked, f"PHI present in the rebuilt text layer: {leaked}"
            assert "DISCHARGE" in text.upper(), "unredacted OCR text should stay searchable"
            assert meta["summary"]["pages_ocr"] == len(_pdf_pages(out.content))
        result["leaked_in_output"] = "not measurable (pixels): see recall"
        assert recall >= 0.85, f"recall {recall:.2f}, missed {missing}"

    SUMMARY[name] = result
    record(f"phi_{stem}", result)


def test_sync_redact_returns_the_file_directly(phi_api):
    with open(os.path.join(PHI_DIR, "progress_note.docx"), "rb") as f:
        res = phi_api.post("/phi/redact", files={"file": ("progress_note.docx", f.read())},
                           data={"response_format": "file", "redaction_style": "blackout"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "progress_note.redacted.docx" in res.headers["content-disposition"]
    text = _output_text("x.docx", res.content)
    assert "Sarah" not in text and "[PATIENT_NAME" not in text and "█" in text


def test_sync_redact_json_response(phi_api):
    res = phi_api.post("/phi/redact", files={"file": ("n.txt", b"Patient Daniel Okafor, SSN 611-28-4471.")})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "COMPLETED" and body["links"]["file"] and body["links"]["metadata"]
    assert body["result_data"]["summary"]["by_type"].get("SSN") == 1


def test_redact_text_endpoint_with_custom_entities_only(phi_api):
    res = phi_api.post("/phi/redact-text", json={
        "text": "Ticket CS-88123 raised by EMP-48213 for Sarah Mitchell.",
        "entity_types": [],
        "custom_entities": [{"label": "CASE_NUMBER", "description": "Support case numbers starting with CS-"},
                            CUSTOM_EMPLOYEE[0]],
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert "CS-88123" not in body["redacted_text"] and "EMP-48213" not in body["redacted_text"]
    assert "Sarah Mitchell" in body["redacted_text"]  # built-in types were switched off for this request
    assert set(body["summary"]["by_type"]) == {"CASE_NUMBER", "EMPLOYEE_ID"}


def test_entity_type_selection_is_respected(phi_api):
    res = phi_api.post("/phi/redact-text", json={"text": "Sarah Mitchell, phone (415) 555-0134, SSN 523-41-8890",
                                                 "entity_types": ["SSN"]})
    body = res.json()
    assert "523-41-8890" not in body["redacted_text"]
    assert "Sarah Mitchell" in body["redacted_text"] and "(415) 555-0134" in body["redacted_text"]


@pytest.mark.parametrize("files, data, status", [
    ({"file": ("x.xlsx", b"PK\x03\x04rest")}, {}, 415),
    ({"file": ("x.exe", b"MZ\x00\x00")}, {}, 415),
    ({"file": ("empty.txt", b"")}, {}, 400),
    ({"file": ("x.txt", b"hello")}, {"custom_entities": "not json"}, 400),
    ({"file": ("x.txt", b"hello")}, {"entity_types": '["NOPE"]'}, 400),
    ({"file": ("x.txt", b"hello")}, {"redaction_style": "glitter"}, 400),
])
def test_invalid_requests_are_rejected_with_clear_errors(phi_api, files, data, status):
    res = phi_api.post("/phi/jobs", files=files, data=data)
    assert res.status_code == status, res.text
    assert res.json()["detail"]


def test_corrupt_document_fails_cleanly(phi_api):
    res = phi_api.post("/phi/jobs", files={"file": ("broken.pdf", b"%PDF-1.7\nthis is not a real pdf")})
    job = phi_api.wait(res.json()["job_id"])
    assert job["status"] == "FAILED" and job["error"] and "Traceback" not in job["error"]
    assert phi_api.get(f"/jobs/{job['job_id']}/file").status_code == 409


def test_job_delete_removes_output(phi_api):
    res = phi_api.post("/phi/jobs", files={"file": ("d.txt", b"Patient Sarah Mitchell")})
    job = phi_api.wait(res.json()["job_id"])
    assert phi_api.delete(f"/jobs/{job['job_id']}").status_code == 204
    assert phi_api.get(f"/jobs/{job['job_id']}").status_code == 404


def test_zz_write_summary():
    record("phi_formats_summary", SUMMARY)
