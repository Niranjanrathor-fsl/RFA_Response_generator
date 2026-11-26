"""Reranking must not run in parallel, and must use small batches.

Four per-question reranks at once, at fastembed's default batch of 64, pushed the
Azure worker into out-of-memory kills mid-generation.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np

from app.config import get_settings
from app.rag import retrieve


class RecordingReranker:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.batch_sizes = []
        self._lock = threading.Lock()

    def rerank(self, query, texts, batch_size=64):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.batch_sizes.append(batch_size)
        time.sleep(0.05)  # long enough for parallel callers to overlap if allowed
        with self._lock:
            self.active -= 1
        return [float(i) for i in range(len(texts))]


def _fake_backend(monkeypatch, reranker):
    points = [SimpleNamespace(payload={"text": f"passage {i}", "source": "doc.pdf"}) for i in range(5)]
    client = SimpleNamespace(
        collection_exists=lambda name: True,
        query_points=lambda **kwargs: SimpleNamespace(points=points),
    )
    sparse = SimpleNamespace(indices=np.array([1]), values=np.array([1.0]))
    monkeypatch.setattr(retrieve, "get_client", lambda settings: client)
    monkeypatch.setattr(retrieve, "DenseEmbedder", lambda settings: SimpleNamespace(embed_one=lambda q: [0.0]))
    monkeypatch.setattr(retrieve, "SparseEmbedder", lambda: SimpleNamespace(embed_one=lambda q: sparse))
    monkeypatch.setattr(retrieve.store, "get_known_document_names", lambda settings: [])
    monkeypatch.setattr(retrieve, "_reranker", lambda: reranker)


def test_per_question_reranks_run_one_at_a_time_with_small_batches(monkeypatch):
    reranker = RecordingReranker()
    _fake_backend(monkeypatch, reranker)
    settings = get_settings().model_copy(update={
        "rag_enabled": True, "rag_cache_enabled": False,
        "rag_question_search_workers": 4, "rag_rerank_batch_size": 16,
    })

    results = retrieve.search_per_question([f"Question {i}?" for i in range(6)], settings)

    assert all(chunks for _, chunks in results)  # every question still got its evidence
    assert reranker.max_active == 1
    assert reranker.batch_sizes == [16] * 6
