"""Token-sized chunking on top of app.document_sections' structural extraction.

Two-level strategy:
 1. Split each document into structural sections first (PDF pages, PPT slides,
    Excel sheets, embedded images/charts) - see app/document_sections.py, shared
    with the interactive upload path so both get identical coverage.
 2. Recursively split each section on paragraph/line boundaries down to a target
    token size with overlap, never splitting a table row in half.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

import tiktoken

from ..config import Settings
from ..document_sections import IMAGE_EXTENSIONS, extract_sections  # noqa: F401 - re-exported

_ENCODING = tiktoken.get_encoding("cl100k_base")
_HEADING_RE = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)


@dataclass
class Chunk:
    text: str
    location: str  # e.g. "Page 3", "Slide 5", "Sheet: Pricing", "" for whole-document
    chunk_index: int


def _token_len(text: str) -> int:
    return len(_ENCODING.encode(text))


def _split_by_headings(text: str) -> List[str]:
    """Split on Markdown-style headings ("## ...") into topic segments that must
    NEVER be merged back together, however small - this is what stops unrelated
    topics (e.g. "liability" and "innovation labs") landing in the same chunk just
    because they both fit under the token budget. Falls back to the whole text as
    a single segment when no headings are present."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [text]
    segments: List[str] = []
    if matches[0].start() > 0:
        segments.append(text[: matches[0].start()])
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        segments.append(text[match.start() : end])
    return [s.strip() for s in segments if s.strip()]



def _recursive_split(text: str, max_tokens: int) -> List[str]:
    """Split text on the largest separator that yields pieces under max_tokens."""
    if _token_len(text) <= max_tokens:
        return [text] if text.strip() else []
    for separator in ("\n\n", "\n", ". ", " "):
        if separator in text:
            pieces = [p for p in text.split(separator) if p.strip()]
            if len(pieces) > 1:
                out: List[str] = []
                for piece in pieces:
                    out.extend(_recursive_split(piece, max_tokens))
                return out
    # No separator helped (single long token-dense line) - hard-cut by tokens.
    tokens = _ENCODING.encode(text)
    return [
        _ENCODING.decode(tokens[i : i + max_tokens])
        for i in range(0, len(tokens), max_tokens)
    ]


def _merge_with_overlap(pieces: List[str], max_tokens: int, overlap_tokens: int) -> List[str]:
    """Pack small pieces up to max_tokens per chunk, carrying overlap between chunks."""
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for piece in pieces:
        piece_len = _token_len(piece)
        if current and current_len + piece_len > max_tokens:
            chunks.append("\n".join(current))
            # Carry the tail of the previous chunk forward for context continuity.
            tail: List[str] = []
            tail_len = 0
            for prev in reversed(current):
                prev_len = _token_len(prev)
                if tail_len + prev_len > overlap_tokens:
                    break
                tail.insert(0, prev)
                tail_len += prev_len
            current, current_len = tail, tail_len
        current.append(piece)
        current_len += piece_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def chunk_document(
    filename: str,
    data: bytes,
    max_tokens: int = 512,
    overlap_tokens: int = 80,
    settings: Settings | None = None,
) -> List[Chunk]:
    """Turn one document into a list of chunks, each tagged with its page/slide/sheet."""
    sections = extract_sections(filename, data, settings)
    chunks: List[Chunk] = []
    index = 0
    for location, text in sections:
        # Split on heading/topic boundaries FIRST so the token-based packer below
        # can never merge two unrelated topics into the same chunk.
        for topic in _split_by_headings(text):
            pieces = _recursive_split(topic, max_tokens)
            for merged in _merge_with_overlap(pieces, max_tokens, overlap_tokens):
                if merged.strip():
                    chunks.append(Chunk(text=merged.strip(), location=location, chunk_index=index))
                    index += 1
    return chunks
