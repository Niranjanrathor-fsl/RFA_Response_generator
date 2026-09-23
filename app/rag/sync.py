"""Incremental ingestion: source changes -> chunk -> embed -> index.

One entry point, sync_once(), used by both the manual CLI and the background
scheduler. It is deliberately trigger-agnostic: if a Graph webhook receiver is
added once the app is publicly hosted, it calls this same function.

Retry model: a document that fails to ingest never records its content_tag, so it
still looks changed. That alone is not enough for a DELTA source - advancing the
delta link would drop the failed item out of every future incremental window - so
the delta link is also held back while any failure is still retryable. Once a
document exhausts RAG_SYNC_MAX_ATTEMPTS it stops holding the link, which is what
keeps the sync from wedging on one permanently broken file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..config import Settings, get_settings
from . import index, store
from .chunking import chunk_document
from .embeddings import DenseEmbedder, SparseEmbedder, generate_document_identity_summary
from .sources import SourceDocument, get_document_source

log = logging.getLogger(__name__)


@dataclass
class SyncReport:
    documents_indexed: int = 0
    documents_skipped: int = 0
    documents_failed: int = 0
    documents_deleted: int = 0
    duplicates_skipped: int = 0
    chunks_indexed: int = 0
    error: str = ""

    def summary(self) -> str:
        return (
            f"{self.documents_indexed} indexed ({self.chunks_indexed} chunks), "
            f"{self.documents_skipped} unchanged, {self.duplicates_skipped} duplicates, "
            f"{self.documents_deleted} deleted, {self.documents_failed} failed"
        )


def _needs_ingestion(
    document: SourceDocument, state: Optional[store.DocumentState], settings: Settings
) -> bool:
    if state is None:
        return True
    if state.sync_status == "failed" and state.attempt_count >= settings.rag_sync_max_attempts:
        log.warning(
            "Giving up on %s after %d failed attempts; it will be retried only if its content changes.",
            document.name, state.attempt_count,
        )
        return False
    # An empty stored tag means "never successfully ingested" - always retry.
    if not state.content_tag:
        return True
    return state.content_tag != document.content_tag


def sync_once(
    settings: Settings | None = None, *, full: bool = False, trigger: str = "manual"
) -> SyncReport:
    """Pull changes from the configured source and bring the index up to date."""
    settings = settings or get_settings()
    report = SyncReport()

    if not settings.rag_enabled:
        report.error = "RAG_ENABLED is false."
        log.error(report.error)
        return report

    with store.advisory_lock(settings) as acquired:
        if not acquired:
            log.info("Another instance is already syncing; skipping this run.")
            return report

        store.ensure_schema(settings)
        source_key = settings.rag_source
        run_id = store.start_ingestion_run(settings, trigger=trigger)
        source = None

        try:
            delta_link = None if full else store.get_sync_state(source_key, settings)
            if full:
                log.info("Running a FULL enumeration (ignoring any saved delta link).")

            source = get_document_source(settings)
            batch = source.fetch_changes(delta_link)
            if batch.resynced:
                log.warning(
                    "The delta token was rejected; this run is a full re-enumeration."
                )

            client = index.get_client(settings)
            collection = settings.qdrant_collection_v2
            dense = DenseEmbedder(settings)
            sparse = SparseEmbedder()

            # Deletions first: a delete-then-recreate in the same batch must end indexed.
            for deletion in batch.deletions:
                index.delete_document(client, collection, deletion.item_id)
                store.mark_document_deleted(deletion.item_id, settings)
                report.documents_deleted += 1

            states = store.get_document_states(settings)
            hash_owners = store.get_content_hash_owners(settings)
            retryable_failures = 0

            for document in batch.documents:
                if not _needs_ingestion(document, states.get(document.item_id), settings):
                    report.documents_skipped += 1
                    continue

                if (
                    settings.rag_skip_duplicate_content
                    and document.content_hash
                    and hash_owners.get(document.content_hash) not in (None, document.item_id)
                ):
                    log.info(
                        "Skipping %s: identical content already indexed as item %s.",
                        document.name, hash_owners[document.content_hash],
                    )
                    report.duplicates_skipped += 1
                    continue

                try:
                    written = _ingest_one(document, client, collection, dense, sparse, settings)
                except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
                    log.warning("Failed to ingest %s: %s: %s", document.name, type(exc).__name__, exc)
                    store.record_document_failure(
                        document.item_id, document.name, source_key,
                        f"{type(exc).__name__}: {exc}", settings,
                    )
                    report.documents_failed += 1
                    previous = states.get(document.item_id)
                    attempts = (previous.attempt_count if previous else 0) + 1
                    if attempts < settings.rag_sync_max_attempts:
                        retryable_failures += 1
                    continue

                if written == 0:
                    # Recorded anyway, so an unreadable file is not retried every sync.
                    report.documents_skipped += 1
                else:
                    report.documents_indexed += 1
                    report.chunks_indexed += written
                    if document.content_hash:
                        hash_owners.setdefault(document.content_hash, document.item_id)

            if retryable_failures:
                # Holding the link back keeps the failed items inside the next
                # incremental window. Advancing past them would mean nothing
                # revisits them until the next full reconcile.
                log.warning(
                    "Not advancing the delta link: %d document(s) failed and are still retryable.",
                    retryable_failures,
                )
            elif batch.delta_link:
                store.save_sync_state(source_key, batch.delta_link, full, settings)

        except Exception as exc:  # noqa: BLE001 - a sync failure must never reach the app
            report.error = f"{type(exc).__name__}: {exc}"
            log.warning("Sync run failed: %s", report.error)
        finally:
            if source is not None:
                source.close()  # release the Graph HTTP connection pool
            store.finish_ingestion_run(
                run_id, report.documents_indexed, report.chunks_indexed, report.error, settings
            )

    log.info("Sync complete: %s.", report.summary())
    return report


def _ingest_one(
    document: SourceDocument, client, collection: str, dense, sparse, settings: Settings
) -> int:
    """Chunk, embed and index one document. Returns the number of points written."""
    data = document.fetch()
    chunks = chunk_document(
        document.name,
        data,
        max_tokens=settings.rag_chunk_tokens,
        overlap_tokens=settings.rag_chunk_overlap_tokens,
        settings=settings,
    )

    # Always clear first: a document that shrinks would otherwise strand the
    # chunks beyond its new length, and they would keep surfacing in retrieval.
    index.delete_document(client, collection, document.item_id)

    if not chunks:
        log.info("No text extracted from %s; recording it so it is not retried every run.", document.name)
        store.record_document(
            item_id=document.item_id, name=document.name, folder_path=document.folder_path,
            source_type=settings.rag_source, web_url=document.web_url,
            content_tag=document.content_tag, content_hash=document.content_hash,
            chunk_count=0, settings=settings,
        )
        return 0

    document_identity_vector = None
    try:
        sample_text = "\n\n".join(c.text for c in chunks[:3])
        identity_summary = generate_document_identity_summary(document.name, sample_text, settings)
        if identity_summary:
            document_identity_vector = dense.embed_one(identity_summary)
    except Exception as exc:  # noqa: BLE001 - identity vector is an enhancement, not required
        log.warning("Could not generate a document identity summary for %s: %s", document.name, exc)

    written = index.index_document(
        client, collection, document, chunks, dense, sparse, settings, document_identity_vector
    )
    log.info("Indexed %s (%s): %d chunks.", document.name, document.folder_path or "root", written)

    store.record_document(
        item_id=document.item_id, name=document.name, folder_path=document.folder_path,
        source_type=settings.rag_source, web_url=document.web_url,
        content_tag=document.content_tag, content_hash=document.content_hash,
        chunk_count=written, settings=settings,
    )
    return written
