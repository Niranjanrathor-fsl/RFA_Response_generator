"""Username/password sign-in.

A third AUTH_MODE for deployments with no identity provider available. No
database: users live in a JSON file on the server, passwords stored only as
salted scrypt hashes.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import users as users_module
from app.config import Settings
from app.main import create_app


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        azure_openai_api_key="k", azure_openai_endpoint="https://e/",
        azure_openai_deployment_name="gpt-5.4",
        auth_mode="password", session_secret="test-secret",
        auth_users_file=str(tmp_path / "users.json"),
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _clear_throttle():
    """The failed-login counter is module-level (one process, one throttle), so
    a lockout in one test would otherwise lock out the next."""
    from app.routes.auth_routes import reset_login_throttle

    reset_login_throttle()
    yield
    reset_login_throttle()


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    users_module.set_password("rfp@firstsource.com", "correct horse battery", settings)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    for module in ("app.auth", "app.main", "app.routes.auth_routes",
                   "app.routes.generate", "app.routes.health"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings, raising=False)
    with TestClient(create_app()) as client:
        yield client


# ------------------------------------------------------------- storage
def test_password_is_never_stored_in_plaintext(tmp_path):
    settings = _settings(tmp_path)
    users_module.set_password("a@b.com", "hunter2-long-enough", settings)
    raw = (tmp_path / "users.json").read_text(encoding="utf-8")
    assert "hunter2-long-enough" not in raw
    record = json.loads(raw)["a@b.com"]
    assert record["algorithm"] == "scrypt"
    assert record["salt"] and record["hash"]


def test_each_user_gets_a_distinct_salt(tmp_path):
    settings = _settings(tmp_path)
    users_module.set_password("a@b.com", "same-password-here", settings)
    users_module.set_password("c@d.com", "same-password-here", settings)
    stored = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))
    assert stored["a@b.com"]["salt"] != stored["c@d.com"]["salt"]
    assert stored["a@b.com"]["hash"] != stored["c@d.com"]["hash"]


def test_verify_accepts_the_right_password_and_rejects_others(tmp_path):
    settings = _settings(tmp_path)
    users_module.set_password("a@b.com", "hunter2-long-enough", settings)
    assert users_module.verify("a@b.com", "hunter2-long-enough", settings) is True
    assert users_module.verify("a@b.com", "hunter3-long-enough", settings) is False
    assert users_module.verify("nobody@b.com", "hunter2-long-enough", settings) is False


def test_usernames_are_case_insensitive(tmp_path):
    settings = _settings(tmp_path)
    users_module.set_password("A@B.com", "hunter2-long-enough", settings)
    assert users_module.verify("a@b.COM", "hunter2-long-enough", settings) is True


# --------------------------------------------------------------- routes
def test_api_is_refused_until_signed_in(app_client):
    response = app_client.post("/api/generate", data={"pasted": "Q1. Anything?"})
    assert response.status_code == 401


def test_login_page_is_served(app_client):
    response = app_client.get("/auth/login")
    assert response.status_code == 200
    assert "password" in response.text.lower()


def test_correct_credentials_sign_the_user_in(app_client):
    response = app_client.post(
        "/auth/login",
        data={"username": "rfp@firstsource.com", "password": "correct horse battery"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    me = app_client.get("/auth/me").json()
    assert me["authenticated"] is True
    assert me["user"]["email"] == "rfp@firstsource.com"


def test_wrong_password_is_refused(app_client):
    response = app_client.post(
        "/auth/login",
        data={"username": "rfp@firstsource.com", "password": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert app_client.get("/auth/me").json()["authenticated"] is False


def test_unknown_user_gives_the_same_message_as_a_wrong_password(app_client):
    """Otherwise the form becomes a way to discover who has an account."""
    wrong_pw = app_client.post(
        "/auth/login",
        data={"username": "rfp@firstsource.com", "password": "wrong"},
        follow_redirects=False,
    )
    no_user = app_client.post(
        "/auth/login",
        data={"username": "ghost@firstsource.com", "password": "wrong"},
        follow_redirects=False,
    )
    assert wrong_pw.status_code == no_user.status_code == 401
    assert "incorrect" in no_user.text.lower()
    assert "ghost" not in no_user.text.lower()


def test_repeated_failures_are_locked_out(app_client):
    """A password form on the public internet gets guessed at."""
    for _ in range(5):
        app_client.post(
            "/auth/login",
            data={"username": "rfp@firstsource.com", "password": "wrong"},
            follow_redirects=False,
        )
    # Even the CORRECT password must now be refused.
    blocked = app_client.post(
        "/auth/login",
        data={"username": "rfp@firstsource.com", "password": "correct horse battery"},
        follow_redirects=False,
    )
    assert blocked.status_code == 429
    assert app_client.get("/auth/me").json()["authenticated"] is False


def test_logout_clears_the_session(app_client):
    app_client.post(
        "/auth/login",
        data={"username": "rfp@firstsource.com", "password": "correct horse battery"},
        follow_redirects=False,
    )
    assert app_client.get("/auth/me").json()["authenticated"] is True
    app_client.get("/auth/logout", follow_redirects=False)
    assert app_client.get("/auth/me").json()["authenticated"] is False


def test_short_passwords_are_rejected(tmp_path):
    """A password form reachable from the internet needs a real password."""
    settings = _settings(tmp_path)
    with pytest.raises(users_module.UserError, match="at least"):
        users_module.set_password("a@b.com", "short", settings)


def test_startup_flags_password_mode_with_no_users(tmp_path):
    settings = _settings(tmp_path, auth_users_file=str(tmp_path / "missing.json"))
    problems = settings.startup_problems()
    assert any("no users" in p.lower() for p in problems)
