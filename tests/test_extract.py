"""Server-side extraction, replacing the browser extractor."""

from __future__ import annotations

import io

import pytest

from app.extract import ExtractedDocument, ExtractionError, build_corpus, extract_text


def test_plain_text():
    doc = extract_text("notes.txt", b"Q1. Describe your governance.\n\nQ2. And your labs?")
    assert "Q1." in doc.text
    assert doc.words > 5
    assert not doc.is_empty


def test_csv_becomes_pipe_delimited():
    doc = extract_text("data.csv", b"Question,Answer\nLiability?,Capped at 12 months\n")
    assert "Question | Answer" in doc.text


def test_json_is_pretty_printed():
    doc = extract_text("payload.json", b'{"q":"Do you build foundational models?"}')
    assert "foundational models" in doc.text


def test_docx_roundtrip():
    from docx import Document

    document = Document()
    document.add_paragraph("Q1. Describe your liability model.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Area"
    table.cell(0, 1).text = "Detail"
    buffer = io.BytesIO()
    document.save(buffer)

    doc = extract_text("rfi.docx", buffer.getvalue())
    assert "liability model" in doc.text
    assert "Area | Detail" in doc.text


def test_pptx_roundtrip_includes_slide_markers():
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    box.text_frame.text = "Kairos architecture overview"
    buffer = io.BytesIO()
    presentation.save(buffer)

    doc = extract_text("deck.pptx", buffer.getvalue())
    assert "Kairos architecture overview" in doc.text
    assert "--- Slide 1 ---" in doc.text


def test_xlsx_roundtrip_includes_sheet_markers():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Questions"
    sheet.append(["Ref", "Query"])
    sheet.append(["Q1", "Describe your labs"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    doc = extract_text("questions.xlsx", buffer.getvalue())
    assert "--- Sheet: Questions ---" in doc.text
    assert "Q1 | Describe your labs" in doc.text


def test_pdf_roundtrip():
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)

    doc = extract_text("scan.pdf", buffer.getvalue())
    # A blank page yields no text - the user must be told, not left guessing.
    assert doc.is_empty
    assert "scanned" in doc.note.lower()


def test_legacy_format_rejected_with_guidance():
    with pytest.raises(ExtractionError) as excinfo:
        extract_text("old.doc", b"\xd0\xcf\x11\xe0")
    assert "legacy" in str(excinfo.value).lower()


def test_unsupported_extension_rejected():
    with pytest.raises(ExtractionError):
        extract_text("archive.zip", b"PK\x03\x04")


def test_per_document_truncation(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_chars_per_document", 100)
    doc = extract_text("long.txt", b"x" * 5000)
    assert len(doc.text) < 200
    assert "truncated" in doc.text


# --------------------------------------------------------------- corpus tests
def test_single_source_has_no_markers():
    corpus, truncated = build_corpus([ExtractedDocument("only.txt", "Just one document.")])
    assert corpus == "Just one document."
    assert "SOURCE DOCUMENT" not in corpus
    assert truncated is False


def test_multiple_sources_are_delimited_and_named():
    corpus, _ = build_corpus([
        ExtractedDocument("ClientRFI.docx", "Q1. Governance?"),
        ExtractedDocument("Capabilities.pptx", "Kairos overview."),
    ])
    assert "===== SOURCE DOCUMENT 1 of 2: ClientRFI.docx =====" in corpus
    assert "===== SOURCE DOCUMENT 2 of 2: Capabilities.pptx =====" in corpus


def test_empty_documents_are_dropped():
    corpus, _ = build_corpus([
        ExtractedDocument("good.txt", "Real content."),
        ExtractedDocument("blank.pdf", "   "),
    ])
    assert "blank.pdf" not in corpus
    assert "SOURCE DOCUMENT" not in corpus  # only one usable source remains


def test_budget_is_shared_evenly_so_one_file_cannot_crowd_out_another():
    corpus, truncated = build_corpus(
        [ExtractedDocument("big.txt", "a" * 5000), ExtractedDocument("small.txt", "b" * 5000)],
        max_total_chars=1000,
    )
    assert truncated is True
    assert "big.txt" in corpus and "small.txt" in corpus
    assert corpus.count("document truncated") == 2


def test_no_usable_documents_returns_empty():
    corpus, truncated = build_corpus([ExtractedDocument("blank.txt", "")])
    assert corpus == ""
    assert truncated is False
