"""Format redactors with a stub model. PDF tests use text-layer pages only, so no OCR or GPU is needed."""
import asyncio
import io
import os
import zipfile

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen import canvas

from shared.phi_catalog import normalize_phi_options
from tests.unit.conftest import StubLLM
from worker.phi.detector import Detector
from worker.phi.docx import redact_docx
from worker.phi.matching import LabelRegistry
from worker.phi.pdf import redact_pdf
from worker.phi.text import decode_bytes, redact_text_file
from worker.pdf_util import document_text, metadata

KNOWN = {"Sarah Mitchell": "PATIENT_NAME", "Daniel Okafor": "PATIENT_NAME", "MRN-4481920": "MRN",
         "Dr. Alan Reyes": "PROVIDER_NAME", "EMP-48213": "EMPLOYEE_ID", "sarah@example.org": "EMAIL"}
CUSTOM = [{"label": "EMPLOYEE_ID", "description": "EMP numbers"}]


def _run(fn, src, dst, progress, **opts):
    options = normalize_phi_options({"custom_entities": CUSTOM, **opts})
    detector = Detector(options, StubLLM(KNOWN))
    return asyncio.run(fn(str(src), str(dst), options, detector, LabelRegistry(options["redaction_style"]), progress))


# ---- Text ----------------------------------------------------------------------

def test_text_file_keeps_format_and_encoding(tmp_path, progress_log):
    _, progress = progress_log
    src = tmp_path / "note.md"
    src.write_bytes("﻿# Note\n\n**Patient:** Sarah Mitchell (MRN-4481920)\n| Dr. Alan Reyes | café |\n".encode("utf-8"))
    dst = tmp_path / "note.redacted.md"
    meta = _run(redact_text_file, src, dst, progress)
    data = dst.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")  # byte order mark preserved
    out = data.decode("utf-8-sig")
    assert out == "# Note\n\n**Patient:** [PATIENT_NAME_1] ([MRN_1])\n| [PROVIDER_NAME_1] | café |\n"
    assert meta["verification"]["status"] == "passed"
    assert meta["occurrences"][0]["line"] == 3


def test_decode_falls_back_to_windows_1252():
    text, encoding, _ = decode_bytes("José".encode("cp1252"))
    assert text == "José" and encoding == "cp1252"


# ---- DOCX ----------------------------------------------------------------------

def _docx_with_everything(path):
    doc = Document()
    doc.core_properties.author = "Dr. Alan Reyes"
    doc.sections[0].header.paragraphs[0].text = "Patient: Sarah Mitchell"
    doc.sections[0].footer.paragraphs[0].text = "Case manager EMP-48213"
    p = doc.add_paragraph("Name: ")
    p.add_run("Sarah").bold = True
    p.add_run(" ")
    p.add_run("Mitchell").italic = True
    p.add_run(" MRN-4481920")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Brother"
    table.rows[0].cells[1].text = "Daniel Okafor"
    link_para = doc.add_paragraph("Email: ")
    r_id = link_para.part.relate_to("mailto:sarah@example.org",
                                    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                                    is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run, t = OxmlElement("w:r"), OxmlElement("w:t")
    t.text = "sarah@example.org"
    run.append(t)
    link.append(run)
    link_para._p.append(link)
    doc.add_paragraph("Metformin 1000 mg BID.")
    doc.save(path)


def _all_xml(path) -> str:
    with zipfile.ZipFile(path) as z:
        return "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.endswith((".xml", ".rels")))


def test_docx_redacts_body_tables_headers_footers_links_and_metadata(tmp_path, progress_log):
    _, progress = progress_log
    src, dst = tmp_path / "in.docx", tmp_path / "out.docx"
    _docx_with_everything(src)
    meta = _run(redact_docx, src, dst, progress)
    xml = _all_xml(dst)
    for value in KNOWN:
        assert value not in xml, value
    assert "Mitchell" not in xml  # the run split across formatting is fully removed
    assert "Metformin 1000 mg BID." in xml
    out = Document(str(dst))  # still a valid Word document
    texts = [p.text for p in out.paragraphs]
    assert "Name: [PATIENT_NAME_1] [MRN_1]" in texts
    assert out.sections[0].header.paragraphs[0].text == "Patient: [PATIENT_NAME_1]"
    assert out.tables[0].rows[0].cells[1].text == "[PATIENT_NAME_2]"
    assert out.core_properties.author == ""
    assert meta["summary"]["hyperlinks_scrubbed"] == 1
    assert meta["verification"]["status"] == "passed"
    assert {o["part"] for o in meta["occurrences"]} >= {"document", "header1", "footer1"}


def test_docx_rejects_a_non_docx_file(tmp_path, progress_log):
    _, progress = progress_log
    src = tmp_path / "fake.docx"
    src.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="not a valid DOCX"):
        _run(redact_docx, src, tmp_path / "out.docx", progress)


