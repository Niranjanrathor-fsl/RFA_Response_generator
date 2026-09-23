"""Describes images, charts, diagrams and scanned pages as text.

Uses the SAME Azure OpenAI chat deployment already used for generation
(gpt-5.4, confirmed vision-capable) - no separate OCR engine, no GPU, no extra
Azure resource. The description becomes ordinary text and flows through the
same chunking/embedding/indexing path as any other content.

Design note: rather than pre-filtering images by pixel size (an unreliable
proxy - a small image can still be meaningful, e.g. a compact inline chart),
every image is sent to the model, which classifies it itself in the same call
that produces the description. Size is only used as a near-zero sanity floor
(skip literal 1px tracking pixels), not a content filter - Azure OpenAI usage
here has no meaningful quota/cost constraint for this corpus size.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

from openai import AzureOpenAI

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

_SKIP_SENTINEL = "DECORATIVE_SKIP"

DESCRIBE_PROMPT = (
    "You are indexing images from business documents into a searchable knowledge base.\n\n"
    f"First decide: is this a purely DECORATIVE element (logo, icon, bullet, divider, "
    f"watermark, background texture) with no informational content? If so, reply with "
    f"exactly the single word {_SKIP_SENTINEL} and nothing else.\n\n"
    "Otherwise, lead with the concrete data: read out ALL visible text and numbers "
    "verbatim. If it contains a table, reproduce it as rows of 'column: value' pairs. "
    "If it contains a chart or graph, state its title and every data point as "
    "'category: value' pairs FIRST. Only mention chart type/axes/layout briefly and "
    "only if it adds real information - do NOT describe what is absent (e.g. missing "
    "axis labels, no gridlines) or add generic visual commentary. Be factual and "
    "dense; do not summarize away specific figures or names."
)


def _client(settings: Settings) -> AzureOpenAI:
    return AzureOpenAI(
        api_key=settings.azure_openai_api_key,
        azure_endpoint=settings.azure_openai_endpoint,
        api_version=settings.azure_openai_api_version,
        timeout=settings.azure_openai_timeout_seconds,
    )


def describe_image(image_bytes: bytes, media_type: str = "image/png", settings: Settings | None = None) -> str:
    """Best-effort raw description call (no decorative-classification). Returns "" on failure."""
    settings = settings or get_settings()
    if not settings.rag_vision_enabled:
        return ""
    try:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        response = _client(settings).chat.completions.create(
            model=settings.azure_openai_deployment_name,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DESCRIBE_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                    ],
                }
            ],
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001 - vision description is best-effort
        log.warning("Vision description failed, skipping this image: %s", exc)
        return ""


def describe_or_skip(
    image_bytes: bytes, media_type: str = "image/png", settings: Settings | None = None
) -> Optional[str]:
    """Like describe_image, but returns None when the model judges the image purely
    decorative (logo/icon/bullet/divider) - this is the classification decision, made
    by looking at the actual content rather than guessing from pixel dimensions."""
    raw = describe_image(image_bytes, media_type, settings)
    if not raw or raw.strip().upper().startswith(_SKIP_SENTINEL):
        return None
    return raw


def is_large_enough(width: int, height: int, settings: Settings | None = None) -> bool:
    """Near-zero sanity floor only - skips literal 1px tracking pixels, nothing more.
    Real decorative-vs-meaningful classification happens inside describe_or_skip()."""
    settings = settings or get_settings()
    minimum = settings.rag_vision_min_image_px
    return width >= minimum and height >= minimum

