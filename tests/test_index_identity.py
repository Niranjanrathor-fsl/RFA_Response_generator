"""Document identity and deletion. No live Qdrant."""

from __future__ import annotations

from app.rag.index import _point_id, delete_document


def test_point_id_is_stable_for_the_same_item():
    assert _point_id("item-abc", 3) == _point_id("item-abc", 3)


def test_same_filename_in_different_folders_gets_different_ids():
    """The collision this whole identity change exists to prevent."""
    assert _point_id("item-in-isg", 0) != _point_id("item-in-hfs", 0)


def test_chunk_index_separates_points_within_a_document():
    assert _point_id("item-abc", 0) != _point_id("item-abc", 1)


class _FakeClient:
    def __init__(self):
        self.deleted = []

    def delete(self, collection_name, points_selector):
        self.deleted.append((collection_name, points_selector))


def test_delete_document_filters_on_item_id():
    client = _FakeClient()
    delete_document(client, "kb", "item-abc")
    collection, selector = client.deleted[0]
    assert collection == "kb"
    condition = selector.filter.must[0]
    assert condition.key == "item_id"
    assert condition.match.value == "item-abc"


# ---------------------------------------------- Qdrant write resilience
# retrieve.py already retries Qdrant QUERIES 3 times, noting that "the
# self-hosted Qdrant VM occasionally drops connections under rapid back-to-back
# requests (seen during ingestion too)". The WRITE path had no retry, so a
# timeout on the final upsert discarded every LLM call already paid for that
# document. Observed live: 1 failure in the first 5 documents of a real ingest.

import pytest

from app.config import Settings
from app.rag import index as index_module
from app.rag.sources import SourceDocument
from app.rag.chunking import Chunk
from datetime import datetime, timezone


def _settings() -> Settings:
    return Settings(
        azure_openai_api_key="k", azure_openai_endpoint="https://e/",
        azure_openai_deployment_name="gpt-5.4",
    )


class _FlakyClient:
    """Fails `failures` times on each write, then succeeds."""

    def __init__(self, failures: int):
        self.remaining = failures
        self.upserts = 0
        self.deletes = 0

    def _maybe_fail(self):
        if self.remaining > 0:
            self.remaining -= 1
            raise TimeoutError("timed out")

    def upsert(self, **kwargs):
        self.upserts += 1
        self._maybe_fail()

    def delete(self, **kwargs):
        self.deletes += 1
        self._maybe_fail()

    def collection_exists(self, name):
        return True


class _Dense:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    def embed_one(self, text):
        return [0.1, 0.2, 0.3]


class _SparseVec:
    class _Arr(list):
        def tolist(self):
            return list(self)

    indices = _Arr([1, 2])
    values = _Arr([0.5, 0.5])


class _Sparse:
    def embed(self, texts):
        return [_SparseVec() for _ in texts]


def _index_one(client, monkeypatch):
    monkeypatch.setattr(index_module, "generate_retrieval_proxy", lambda *a, **k: "")
    document = SourceDocument(
        item_id="i1", name="a.docx", folder_path="ISG", content_tag="t",
        content_hash="h", modified_at=datetime.now(timezone.utc),
        web_url="https://sp/a.docx", size=1,
    )
    chunks = [Chunk(text="some chunk text", location="", chunk_index=0)]
    return index_module.index_document(
        client, "kb", document, chunks, _Dense(), _Sparse(), _settings()
    )


def test_upsert_retries_a_transient_qdrant_timeout(monkeypatch):
    client = _FlakyClient(failures=2)
    written = _index_one(client, monkeypatch)
    assert written == 1
    assert client.upserts == 3  # two timeouts, then success


def test_upsert_gives_up_after_repeated_failures(monkeypatch):
    """It must still raise so sync records the failure and retries the document
    later, rather than silently reporting success with nothing written."""
    client = _FlakyClient(failures=99)
    with pytest.raises(TimeoutError):
        _index_one(client, monkeypatch)


def test_delete_document_retries_a_transient_qdrant_timeout():
    client = _FlakyClient(failures=1)
    delete_document(client, "kb", "item-abc")
    assert client.deletes == 2
