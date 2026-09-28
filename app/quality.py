"""Live per-response quality self-assessment.

Separate from the offline DeepEval harness in tests/eval/ (which needs a golden
dataset and is run manually/in batch). This is a single, fast LLM call executed
synchronously right after generation so the UI can show an immediate
green/amber/red groundedness badge. Best-effort: any failure here is logged and
swallowed - a quality badge must never block or fail the actual response.
"""

from __future__ import annotations

import logging

from .config import Settings
from .llm import LLMClient, extract_json
from .schemas import QualityAssessment, ResponseDocument

log = logging.getLogger(__name__)

# The auditor must see everything the generator saw, or it flags grounded claims
# as invented: the knowledge base alone is ~47k characters, and an RFI with
# per-question retrieval adds ~8 passages per question. ~250k characters is
# roughly 60k tokens - well within the model's context.
_MAX_REFERENCE_CHARS = 250_000
_MAX_ANSWER_CHARS = 40_000

_SYSTEM_PROMPT = (
    "You are a strict quality auditor for AI-generated business documents. Check "
    "whether the ANSWER's factual claims (numbers, names, capabilities, commitments) "
    "are supported by the REFERENCE MATERIAL provided. You are grading groundedness "
    "only, not writing quality or tone. Unsupported or invented figures are serious "
    "faults; qualitative, reasonably-inferred statements are acceptable.\n\n"
    "Reply with ONLY a JSON object, no markdown fences, no commentary:\n"
    '{"score": <integer 0-100>, "flag": "green"|"amber"|"red", "reason": "<one short sentence>"}\n\n'
    "Guidance: score >= 80 with no unsupported claims -> green. "
    "50-79, or a few minor unsupported details -> amber. "
    "Below 50, or any invented figures/facts -> red."
)


def _flatten_document(document: ResponseDocument) -> str:
    parts = [document.title, document.subtitle]
    for metric in document.metrics:
        parts.append(f"{metric.label}: {metric.value}")
    for tab in document.tabs:
        parts.append(f"## {tab.name}")
        if tab.intro:
            parts.append(tab.intro)
        parts.extend(tab.bullets)
        for item in tab.qa:
            parts.append(f"{item.n} {item.q}\n{item.a}")
        if tab.table:
            parts.append(" | ".join(tab.table.headers))
            for row in tab.table.rows:
                parts.append(" | ".join(row))
        if tab.callout:
            parts.append(f"{tab.callout.title}: {tab.callout.body}")
    return "\n".join(p for p in parts if p)


def assess_quality(
    document: ResponseDocument,
    reference_material: str,
    settings: Settings,
) -> QualityAssessment | None:
    """Grade how well `document` is grounded in `reference_material`.

    Returns None (badge simply omitted) on any error - never raises.
    """
    try:
        client = LLMClient(settings)
        answer_text = _flatten_document(document)[:_MAX_ANSWER_CHARS]
        reference_text = reference_material[:_MAX_REFERENCE_CHARS]
        user_prompt = (
            'REFERENCE MATERIAL:\n"""\n' + reference_text + '\n"""\n\n'
            'ANSWER TO GRADE:\n"""\n' + answer_text + '\n"""'
        )
        raw = client.ask(_SYSTEM_PROMPT, user_prompt)
        parsed = extract_json(raw)
        if not parsed:
            return None
        score = max(0, min(100, int(parsed.get("score", 0))))
        flag = parsed.get("flag")
        if flag not in ("green", "amber", "red"):
            flag = "green" if score >= 80 else "amber" if score >= 50 else "red"
        reason = str(parsed.get("reason") or "").strip()[:200]
        return QualityAssessment(score=score, flag=flag, reason=reason)
    except Exception as exc:  # noqa: BLE001 - a quality badge must never break generation
        log.warning("Quality self-assessment failed, omitting badge: %s", exc)
        return None
