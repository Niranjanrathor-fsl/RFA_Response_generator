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

import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional

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


@lru_cache
def _reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name="BAAI/bge-reranker-base")


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
        scores = list(_reranker().rerank(query, pairs_texts))

        ranked = sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)
        top = ranked[: settings.rag_rerank_top_k]
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


def format_grounding_block(chunks: List[RetrievedChunk]) -> str:
    """Render retrieved chunks as a citeable block to append to the prompt."""
    if not chunks:
        return ""
    lines = ["===== SHAREPOINT ANALYST DOCUMENTS (retrieved, cite by source) ====="]
    for chunk in chunks:
        location = f", {chunk.location}" if chunk.location else ""
        lines.append(f"\n[Source: {chunk.source}{location}]\n{chunk.text}")
    return "\n".join(lines)

