"""Startup validation for the sync settings."""

from __future__ import annotations

import logging

import pytest

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


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False)])
def test_allowlist_switch_loaded_from_environment(monkeypatch, value, expected):
    monkeypatch.setenv("AUTH_USE_ALLOWLIST", value)
    assert Settings(_env_file=None).auth_use_allowlist is expected


def test_application_file_logging_excludes_access_logs_and_avoids_duplicates(tmp_path):
    from app.main import configure_logging

    path = tmp_path / "logs" / "app.log"
    try:
        configure_logging("INFO", str(path))
        configure_logging("INFO", str(path))
        logging.getLogger("app.routes.auth_routes").info("Login event for logging test")
        logging.getLogger("uvicorn.access").info("sensitive callback URL")
        logging.getLogger("httpx").info("sensitive external URL")
        content = path.read_text(encoding="utf-8")
        assert content.count("Login event for logging test") == 1
        assert "sensitive callback URL" not in content
        assert "sensitive external URL" not in content
    finally:
        configure_logging("INFO")
