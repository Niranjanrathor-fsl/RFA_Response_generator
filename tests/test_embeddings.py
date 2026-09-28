"""DenseEmbedder request sizing: large documents must never exceed the endpoint's limits."""

from __future__ import annotations

from types import SimpleNamespace

from app.config import get_settings
from app.rag import embeddings as embeddings_module
from app.rag.embeddings import DenseEmbedder


class _FakeEmbeddingsAPI:
    def __init__(self) -> None:
        self.calls = []

    def create(self, model, input):  # noqa: A002 - mirrors the SDK signature
        self.calls.append(list(input))
        return SimpleNamespace(data=[SimpleNamespace(embedding=[float(len(t))]) for t in input])


def _embedder(monkeypatch) -> tuple[DenseEmbedder, _FakeEmbeddingsAPI]:
    settings = get_settings().model_copy(update={
        "azure_openai_embedding_api_key": "k",
        "azure_openai_embedding_endpoint": "https://example.invalid",
        "azure_openai_embedding_deployment": "d",
    })
    api = _FakeEmbeddingsAPI()
    monkeypatch.setattr(embeddings_module, "AzureOpenAI",
                        lambda **kwargs: SimpleNamespace(embeddings=api))
    return DenseEmbedder(settings), api


def test_large_document_is_split_across_requests(monkeypatch):
    # ISG Generative AI Services 2025.xlsx failed with "maximum request size is
    # 300000 tokens per request" because every chunk went in one call.
    embedder, api = _embedder(monkeypatch)
    texts = ["word " * 1000] * 400  # ~400k tokens in total

    vectors = embedder.embed(texts)

    assert len(vectors) == len(texts)
    assert len(api.calls) > 1
    for request in api.calls:
        tokens = sum(embeddings_module._token_len(t) for t in request)
        assert tokens <= embeddings_module.MAX_TOKENS_PER_REQUEST
        assert len(request) <= embeddings_module.MAX_INPUTS_PER_REQUEST


def test_order_is_preserved_across_batches(monkeypatch):
    embedder, _ = _embedder(monkeypatch)
    texts = [("x " * n) for n in range(1, 3000)]

    vectors = embedder.embed(texts)

    assert [v[0] for v in vectors] == [float(len(t)) for t in texts]


def test_single_input_over_the_model_limit_is_truncated(monkeypatch):
    embedder, api = _embedder(monkeypatch)

    embedder.embed(["word " * 20_000])

    sent = api.calls[0][0]
    assert embeddings_module._token_len(sent) <= embeddings_module.MAX_TOKENS_PER_INPUT
