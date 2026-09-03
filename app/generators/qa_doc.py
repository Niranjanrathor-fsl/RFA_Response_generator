"""Question-and-answer response document (self-contained branded HTML).

Same brand system as the dashboard but laid out as a linear, printable Q&A
response rather than a tabbed dashboard - the format analysts and procurement
teams usually want back.
"""

from __future__ import annotations

from typing import Sequence

from ..schemas import ResponseDocument, SourceInfo
from . import dashboard
from ._common import MEDIA_TYPES, output_filename


def generate(
    document: ResponseDocument,
    sources: Sequence[SourceInfo] | None = None,
) -> tuple[bytes, str, str]:
    html = dashboard.render_html(
        document,
        sources,
        template_name="qa.html.j2",
        template_kind="qa",
    )
    filename = output_filename(document.title, "html", "responses")
    return html.encode("utf-8"), filename, MEDIA_TYPES["html"]
