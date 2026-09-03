"""Interactive HTML dashboard generator.

Produces a single self-contained, fully branded HTML file: logos and fonts are
base64-embedded so the file can be emailed or dropped on a share without an
asset folder alongside it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import brand
from ..config import get_settings
from ..schemas import ResponseDocument, SourceInfo
from ._common import MEDIA_TYPES, output_filename

# SVG path bodies for the tab/metric icon keywords.
ICON_PATHS = {
    "doc": '<path d="M6 2h9l5 5v15H6z"/><path d="M14 2v6h6"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "shield": '<path d="M12 3l8 4v5c0 5-3.4 8.3-8 9-4.6-.7-8-4-8-9V7z"/>',
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>',
    "bulb": '<path d="M9 18h6M10 22h4M12 2a6 6 0 0 0-4 10.5V16h8v-3.5A6 6 0 0 0 12 2z"/>',
    "layers": '<path d="M12 2 2 8l10 6 10-6z"/><path d="M2 14l10 6 10-6"/>',
    "users": '<circle cx="9" cy="8" r="4"/><path d="M2 21c0-4 3.5-6 7-6s7 2 7 6"/><path d="M17 11a4 4 0 1 0-1-7.9"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l4 2"/>',
    "growth": '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
    "lock": '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    "flag": '<path d="M5 21V4h9l-1 3h7v9h-8l-1-3H5"/>',
}
DEFAULT_ICON = "doc"


def _icon_svg(keyword: str | None) -> str:
    body = ICON_PATHS.get((keyword or "").lower(), ICON_PATHS[DEFAULT_ICON])
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )


@lru_cache
def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(get_settings().base_dir / "app" / "templates")),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["icon"] = _icon_svg
    return env


def _context(
    document: ResponseDocument,
    sources: Sequence[SourceInfo] | None,
    template_kind: str,
) -> dict:
    tab_ids: List[str] = [f"tab-{index}" for index in range(len(document.tabs))]
    return {
        "doc": document,
        "tabs": list(zip(tab_ids, document.tabs)),
        "sources": list(sources or []),
        "brand": {
            "dark": brand.DARK_BLUE,
            "orange": brand.ORANGE,
            "mid": brand.MID_BLUE,
            "bright": brand.BRIGHT_BLUE,
            "light": brand.LIGHT_BLUE,
            "gray": brand.GRAY,
            "white": brand.WHITE,
            "ink": brand.INK,
            "font_stack": brand.FONT_STACK,
            "tagline": brand.TAGLINE,
        },
        "font_face_css": brand.font_face_css(),
        "logo_white": brand.logo_data_uri(brand.LOGO_WHITE),
        "logo_dark": brand.logo_data_uri(brand.LOGO_DARK),
        "footer": brand.footer_text(),
        "question_count": document.question_count,
        "mode": document.mode,
        "kind": template_kind,
    }


def render_html(
    document: ResponseDocument,
    sources: Sequence[SourceInfo] | None = None,
    template_name: str = "dashboard.html.j2",
    template_kind: str = "dashboard",
) -> str:
    template = _environment().get_template(template_name)
    return template.render(**_context(document, sources, template_kind))


def generate(
    document: ResponseDocument,
    sources: Sequence[SourceInfo] | None = None,
) -> tuple[bytes, str, str]:
    html = render_html(document, sources)
    filename = output_filename(document.title, "html", "dashboard")
    return html.encode("utf-8"), filename, MEDIA_TYPES["html"]
