"""Comprehensive per-format document section extraction, shared by BOTH:

  - the interactive upload path (app/extract.py, used by /api/generate), and
  - the RAG/SharePoint ingestion path (app/rag/chunking.py).

Building this once here (instead of twice) means both paths get the exact
same coverage for text, tables, embedded images, charts and scanned pages.

Design principles (see chat history for the full reasoning):
  - Every embedded image is sent to vision; the MODEL decides whether it is
    purely decorative (logo/icon/bullet) or meaningful, in the same call that
    produces the description. Pixel size is only a near-zero sanity floor
    (skips literal 1px tracking images), never a content filter - Azure OpenAI
    usage here has no meaningful quota/cost constraint for this corpus size.
  - Native charts (PowerPoint, Excel) are read from their underlying DATA
    (categories/series/values), not rendered and vision-guessed - this is
    exact, not a lossy approximation.
  - Images are found regardless of WHERE they live in a file: PowerPoint
    grouped shapes are traversed recursively; Word headers/footers (a
    different OOXML part to the body) are scanned in addition to the body;
    PDF pages get their individual embedded image objects extracted, not just
    a whole-page render when the page has substantial surrounding text.
"""

from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path
from typing import List, Tuple

from .config import Settings, get_settings
from .rag.vision import describe_or_skip, is_large_enough

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Binary formats with no text to recover. Skipped silently (logged), never retried.
REJECTED_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".wmv", ".mkv", ".webm",
    ".mp3", ".wav", ".m4a",
    ".zip", ".7z", ".rar", ".tar", ".gz",
    ".exe", ".dll", ".msi", ".iso",
}

# Legacy Office binaries with no reliable pure-Python reader. .xls is NOT here:
# xlrd 2.x reads it. .doc/.ppt would need antiword/LibreOffice.
UNREADABLE_LEGACY_EXTENSIONS = {".doc", ".ppt"}


