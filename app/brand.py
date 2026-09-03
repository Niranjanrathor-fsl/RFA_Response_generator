"""Firstsource brand constants and asset helpers.

Single source of truth for colours, fonts and logos so every generator
(HTML, Word, PowerPoint, Excel) produces identically branded output.
Mirrors knowledge/brand-quick-reference.md.
"""

from __future__ import annotations

import base64
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Dict

from .config import get_settings

# ------------------------------------------------------------------ palette
DARK_BLUE = "#1E2247"
ORANGE = "#DF6014"
MID_BLUE = "#113190"
BRIGHT_BLUE = "#2844C4"
LIGHT_BLUE = "#6CB1DB"
GRAY = "#ECF1F5"
WHITE = "#FFFFFF"
BLACK = "#000000"
INK = "#2B3050"

# Brand-approved data-visualisation series order.
SERIES_ORDER = (DARK_BLUE, MID_BLUE, BRIGHT_BLUE, ORANGE, LIGHT_BLUE)

FONT_STACK = "'Neue Haas Grotesk Display', 'Franklin Gothic Medium', Arial, sans-serif"
# Office documents cannot use a webfont; fall back to the brand's own fallback.
OFFICE_FONT = "Franklin Gothic Medium"

TAGLINE = "We make it happen!"

# Filename -> css font-weight. Note "Mediu" is the real filename (no trailing m).
FONT_WEIGHTS: Dict[str, int] = {
    "NeueHaasDisplayRoman.ttf": 400,
    "NeueHaasDisplayMediu.ttf": 500,
    "NeueHaasDisplayBold.ttf": 700,
    "NeueHaasDisplayBlack.ttf": 900,
}

LOGO_WHITE = "Firstsource-logo-white.png"
LOGO_DARK = "Firstsource-logo-standalone.png"
LOGO_RPSG = "RPSG-Group-logo-standalone.png"


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def office_hex(value: str) -> str:
    """Office XML wants RRGGBB with no leading hash."""
    return value.lstrip("#").upper()


def footer_text(year: int | None = None) -> str:
    return f"Copyright © {year or date.today().year} Firstsource. All rights reserved."


@lru_cache
def logo_bytes(name: str) -> bytes:
    path = get_settings().logo_dir / name
    if not path.is_file():
        raise FileNotFoundError(f"Brand logo missing: {path}")
    return path.read_bytes()


def logo_path(name: str) -> Path:
    return get_settings().logo_dir / name


@lru_cache
def logo_data_uri(name: str) -> str:
    encoded = base64.b64encode(logo_bytes(name)).decode("ascii")
    return f"data:image/png;base64,{encoded}"


@lru_cache
def font_face_css() -> str:
    """@font-face rules with the TTFs base64-embedded, so a downloaded
    dashboard HTML file stays on-brand with no external asset folder."""
    settings = get_settings()
    if not settings.embed_fonts_in_html:
        return ""
    blocks = []
    for filename, weight in FONT_WEIGHTS.items():
        path = settings.font_dir / filename
        if not path.is_file():
            continue
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        blocks.append(
            "@font-face{font-family:'Neue Haas Grotesk Display';"
            f"font-style:normal;font-weight:{weight};font-display:swap;"
            f"src:url(data:font/ttf;base64,{encoded}) format('truetype');}}"
        )
    return "\n".join(blocks)
