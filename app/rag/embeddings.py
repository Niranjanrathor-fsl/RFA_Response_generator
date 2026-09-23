"""Embeddings for the RAG index: dense (Azure OpenAI) + sparse (BM25).

Dense captures meaning; sparse (BM25) catches exact terms (product names, codes)
that a meaning-only search can miss. Both are combined at query time with
Reciprocal Rank Fusion (see retrieve.py).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import List

from openai import AzureOpenAI

from ..config import Settings, get_settings

log = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Raised when an embedding call fails or is misconfigured."""


class DenseEmbedder:
    """Wraps the Azure OpenAI embeddings endpoint (separate deployment from chat)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.azure_openai_embedding_api_key:
            raise EmbeddingError("AZURE_OPENAI_EMBEDDING_API_KEY is not configured.")
        if not self.settings.azure_openai_embedding_endpoint:
            raise EmbeddingError("AZURE_OPENAI_EMBEDDING_ENDPOINT is not configured.")
        if not self.settings.azure_openai_embedding_deployment:
            raise EmbeddingError(
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT is not configured. "
                "Deploy an embeddings model (e.g. text-embedding-3-large) and set it."
            )
        self._client = AzureOpenAI(
            api_key=self.settings.azure_openai_embedding_api_key,
            azure_endpoint=self.settings.azure_openai_embedding_endpoint,
            api_version=self.settings.azure_openai_embedding_api_version,
        )

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        response = self._client.embeddings.create(
            model=self.settings.azure_openai_embedding_deployment,
            input=texts,
        )
        return [item.embedding for item in response.data]

    def embed_one(self, text: str) -> List[float]:
        return self.embed([text])[0]


@lru_cache
def _sparse_model():
    # Imported lazily: fastembed downloads a small ONNX model on first use.
    from fastembed import SparseTextEmbedding

    return SparseTextEmbedding(model_name="Qdrant/bm25")


class SparseEmbedder:
    """BM25 sparse vectors via fastembed - no network calls, no GPU."""

    def embed(self, texts: List[str]):
        """Returns a list of fastembed SparseEmbedding objects (.indices, .values)."""
        if not texts:
            return []
        return list(_sparse_model().embed(texts))

    def embed_one(self, text: str):
        return self.embed([text])[0]


# ------------------------------------------------------- Phase 4: multi-vector
# These generate TEXT via the chat model (gpt-5.4); the caller embeds the result
# with DenseEmbedder. Kept here (not in llm.py) since they're RAG-indexing-specific.

def generate_retrieval_proxy(chunk_text: str, settings: Settings | None = None) -> str:
    """Short, retrieval-optimized proxy for one chunk - a summary or hypothetical
    questions it answers. This is what gets embedded for the dense_summary vector;
    the FULL original chunk (unchanged) is still what's sent to the LLM at generation
    time - this is the "decouple matching from LLM context" mechanism."""
    from ..llm import LLMClient

    settings = settings or get_settings()
    client = LLMClient(settings)
    prompt = (
        "Summarize what this passage could answer, as EITHER a 1-2 sentence dense "
        "summary OR 3-5 short hypothetical questions it answers. Be concise and "
        "keyword-rich - this will be used purely for search matching, not display. "
        "Output ONLY the summary/questions, nothing else.\n\n"
        f"PASSAGE:\n{chunk_text[:3000]}"
    )
    return client.ask("You write concise, keyword-rich retrieval summaries.", prompt).strip()


def generate_document_identity_summary(
    document_name: str, sample_text: str, settings: Settings | None = None
) -> str:
    """One summary per SOURCE DOCUMENT (not per chunk) - filename, type, topic, and
    key named entities/products. Embedded once and reused for every chunk from this
    document's dense_metadata vector, so document/client/entity-named queries match
    strongly even if a specific chunk doesn't repeat those words densely."""
    from ..llm import LLMClient

    settings = settings or get_settings()
    client = LLMClient(settings)
    prompt = (
        f"Document filename: {document_name}\n\n"
        "Based on this excerpt, write a short identity summary covering: the "
        "document's type/topic/client or subject, and 5-10 key named entities, "
        "products or terms it discusses. 2-4 sentences. Output ONLY the summary.\n\n"
        f"EXCERPT:\n{sample_text[:4000]}"
    )
    return client.ask("You write concise document identity summaries for search indexing.", prompt).strip()


def is_table_heavy(chunk_text: str, threshold: float) -> bool:
    """Heuristic: a chunk is table-heavy if this fraction of its lines are pipe-delimited."""
    lines = [line for line in chunk_text.split("\n") if line.strip()]
    if not lines:
        return False
    pipe_lines = sum(1 for line in lines if " | " in line)
    return (pipe_lines / len(lines)) >= threshold


def generate_table_paraphrase(chunk_text: str, settings: Settings | None = None) -> str:
    """Natural-language paraphrase of a table-heavy chunk, for the dense_table vector.
    Raw 'col | col | col' rows are out-of-distribution for embedding models trained
    mostly on prose; a paraphrase embeds far more meaningfully for search matching.
    The original chunk (with the real table) is unchanged for the LLM context."""
    from ..llm import LLMClient

    settings = settings or get_settings()
    client = LLMClient(settings)
    prompt = (
        "Paraphrase this table as natural-language prose for search indexing - "
        "describe what it compares or shows and mention the key values, without "
        "just repeating the raw pipe-delimited rows. Output ONLY the paraphrase.\n\n"
        f"TABLE:\n{chunk_text[:3000]}"
    )
    return client.ask("You paraphrase tables into natural language for search indexing.", prompt).strip()