class ExtractionError(ValueError):
    """Raised when a file cannot be turned into text."""


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _image_dims_ok(blob: bytes, settings: Settings) -> bool:
    """Near-zero sanity floor only - real decorative/meaningful judgment happens
    inside describe_or_skip(). Returns False (skip) if the blob isn't a raster
    image PIL can open at all (e.g. an EMF/WMF vector graphic)."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(blob)) as img:
            return is_large_enough(img.width, img.height, settings)
    except Exception:  # noqa: BLE001
        return False


def _digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _describe_image_chunk(blob: bytes, settings: Settings, label: str) -> str:
    """Vision-describe one image, with audit logging either way."""
    description = describe_or_skip(blob, settings=settings)
    if description:
        log.info("Vision KEPT %s (%d bytes).", label, len(blob))
    else:
        log.info("Vision SKIPPED %s as decorative or unreadable (%d bytes).", label, len(blob))
    return description or ""


# --------------------------------------------------------------------- PDF
def extract_pdf_sections(data: bytes, settings: Settings) -> List[Tuple[str, str]]:
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

    render_doc = None
    if settings.rag_vision_enabled:
        try:
            import pymupdf

            render_doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:  # noqa: BLE001 - rendering is optional
            log.warning("Could not open PDF for image handling: %s", exc)

    sections: List[Tuple[str, str]] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - skip unreadable pages, don't fail the doc
            log.warning("PDF page %d could not be read: %s", index, exc)
            text = ""

        extra: List[str] = []
        if render_doc is not None:
            page_num = index - 1
            # Mechanism 1: pull out each embedded image object individually, so a
            # chart embedded on an otherwise text-heavy page is never missed.
            try:
                for img_i, img_info in enumerate(render_doc[page_num].get_images(full=True), start=1):
                    xref = img_info[0]
                    try:
                        image_dict = render_doc.extract_image(xref)
                        blob = image_dict["image"]
                        if not _image_dims_ok(blob, settings):
                            continue
                        description = _describe_image_chunk(blob, settings, f"PDF page {index} image {img_i}")
                        if description:
                            extra.append(f"[Image {img_i}] {description}")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Could not process image %d on PDF page %d: %s", img_i, index, exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not enumerate images on PDF page %d: %s", index, exc)

            # Mechanism 2: if the page still has little/no text (likely scanned,
            # or a vector-drawn chart with no embedded image object at all),
            # render the whole page and let vision read it.
            if len(text.strip()) < settings.rag_vision_min_page_chars:
                try:
                    pixmap = render_doc[page_num].get_pixmap(dpi=150)
                    description = _describe_image_chunk(
                        pixmap.tobytes("png"), settings, f"PDF page {index} (whole-page render)"
                    )
                    if description:
                        extra.append(description)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not render PDF page %d as an image: %s", index, exc)

        if extra:
            text = (text + "\n" + "\n".join(extra)).strip() if text.strip() else "\n".join(extra)
        if text.strip():
            sections.append((f"Page {index}", text))

    if render_doc is not None:
        render_doc.close()
    return sections


# -------------------------------------------------------------------- DOCX
def extract_docx_sections(data: bytes, settings: Settings) -> List[Tuple[str, str]]:
    import docx
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    document = docx.Document(io.BytesIO(data))
    parts: List[str] = []
    for p in document.paragraphs:
        if not p.text.strip():
            continue
        # Mark heading-styled paragraphs the same way Markdown does ("## Heading"),
        # so the chunker can later split on topic boundaries instead of merging
        # unrelated sections just because they fit under the token budget.
        style_name = (p.style.name if p.style else "") or ""
        if style_name.startswith("Heading") or style_name == "Title":
            parts.append(f"\n\n## {p.text.strip()}")
        else:
            parts.append(p.text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    sections: List[Tuple[str, str]] = [("", "\n".join(parts))] if parts else []

    if settings.rag_vision_enabled:
        # Body images live in document.part; header/footer images live in their
        # OWN separate OOXML parts, invisible to document.part.rels.
        doc_parts = [document.part]
        for section in document.sections:
            for hf in (section.header, section.footer):
                try:
                    if hf is not None and hf.part is not None:
                        doc_parts.append(hf.part)
                except Exception:  # noqa: BLE001 - some sections may have no header/footer
                    continue

        image_num = 0
        seen_partnames = set()
        seen_digests = set()
        for doc_part in doc_parts:
            for rel in doc_part.rels.values():
                if rel.reltype != RT.IMAGE:
                    continue
                partname = rel.target_part.partname
                if partname in seen_partnames:
                    continue  # same image reused (e.g. logo on every page header)
                seen_partnames.add(partname)
                image_num += 1
                try:
                    blob = rel.target_part.blob
                    digest = _digest(blob)
                    if digest in seen_digests:
                        # The same picture stored again under another part name. Form-style
                        # documents do this for every checkbox: one upload held 200 copies of
                        # two icons and spent 5+ minutes on identical vision calls.
                        continue
                    seen_digests.add(digest)
                    if not _image_dims_ok(blob, settings):
                        continue
                    description = _describe_image_chunk(blob, settings, f"DOCX image {image_num}")
                    if description:
                        sections.append((f"Image {image_num}", description))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not process embedded image %d: %s", image_num, exc)
    return sections


# -------------------------------------------------------------------- PPTX
def _iter_pptx_shapes(shapes):
    """Recursively yield every shape, descending into group shapes."""
    for shape in shapes:
        yield shape
        if shape.shape_type == 6:  # MSO_SHAPE_TYPE.GROUP
            yield from _iter_pptx_shapes(shape.shapes)


def _pptx_chart_to_text(chart) -> str:
    lines: List[str] = [f"Chart type: {chart.chart_type}"]
    try:
        if chart.has_title:
            title = chart.chart_title.text_frame.text.strip()
            if title:
                lines.insert(0, f"Chart title: {title}")
    except Exception:  # noqa: BLE001 - title is a nice-to-have, not required
        pass
    try:
        categories = [str(c) for c in chart.plots[0].categories]
    except Exception:  # noqa: BLE001
        categories = []
    for series in chart.series:
        try:
            values = list(series.values)
        except Exception:  # noqa: BLE001
            continue
        if categories and len(categories) == len(values):
            pairs = ", ".join(f"{c}: {v}" for c, v in zip(categories, values))
        else:
            pairs = ", ".join(str(v) for v in values)
        lines.append(f"Series '{series.name}': {pairs}")
    return "\n".join(lines)


def extract_pptx_sections(data: bytes, settings: Settings) -> List[Tuple[str, str]]:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    sections: List[Tuple[str, str]] = []
    for index, slide in enumerate(presentation.slides, start=1):
        slide_parts: List[str] = []
        for shape in _iter_pptx_shapes(slide.shapes):
            if shape.shape_type == 6:  # GROUP - container only, children yielded separately
                continue
            if shape.has_text_frame and shape.text_frame.text.strip():
                slide_parts.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        slide_parts.append(" | ".join(cells))
            if getattr(shape, "has_chart", False):
                try:
                    slide_parts.append(_pptx_chart_to_text(shape.chart))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not read native chart on slide %d: %s", index, exc)
            if settings.rag_vision_enabled and shape.shape_type == 13:  # MSO_SHAPE_TYPE.PICTURE
                try:
                    width_px, height_px = shape.image.size
                    if is_large_enough(width_px, height_px, settings):
                        description = _describe_image_chunk(
                            shape.image.blob, settings, f"Slide {index} picture"
                        )
                        if description:
                            slide_parts.append(f"[Image] {description}")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not process a picture on slide %d: %s", index, exc)
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        if notes:
            slide_parts.append(f"[Speaker notes] {notes}")
        if slide_parts:
            sections.append((f"Slide {index}", "\n".join(slide_parts)))
    return sections


# -------------------------------------------------------------------- XLSX
def _xlsx_chart_title(chart) -> str:
    try:
        return chart.title.tx.rich.p[0].r[0].t.strip()
    except Exception:  # noqa: BLE001 - title is a nice-to-have, not required
        return ""


def _xlsx_chart_to_text(chart, index: int) -> str:
    lines: List[str] = [f"Chart {index} type: {type(chart).__name__}"]
    title = _xlsx_chart_title(chart)
    if title:
        lines.insert(0, f"Chart {index} title: {title}")
    for series in getattr(chart, "series", []):
        try:
            cache = series.val.numRef.numCache
            values = [pt.v for pt in cache.pt] if cache else None
        except Exception:  # noqa: BLE001
            values = None
        ref = None
        try:
            ref = series.val.numRef.f
        except Exception:  # noqa: BLE001
            pass
        if values:
            lines.append(f"Series values: {values}")
        elif ref:
            # Cache wasn't populated (rare for genuinely Excel-authored files) -
            # the raw cell range is still a useful signal, and the underlying
            # cells are also captured separately as ordinary sheet rows.
            lines.append(f"Series range: {ref}")
    return "\n".join(lines)


def extract_xlsx_sections(data: bytes, settings: Settings) -> List[Tuple[str, str]]:
    from openpyxl import load_workbook

    # Embedded-image/chart access needs the non-read-only loader; our corpora
    # are small/medium documents so the extra memory use is acceptable.
    read_only = not settings.rag_vision_enabled
    workbook = load_workbook(io.BytesIO(data), read_only=read_only, data_only=True)
    sections: List[Tuple[str, str]] = []
    try:
        for sheet in workbook.worksheets:
            rows: List[str] = []
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if v is None else str(v).strip() for v in row]
                if any(cells):
                    rows.append(" | ".join(cells))

            if settings.rag_vision_enabled:
                for i, chart in enumerate(getattr(sheet, "_charts", []), start=1):
                    try:
                        rows.append(_xlsx_chart_to_text(chart, i))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Could not read native chart %d on sheet %s: %s", i, sheet.title, exc)

                for i, image in enumerate(getattr(sheet, "_images", []), start=1):
                    try:
                        blob = image._data()
                        if not _image_dims_ok(blob, settings):
                            continue
                        description = _describe_image_chunk(
                            blob, settings, f"Sheet '{sheet.title}' image {i}"
                        )
                        if description:
                            rows.append(f"[Image {i}] {description}")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Could not process embedded image on sheet %s: %s", sheet.title, exc)

            if rows:
                sections.append((f"Sheet: {sheet.title}", "\n".join(rows)))
    finally:
        workbook.close()
    return sections


# --------------------------------------------------------------- text/image
def extract_text_sections(data: bytes, settings: Settings) -> List[Tuple[str, str]]:
    text = _decode(data).strip()
    return [("", text)] if text else []


def extract_html_sections(data: bytes, settings: Settings) -> List[Tuple[str, str]]:
    """Visible text from an HTML page. Scripts and styles are removed entirely -
    an app-generated page is mostly JavaScript, and indexing that would bury the
    real content. Tables become pipe-delimited rows, matching the docx/xlsx
    convention so downstream table detection works identically."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_decode(data), "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    table_rows: List[str] = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if any(cells):
                table_rows.append(" | ".join(cells))
        # Remove so the body text below does not repeat the table content.
        table.decompose()

    parts: List[str] = []
    body_text = soup.get_text("\n", strip=True)
    if body_text:
        parts.append(body_text)
    parts.extend(table_rows)
    joined = "\n".join(parts).strip()
    return [("", joined)] if joined else []


