"""Server-side knowledge base loader.

The three reference documents live on the server under ``knowledge/`` and are
read once at startup. They are injected into every model prompt but are NEVER
exposed through an API route or sent to the browser, so end users can neither
read nor edit them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import get_settings

log = logging.getLogger(__name__)

ANALYST_KB_FILE = "analyst-knowledge-base.md"
KAIROS_GLOSSARY_FILE = "kairos-glossary.md"
BRAND_REFERENCE_FILE = "brand-quick-reference.md"

REQUIRED_FILES = (ANALYST_KB_FILE, KAIROS_GLOSSARY_FILE, BRAND_REFERENCE_FILE)


class KnowledgeBaseError(RuntimeError):
    """Raised when the server-side knowledge base cannot be loaded."""


@dataclass(frozen=True)
class KnowledgeBase:
    analyst_kb: str
    kairos_glossary: str
    brand_reference: str

    @property
    def total_chars(self) -> int:
        return len(self.analyst_kb) + len(self.kairos_glossary) + len(self.brand_reference)

    def prompt_block(self) -> str:
        """The authoritative reference material injected into the system prompt."""
        return (
            "===== FIRSTSOURCE ANALYST KNOWLEDGE BASE (authoritative) =====\n"
            f"{self.analyst_kb}\n\n"
            "===== KAIROS / INTELLIGENCE THAT OPERATES GLOSSARY (authoritative) =====\n"
            f"{self.kairos_glossary}\n\n"
            "===== FIRSTSOURCE BRAND REFERENCE =====\n"
            f"{self.brand_reference}\n"
        )


def _read(path: Path, limit: int) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if len(text) > limit:
        log.warning(
            "Knowledge file %s truncated from %d to %d chars "
            "(raise MAX_KNOWLEDGE_CHARS_PER_FILE to include all of it).",
            path.name,
            len(text),
            limit,
        )
        text = text[:limit] + "\n[... truncated ...]"
    return text


def load_knowledge_base(directory: Path | None = None) -> KnowledgeBase:
    settings = get_settings()
    directory = directory or settings.knowledge_dir
    missing = [name for name in REQUIRED_FILES if not (directory / name).is_file()]
    if missing:
        raise KnowledgeBaseError(
            f"Missing knowledge file(s) in {directory}: {', '.join(missing)}. "
            "These must be deployed alongside the application."
        )
    limit = settings.max_knowledge_chars_per_file
    kb = KnowledgeBase(
        analyst_kb=_read(directory / ANALYST_KB_FILE, limit),
        kairos_glossary=_read(directory / KAIROS_GLOSSARY_FILE, limit),
        brand_reference=_read(directory / BRAND_REFERENCE_FILE, limit),
    )
    log.info(
        "Knowledge base loaded from %s (%d chars across %d files).",
        directory,
        kb.total_chars,
        len(REQUIRED_FILES),
    )
    return kb


@lru_cache
def get_knowledge_base() -> KnowledgeBase:
    return load_knowledge_base()
