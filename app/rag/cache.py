"""Semantic cache for RAG retrieval (Phase 4).

Skips repeat/embedding+hybrid-search+rerank for a sufficiently similar query, using
a SEPARATE small Qdrant collection for similarity search (reuses infrastructure we
already run and trust, instead of depending on a Postgres vector extension like
pgvector on the shared RDS instance, which is not something we control/want to risk
requesting on infra shared with another project).

Postgres (app/rag/store.py) tracks hit/write audit history only - the actual
similarity search and cached payload live in Qdrant.

Invalidation: a cache entry records which source documents it depended on. At
lookup time, if ANY of those documents' content hash has changed since the entry
was cached (per the `documents` table Phase 2 already tracks), the entry is treated
as stale and skipped - this is a correctness-preserving cache, not just a TTL cache.
A TTL is also enforced as a simpler secondary safety net.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from qdrant_client import QdrantClient, models

from ..config import Settings, get_settings
from . import store
from .embeddings import DenseEmbedder
from .retrieve import RetrievedChunk

log = logging.getLogger(__name__)

CACHE_VECTOR_NAME = "query_dense"
_ID_NAMESPACE = uuid.UUID("2b1a7e3c-4b5e-4a3f-9d5a-7d8f6a2b9c11")


def _client(settings: Settings) -> QdrantClient:
    return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)


def _ensure_cache_collection(client: QdrantClient, settings: Settings, dim: int) -> None:
    name = settings.qdrant_cache_collection
    if client.collection_exists(name):
        return
    client.create_collection(
        collection_name=name,
        vectors_config={CACHE_VECTOR_NAME: models.VectorParams(size=dim, distance=models.Distance.COSINE)},
    )
    log.info("Created Qdrant semantic cache collection '%s' (dim=%d).", name, dim)


def lookup(query: str, settings: Settings | None = None) -> Optional[List[RetrievedChunk]]:
    """Returns a cached chunk list if a sufficiently similar, non-stale entry exists."""
    settings = settings or get_settings()
    if not settings.rag_cache_enabled:
        return None
    try:
        client = _client(settings)
        if not client.collection_exists(settings.qdrant_cache_collection):
            return None
        query_vec = DenseEmbedder(settings).embed_one(query)
        results = client.query_points(
            collection_name=settings.qdrant_cache_collection,
            query=query_vec,
            using=CACHE_VECTOR_NAME,
            limit=1,
            with_payload=True,
        ).points
        if not results or results[0].score < settings.rag_cache_similarity_threshold:
            return None

        point = results[0]
        payload = point.payload
        cached_at = datetime.fromisoformat(payload["cached_at"])
        if datetime.now(timezone.utc) - cached_at > timedelta(hours=settings.rag_cache_ttl_hours):
            log.info("Semantic cache entry expired (TTL), treating as a miss.")
            return None

        cached_sources: dict = payload.get("source_hashes", {})
        current_states = store.get_document_states(settings)
        for item_id, cached_tag in cached_sources.items():
            state = current_states.get(item_id)
            if state is None or state.content_tag != cached_tag:
                log.info(
                    "Semantic cache entry stale (item '%s' changed or gone), treating as a miss.",
                    item_id,
                )
                return None

        store.record_cache_hit(payload["cache_id"], settings)
        chunks = [RetrievedChunk(**item) for item in json.loads(payload["result_json"])]
        log.info("Semantic cache HIT (similarity=%.4f) for query: %s", point.score, query[:80])
        return chunks
    except Exception as exc:  # noqa: BLE001 - cache is a pure optimization, never fatal
        log.warning("Semantic cache lookup failed, proceeding without cache: %s", exc)
        return None


def store_result(query: str, chunks: List[RetrievedChunk], settings: Settings | None = None) -> None:
    """Cache a retrieval result, tagged with the content hash of every source it used
    so a later change to any of those documents correctly invalidates this entry."""
    settings = settings or get_settings()
    if not settings.rag_cache_enabled or not chunks:
        return
    try:
        client = _client(settings)
        query_vec = DenseEmbedder(settings).embed_one(query)
        _ensure_cache_collection(client, settings, len(query_vec))

        current_states = store.get_document_states(settings)
        source_items = {c.item_id for c in chunks if c.item_id}
        source_hashes = {
            item_id: current_states[item_id].content_tag
            for item_id in source_items
            if item_id in current_states
        }

        cache_id = str(uuid.uuid5(_ID_NAMESPACE, query))
        payload = {
            "cache_id": cache_id,
            "query_text": query,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "source_hashes": source_hashes,
            "result_json": json.dumps([asdict(c) for c in chunks]),
        }
        client.upsert(
            collection_name=settings.qdrant_cache_collection,
            points=[models.PointStruct(id=cache_id, vector={CACHE_VECTOR_NAME: query_vec}, payload=payload)],
        )
        store.record_cache_write(cache_id, query, source_items, settings)
    except Exception as exc:  # noqa: BLE001 - caching is best-effort, must not break retrieval
        log.warning("Semantic cache write failed, continuing without caching: %s", exc)