def extract_xls_sections(data: bytes, settings: Settings) -> List[Tuple[str, str]]:
    """Legacy Excel 97-2003 workbooks via xlrd (v2.x reads .xls only, which is
    exactly the gap openpyxl leaves). Same 'Sheet: name' / pipe-row shape as the
    modern .xlsx extractor so both look identical to the chunker."""
    import xlrd

    # A modern workbook saved under a legacy name (e.g. "Forrester responses.XLS")
    # is a zip archive, which xlrd refuses outright. Route it to the .xlsx reader.
    if data[:4] == b"PK\x03\x04":
        return extract_xlsx_sections(data, settings)

    book = xlrd.open_workbook(file_contents=data)
    sections: List[Tuple[str, str]] = []
    for sheet in book.sheets():
        rows: List[str] = []
        for row_index in range(sheet.nrows):
            cells = [str(sheet.cell_value(row_index, c)).strip() for c in range(sheet.ncols)]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            sections.append((f"Sheet: {sheet.name}", "\n".join(rows)))
    return sections


def _looks_like_text(data: bytes, sample_bytes: int = 4096) -> bool:
    """Is this plausibly a text file under an extension we do not recognise?
    Lets future formats (.xml, .yaml, .rst) work without a code change, while
    stopping binary junk being indexed as gibberish."""
    chunk = data[:sample_bytes]
    if not chunk or b"\x00" in chunk:
        return False
    for encoding in ("utf-8", "cp1252"):
        try:
            decoded = chunk.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return False
    if not decoded:
        return False
    printable = sum(1 for ch in decoded if ch.isprintable() or ch in "\r\n\t")
    return printable / len(decoded) >= 0.9


