"""Generation and render routes.

Two steps, deliberately:

  POST /api/generate       uploads -> extracted text -> model -> validated JSON
  POST /api/render/{fmt}   validated JSON -> a downloadable file

Splitting them keeps the service stateless (nothing is persisted server-side) and
means the browser can produce a second or third output format, or re-download a
file, without paying for another model call.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError

from ..auth import require_user
from ..config import get_settings
from ..extract import ExtractedDocument, ExtractionError, build_corpus, extract_text
from ..generators import FORMAT_LABELS, generate as generate_output
from ..knowledge import KnowledgeBaseError, get_knowledge_base
from ..llm import LLMClient, LLMError
from ..prompts import build_system_prompt, build_user_prompt
from ..schemas import GenerateResult, RenderRequest, ResponseDocument, SourceInfo

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["generate"])

AUDIENCE_LABELS: Dict[str, str] = {
    "business": "business (SVP/VP, sales, solutioning)",
    "technical": "technical (architects and engineering)",
}


def _source_info(documents: Sequence[ExtractedDocument]) -> List[SourceInfo]:
    return [
        SourceInfo(name=d.name, chars=d.chars, words=d.words, note=d.note)
        for d in documents
    ]


async def _read_uploads(files: Optional[Sequence[UploadFile]]) -> List[ExtractedDocument]:
    settings = get_settings()
    real_files = [f for f in (files or []) if f and f.filename]
    if len(real_files) > settings.max_files:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Too many files: {len(real_files)}. The limit is {settings.max_files}.",
        )

    documents: List[ExtractedDocument] = []
    seen: set[tuple[str, int]] = set()
    for upload in real_files:
        # Reject on the declared size first - reading a huge part into memory
        # just to measure it is how a worker gets OOM-killed.
        declared = getattr(upload, "size", None)
        if declared is not None and declared > settings.max_upload_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"'{upload.filename}' is {declared / 1_048_576:.1f} MB, over the "
                    f"{settings.max_upload_mb} MB limit."
                ),
            )
        data = await upload.read()
        await upload.close()
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"'{upload.filename}' is {len(data) / 1_048_576:.1f} MB, over the "
                    f"{settings.max_upload_mb} MB limit."
                ),
            )
        key = (upload.filename, len(data))
        if key in seen:
            log.info("Skipping duplicate upload %s", upload.filename)
            continue
        seen.add(key)
        try:
            # Parsing a large PDF/PPTX is CPU-bound; keep it off the event loop.
            documents.append(await run_in_threadpool(extract_text, upload.filename, data))
        except ExtractionError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{upload.filename}: {exc}",
            ) from exc
    return documents


@router.post("/generate", response_model=GenerateResult)
async def generate_document(
    files: Optional[List[UploadFile]] = File(default=None),
    pasted: str = Form(default=""),
    title: str = Form(default=""),
    audience: str = Form(default="business"),
    user: dict = Depends(require_user),
) -> GenerateResult:
    settings = get_settings()
    documents = await _read_uploads(files)

    pasted = (pasted or "").strip()
    if pasted:
        documents.append(ExtractedDocument(name="Pasted content", text=pasted))

    usable = [d for d in documents if not d.is_empty]
    if not usable:
        empty_names = ", ".join(d.name for d in documents) or "none"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "No readable text found. Add at least one document with selectable "
                f"text, or paste the content directly. Files received: {empty_names}."
            ),
        )

    corpus, truncated = build_corpus(usable)
    prompt_corpus = corpus[: settings.max_prompt_corpus_chars]
    if len(corpus) > settings.max_prompt_corpus_chars:
        truncated = True

    try:
        knowledge = get_knowledge_base()
    except KnowledgeBaseError as exc:
        log.error("Knowledge base unavailable: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    audience_label = AUDIENCE_LABELS.get(audience, AUDIENCE_LABELS["business"])
    source_names = [d.name for d in usable]
    system_prompt = build_system_prompt(knowledge, audience_label)
    user_prompt = build_user_prompt(prompt_corpus, source_names, title.strip())

    try:
        client = LLMClient(settings)
        # The Anthropic SDK call is synchronous and can take minutes. Running it
        # in a worker thread keeps the event loop free for other users.
        raw_document = await run_in_threadpool(
            client.generate_document, system_prompt, user_prompt
        )
    except LLMError as exc:
        log.warning("Generation failed for %s: %s", user.get("email"), exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    try:
        document = ResponseDocument.model_validate(raw_document)
    except ValidationError as exc:
        log.warning("Model output failed validation: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The model returned a structure this application cannot render. Try again.",
        ) from exc

    if title.strip():
        document.title = title.strip()

    log.info(
        "Generated '%s' for %s: mode=%s questions=%d sources=%d corpus=%d chars",
        document.title, user.get("email"), document.mode,
        document.question_count, len(usable), len(corpus),
    )

    return GenerateResult(
        document=document,
        mode=document.mode,
        question_count=document.question_count,
        sources=_source_info(usable),
        model=client.model,
        corpus_chars=len(corpus),
        truncated=truncated,
    )


@router.post("/render/{fmt}")
def render_output(
    fmt: str,
    payload: RenderRequest,
    user: dict = Depends(require_user),
) -> Response:
    if fmt not in FORMAT_LABELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown format '{fmt}'. Valid formats: {', '.join(FORMAT_LABELS)}.",
        )
    try:
        content, filename, media_type = generate_output(
            fmt, payload.document, sources=payload.sources
        )
    except FileNotFoundError as exc:  # a brand asset is missing on the server
        log.error("Brand asset missing while rendering %s: %s", fmt, exc)
        raise HTTPException(
            status_code=500,
            detail="A brand asset is missing from the deployment. Check static/logos.",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
        log.exception("Rendering %s failed", fmt)
        raise HTTPException(
            status_code=500, detail=f"Could not build the {FORMAT_LABELS[fmt]} file: {exc}"
        ) from exc

    log.info("Rendered %s (%s, %d bytes) for %s", fmt, filename, len(content), user.get("email"))
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Output-Filename": filename,
            "Access-Control-Expose-Headers": "X-Output-Filename, Content-Disposition",
        },
    )
