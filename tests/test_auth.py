"""Auth behaviour in both modes."""

from __future__ import annotations

import pytest

from app import auth
from app.config import Settings


def test_disabled_mode_allows_anonymous_use(client):
    assert client.post("/api/generate", data={"pasted": ""}).status_code == 400  # not 401
    body = client.get("/auth/me").json()
    assert body["authenticated"] is True


def test_email_domain_allow_list():
    settings = Settings(oidc_allowed_email_domains="firstsource.com, example.org")
    assert auth.email_is_allowed("moumita.patil@firstsource.com", settings)
    assert auth.email_is_allowed("someone@example.org", settings)
    assert not auth.email_is_allowed("outsider@gmail.com", settings)


def test_empty_allow_list_permits_any_authenticated_user():
    settings = Settings(oidc_allowed_email_domains="")
    assert auth.email_is_allowed("anyone@anywhere.com", settings)


@pytest.mark.parametrize(
    "claims,expected_email",
    [
        ({"email": "a@firstsource.com", "name": "A Person"}, "a@firstsource.com"),
        ({"preferred_username": "b@firstsource.com"}, "b@firstsource.com"),
        ({"upn": "c@firstsource.com"}, "c@firstsource.com"),
        ({}, ""),
    ],
)
def test_user_claims_mapping_handles_provider_variations(claims, expected_email):
    user = auth.user_from_claims(claims)
    assert user["email"] == expected_email
    assert user["name"]


def test_oidc_mode_requires_a_session(monkeypatch):
    """With SSO on and no session, API calls must be refused."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_DISCOVERY_URL", "https://login.example.com/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "client-id")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "client-secret")

    from app.config import get_settings

    get_settings.cache_clear()
    auth.reset_oauth()
    try:
        from app.main import create_app

        with TestClient(create_app()) as oidc_client:
            response = oidc_client.post("/api/generate", data={"pasted": "Q1?"})
            assert response.status_code == 401
            assert oidc_client.get("/auth/me").json()["authenticated"] is False
    finally:
        get_settings.cache_clear()
        auth.reset_oauth()


def test_production_config_warnings():
    settings = Settings(
        environment="production",
        azure_openai_api_key="key",
        azure_openai_endpoint="https://test.openai.azure.com/",
        azure_openai_deployment_name="gpt-4o",
        auth_mode="disabled",
        session_secret="change-me-in-production",
        session_https_only=False,
    )
    problems = " ".join(settings.startup_problems())
    assert "SESSION_SECRET" in problems
    assert "HTTPS" in problems
    assert "unauthenticated" in problems


def test_missing_api_key_is_flagged_at_startup():
    problems = " ".join(Settings(azure_openai_api_key="").startup_problems())
    assert "AZURE_OPENAI_API_KEY" in problems
