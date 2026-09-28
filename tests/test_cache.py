"""Semantic cache invalidation.

A cached retrieval result must not outlive a change to the index (new documents
ingested) or to how retrieval works (top_k, near-duplicate threshold, ...).
Previously only a change to a document *inside* the cached result invalidated it,
so newly ingested documents and retrieval fixes were invisible to repeat queries.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from app.config import get_settings
from app.rag import cache
from app.rag.store import DocumentState


def _state(item_id, tag="t1", status="ok"):
    return DocumentState(item_id=item_id, name=item_id, content_tag=tag, content_hash="h",
                         sync_status=status, attempt_count=0)


def _settings(**overrides):
    return get_settings().model_copy(update={"rag_cache_enabled": True, **overrides})


def test_signature_changes_when_a_document_is_added():
    s = _settings()
    before = cache._retrieval_signature(s, {"a": _state("a")})
    after = cache._retrieval_signature(s, {"a": _state("a"), "b": _state("b")})
    assert before != after


def test_signature_ignores_documents_that_failed_to_index():
    s = _settings()
    assert cache._retrieval_signature(s, {"a": _state("a")}) == cache._retrieval_signature(
        s, {"a": _state("a"), "b": _state("b", tag="", status="failed")}
    )


def test_signature_changes_with_retrieval_settings():
    states = {"a": _state("a")}
    base = cache._retrieval_signature(_settings(), states)
    assert cache._retrieval_signature(_settings(rag_rerank_top_k=5), states) != base
    assert cache._retrieval_signature(_settings(rag_near_duplicate_threshold=0.9), states) != base


class _FakeQdrant:
    def __init__(self, payload):
        self.payload = payload

    def collection_exists(self, name):
        return True

    def query_points(self, **kwargs):
        return SimpleNamespace(points=[SimpleNamespace(score=0.99, payload=self.payload)])


def _lookup_with(monkeypatch, settings, cached_signature, states):
    payload = {
        "cache_id": "c1",
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "source_hashes": {"a": "t1"},
        "signature": cached_signature,
        "result_json": json.dumps([{"text": "x", "source": "a.docx", "location": "",
                                    "web_url": "", "score": 1.0, "item_id": "a"}]),
    }
    monkeypatch.setattr(cache, "_client", lambda s: _FakeQdrant(payload))
    monkeypatch.setattr(cache, "DenseEmbedder", lambda s: SimpleNamespace(embed_one=lambda q: [0.0]))
    monkeypatch.setattr(cache.store, "get_document_states", lambda s: states)
    monkeypatch.setattr(cache.store, "record_cache_hit", lambda *a: None)
    return cache.lookup("question", settings)


def test_lookup_hits_when_nothing_changed(monkeypatch):
    s, states = _settings(), {"a": _state("a")}
    assert _lookup_with(monkeypatch, s, cache._retrieval_signature(s, states), states)


def test_lookup_misses_after_new_documents_are_ingested(monkeypatch):
    s = _settings()
    old = cache._retrieval_signature(s, {"a": _state("a")})
    assert _lookup_with(monkeypatch, s, old, {"a": _state("a"), "b": _state("b")}) is None


def test_lookup_misses_entries_written_before_signatures_existed(monkeypatch):
    s, states = _settings(), {"a": _state("a")}
    assert _lookup_with(monkeypatch, s, None, states) is None
