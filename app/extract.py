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

log = logging.getLogger(__name__)

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".text", ".log", ".rst"}
CSV_EXTENSIONS = {".csv", ".tsv"}
JSON_EXTENSIONS = {".json"}
PDF_EXTENSIONS = {".pdf"}
WORD_EXTENSIONS = {".docx", ".docm", ".dotx"}
PPT_EXTENSIONS = {".pptx", ".pptm", ".potx"}
EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xltx"}

SUPPORTED_EXTENSIONS = (
    TEXT_EXTENSIONS
    | CSV_EXTENSIONS
    | JSON_EXTENSIONS
    | PDF_EXTENSIONS
    | WORD_EXTENSIONS
    | PPT_EXTENSIONS
    | EXCEL_EXTENSIONS
)

# Legacy binary formats the modern parsers cannot open.
LEGACY_EXTENSIONS = {".doc", ".ppt", ".xls"}


class ExtractionError(ValueError):
    """Raised when a file cannot be turned into text."""


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
def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


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
def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise ExtractionError(f"PDF could not be opened: {exc}") from exc
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001 - pypdf raises assorted types here
            raise ExtractionError(
                "PDF is password-protected. Remove the password and re-upload."
            ) from exc
    pages: List[str] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 - never fail the whole doc on one page
            log.warning("PDF page %d could not be read: %s", index, exc)
    return "\n\n".join(p for p in pages if p.strip())


def _extract_docx(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    parts: List[str] = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_pptx(data: bytes) -> str:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    parts: List[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        slide_parts: List[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                slide_parts.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        slide_parts.append(" | ".join(cells))
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        if slide_parts or notes:
            body = "\n".join(slide_parts)
            if notes:
                body += f"\n[Speaker notes] {notes}"
            parts.append(f"--- Slide {index} ---\n{body}")
    return "\n\n".join(parts)


def _extract_xlsx(data: bytes) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts: List[str] = []
    try:
        for sheet in workbook.worksheets:
            rows: List[str] = []
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if v is None else str(v).strip() for v in row]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"--- Sheet: {sheet.title} ---\n" + "\n".join(rows))
    finally:
        workbook.close()
    return "\n\n".join(parts)


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
    elif suffix in CSV_EXTENSIONS:
        text = _extract_csv(data, "\t" if suffix == ".tsv" else ",")
    elif suffix in JSON_EXTENSIONS:
        text = _extract_json(data)
    elif suffix in TEXT_EXTENSIONS or not suffix:
        text = _decode(data)
    else:
        raise ExtractionError(
            f"Unsupported file type '{suffix}'. Supported: PDF, Word (.docx), "
            "PowerPoint (.pptx), Excel (.xlsx), and text/CSV/TSV/JSON/Markdown."
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