# ---- PDF (text layer) ---------------------------------------------------------------

def _pdf(path, pages, encrypt=None):
    c = canvas.Canvas(str(path), encrypt=encrypt)
    c.setAuthor("Dr. Alan Reyes")
    c.setTitle("Sarah Mitchell chart")
    for lines in pages:
        y = 720
        for line in lines:
            c.setFont("Helvetica", 11)
            c.drawString(72, y, line)
            y -= 18
        c.showPage()
    c.save()


def test_pdf_text_layer_redaction_removes_text_and_keeps_layout(tmp_path, progress_log):
    events, progress = progress_log
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    body = ["Patient Name: Sarah Mitchell", "MRN: MRN-4481920", "Attending: Dr. Alan Reyes",
            "Case manager EMP-48213", "Diagnosis: essential hypertension", "Plan: continue Metformin 1000 mg",
            "Signed for Sarah Mitchell"]
    _pdf(src, [body, ["Page two mentions Daniel Okafor", "and again Sarah Mitchell", "with more clinical notes"]])
    meta = _run(redact_pdf, src, dst, progress)
    pages = document_text(str(dst))
    text = " ".join(pages)
    for value in ("Sarah Mitchell", "MRN-4481920", "Dr. Alan Reyes", "EMP-48213", "Daniel Okafor"):
        assert value not in text, value
    assert "essential hypertension" in text and "Metformin 1000 mg" in text  # rebuilt text layer stays searchable
    assert "[PATIENT_NAME_1]" in text  # labelled placeholder is written on the box
    info = metadata(str(dst))
    assert not info.get("Author") and not info.get("Title")
    assert len(pages) == 2
    assert meta["summary"]["pages_text_layer"] == 2 and meta["summary"]["pages_ocr"] == 0
    assert meta["summary"]["by_type"]["PATIENT_NAME"] == 4
    assert meta["verification"]["status"] == "passed"
    assert all(len(o["boxes"]) >= 1 and o["page"] in (1, 2) for o in meta["occurrences"])
    assert events and events[-1][0] == "verifying"


def test_partially_redacted_words_keep_their_unredacted_part(tmp_path, progress_log):
    _, progress = progress_log
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    _pdf(src, [["Reference ID-MRN-4481920 on file", "Second line with words", "Third line with words"]])
    _run(redact_pdf, src, dst, progress)
    text = document_text(str(dst))[0]
    assert "4481920" not in text and "ID-" in text


def test_pdf_blackout_style_writes_no_labels(tmp_path, progress_log):
    _, progress = progress_log
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    _pdf(src, [["Patient Name: Sarah Mitchell", "Some other clinical text here", "More words to keep the layer"]])
    _run(redact_pdf, src, dst, progress, redaction_style="blackout")
    text = document_text(str(dst))[0]
    assert "Sarah Mitchell" not in text and "[PATIENT_NAME" not in text and "clinical" in text


def test_pdf_password_protected_is_rejected(tmp_path, progress_log):
    _, progress = progress_log
    src = tmp_path / "locked.pdf"
    _pdf(src, [["secret words here and more", "line two", "line three"]],
         encrypt=StandardEncryption("pw", ownerPassword="owner", strength=128))
    with pytest.raises(ValueError, match="password protected"):
        _run(redact_pdf, src, tmp_path / "out.pdf", progress)


def test_ocr_padding_stays_inside_the_page_and_never_inverts():
    from worker.phi.pdf import _padded
    for rect in [(135.4, 121.9, 202.6, 134.4), (0.0, 0.0, 10.0, 12.0), (600.0, 780.0, 612.0, 792.0)]:
        x0, y0, x1, y1 = _padded(rect, False, 612.0, 792.0)
        assert 0 <= x0 <= rect[0] and rect[2] <= x1 <= 612 and 0 <= y0 <= rect[1] and rect[3] <= y1 <= 792
