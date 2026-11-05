"""Microsoft SSO exercised against a local, signed OIDC provider double."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from authlib.integrations.starlette_client import OAuth
from authlib.jose import JsonWebKey, jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import auth
from app.access import AccessPolicyError, allowed_emails
from app.config import Settings

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
OBJECT_ID = "33333333-3333-3333-3333-333333333333"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"


@pytest.fixture
def microsoft(tmp_path, monkeypatch):
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps([{"name": "Allowed User", "email": "allowed@example.com"}]))
    settings = Settings(
        _env_file=None, auth_mode="microsoft",
        ms_tenant_id=TENANT, ms_client_id=CLIENT_ID, ms_client_secret="test-client-secret",
        session_secret="test-session-secret-with-at-least-32-characters",
        session_cookie_name="test_microsoft", session_https_only=False,
        public_base_url="http://localhost:8000", auth_allowed_users_file=str(allowlist),
        rag_enabled=False, rag_sync_enabled=False, pg_enabled=False,
    )
    for module in ("app.auth", "app.main", "app.routes.auth_routes", "app.config",
                   "app.routes.generate", "app.routes.health"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    key = JsonWebKey.import_key(pem, {"kid": "test-key"})
    context = {
        "settings": settings, "allowlist": allowlist,
        "claims": {}, "profile": {
            "id": OBJECT_ID, "displayName": "Approved User",
            "userPrincipalName": "Allowed@Example.COM", "userType": "Member",
        },
        "graph_status": 200, "graph_calls": 0, "token_calls": 0,
    }

    def provider(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "jwks_uri": ISSUER + "/keys",
                "id_token_signing_alg_values_supported": ["RS256"],
                "response_types_supported": ["code"],
            })
        if request.url.path.endswith("/keys"):
            return httpx.Response(200, json={"keys": [key.as_dict(is_private=False)]})
        if request.url.path.endswith("/token"):
            context["token_calls"] += 1
            form = parse_qs(request.content.decode())
            verifier = form["code_verifier"][0]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            assert challenge == context["challenge"]
            claims = {
                "iss": ISSUER, "aud": CLIENT_ID, "sub": "test-subject", "tid": TENANT,
                "oid": OBJECT_ID, "iat": int(time.time()), "exp": int(time.time()) + 600,
                "nonce": context["nonce"], "email": "irrelevant@example.com", "amr": ["pwd", "mfa"],
            }
            claims.update(context["claims"])
            encoded = jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, key).decode()
            if context.get("bad_signature"):
                pieces = encoded.split(".")
                pieces[2] = ("A" if pieces[2][0] != "A" else "B") + pieces[2][1:]
                encoded = ".".join(pieces)
            return httpx.Response(200, json={
                "access_token": "test-graph-token", "id_token": encoded,
                "token_type": "Bearer", "expires_in": 600,
            })
        if request.url.host == "graph.microsoft.com":
            context["graph_calls"] += 1
            assert request.headers["authorization"] == "Bearer test-graph-token"
            return httpx.Response(context["graph_status"], json=context["profile"])
        raise AssertionError(f"Unexpected test network target: {request.url.host}")

    transport = httpx.MockTransport(provider)
    oauth = OAuth()
    oauth.register(
        "sso", client_id=CLIENT_ID, client_secret="test-client-secret",
        server_metadata_url=ISSUER + "/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid profile email https://graph.microsoft.com/User.Read",
            "code_challenge_method": "S256", "transport": transport,
        },
    )
    monkeypatch.setattr("app.routes.auth_routes.get_oauth", lambda settings=None: oauth)
    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.routes.auth_routes.httpx", SimpleNamespace(
        AsyncClient=lambda **kw: real_client(transport=transport, **kw),
        HTTPError=httpx.HTTPError,
    ))
    from app.main import create_app

    with TestClient(create_app(), base_url=settings.public_base_url, follow_redirects=False) as client:
        context["client"] = client
        yield context
    auth.reset_oauth()
    auth.reset_sessions()


def test_mfa_required(microsoft):
    microsoft["claims"] = {"amr": ["pwd"]}
    assert finish(microsoft).status_code == 403
    assert microsoft["graph_calls"] == 0
    assert microsoft["client"].get("/auth/me").json()["authenticated"] is False


def test_mfa_check_can_be_disabled(microsoft):
    microsoft["settings"].ms_require_mfa = False
    microsoft["claims"] = {"amr": ["pwd"]}
    assert finish(microsoft).status_code == 303


def test_idle_timeout_signs_out(microsoft, monkeypatch):
    assert finish(microsoft).status_code == 303
    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + microsoft["settings"].session_idle_timeout_seconds + 1)
    assert microsoft["client"].get("/auth/me").json()["authenticated"] is False


def test_activity_extends_idle_window(microsoft, monkeypatch):
    assert finish(microsoft).status_code == 303
    idle = microsoft["settings"].session_idle_timeout_seconds
    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + idle - 5)
    assert microsoft["client"].get("/auth/me").json()["authenticated"] is True
    monkeypatch.setattr(auth.time, "time", lambda: now + 2 * idle - 10)
    assert microsoft["client"].get("/auth/me").json()["authenticated"] is True


def test_logout_revokes_stolen_cookie(microsoft):
    assert finish(microsoft).status_code == 303
    client = microsoft["client"]
    stolen = dict(client.cookies)
    csrf = client.get("/auth/me").json()["csrf"]
    assert client.post("/auth/logout", data={"csrf": csrf}).status_code == 303
    for name, value in stolen.items():
        client.cookies.set(name, value)
    assert client.get("/auth/me").json()["authenticated"] is False


def test_admin_can_revoke_user_sessions(microsoft):
    assert finish(microsoft).status_code == 303
    assert auth.revoke_user_sessions(OBJECT_ID) == 1
    assert microsoft["client"].get("/auth/me").json()["authenticated"] is False


GROUP_ID = "11111111-1111-1111-1111-111111111111"


def test_managed_provisioning_by_group(microsoft):
    microsoft["allowlist"].unlink()
    microsoft["settings"].ms_allowed_group_ids = GROUP_ID
    microsoft["claims"] = {"groups": [GROUP_ID.upper()]}
    assert finish(microsoft).status_code == 303
    assert microsoft["client"].get("/auth/me").json()["authenticated"] is True


def test_managed_provisioning_by_app_role(microsoft):
    microsoft["settings"].ms_required_app_roles = "RFP.User"
    microsoft["claims"] = {"roles": ["RFP.User"]}
    assert finish(microsoft).status_code == 303


@pytest.mark.parametrize("claims", [{}, {"groups": ["22222222-2222-2222-2222-222222222222"]},
                                    {"_claim_names": {"groups": "src1"}}])
def test_managed_provisioning_denies_unassigned(microsoft, claims):
    microsoft["settings"].ms_allowed_group_ids = GROUP_ID
    microsoft["claims"] = claims
    assert finish(microsoft).status_code == 403
    assert microsoft["graph_calls"] == 0


@pytest.mark.parametrize("changes", [
    {"session_idle_timeout_seconds": 0}, {"session_idle_timeout_seconds": 10 ** 6},
    {"ms_allowed_group_ids": "not-a-guid"},
])
def test_session_and_group_config_validated(microsoft, changes):
    with pytest.raises(ValueError):
        microsoft["settings"].model_copy(update=changes).validate_microsoft_auth()


def begin(context):
    response = context["client"].get("/auth/microsoft")
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    context["nonce"] = query["nonce"][0]
    context["challenge"] = query["code_challenge"][0]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://localhost:8000/auth/callback"]
    assert query["response_type"] == ["code"]
    return query["state"][0]


def finish(context, state=None):
    if state is None:
        state = begin(context)
    return context["client"].get("/auth/callback", params={"state": state, "code": "test-code"})


def test_branded_login_and_assets(microsoft):
    client = microsoft["client"]
    assert client.get("/").headers["location"] == "/auth/login"
    page = client.get("/auth/login")
    assert page.status_code == 200
    assert "Sign in with Microsoft" in page.text
    assert "/static/css/app.css" in page.text
    assert "/static/logos/Firstsource-logo-white.png" in page.text
    assert 'type="password"' not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert client.get("/static/css/app.css").status_code == 200
    for url in ("/access/d_and_above_dso.json", "/d_and_above_dso.json", "/.env"):
        assert client.get(url).status_code == 404
    assert client.post("/auth/login", data={"username": "allowed@example.com", "password": "any"}).status_code == 404


@pytest.mark.parametrize("method,url", [
    ("post", "/api/generate"), ("post", "/api/jobs"),
    ("get", "/api/jobs/other-job"), ("post", "/api/render/docx"),
])
def test_all_work_routes_require_authentication(microsoft, method, url):
    response = getattr(microsoft["client"], method)(url)
    assert response.status_code == 401
    assert response.headers["x-login-url"] == "/auth/login"


def test_allowed_user_gets_session_without_tokens(microsoft):
    response = finish(microsoft)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    client = microsoft["client"]
    me = client.get("/auth/me").json()
    assert me["authenticated"]
    assert me["user"]["email"] == "allowed@example.com"
    assert me["csrf"]
    assert client.get("/").status_code == 200
    assert client.post("/api/generate", data={"pasted": ""}).status_code == 400
    cookie = client.cookies.get("test_microsoft").split(".")[0]
    payload = base64.b64decode(cookie).decode()
    assert "test-graph-token" not in payload
    assert "id_token" not in payload
    assert "code_verifier" not in payload
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=lax" in response.headers["set-cookie"].lower()


@pytest.mark.parametrize("profile", [
    {"userPrincipalName": "outsider@example.com"},
    {"userPrincipalName": "allowed@example.com.attacker.test"},
    {"userPrincipalName": "outsider@example.com", "mail": "allowed@example.com"},
    {"userType": "Guest"},
    {"id": "different-object"},
    {"userPrincipalName": None},
])
def test_unlisted_and_untrusted_accounts_denied(microsoft, profile):
    microsoft["profile"].update(profile)
    # A token email claiming to be allowed must not override directory identity.
    microsoft["claims"]["email"] = "allowed@example.com"
    response = finish(microsoft)
    assert response.status_code == 403
    assert "Access denied" in response.text
    assert not microsoft["client"].get("/auth/me").json()["authenticated"]
    assert microsoft["client"].post("/api/jobs").status_code == 401


def test_wrong_tenant_denied_before_graph(microsoft):
    microsoft["claims"]["tid"] = "other-tenant"
    assert finish(microsoft).status_code == 403
    assert microsoft["graph_calls"] == 0


@pytest.mark.parametrize("claims", [
    {"nonce": "wrong"}, {"iss": "https://evil.example"}, {"aud": "wrong-app"},
    {"exp": 1}, {"nonce": None},
])
def test_real_oidc_validation_rejects_bad_tokens(microsoft, claims):
    microsoft["claims"].update(claims)
    assert finish(microsoft).status_code == 401
    assert microsoft["graph_calls"] == 0


def test_invalid_signature_rejected(microsoft):
    microsoft["bad_signature"] = True
    assert finish(microsoft).status_code == 401
    assert microsoft["graph_calls"] == 0


def test_wrong_state_and_callback_replay_denied(microsoft):
    state = begin(microsoft)
    assert finish(microsoft, state="wrong").status_code == 401
    assert microsoft["token_calls"] == 0
    state = begin(microsoft)
    assert finish(microsoft, state).status_code == 303
    assert finish(microsoft, state).status_code == 401


def test_cancelled_and_missing_flow_fail_cleanly(microsoft):
    client = microsoft["client"]
    assert client.get("/auth/callback?code=test").status_code == 401
    state = begin(microsoft)
    response = client.get("/auth/callback", params={"state": state, "error": "access_denied"})
    assert response.status_code == 401
    assert "cancelled" in response.text


def test_graph_failure_and_broken_policy_fail_closed(microsoft):
    microsoft["graph_status"] = 503
    assert finish(microsoft).status_code == 503
    assert not microsoft["client"].get("/auth/me").json()["authenticated"]
    microsoft["allowlist"].write_text("invalid json")
    assert microsoft["client"].get("/auth/microsoft").status_code == 503


def test_removed_user_loses_existing_access(microsoft):
    assert finish(microsoft).status_code == 303
    microsoft["allowlist"].write_text('[{"email":"someoneelse@example.com"}]')
    assert microsoft["client"].get("/auth/me").json()["authenticated"] is False
    assert microsoft["client"].post("/api/generate").status_code == 401


def test_missing_policy_revokes_existing_access(microsoft):
    assert finish(microsoft).status_code == 303
    microsoft["allowlist"].unlink()
    assert microsoft["client"].post("/api/jobs").status_code == 503


def test_expired_session_denied(microsoft, monkeypatch):
    assert finish(microsoft).status_code == 303
    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + microsoft["settings"].session_max_age_seconds + 1)
    assert microsoft["client"].get("/auth/me").json()["authenticated"] is False


def test_logout_uses_csrf_protected_post(microsoft):
    assert finish(microsoft).status_code == 303
    client = microsoft["client"]
    csrf = client.get("/auth/me").json()["csrf"]
    assert client.get("/auth/logout").status_code == 405
    assert client.post("/auth/logout", data={"csrf": "wrong"}).status_code == 403
    assert client.post(
        "/auth/logout", data={"csrf": csrf}, headers={"origin": "https://evil.example"}
    ).status_code == 403
    response = client.post("/auth/logout", data={"csrf": csrf})
    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).hostname == "login.microsoftonline.com"
    assert not client.get("/auth/me").json()["authenticated"]


@pytest.mark.parametrize("content", ["[]", "{}", "bad json", '[{"name":"No email"}]',
                                     '[{"email":"bad"}]',
                                     '[{"email":"A@example.com"},{"email":"a@example.com"}]'])
def test_invalid_allowlist_refuses_startup(microsoft, content):
    microsoft["allowlist"].write_text(content)
    from app.main import create_app
    with pytest.raises(AccessPolicyError):
        create_app()


def test_allowlist_normalizes_exact_addresses(microsoft):
    microsoft["allowlist"].write_text('[{"email":"  ALLOWED@EXAMPLE.COM  "}]')
    assert allowed_emails(microsoft["settings"]) == {"allowed@example.com"}


def test_legacy_cookie_cannot_bypass_microsoft(microsoft):
    from itsdangerous import TimestampSigner
    payload = base64.b64encode(json.dumps({"user": {"email": "allowed@example.com", "name": "Old user"}}).encode())
    forged_legacy = TimestampSigner(microsoft["settings"].session_secret).sign(payload).decode()
    microsoft["client"].cookies.set("test_microsoft", forged_legacy)
    assert microsoft["client"].get("/auth/me").json()["authenticated"] is False


@pytest.mark.parametrize("changes", [
    {"ms_tenant_id": "common"}, {"ms_client_id": ""}, {"ms_client_secret": ""},
    {"session_secret": "short"}, {"public_base_url": "http://example.com"},
    {"public_base_url": "https://example.com", "session_https_only": False},
    {"session_max_age_seconds": 0},
])
def test_microsoft_config_fails_closed(microsoft, changes):
    settings = microsoft["settings"].model_copy(update=changes)
    with pytest.raises(ValueError):
        settings.validate_microsoft_auth()
