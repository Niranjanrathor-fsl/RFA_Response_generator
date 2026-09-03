"""Shared helpers for the output generators."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

MEDIA_TYPES = {
    "html": "text/html; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str, fallback: str = "firstsource-response") -> str:
    normalised = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_RE.sub("-", ascii_only).strip("-")
    return (slug or fallback)[:70]


def output_filename(title: str, extension: str, suffix: str = "") -> str:
    stamp = date.today().isoformat()
    parts = [slugify(title), suffix, stamp]
    stem = "-".join(p for p in parts if p)
    return f"{stem}.{extension}"


def split_paragraphs(text: str) -> list[str]:
    """Split an answer body into paragraphs for Word/PowerPoint rendering."""
    if not text:
        return []
    chunks = re.split(r"\n\s*\n", text.strip())
    return [c.strip() for c in chunks if c.strip()]

