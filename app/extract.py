"""Server-side document text extraction.

Replaces the browser-side assets/lib/extractor.js from the Cowork skill. Running
extraction on the server means we can use real, maintained parsers (pypdf,
python-docx, python-pptx, openpyxl) instead of hand-rolled ZIP/PDF parsing, and
large or awkward files no longer depend on what the browser can manage.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

from .config import get_settings
from .document_sections import (
    ExtractionError,
    _decode,
    extract_docx_sections,
    extract_html_sections,
    extract_pdf_sections,
    extract_pptx_sections,
    extract_xls_sections,
    extract_xlsx_sections,
)

log = logging.getLogger(__name__)

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".text", ".log", ".rst"}
CSV_EXTENSIONS = {".csv", ".tsv"}
JSON_EXTENSIONS = {".json"}
PDF_EXTENSIONS = {".pdf"}
WORD_EXTENSIONS = {".docx", ".docm", ".dotx"}
PPT_EXTENSIONS = {".pptx", ".pptm", ".potx"}
EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xltx"}
LEGACY_EXCEL_EXTENSIONS = {".xls"}
HTML_EXTENSIONS = {".html", ".htm"}

SUPPORTED_EXTENSIONS = (
    TEXT_EXTENSIONS
    | CSV_EXTENSIONS
    | JSON_EXTENSIONS
    | PDF_EXTENSIONS
    | WORD_EXTENSIONS
    | PPT_EXTENSIONS
    | EXCEL_EXTENSIONS
    | LEGACY_EXCEL_EXTENSIONS
    | HTML_EXTENSIONS
)

# Legacy binary formats the modern parsers cannot open. (.xls is read by xlrd.)
LEGACY_EXTENSIONS = {".doc", ".ppt"}


@dataclass
class ExtractedDocument:
    name: str
    text: str
    note: str = ""

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


# --------------------------------------------------------------------- helpers
def _clean(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: List[str] = []
    blanks = 0
    for line in lines:
        if line.strip():
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    return "\n".join(out).strip()


# -------------------------------------------------------------------- parsers
# PDF/DOCX/PPTX/XLSX delegate to app.document_sections - the same comprehensive
# per-format extraction (including embedded images/charts/scanned pages) used
# by the RAG ingestion pipeline, so both paths have identical coverage.
def _join_plain(sections: List[Tuple[str, str]]) -> str:
    return "\n\n".join(text for _, text in sections if text.strip())


def _join_with_markers(sections: List[Tuple[str, str]]) -> str:
    return "\n\n".join(f"--- {location} ---\n{text}" for location, text in sections if text.strip())


def _extract_pdf(data: bytes) -> str:
    return _join_plain(extract_pdf_sections(data, get_settings()))


def _extract_docx(data: bytes) -> str:
    return _join_plain(extract_docx_sections(data, get_settings()))


def _extract_pptx(data: bytes) -> str:
    return _join_with_markers(extract_pptx_sections(data, get_settings()))


def _extract_xlsx(data: bytes) -> str:
    return _join_with_markers(extract_xlsx_sections(data, get_settings()))


def _extract_xls(data: bytes) -> str:
    return _join_with_markers(extract_xls_sections(data, get_settings()))


def _extract_html(data: bytes) -> str:
    return _join_plain(extract_html_sections(data, get_settings()))


def _extract_csv(data: bytes, delimiter: str) -> str:
    text = _decode(data)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return "\n".join(" | ".join(c.strip() for c in row) for row in reader if any(row))


def _extract_json(data: bytes) -> str:
    text = _decode(data)
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except json.JSONDecodeError:
        return text


# ------------------------------------------------------------------- public API
def extract_text(filename: str, data: bytes) -> ExtractedDocument:
    """Turn an uploaded file into plain text, capped at max_chars_per_document."""
    settings = get_settings()
    suffix = Path(filename).suffix.lower()
    note = ""

    if suffix in LEGACY_EXTENSIONS:
        raise ExtractionError(
            f"{suffix} is a legacy binary format. Save it as "
            f"{suffix}x (e.g. .docx) and re-upload, or paste the text instead."
        )

    if suffix in PDF_EXTENSIONS:
        text = _extract_pdf(data)
        if not text.strip():
            note = (
                "No selectable text found - this looks like a scanned or image-only "
                "PDF. Paste the text instead, or run OCR first."
            )
    elif suffix in WORD_EXTENSIONS:
        text = _extract_docx(data)
    elif suffix in PPT_EXTENSIONS:
        text = _extract_pptx(data)
    elif suffix in EXCEL_EXTENSIONS:
        text = _extract_xlsx(data)
    elif suffix in LEGACY_EXCEL_EXTENSIONS:
        text = _extract_xls(data)
    elif suffix in HTML_EXTENSIONS:
        text = _extract_html(data)
    elif suffix in CSV_EXTENSIONS:
        text = _extract_csv(data, "\t" if suffix == ".tsv" else ",")
    elif suffix in JSON_EXTENSIONS:
        text = _extract_json(data)
    elif suffix in TEXT_EXTENSIONS or not suffix:
        text = _decode(data)
    else:
        raise ExtractionError(
            f"Unsupported file type '{suffix}'. Supported: PDF, Word (.docx), "
            "PowerPoint (.pptx), Excel (.xlsx/.xls), HTML, and "
            "text/CSV/TSV/JSON/Markdown."
        )

    text = _clean(text)
    if len(text) > settings.max_chars_per_document:
        text = text[: settings.max_chars_per_document] + "\n[... document truncated ...]"
        note = note or "Document was long and has been truncated for the model."
    return ExtractedDocument(name=filename, text=text, note=note)


def build_corpus(
    documents: Sequence[ExtractedDocument],
    max_total_chars: int | None = None,
) -> Tuple[str, bool]:
    """Merge documents into one delimited corpus.

    A single source is passed through unchanged. Two or more are wrapped in
    "===== SOURCE DOCUMENT n of N: <name> =====" markers so the model can attribute
    facts back to the file they came from. The character budget is split evenly so
    one large deck cannot crowd out a short RFI.

    Returns (corpus, truncated).
    """
    settings = get_settings()
    limit = max_total_chars or settings.max_total_corpus_chars
    usable = [d for d in documents if not d.is_empty]
    if not usable:
        return "", False
    if len(usable) == 1:
        text = usable[0].text
        if len(text) > limit:
            return text[:limit] + "\n[... content truncated ...]", True
        return text, False

    share = max(1, limit // len(usable))
    truncated = False
    blocks: List[str] = []
    for index, doc in enumerate(usable, start=1):
        body = doc.text
        if len(body) > share:
            body = body[:share] + "\n[... document truncated ...]"
            truncated = True
        blocks.append(
            f"===== SOURCE DOCUMENT {index} of {len(usable)}: {doc.name} =====\n{body}"
        )
    return "\n\n".join(blocks), truncated
