"""Output format generators.

Every generator takes a validated ResponseDocument and returns
(bytes, filename, media_type) so the API route can stream it back identically
regardless of format.
"""

from __future__ import annotations

from typing import Callable, Dict, Tuple

from ..schemas import ResponseDocument
from . import dashboard, excel, powerpoint, qa_doc, word

GeneratorResult = Tuple[bytes, str, str]

FORMAT_LABELS: Dict[str, str] = {
    "dashboard": "Interactive Dashboard",
    "qa": "Response to Questions",
    "docx": "Word",
    "pptx": "PowerPoint",
    "xlsx": "Excel",
}

_GENERATORS: Dict[str, Callable[..., GeneratorResult]] = {
    "dashboard": dashboard.generate,
    "qa": qa_doc.generate,
    "docx": word.generate,
    "pptx": powerpoint.generate,
    "xlsx": excel.generate,
}


def generate(fmt: str, document: ResponseDocument, **kwargs) -> GeneratorResult:
    if fmt not in _GENERATORS:
        raise KeyError(f"Unknown output format '{fmt}'.")
    return _GENERATORS[fmt](document, **kwargs)


def available_formats() -> Dict[str, str]:
    return dict(FORMAT_LABELS)


__all__ = ["generate", "available_formats", "FORMAT_LABELS", "GeneratorResult"]
