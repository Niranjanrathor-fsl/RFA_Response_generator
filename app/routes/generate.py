"""Generation and render routes.

Two steps, deliberately:

  POST /api/generate       uploads -> extracted text -> model -> validated JSON
  POST /api/render/{fmt}   validated JSON -> a downloadable file

Splitting them means the browser can produce a second or third output format, or
re-download a file, without paying for another model call.

  POST /api/jobs, GET /api/jobs/{id}   /api/generate as a background job

A full RFI takes longer than Azure App Service lets one request stay open, so the
browser starts a job and polls it (see app/jobs.py).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError

from .. import jobs
from ..auth import require_user
from ..config import get_settings
from ..extract import ExtractedDocument, ExtractionError, build_corpus, extract_text
from ..generators import FORMAT_LABELS, generate as generate_output
from ..knowledge import KnowledgeBaseError, get_knowledge_base
from ..llm import LLMClient, LLMError
from ..prompts import build_system_prompt, build_user_prompt
from ..quality import assess_quality
from ..questions import DetectedQuestion, detect_questions, understand_questions
from ..rag.retrieve import (
    format_grounding_block,
    format_question_grounding,
    search as rag_search,
    search_per_question,
)
from ..schemas import GenerateResult, RenderRequest, ResponseDocument, SourceInfo

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["generate"])

AUDIENCE_LABELS: Dict[str, str] = {
    "business": "business (SVP/VP, sales, solutioning)",
    "technical": "technical (architects and engineering)",
    "analyst": "analyst (industry analyst firms, e.g. Everest Group, HFS Research, ISG, Gartner, or similar)",
}


def _source_info(documents: Sequence[ExtractedDocument]) -> List[SourceInfo]:
    return [
        SourceInfo(name=d.name, chars=d.chars, words=d.words, note=d.note)
        for d in documents
    ]


def _question_label(question: DetectedQuestion) -> str:
    """How a question heads its evidence in the prompt: where it came from, and what
    kind of answer it needs, so the model can match evidence to question."""
    details = [part for part in (
        f"from {question.location}" if question.location else "",
        f"type: {question.kind}" if question.kind else "",
    ) if part]
    return question.text + (f" ({'; '.join(details)})" if details else "")


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


async def _read_sources(
    files: Optional[Sequence[UploadFile]], pasted: str
) -> List[ExtractedDocument]:
    """The uploads plus any pasted text, or a 400 if none of it is readable."""
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
    return usable


@router.post("/generate", response_model=GenerateResult)
async def generate_document(
    files: Optional[List[UploadFile]] = File(default=None),
    pasted: str = Form(default=""),
    title: str = Form(default=""),
    audience: str = Form(default="business"),
    user: dict = Depends(require_user),
) -> GenerateResult:
    """Generate in one request. Fine locally; behind Azure's ~230 s request limit
    use /api/jobs instead, which is what the browser does."""
    usable = await _read_sources(files, pasted)
    return await _generate(usable, title, audience, user)


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
async def start_generation_job(
    files: Optional[List[UploadFile]] = File(default=None),
    pasted: str = Form(default=""),
    title: str = Form(default=""),
    audience: str = Form(default="business"),
    user: dict = Depends(require_user),
) -> Dict[str, str]:
    """Same as /generate, but returns a job id at once; poll GET /api/jobs/{id}.

    Uploads are read before returning, so a bad file still fails immediately.
    """
    usable = await _read_sources(files, pasted)
    job_id = jobs.start(_owner(user), lambda: _generate(usable, title, audience, user))
    return {"job_id": job_id}


@router.get("/jobs/{job_id}", response_model=None)
async def generation_job_status(job_id: str, user: dict = Depends(require_user)) -> Dict:
    """{"status": "running" | "done" | "error", ...}. "done" carries the same
    `result` /generate returns; "error" carries `status_code` and `detail`."""
    record = jobs.get(job_id, _owner(user))
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or expired generation job.")
    return record


def _owner(user: dict) -> str:
    return str(user.get("email") or "")


async def _generate(
    usable: List[ExtractedDocument], title: str, audience: str, user: dict
) -> GenerateResult:
    settings = get_settings()
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

    # Best-effort: retrieval failures never block generation, just skip grounding.
    # An RFI gets one search per question; anything else gets one search overall.
    questions = detect_questions(
        prompt_corpus, settings.rag_max_questions,
        source_name=usable[0].name if len(usable) == 1 else "",
    )
    if questions:
        if settings.rag_enabled:
            questions = await run_in_threadpool(understand_questions, questions, prompt_corpus, settings)
        per_question = await run_in_threadpool(
            search_per_question, [q.search_query for q in questions], settings
        )
        grounding_block = format_question_grounding(
            [(_question_label(q), chunks) for q, (_, chunks) in zip(questions, per_question)]
        )
        retrieved = [chunk for _, chunks in per_question for chunk in chunks]
    else:
        retrieved = await run_in_threadpool(rag_search, prompt_corpus[:8000])
        grounding_block = format_grounding_block(retrieved)
    if retrieved:
        log.info("RAG grounded with %d chunks from %d source(s) for %d detected question(s).",
                  len(retrieved), len({c.source for c in retrieved}), len(questions))

    user_prompt = build_user_prompt(prompt_corpus, source_names, title.strip(), grounding_block)

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

    # Best-effort: a live groundedness badge for the UI. Only meaningful for
    # RFI content (real question/answer pairs to fact-check) - a summary-mode
    # dashboard/template built from the user's own use case isn't a factual
    # QA task, so grading it against reference material would just produce a
    # misleadingly low score. Skip entirely for summary mode, regardless of
    # which output format(s) the user requested.
    quality = None
    if document.mode == "rfi":
        reference_material = "\n\n".join(
            filter(None, [grounding_block, prompt_corpus, knowledge.prompt_block()])
        )
        quality = await run_in_threadpool(assess_quality, document, reference_material, settings)

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
        quality=quality,
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
