"""Hybrid retrieval: multi-vector fusion + rerank + semantic cache (Phase 4).

Reranker note: the target architecture specifies bge-reranker-v2-m3. That model
needs sentence-transformers + torch (heavy for a web app). We use
BAAI/bge-reranker-base via fastembed instead - same model family, ONNX runtime,
no GPU. Swap in the larger model later (e.g. run it on the Azure VM as a small
reranking service) if evaluation shows the smaller model is a bottleneck.

Multi-vector: fuses up to 5 named vectors per chunk (dense_content, sparse_bm25,
dense_summary, dense_metadata, dense_table) with RRF - see app/rag/index.py for
what each vector holds. A query naming a known document is additionally hard-
filtered to that document's chunks (see _match_known_source).
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional, Tuple

from qdrant_client import models

from ..config import Settings, get_settings
from . import store
from .embeddings import DenseEmbedder, SparseEmbedder
from .index import DENSE_CONTENT, DENSE_METADATA, DENSE_SUMMARY, DENSE_TABLE, SPARSE_BM25, get_client

log = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    text: str
    source: str
    location: str
    web_url: str
    score: float
    item_id: str = ""


_reranker_lock = threading.Lock()
# One rerank at a time per process. Each ONNX run allocates working memory in
# proportion to its batch, and 4 parallel per-question reranks drove the Azure
# worker (7 GB, shared) into out-of-memory kills. Parallelism bought little:
# ONNX already spreads a single run across every core. Embedding and Qdrant
# calls still run in parallel; only this CPU-bound step queues.
_rerank_run_lock = threading.Lock()


@lru_cache
def _load_reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name="BAAI/bge-reranker-base")


def _reranker():
    # Per-question searches run in parallel; without the lock each thread's first
    # call would load its own copy of the model.
    with _reranker_lock:
        return _load_reranker()


def _match_known_source(query: str, settings: Settings) -> Optional[str]:
    """If the query names a specific ingested document, return its exact filename
    so the search can be hard-filtered to it - the most reliable way to satisfy a
    document/client-specific question, independent of vector similarity."""
    known_names = store.get_known_document_names(settings)
    query_lower = query.lower()
    for name in known_names:
        stem = name.rsplit(".", 1)[0].lower()
        if len(stem) >= 6 and stem in query_lower:
            return name
    return None


def _normalise(text: str) -> str:
    # A deck and its PDF export differ in case, bullets and line breaks, not words.
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _drop_near_duplicates(ranked: list, top_k: int, threshold: float) -> list:
    """Take the best-ranked chunks, skipping any whose text closely matches one
    already taken, until ``top_k`` are chosen.

    The corpus holds several drafts of the same document (v1/v2 workbooks, a deck
    and its PDF), whose passages content-hash dedup cannot catch because the files
    differ. Measured on failing eval questions, a quarter of retrieved passages
    were copies. Real copies scored >= 0.81 similar; the most similar distinct
    passages 0.67. A threshold of 1.0 or more disables suppression.
    """
    kept: list = []
    kept_texts: List[str] = []
    for item in ranked:
        if len(kept) >= top_k:
            break
        text = _normalise(item[1][0])
        if threshold < 1.0 and any(
            difflib.SequenceMatcher(None, text, other).ratio() >= threshold for other in kept_texts
        ):
            continue
        kept.append(item)
        kept_texts.append(text)
    return kept


def search(query: str, settings: Settings | None = None) -> List[RetrievedChunk]:
    """Hybrid multi-vector search + rerank. Returns [] if RAG is disabled,
    unconfigured, or unreachable. Checks the semantic cache first (if enabled)."""
    settings = settings or get_settings()
    if not settings.rag_enabled:
        return []

    if settings.rag_cache_enabled:
        from . import cache  # lazy: cache.py imports RetrievedChunk from this module

        cached = cache.lookup(query, settings)
        if cached is not None:
            return cached

    chunks = _search_uncached(query, settings)

    if settings.rag_cache_enabled and chunks:
        from . import cache

        cache.store_result(query, chunks, settings)

    return chunks


def _search_uncached(query: str, settings: Settings) -> List[RetrievedChunk]:
    try:
        client = get_client(settings)
        collection = settings.qdrant_collection_v2
        if not client.collection_exists(collection):
            log.warning("Qdrant collection '%s' does not exist yet - run ingestion first.", collection)
            return []

        dense_vec = DenseEmbedder(settings).embed_one(query)
        sparse_vec = SparseEmbedder().embed_one(query)
        sparse_query = models.SparseVector(indices=sparse_vec.indices.tolist(), values=sparse_vec.values.tolist())

        query_filter = None
        matched_source = _match_known_source(query, settings)
        if matched_source:
            log.info("Query names a known document ('%s') - restricting retrieval to it.", matched_source)
            query_filter = models.Filter(
                must=[models.FieldCondition(key="source", match=models.MatchValue(value=matched_source))]
            )

        # Every named vector is prefetched; dense_table is absent on non-table
        # chunks (Qdrant supports per-point optional vectors) and simply
        # contributes no matches for those points - harmless to always include.
        prefetch = [
            models.Prefetch(query=dense_vec, using=DENSE_CONTENT, limit=settings.rag_retrieve_top_k, filter=query_filter),
            models.Prefetch(query=sparse_query, using=SPARSE_BM25, limit=settings.rag_retrieve_top_k, filter=query_filter),
            models.Prefetch(query=dense_vec, using=DENSE_SUMMARY, limit=settings.rag_retrieve_top_k, filter=query_filter),
            models.Prefetch(query=dense_vec, using=DENSE_METADATA, limit=settings.rag_retrieve_top_k, filter=query_filter),
            models.Prefetch(query=dense_vec, using=DENSE_TABLE, limit=settings.rag_retrieve_top_k, filter=query_filter),
        ]

        # The self-hosted Qdrant VM occasionally drops connections under rapid
        # back-to-back requests (seen during ingestion too) - retry rather than
        # silently returning "no grounding" for what would otherwise be a good query.
        results = None
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                results = client.query_points(
                    collection_name=collection,
                    prefetch=prefetch,
                    query=models.FusionQuery(fusion=models.Fusion.RRF),
                    limit=settings.rag_retrieve_top_k,
                    with_payload=True,
                ).points
                break
            except Exception as exc:  # noqa: BLE001 - retry transient network errors
                last_exc = exc
                if attempt < 2:
                    log.warning(
                        "Qdrant query_points failed (attempt %d/3): %s: %s - retrying.",
                        attempt + 1, type(exc).__name__, exc,
                    )
                    time.sleep(0.5 * (attempt + 1))
        if results is None:
            raise last_exc

        if not results:
            return []

        candidates = [(r.payload.get("text", ""), r) for r in results]
        pairs_texts = [t for t, _ in candidates]
        reranker = _reranker()
        with _rerank_run_lock:
            scores = list(reranker.rerank(query, pairs_texts, batch_size=settings.rag_rerank_batch_size))

        ranked = sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)
        top = _drop_near_duplicates(ranked, settings.rag_rerank_top_k, settings.rag_near_duplicate_threshold)
        # Trim the tail: even within top_k, drop chunks trailing far behind the
        # best match for this query - keeps context tight instead of padding out
        # to a fixed count with weakly-relevant chunks (calibrated against real
        # bge-reranker-base scores: true matches vs. irrelevant differ by ~4-5+ points).
        if top:
            best_score = top[0][0]
            top = [item for item in top if item[0] >= best_score - settings.rag_rerank_score_margin]

        return [
            RetrievedChunk(
                text=payload.get("text", ""),
                source=payload.get("source", ""),
                location=payload.get("location", ""),
                web_url=payload.get("web_url", ""),
                score=float(score),
                item_id=payload.get("item_id", ""),
            )
            for score, (_, point) in top
            for payload in [point.payload]
        ]
    except Exception as exc:  # noqa: BLE001 - grounding is best-effort, never break generation
        log.warning(
            "RAG retrieval failed, continuing without SharePoint grounding: %s: %s",
            type(exc).__name__, exc,
        )
        return []


def search_per_question(
    questions: List[str], settings: Settings | None = None
) -> List[Tuple[str, List[RetrievedChunk]]]:
    """One search per question, in parallel, each keeping fewer chunks than a
    standalone search so a 40-question RFI does not flood the prompt."""
    settings = settings or get_settings()
    per_question = settings.model_copy(update={"rag_rerank_top_k": settings.rag_per_question_top_k})
    workers = max(1, min(settings.rag_question_search_workers, len(questions) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda q: search(q, per_question), questions))
    return list(zip(questions, results))


def format_question_grounding(results: List[Tuple[str, List[RetrievedChunk]]]) -> str:
    """Render per-question retrieval so the model knows which evidence belongs to
    which question. A passage retrieved for several questions is printed once."""
    if not any(chunks for _, chunks in results):
        return ""
    lines = [
        "===== SHAREPOINT ANALYST DOCUMENTS (retrieved per question, cite by source) =====",
        "Each question below is followed by the passages retrieved for it. Answer each "
        "question from its own passages first; use the others only if they genuinely apply.",
    ]
    shown = set()
    for question, chunks in results:
        lines.append(f"\n--- Evidence for: {question} ---")
        if not chunks:
            lines.append("(no matching passages found in the SharePoint documents)")
        for chunk in chunks:
            location = f", {chunk.location}" if chunk.location else ""
            key = (chunk.source, chunk.location, chunk.text)
            if key in shown:
                lines.append(f"[Source: {chunk.source}{location}] - see passage above")
                continue
            shown.add(key)
            lines.append(f"[Source: {chunk.source}{location}]\n{chunk.text}")
    return "\n".join(lines)


def format_grounding_block(chunks: List[RetrievedChunk]) -> str:
    """Render retrieved chunks as a citeable block to append to the prompt."""
    if not chunks:
        return ""
    lines = ["===== SHAREPOINT ANALYST DOCUMENTS (retrieved, cite by source) ====="]
    for chunk in chunks:
        location = f", {chunk.location}" if chunk.location else ""
        lines.append(f"\n[Source: {chunk.source}{location}]\n{chunk.text}")
    return "\n".join(lines)

