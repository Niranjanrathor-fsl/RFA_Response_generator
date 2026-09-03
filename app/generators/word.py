"""Word (.docx) generator - python-docx.

Replaces the hand-rolled OOXML writer in the Cowork skill's officegen.js. A real
library gives us proper styles, tables, headers/footers and page numbering.
"""

from __future__ import annotations

import io
from typing import Sequence

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from .. import brand
from ..schemas import ResponseDocument, SourceInfo
from ._common import MEDIA_TYPES, output_filename, split_paragraphs

DARK = RGBColor(*brand.hex_to_rgb(brand.DARK_BLUE))
ORANGE = RGBColor(*brand.hex_to_rgb(brand.ORANGE))
BRIGHT = RGBColor(*brand.hex_to_rgb(brand.BRIGHT_BLUE))
BODY_GREY = RGBColor(0x33, 0x38, 0x4F)


def _shade(cell, hex_colour: str) -> None:
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), brand.office_hex(hex_colour))
    cell._tc.get_or_add_tcPr().append(shading)


def _configure_styles(document: Document) -> None:
    normal = document.styles["Normal"]
    normal.font.name = brand.OFFICE_FONT
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = BODY_GREY
    # Ensure the east-asian font mapping matches, or Word substitutes silently.
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), brand.OFFICE_FONT)

    for name, size, colour, bold in (
        ("Heading 1", 19, DARK, True),
        ("Heading 2", 15, DARK, True),
        ("Heading 3", 12, BRIGHT, True),
    ):
        style = document.styles[name]
        style.font.name = brand.OFFICE_FONT
        style.font.size = Pt(size)
        style.font.color.rgb = colour
        style.font.bold = bold


def _add_footer(document: Document) -> None:
    for section in document.sections:
        paragraph = section.footer.paragraphs[0]
        paragraph.text = f"{brand.footer_text()}    |    {brand.TAGLINE}"
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.runs[0]
        run.font.size = Pt(8)
        run.font.color.rgb = DARK
        run.font.name = brand.OFFICE_FONT


def _cover(document: Document, doc: ResponseDocument, sources: Sequence[SourceInfo]) -> None:
    logo = brand.logo_path(brand.LOGO_DARK)
    if logo.is_file():
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        paragraph.add_run().add_picture(str(logo), width=Inches(2.1))

    document.add_paragraph()
    title = document.add_paragraph()
    title_run = title.add_run(doc.title)
    title_run.font.size = Pt(28)
    title_run.font.bold = True
    title_run.font.color.rgb = DARK
    title_run.font.name = brand.OFFICE_FONT

    if doc.subtitle:
        subtitle = document.add_paragraph()
        subtitle_run = subtitle.add_run(doc.subtitle)
        subtitle_run.font.size = Pt(13)
        subtitle_run.font.color.rgb = BODY_GREY
        subtitle_run.font.name = brand.OFFICE_FONT

    if doc.mode == "rfi":
        note = document.add_paragraph()
        note_run = note.add_run(
            f"{doc.question_count} question"
            f"{'' if doc.question_count == 1 else 's'} answered."
        )
        note_run.font.size = Pt(11)
        note_run.font.bold = True
        note_run.font.color.rgb = ORANGE
        note_run.font.name = brand.OFFICE_FONT

    if sources:
        document.add_paragraph()
        heading = document.add_paragraph()
        heading_run = heading.add_run("Source documents")
        heading_run.font.size = Pt(11)
        heading_run.font.bold = True
        heading_run.font.color.rgb = DARK
        heading_run.font.name = brand.OFFICE_FONT
        for source in sources:
            document.add_paragraph(source.name, style="List Bullet")

    if doc.metrics:
        document.add_paragraph()
        columns = min(3, len(doc.metrics))
        rows = (len(doc.metrics) + columns - 1) // columns
        table = document.add_table(rows=rows * 2, cols=columns)
        table.style = "Table Grid"
        for index, metric in enumerate(doc.metrics):
            row, column = divmod(index, columns)
            value_cell = table.cell(row * 2, column)
            label_cell = table.cell(row * 2 + 1, column)
            value_cell.text = metric.value
            label_cell.text = metric.label
            _shade(value_cell, brand.GRAY)
            _shade(label_cell, brand.GRAY)
            value_run = value_cell.paragraphs[0].runs[0] if value_cell.paragraphs[0].runs else None
            if value_run:
                value_run.font.size = Pt(18)
                value_run.font.bold = True
                value_run.font.color.rgb = ORANGE if metric.accent else DARK
            label_run = label_cell.paragraphs[0].runs[0] if label_cell.paragraphs[0].runs else None
            if label_run:
                label_run.font.size = Pt(8.5)
                label_run.font.color.rgb = BODY_GREY

    document.add_section(WD_SECTION.NEW_PAGE)


def _render_table(document: Document, table_data) -> None:
    if not table_data or not table_data.rows:
        return
    width = max(
        len(table_data.headers),
        max((len(row) for row in table_data.rows), default=0),
    )
    if width == 0:
        return
    has_header = bool(table_data.headers)
    table = document.add_table(rows=(1 if has_header else 0) + len(table_data.rows), cols=width)
    table.style = "Table Grid"
    offset = 0
    if has_header:
        for column in range(width):
            cell = table.cell(0, column)
            cell.text = table_data.headers[column] if column < len(table_data.headers) else ""
            _shade(cell, brand.DARK_BLUE)
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.bold = True
                    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                    run.font.size = Pt(9.5)
        offset = 1
    for row_index, row in enumerate(table_data.rows):
        for column in range(width):
            cell = table.cell(row_index + offset, column)
            cell.text = row[column] if column < len(row) else ""
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(9.5)
    document.add_paragraph()


def generate(
    document_data: ResponseDocument,
    sources: Sequence[SourceInfo] | None = None,
) -> tuple[bytes, str, str]:
    sources = list(sources or [])
    document = Document()
    _configure_styles(document)
    _cover(document, document_data, sources)

    for tab in document_data.tabs:
        document.add_heading(tab.name, level=1)
        if tab.intro:
            document.add_paragraph(tab.intro)

        for bullet in tab.bullets:
            document.add_paragraph(bullet, style="List Bullet")

        for item in tab.qa:
            label = f"{item.n}. {item.q}".strip(". ") if item.n else item.q
            document.add_heading(label, level=3)
            for paragraph_text in split_paragraphs(item.a):
                document.add_paragraph(paragraph_text)

        _render_table(document, tab.table)

        if tab.callout and (tab.callout.title or tab.callout.body):
            callout = document.add_table(rows=1, cols=1)
            callout.style = "Table Grid"
            cell = callout.cell(0, 0)
            _shade(cell, brand.GRAY)
            if tab.callout.title:
                title_paragraph = cell.paragraphs[0]
                title_run = title_paragraph.add_run(tab.callout.title)
                title_run.font.bold = True
                title_run.font.color.rgb = DARK
                title_run.font.size = Pt(11)
                if tab.callout.body:
                    cell.add_paragraph(tab.callout.body)
            elif tab.callout.body:
                cell.paragraphs[0].add_run(tab.callout.body)
            document.add_paragraph()

    _add_footer(document)
    buffer = io.BytesIO()
    document.save(buffer)
    filename = output_filename(document_data.title, "docx")
    return buffer.getvalue(), filename, MEDIA_TYPES["docx"]
