"""Sync engine decision logic. Every collaborator is faked - no network, no LLM."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.rag import sync as sync_module
from app.rag.sources import SourceBatch, SourceDeletion, SourceDocument
from app.rag.store import DocumentState


def _settings(**overrides) -> Settings:
    """An enabled Settings. conftest forces RAG_ENABLED=false for the app-level
    tests; these exercise the engine's decision logic, which runs only when it
    is on, so they build their own."""
    base = dict(
        azure_openai_api_key="k", azure_openai_endpoint="https://e/",
        azure_openai_deployment_name="gpt-5.4", rag_enabled=True, rag_source="local",
        pg_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


def _doc(item_id="i1", name="a.docx", tag="ctag-1", content_hash="hash-1", payload=b"bytes"):
    return SourceDocument(
        item_id=item_id, name=name, folder_path="ISG/2026", content_tag=tag,
        content_hash=content_hash, modified_at=datetime.now(timezone.utc),
        web_url="https://sp/a.docx", size=10, fetch=lambda: payload,
    )


class _FakeSource:
    def __init__(self, batch):
        self.batch = batch
        self.closed = False

    def fetch_changes(self, delta_link):
        return self.batch

    def close(self):
        self.closed = True


@pytest.fixture
def harness(monkeypatch):
    """Fakes every boundary the engine touches, and records what it did."""
    calls = {
        "indexed": [], "deleted_qdrant": [], "recorded": [], "failures": [],
        "deleted_pg": [], "saved_link": [], "chunks": None,
    }

    monkeypatch.setattr(sync_module.store, "ensure_schema", lambda s: None)
    monkeypatch.setattr(sync_module.store, "start_ingestion_run", lambda s, trigger="manual": 1)
    monkeypatch.setattr(sync_module.store, "finish_ingestion_run", lambda *a, **k: None)
    monkeypatch.setattr(sync_module.store, "get_sync_state", lambda k, s: None)
    monkeypatch.setattr(sync_module.store, "get_content_hash_owners", lambda s: {})
    monkeypatch.setattr(sync_module.store, "save_sync_state",
                        lambda k, link, full, s: calls["saved_link"].append((link, full)))
    monkeypatch.setattr(sync_module.store, "mark_document_deleted",
                        lambda i, s: calls["deleted_pg"].append(i))
    monkeypatch.setattr(sync_module.store, "record_document",
                        lambda **kw: calls["recorded"].append(kw))
    monkeypatch.setattr(sync_module.store, "record_document_failure",
                        lambda *a, **k: calls["failures"].append(a[0]))
    monkeypatch.setattr(sync_module.index, "get_client", lambda s: object())
    monkeypatch.setattr(sync_module.index, "delete_document",
                        lambda c, coll, i: calls["deleted_qdrant"].append(i))
    monkeypatch.setattr(sync_module.index, "index_document",
                        lambda *a, **k: (calls["indexed"].append(a[2].item_id), 3)[1])
    monkeypatch.setattr(sync_module, "DenseEmbedder", lambda s: object())
    monkeypatch.setattr(sync_module, "SparseEmbedder", lambda: object())
    monkeypatch.setattr(sync_module, "generate_document_identity_summary", lambda *a, **k: "")
    monkeypatch.setattr(sync_module, "chunk_document",
                        lambda *a, **k: calls["chunks"] if calls["chunks"] is not None else ["c1"])
    return calls


def _run(monkeypatch, harness, batch, states=None, settings=None, **kwargs):
    monkeypatch.setattr(sync_module.store, "get_document_states", lambda s: states or {})
    source = _FakeSource(batch)
    harness["source"] = source
    monkeypatch.setattr(sync_module, "get_document_source", lambda s: source)
    return sync_module.sync_once(settings or _settings(), **kwargs)


def test_new_document_is_indexed(monkeypatch, harness):
    report = _run(monkeypatch, harness, SourceBatch([_doc()], [], "link-1"))
    assert harness["indexed"] == ["i1"]
    assert report.documents_indexed == 1


def test_unchanged_content_tag_does_no_work(monkeypatch, harness):
    """The core cost control: a rename or metadata edit must cost nothing."""
    states = {"i1": DocumentState("i1", "a.docx", "ctag-1", "hash-1", "ok", 0)}
    report = _run(monkeypatch, harness, SourceBatch([_doc(tag="ctag-1")], [], "l"), states)
    assert harness["indexed"] == []
    assert report.documents_skipped == 1


def test_changed_content_tag_reindexes(monkeypatch, harness):
    states = {"i1": DocumentState("i1", "a.docx", "OLD-ctag", "hash-1", "ok", 0)}
    _run(monkeypatch, harness, SourceBatch([_doc(tag="NEW-ctag")], [], "l"), states)
    assert harness["indexed"] == ["i1"]


def test_reindex_deletes_old_chunks_first(monkeypatch, harness):
    """Otherwise a document that shrinks leaves orphan chunks behind."""
    states = {"i1": DocumentState("i1", "a.docx", "OLD", "hash-1", "ok", 0)}
    _run(monkeypatch, harness, SourceBatch([_doc(tag="NEW")], [], "l"), states)
    assert harness["deleted_qdrant"] == ["i1"]


def test_deletion_removes_chunks_and_marks_the_row(monkeypatch, harness):
    report = _run(monkeypatch, harness, SourceBatch([], [SourceDeletion("i9")], "l"))
    assert harness["deleted_qdrant"] == ["i9"]
    assert harness["deleted_pg"] == ["i9"]
    assert report.documents_deleted == 1


def test_delta_link_is_saved_after_processing(monkeypatch, harness):
    _run(monkeypatch, harness, SourceBatch([_doc()], [], "link-42"))
    assert harness["saved_link"] == [("link-42", False)]


def test_failure_is_recorded_and_does_not_abort_the_run(monkeypatch, harness):
    def explode(*a, **k):
        raise RuntimeError("parser blew up")

    monkeypatch.setattr(sync_module, "chunk_document", explode)
    report = _run(monkeypatch, harness, SourceBatch([_doc("i1"), _doc("i2", "b.docx")], [], "l"))
    assert report.documents_failed == 2
    assert harness["failures"] == ["i1", "i2"]


def test_document_with_zero_chunks_still_records_state(monkeypatch, harness):
    """An empty PDF must not be retried on every single sync forever."""
    harness["chunks"] = []
    report = _run(monkeypatch, harness, SourceBatch([_doc()], [], "l"))
    assert report.documents_skipped == 1
    assert [r["item_id"] for r in harness["recorded"]] == ["i1"]


def test_duplicate_content_is_indexed_only_once(monkeypatch, harness):
    monkeypatch.setattr(sync_module.store, "get_content_hash_owners", lambda s: {"hash-1": "i1"})
    report = _run(
        monkeypatch, harness,
        SourceBatch([_doc("i2", "a (1).docx", tag="t2", content_hash="hash-1")], [], "l"),
    )
    assert harness["indexed"] == []
    assert report.duplicates_skipped == 1


def test_permanently_failing_document_is_abandoned_after_max_attempts(monkeypatch, harness):
    settings = _settings()
    states = {"i1": DocumentState("i1", "a.docx", "", "", "failed", settings.rag_sync_max_attempts)}
    report = _run(monkeypatch, harness, SourceBatch([_doc()], [], "l"), states, settings=settings)
    assert harness["indexed"] == []
    assert report.documents_skipped == 1


def test_lock_not_acquired_means_no_work(monkeypatch, harness):
    from contextlib import contextmanager

    @contextmanager
    def busy(settings, key=None):
        yield False

    monkeypatch.setattr(sync_module.store, "advisory_lock", busy)
    report = _run(monkeypatch, harness, SourceBatch([_doc()], [], "l"))
    assert harness["indexed"] == []
    assert report.documents_indexed == 0


def test_rag_disabled_does_no_work(monkeypatch, harness):
    """The master switch still wins: nothing is touched when RAG is off."""
    report = _run(
        monkeypatch, harness, SourceBatch([_doc()], [], "l"),
        settings=_settings(rag_enabled=False),
    )
    assert harness["indexed"] == []
    assert "RAG_ENABLED is false" in report.error


def test_reset_flag_clears_state_before_a_full_run(monkeypatch):
    """The wiped-collection trap: without clearing the stored content tags, the
    next sync concludes 'nothing changed' and a freshly wiped collection stays
    empty forever."""
    from app.rag import ingest as ingest_module
    from app.rag.sync import SyncReport

    settings = Settings(
        azure_openai_api_key="k", azure_openai_endpoint="https://e/",
        azure_openai_deployment_name="gpt-5.4", rag_enabled=True, rag_source="sharepoint",
    )
    calls = {"reset": [], "full": []}
    monkeypatch.setattr(ingest_module, "get_settings", lambda: settings)
    monkeypatch.setattr(ingest_module.store, "ensure_schema", lambda s: None)
    monkeypatch.setattr(ingest_module.store, "reset_sync_state",
                        lambda key, s: calls["reset"].append(key))
    monkeypatch.setattr(
        ingest_module, "sync_once",
        lambda s, full=False, trigger="manual": (calls["full"].append(full), SyncReport())[1],
    )

    ingest_module.run(full=False, reset=True)

    assert calls["reset"] == ["sharepoint"]
    assert calls["full"] == [True]  # --reset must imply a full re-enumeration


def test_delta_link_is_held_back_while_a_failure_is_still_retryable(monkeypatch, harness):
    """Advancing past a failed document would drop it out of every future
    incremental window - nothing would revisit it until the next full reconcile."""
    def explode(*a, **k):
        raise RuntimeError("transient embedding 500")

    monkeypatch.setattr(sync_module, "chunk_document", explode)
    _run(monkeypatch, harness, SourceBatch([_doc()], [], "link-99"))
    assert harness["saved_link"] == []


def test_delta_link_advances_once_a_failure_is_no_longer_retryable(monkeypatch, harness):
    """Otherwise one permanently broken file wedges the sync forever."""
    def explode(*a, **k):
        raise RuntimeError("permanently broken")

    settings = _settings()
    states = {
        "i1": DocumentState(
            "i1", "a.docx", "", "", "failed", settings.rag_sync_max_attempts - 1
        )
    }
    monkeypatch.setattr(sync_module, "chunk_document", explode)
    _run(monkeypatch, harness, SourceBatch([_doc()], [], "link-99"), states, settings=settings)
    assert harness["saved_link"] == [("link-99", False)]


def test_the_source_is_closed_even_when_the_run_fails(monkeypatch, harness):
    """A fresh httpx client per sync, never closed, leaks sockets in a process
    meant to run for weeks."""
    monkeypatch.setattr(sync_module.index, "get_client", _boom)
    _run(monkeypatch, harness, SourceBatch([_doc()], [], "l"))
    assert harness["source"].closed is True


def _boom(*a, **k):
    raise RuntimeError("qdrant unreachable")
