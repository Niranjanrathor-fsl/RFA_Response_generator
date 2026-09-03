"""PowerPoint (.pptx) generator - python-pptx.

Brand rules applied: dark-blue gradient title slide with the white logo, white
content slides with the dark logo, Light Blue accent rule under headings, Orange
used sparingly for emphasis only.
"""

from __future__ import annotations

import io
from typing import List, Sequence

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt

from .. import brand
from ..schemas import ResponseDocument, SourceInfo, Tab
from ._common import MEDIA_TYPES, output_filename, split_paragraphs

SLIDE_WIDTH = Inches(13.333)
SLIDE_HEIGHT = Inches(7.5)

DARK = RGBColor(*brand.hex_to_rgb(brand.DARK_BLUE))
MID = RGBColor(*brand.hex_to_rgb(brand.MID_BLUE))
BRIGHT = RGBColor(*brand.hex_to_rgb(brand.BRIGHT_BLUE))
LIGHT = RGBColor(*brand.hex_to_rgb(brand.LIGHT_BLUE))
ORANGE = RGBColor(*brand.hex_to_rgb(brand.ORANGE))
GRAY = RGBColor(*brand.hex_to_rgb(brand.GRAY))
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
BODY = RGBColor(0x33, 0x38, 0x4F)

# Rough capacity of one content slide before we spill onto a continuation slide.
MAX_BULLETS_PER_SLIDE = 7
MAX_QA_CHARS_PER_SLIDE = 1500


def _blank_layout(presentation: Presentation):
    return presentation.slide_layouts[6]


def _add_textbox(
    slide,
    text: str,
    left: Emu,
    top: Emu,
    width: Emu,
    height: Emu,
    size: int,
    colour: RGBColor,
    bold: bool = False,
    align=PP_ALIGN.LEFT,
    line_spacing: float = 1.15,
):
    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    paragraph = frame.paragraphs[0]
    paragraph.alignment = align
    paragraph.line_spacing = line_spacing
    run = paragraph.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = colour
    run.font.name = brand.OFFICE_FONT
    return box


def _gradient_background(slide) -> None:
    """Brand-approved diagonal Dark -> Mid -> Bright gradient, full bleed."""
    shape = slide.shapes.add_shape(1, 0, 0, SLIDE_WIDTH, SLIDE_HEIGHT)  # 1 = rectangle
    shape.line.fill.background()
    fill = shape.fill
    fill.gradient()
    fill.gradient_angle = 20.0
    stops = fill.gradient_stops
    stops[0].color.rgb = DARK
    stops[0].position = 0.0
    stops[1].color.rgb = BRIGHT
    stops[1].position = 1.0
    shape.shadow.inherit = False
    return shape


def _logo(slide, white: bool, top: Emu, left: Emu | None = None, width: Inches = Inches(1.9)):
    name = brand.LOGO_WHITE if white else brand.LOGO_DARK
    path = brand.logo_path(name)
    if not path.is_file():
        return None
    if left is None:
        left = SLIDE_WIDTH - width - Inches(0.6)
    return slide.shapes.add_picture(str(path), left, top, width=width)


def _accent_rule(slide, top: Emu) -> None:
    rule = slide.shapes.add_shape(1, Inches(0.75), top, Inches(1.1), Pt(3))
    rule.fill.solid()
    rule.fill.fore_color.rgb = LIGHT
    rule.line.fill.background()
    rule.shadow.inherit = False


def _footer(slide) -> None:
    _add_textbox(
        slide,
        brand.footer_text(),
        Inches(0.75),
        SLIDE_HEIGHT - Inches(0.5),
        Inches(8.0),
        Inches(0.3),
        8,
        RGBColor(0x8A, 0x90, 0xA8),
    )


def _title_slide(presentation: Presentation, doc: ResponseDocument, sources: Sequence[SourceInfo]):
    slide = presentation.slides.add_slide(_blank_layout(presentation))
    _gradient_background(slide)
    _logo(slide, white=True, top=Inches(0.6), left=Inches(0.75))
    _add_textbox(
        slide, doc.title, Inches(0.75), Inches(2.6), Inches(11.0), Inches(1.8), 40, WHITE, bold=True
    )
    if doc.subtitle:
        _add_textbox(
            slide, doc.subtitle, Inches(0.75), Inches(4.4), Inches(10.0), Inches(1.2), 17, WHITE
        )
    footer_bits: List[str] = [brand.TAGLINE]
    if doc.mode == "rfi":
        footer_bits.insert(0, f"{doc.question_count} questions answered")
    if sources:
        footer_bits.insert(0, f"{len(sources)} source document{'' if len(sources) == 1 else 's'}")
    _add_textbox(
        slide, "   |   ".join(footer_bits), Inches(0.75), Inches(6.4), Inches(11.0), Inches(0.4), 11, LIGHT
    )
    return slide


def _content_slide(presentation: Presentation, heading: str):
    slide = presentation.slides.add_slide(_blank_layout(presentation))
    _add_textbox(slide, heading, Inches(0.75), Inches(0.45), Inches(9.5), Inches(0.7), 26, DARK, bold=True)
    _accent_rule(slide, Inches(1.25))
    _logo(slide, white=False, top=Inches(0.45), width=Inches(1.6))
    _footer(slide)
    return slide


