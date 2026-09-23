"""Qdrant collection management: create once, upsert chunks with metadata.

Phase 4 multi-vector schema: each chunk point carries several named vectors,
decoupling what's used for MATCHING from what's sent to the LLM (which is always
the full original chunk text, unchanged, in the payload):

  dense_content  - full chunk text embedding (the original single "dense" vector)
  sparse_bm25    - full chunk text, BM25 (the original "sparse" vector)
  dense_summary  - short LLM-generated retrieval proxy (summary/hypothetical
                   questions) - matches natural-language queries better than a
                   long chunk diluted by tables/numbers
  dense_metadata - one per-document identity summary (source/topic/entities),
                   reused across every chunk from that document
  dense_table    - CONDITIONAL, only present on table-heavy chunks: a natural-
                   language paraphrase of the table (raw pipe-delimited rows
                   embed poorly). Qdrant supports per-point optional vectors -
                   points without this vector simply never match a dense_table query.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import List, Optional

from qdrant_client import QdrantClient, models

from ..config import Settings, get_settings
from .chunking import Chunk
from .embeddings import (
    DenseEmbedder,
    SparseEmbedder,
    generate_retrieval_proxy,
    generate_table_paraphrase,
    is_table_heavy,
)
from .sources import SourceDocument

log = logging.getLogger(__name__)

DENSE_CONTENT = "dense_content"
DENSE_SUMMARY = "dense_summary"
DENSE_METADATA = "dense_metadata"
DENSE_TABLE = "dense_table"
SPARSE_BM25 = "sparse_bm25"

_ID_NAMESPACE = uuid.UUID("6f6b3f9a-6e1f-4c9a-9b1a-9f6a2b6f3f1a")


def get_client(settings: Settings | None = None) -> QdrantClient:
    settings = settings or get_settings()
    return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)


def ensure_collection(client: QdrantClient, collection: str, dense_dim: int) -> None:
    if client.collection_exists(collection):
        return
    client.create_collection(
        collection_name=collection,
        vectors_config={
            DENSE_CONTENT: models.VectorParams(size=dense_dim, distance=models.Distance.COSINE),
            DENSE_SUMMARY: models.VectorParams(size=dense_dim, distance=models.Distance.COSINE),
            DENSE_METADATA: models.VectorParams(size=dense_dim, distance=models.Distance.COSINE),
            DENSE_TABLE: models.VectorParams(size=dense_dim, distance=models.Distance.COSINE),
        },
        sparse_vectors_config={SPARSE_BM25: models.SparseVectorParams()},
    )
    log.info("Created Qdrant collection '%s' (multi-vector, dense dim=%d).", collection, dense_dim)

    # Payload indexes make delete-by-item_id cheap, and remove a full scan from
    # the document-name filter already used in retrieve.py.
    for field_name in ("item_id", "source"):
        try:
            client.create_payload_index(
                collection_name=collection,
                field_name=field_name,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        except Exception as exc:  # noqa: BLE001 - index is an optimisation, not required
            log.warning("Could not create the '%s' payload index: %s", field_name, exc)


def _point_id(item_id: str, chunk_index: int) -> str:
    # Stable, deterministic UUID so re-ingesting the same document updates the same
    # points in place instead of duplicating them. Keyed on the SOURCE ITEM ID, not
    # the filename: two files in different SharePoint subfolders can share a name,
    # and files get renamed and moved. Qdrant point IDs must be an unsigned integer
    # or a UUID - a raw hash string is rejected.
    return str(uuid.uuid5(_ID_NAMESPACE, f"{item_id}::{chunk_index}"))


def delete_document(client: QdrantClient, collection: str, item_id: str) -> None:
    """Remove every chunk belonging to one source document.

    Called both when a document is deleted at source and immediately before
    re-indexing a changed document - without the latter, a document that shrinks
    from 40 chunks to 20 strands 20 orphans that keep surfacing in retrieval.
    """
    try:
        client.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(key="item_id", match=models.MatchValue(value=item_id))]
                )
            ),
        )
    except Exception as exc:  # noqa: BLE001 - never abort a run over one deletion
        log.warning("Could not delete chunks for item %s: %s", item_id, exc)


def _content_type(chunk: Chunk, settings: Settings) -> str:
    if chunk.location.startswith("Image"):
        return "image"
    if is_table_heavy(chunk.text, settings.rag_table_chunk_threshold):
        return "table"
    return "text"


def index_document(
    client: QdrantClient,
    collection: str,
    document: SourceDocument,
    chunks: List[Chunk],
    dense: DenseEmbedder,
    sparse: SparseEmbedder,
    settings: Settings | None = None,
    document_identity_vector: Optional[List[float]] = None,
) -> int:
    """Embed and upsert every chunk of one document. Returns the number of points written.

    document_identity_vector: the document-level dense_metadata vector, generated
    ONCE per document by the caller (see ingest.py) and reused for every chunk here.
    """
    if not chunks:
        return 0
    settings = settings or get_settings()
    texts = [c.text for c in chunks]
    dense_vectors = dense.embed(texts)
    sparse_vectors = sparse.embed(texts)
    ensure_collection(client, collection, dense_dim=len(dense_vectors[0]))

    document_type = Path(document.name).suffix.lstrip(".").lower() or "text"

    points = []
    for chunk, dense_vec, sparse_vec in zip(chunks, dense_vectors, sparse_vectors):
        content_type = _content_type(chunk, settings)

        try:
            summary_text = generate_retrieval_proxy(chunk.text, settings)
            summary_vec = dense.embed_one(summary_text) if summary_text else dense_vec
        except Exception as exc:  # noqa: BLE001 - fall back to the content vector, never fail ingestion
            log.warning("Could not generate a retrieval summary for a chunk of %s: %s", document.name, exc)
            summary_vec = dense_vec

        vector: dict = {
            DENSE_CONTENT: dense_vec,
            DENSE_SUMMARY: summary_vec,
            SPARSE_BM25: models.SparseVector(indices=sparse_vec.indices.tolist(), values=sparse_vec.values.tolist()),
        }
        if document_identity_vector is not None:
            vector[DENSE_METADATA] = document_identity_vector

        if content_type == "table":
            try:
                paraphrase = generate_table_paraphrase(chunk.text, settings)
                if paraphrase:
                    vector[DENSE_TABLE] = dense.embed_one(paraphrase)
            except Exception as exc:  # noqa: BLE001 - table vector is an enhancement, not required
                log.warning("Could not generate a table paraphrase for a chunk of %s: %s", document.name, exc)

        points.append(
            models.PointStruct(
                id=_point_id(document.item_id, chunk.chunk_index),
                vector=vector,
                payload={
                    "item_id": document.item_id,
                    "source": document.name,
                    "folder_path": document.folder_path,
                    "location": chunk.location,
                    "text": chunk.text,
                    "web_url": document.web_url,
                    "modified_at": document.modified_at.isoformat(),
                    "chunk_index": chunk.chunk_index,
                    "content_type": content_type,
                    "document_type": document_type,
                },
            )
        )
    client.upsert(collection_name=collection, points=points)
    return len(points)