def extract_image_sections(data: bytes, settings: Settings) -> List[Tuple[str, str]]:
    """Standalone image file (screenshot, chart export, scanned page as a photo, ...)."""
    if not settings.rag_vision_enabled or not _image_dims_ok(data, settings):
        return []
    description = _describe_image_chunk(data, settings, "standalone image")
    return [("", description)] if description else []


_SECTION_EXTRACTORS = {
    ".pdf": extract_pdf_sections,
    ".docx": extract_docx_sections,
    ".docm": extract_docx_sections,
    ".dotx": extract_docx_sections,
    ".pptx": extract_pptx_sections,
    ".pptm": extract_pptx_sections,
    ".potx": extract_pptx_sections,
    ".xlsx": extract_xlsx_sections,
    ".xlsm": extract_xlsx_sections,
    ".xltx": extract_xlsx_sections,
    ".xls": extract_xls_sections,
    ".html": extract_html_sections,
    ".htm": extract_html_sections,
    ".png": extract_image_sections,
    ".jpg": extract_image_sections,
    ".jpeg": extract_image_sections,
    ".gif": extract_image_sections,
    ".webp": extract_image_sections,
    ".bmp": extract_image_sections,
}


def extract_sections(
    filename: str, data: bytes, settings: Settings | None = None
) -> List[Tuple[str, str]]:
    """Split a document into (location_label, text) sections, one per page/slide/sheet,
    with embedded images/charts/scanned pages folded in as ordinary text.

    Returns [] for anything unreadable rather than raising: one unusable file in a
    117-file SharePoint corpus must not abort the run.
    """
    settings = settings or get_settings()
    suffix = Path(filename).suffix.lower()

    if suffix in REJECTED_EXTENSIONS:
        log.info("Skipping %s: %s is a binary format with no extractable text.", filename, suffix)
        return []
    if suffix in UNREADABLE_LEGACY_EXTENSIONS:
        log.info("Skipping %s: %s is a legacy binary format with no reliable reader.", filename, suffix)
        return []

    extractor = _SECTION_EXTRACTORS.get(suffix)
    if extractor is not None:
        return extractor(data, settings)

    if _looks_like_text(data):
        log.info("Reading %s as plain text (unrecognised extension %s).", filename, suffix or "(none)")
        return extract_text_sections(data, settings)

    log.info("Skipping %s: unrecognised extension %s and content is not text.", filename, suffix or "(none)")
    return []
