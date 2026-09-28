"""Find the questions in an uploaded RFI, questionnaire or survey.

The model still decides what to answer; this exists so retrieval can run once per
question instead of once for the whole upload. One blunt query over a 40-question
RFI returns a little about everything and nothing specific about most questions.

Deliberately heuristic (no LLM call): it is free, instant and deterministic, and a
miss is cheap - an undetected question is still answered, just without its own
search. Heuristics mirror what the system prompt tells the model to treat as a
question: numbered items, lines ending in "?", and "describe / explain / provide"
style requests, including those inside spreadsheet rows.

Each question is tagged with where it came from (file, page / slide / sheet), and
an optional query-understanding step (one LLM call per upload, not per question)
rewrites every question into a standalone search query. RFI questions lean on
their document for meaning - "Why This Matters", "your headcount", "this
programme" - and a search for those words alone matches nothing specific.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import List

from .config import Settings, get_settings
from .llm import LLMClient, extract_json

log = logging.getLogger(__name__)


@dataclass
class DetectedQuestion:
    text: str
    location: str = ""  # "rfi.pdf, Page 3" - blank when unknown
    search_query: str = ""  # what retrieval searches for; the question itself by default
    kind: str = ""  # fact / yes_no / list / numeric / narrative / case_study / other
    entities: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.search_query = self.search_query or self.text

# "Q1.", "Q2)", "Question 3:", "3.", "1.2", "4)", "(a)", "b." - the label is dropped
# so the search query is the question itself.
_LABEL = re.compile(
    r"^\s*(?:Q(?:uestion)?\s*\d+(?:\.\d+)*\s*[.):\-]?|\d+(?:\.\d+)*\s*[.):]?|\(?[a-z]\)|[a-z]\.)\s+",
    re.IGNORECASE,
)
_REQUEST = re.compile(
    r"^(?:please\s+)?(?:describe|explain|provide|outline|detail|elaborate|confirm|indicate|"
    r"specify|share|summari[sz]e|identify|list|state|give|demonstrate|clarify|include)\b"
    r"(?!\s+of\b)",
    re.IGNORECASE,
)
_Q_LABEL = re.compile(r"^\s*Q(?:uestion)?\s*\d+(?:\.\d+)*\s*[.):\-]?\s+", re.IGNORECASE)
_MARKER = re.compile(r"^\s*(?:=====|---)")
_DOCUMENT_MARKER = re.compile(r"^\s*===== SOURCE DOCUMENT \d+ of \d+: (.+?) =====\s*$")
_SECTION_MARKER = re.compile(r"^\s*--- (.+?) ---\s*$")
_MAX_QUESTION_CHARS = 1000


def _as_question(fragment: str) -> str:
    """The question text if this line or cell is one, otherwise ""."""
    body = _LABEL.sub("", fragment, count=1).strip()
    words = len(body.split())
    # An explicit "Q1" / "Question 1" label is enough on its own: analyst RFIs often
    # phrase questions as topics ("Question 1: Contractual Liability ...").
    if _Q_LABEL.match(fragment) and words >= 2:
        return body[:_MAX_QUESTION_CHARS]
    if body.endswith("?") and words >= 3:
        return body[:_MAX_QUESTION_CHARS]
    if _REQUEST.match(body) and words >= 4:
        return body[:_MAX_QUESTION_CHARS]
    return ""


def detect_questions(
    text: str, max_questions: int = 60, source_name: str = ""
) -> List[DetectedQuestion]:
    """Questions in reading order, de-duplicated, at most ``max_questions``.
    ``source_name`` names the file when the text is a single upload (a merged
    multi-file corpus carries its own per-document markers)."""
    questions: List[DetectedQuestion] = []
    seen = set()
    document, section = source_name, ""
    for line in text.splitlines():
        if not line.strip():
            continue
        if _MARKER.match(line):
            doc_match = _DOCUMENT_MARKER.match(line)
            section_match = _SECTION_MARKER.match(line)
            if doc_match:
                document, section = doc_match.group(1), ""
            elif section_match:
                section = section_match.group(1)
            continue
        # Spreadsheet rows arrive pipe-delimited; the question is usually one cell.
        fragments = line.split("|") if " | " in line else [line]
        for fragment in fragments:
            question = _as_question(fragment)
            key = " ".join(question.lower().split())
            if not question or key in seen:
                continue
            seen.add(key)
            location = ", ".join(part for part in (document, section) if part)
            questions.append(DetectedQuestion(question, location))
            if len(questions) >= max_questions:
                return questions
    return questions


_UNDERSTANDING_SYSTEM = (
    "You prepare search queries for a retrieval system over Firstsource's analyst "
    "documents (RFI responses, analyst surveys, briefing decks, capability overviews)."
)
_UNDERSTANDING_PROMPT = """Below is the opening of an uploaded document set, then the questions found in it.

For EACH question return:
- "query": a standalone search query (8-25 words) that finds the evidence needed to
  answer it. Resolve anything that depends on the document: "you/your" is
  Firstsource; name the programme, award, analyst firm, service line or topic the
  question sits under; expand vague headings ("Why This Matters") into what is
  actually being asked. Do not answer the question.
- "type": one of fact, yes_no, list, numeric, narrative, case_study, other.
- "entities": named things the answer must be about (firms, products, clients,
  regions, years) - [] if none.

Reply with JSON only: {{"questions": [{{"i": 1, "query": "...", "type": "...", "entities": []}}]}}

DOCUMENT OPENING:
{context}

QUESTIONS:
{questions}"""
_UNDERSTANDING_CONTEXT_CHARS = 4000


def understand_questions(
    questions: List[DetectedQuestion], corpus: str, settings: Settings | None = None
) -> List[DetectedQuestion]:
    """Rewrite each question into a standalone search query with its type and key
    entities. Best-effort: any failure returns the questions unchanged, so
    retrieval falls back to searching the raw question text."""
    settings = settings or get_settings()
    if not questions or not settings.rag_query_understanding_enabled:
        return questions
    listing = "\n".join(
        f"{i}. {q.text}" + (f"  [from {q.location}]" if q.location else "")
        for i, q in enumerate(questions, start=1)
    )
    prompt = _UNDERSTANDING_PROMPT.format(
        context=corpus[:_UNDERSTANDING_CONTEXT_CHARS], questions=listing
    )
    try:
        reply = LLMClient(settings).ask(_UNDERSTANDING_SYSTEM, prompt)
        parsed = extract_json(reply) or {}
        entries = parsed.get("questions") or []
    except Exception as exc:  # noqa: BLE001 - understanding is an enhancement, never fatal
        log.warning("Query understanding failed, searching the raw questions: %s", exc)
        return questions

    understood = list(questions)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("i")
        query = str(entry.get("query") or "").strip()
        if not isinstance(index, int) or not 1 <= index <= len(questions) or not query:
            continue
        entities = entry.get("entities")
        understood[index - 1] = replace(
            questions[index - 1],
            search_query=query,
            kind=str(entry.get("type") or ""),
            entities=[str(e) for e in entities] if isinstance(entities, list) else [],
        )
    log.info("Query understanding rewrote %d of %d question(s).",
             sum(q.search_query != q.text for q in understood), len(questions))
    return understood
