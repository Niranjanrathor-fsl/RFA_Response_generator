"""Startup validation for the sync settings."""

from __future__ import annotations

from app.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        azure_openai_api_key="k", azure_openai_endpoint="https://e/",
        azure_openai_deployment_name="gpt-5.4",
    )
    base.update(overrides)
    return Settings(**base)


def test_sync_without_rag_is_flagged():
    problems = _settings(rag_sync_enabled=True, rag_enabled=False).startup_problems()
    assert any("RAG_ENABLED is false" in p for p in problems)


def test_sharepoint_sync_without_postgres_is_flagged():
    problems = _settings(
        rag_sync_enabled=True, rag_enabled=True, rag_source="sharepoint", pg_enabled=False,
        azure_openai_embedding_api_key="k", azure_openai_embedding_endpoint="https://e/",
        azure_openai_embedding_deployment="emb", ms_tenant_id="t", ms_client_id="c",
        ms_client_secret="s", sharepoint_site_url="https://sp/",
    ).startup_problems()
    assert any("PG_ENABLED=true" in p for p in problems)


def test_zero_interval_is_flagged():
    problems = _settings(
        rag_sync_enabled=True, rag_enabled=True, rag_source="local",
        rag_sync_interval_minutes=0,
        azure_openai_embedding_api_key="k", azure_openai_embedding_endpoint="https://e/",
        azure_openai_embedding_deployment="emb",
    ).startup_problems()
    assert any("RAG_SYNC_INTERVAL_MINUTES" in p for p in problems)


def test_defaults_are_conservative():
    settings = _settings()
    assert settings.rag_sync_enabled is False
    assert settings.rag_sync_interval_minutes == 15
    assert settings.rag_sync_full_reconcile_hours == 24
    assert settings.rag_skip_duplicate_content is True