def _metrics_slide(presentation: Presentation, doc: ResponseDocument) -> None:
    if not doc.metrics:
        return
    slide = _content_slide(presentation, "Headline metrics")
    metrics = doc.metrics[:8]
    columns = 4 if len(metrics) > 3 else max(1, len(metrics))
    card_width = Inches(2.85)
    card_height = Inches(1.55)
    gap = Inches(0.25)
    start_left = Inches(0.75)
    start_top = Inches(1.9)
    for index, metric in enumerate(metrics):
        row, column = divmod(index, columns)
        left = Emu(int(start_left) + column * (int(card_width) + int(gap)))
        top = Emu(int(start_top) + row * (int(card_height) + int(gap)))
        card = slide.shapes.add_shape(1, left, top, card_width, card_height)
        card.fill.solid()
        card.fill.fore_color.rgb = GRAY
        card.line.fill.background()
        card.shadow.inherit = False
        bar = slide.shapes.add_shape(1, left, top, Pt(5), card_height)
        bar.fill.solid()
        bar.fill.fore_color.rgb = ORANGE if metric.accent else BRIGHT
        bar.line.fill.background()
        bar.shadow.inherit = False
        _add_textbox(
            slide, metric.value, Emu(int(left) + int(Inches(0.22))), Emu(int(top) + int(Inches(0.18))),
            Emu(int(card_width) - int(Inches(0.35))), Inches(0.6), 24,
            ORANGE if metric.accent else DARK, bold=True,
        )
        _add_textbox(
            slide, metric.label.upper(), Emu(int(left) + int(Inches(0.22))),
            Emu(int(top) + int(Inches(0.85))), Emu(int(card_width) - int(Inches(0.35))),
            Inches(0.55), 10, BODY,
        )


def _bullet_body(slide, items: Sequence[str], top: Emu = Inches(1.65)) -> None:
    box = slide.shapes.add_textbox(Inches(0.75), top, Inches(11.6), Inches(5.0))
    frame = box.text_frame
    frame.word_wrap = True
    for index, item in enumerate(items):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.line_spacing = 1.25
        paragraph.space_after = Pt(7)
        run = paragraph.add_run()
        run.text = f"▪  {item}"
        run.font.size = Pt(14)
        run.font.color.rgb = BODY
        run.font.name = brand.OFFICE_FONT


def _tab_slides(presentation: Presentation, tab: Tab) -> None:
    blocks: List[str] = []
    if tab.intro:
        blocks.append(tab.intro)
    blocks.extend(tab.bullets)
    if tab.table and tab.table.rows:
        if tab.table.headers:
            blocks.append(" | ".join(tab.table.headers))
        for row in tab.table.rows[:12]:
            blocks.append(" | ".join(row))
    if tab.callout and (tab.callout.title or tab.callout.body):
        blocks.append(f"{tab.callout.title}: {tab.callout.body}".strip(": "))

    chunks = [blocks[i : i + MAX_BULLETS_PER_SLIDE] for i in range(0, len(blocks), MAX_BULLETS_PER_SLIDE)]
    for index, chunk in enumerate(chunks or [[]]):
        heading = tab.name if index == 0 else f"{tab.name} (cont.)"
        slide = _content_slide(presentation, heading)
        if chunk:
            _bullet_body(slide, chunk)

    # Questions get their own slides so answers stay readable.
    pending: List[tuple[str, str]] = []
    pending_chars = 0
    slide_index = 0

    def flush() -> None:
        nonlocal pending, pending_chars, slide_index
        if not pending:
            return
        slide_index += 1
        heading = f"{tab.name} - responses" + (f" ({slide_index})" if slide_index > 1 else "")
        slide = _content_slide(presentation, heading)
        box = slide.shapes.add_textbox(Inches(0.75), Inches(1.65), Inches(11.6), Inches(5.1))
        frame = box.text_frame
        frame.word_wrap = True
        first = True
        for label, answer in pending:
            question_paragraph = frame.paragraphs[0] if first else frame.add_paragraph()
            first = False
            question_paragraph.space_before = Pt(0 if question_paragraph is frame.paragraphs[0] else 10)
            question_run = question_paragraph.add_run()
            question_run.text = label
            question_run.font.size = Pt(13)
            question_run.font.bold = True
            question_run.font.color.rgb = DARK
            question_run.font.name = brand.OFFICE_FONT
            for paragraph_text in split_paragraphs(answer):
                answer_paragraph = frame.add_paragraph()
                answer_paragraph.line_spacing = 1.2
                answer_run = answer_paragraph.add_run()
                answer_run.text = paragraph_text
                answer_run.font.size = Pt(11.5)
                answer_run.font.color.rgb = BODY
                answer_run.font.name = brand.OFFICE_FONT
        pending = []
        pending_chars = 0

    for item in tab.qa:
        label = f"{item.n}  {item.q}".strip() if item.n else item.q
        size = len(label) + len(item.a)
        if pending and pending_chars + size > MAX_QA_CHARS_PER_SLIDE:
            flush()
        pending.append((label, item.a))
        pending_chars += size
    flush()


def generate(
    document_data: ResponseDocument,
    sources: Sequence[SourceInfo] | None = None,
) -> tuple[bytes, str, str]:
    sources = list(sources or [])
    presentation = Presentation()
    presentation.slide_width = SLIDE_WIDTH
    presentation.slide_height = SLIDE_HEIGHT

    _title_slide(presentation, document_data, sources)
    _metrics_slide(presentation, document_data)
    for tab in document_data.tabs:
        _tab_slides(presentation, tab)

    buffer = io.BytesIO()
    presentation.save(buffer)
    filename = output_filename(document_data.title, "pptx")
    return buffer.getvalue(), filename, MEDIA_TYPES["pptx"]
