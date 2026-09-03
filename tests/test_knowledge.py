"""The knowledge base must load from disk and stay server-side."""

from __future__ import annotations

import pytest

from app.knowledge import KnowledgeBaseError, load_knowledge_base


def test_loads_all_three_reference_documents():
    kb = load_knowledge_base()
    assert "Intelligence that Operates" in kb.analyst_kb
    assert kb.kairos_glossary.strip()
    assert "#DF6014" in kb.brand_reference
    assert kb.total_chars > 10_000


def test_prompt_block_contains_every_source():
    block = load_knowledge_base().prompt_block()
    assert "ANALYST KNOWLEDGE BASE" in block
    assert "GLOSSARY" in block
    assert "BRAND REFERENCE" in block


def test_missing_directory_raises(tmp_path):
    with pytest.raises(KnowledgeBaseError) as excinfo:
        load_knowledge_base(tmp_path)
    assert "Missing knowledge file" in str(excinfo.value)


def test_knowledge_base_is_not_exposed_over_http(client):
    """No route may return the knowledge base to the browser."""
    assert client.get("/api/knowledge").status_code == 404
    assert client.get("/static/../knowledge/analyst-knowledge-base.md").status_code in (403, 404)
    config = client.get("/api/config").json()
    serialised = str(config)
    assert "liability" not in serialised.lower()
    assert "sk-ant" not in serialised
