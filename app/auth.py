"""Authentication.

Modes, chosen with the AUTH_MODE environment variable:

* ``microsoft`` - tenant-specific Microsoft SSO with an exact user allowlist.
* ``disabled`` - no login. Only appropriate when the app is reachable solely from
  the internal network or VPN.
* ``password`` - username and password, checked against a JSON file of salted
  scrypt hashes on the server (see app/users.py). For deployments where no
  identity provider is available.
* ``oidc`` - OpenID Connect. Wired for Azure AD (Microsoft Entra ID) but works with
  any compliant provider. Your engineering team supplies the discovery URL, client
  ID and client secret; no code changes required.

The session is a signed, HTTP-only cookie holding only the user's email and name.
No user database, no server-side session store.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request, status

from .config import Settings, get_settings
from .access import AccessPolicyError, allowed_emails

log = logging.getLogger(__name__)

SESSION_USER_KEY = "user"

# Server-side session registry: sid -> {"oid", "created", "last_seen"}. The cookie is
# only valid while its sid is registered, which gives idle timeout and real
# revocation (logout, admin revoke). In-process: a restart signs everyone out.
_sessions: Dict[str, Dict[str, Any]] = {}


def register_session(object_id: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    now = time.time()
    for sid in [s for s, v in _sessions.items() if now - v["created"] >= settings.session_max_age_seconds]:
        _sessions.pop(sid, None)
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = {"oid": object_id, "created": now, "last_seen": now}
    return sid


def revoke_session(sid: Any) -> None:
    if isinstance(sid, str):
        _sessions.pop(sid, None)


def revoke_user_sessions(object_id: str) -> int:
    """Terminate every live session of one directory account (e.g. a leaver)."""
    sids = [s for s, v in _sessions.items() if v["oid"] == object_id]
    for sid in sids:
        _sessions.pop(sid, None)
    return len(sids)


def reset_sessions() -> None:
    """Test hook - drop all server-side sessions."""
    _sessions.clear()


def _live_session(session: Any, settings: Settings) -> bool:
    entry = _sessions.get(session.get("sid")) if isinstance(session.get("sid"), str) else None
    now = time.time()
    if (
        entry is None
        or entry["oid"] != session.get("object_id")
        or now - entry["created"] >= settings.session_max_age_seconds
        or now - entry["last_seen"] >= settings.session_idle_timeout_seconds
    ):
        return False
    entry["last_seen"] = now
    return True

_oauth: Any = None
_oauth_config: tuple | None = None


def get_oauth(settings: Settings | None = None) -> Any:
    """Lazily build the Authlib OAuth registry for the configured provider."""
    global _oauth, _oauth_config
    settings = settings or get_settings()
    if settings.auth_mode not in {"oidc", "microsoft"}:
        return None
    microsoft = settings.auth_mode == "microsoft"
    metadata_url = (
        settings.microsoft_authority + "/v2.0/.well-known/openid-configuration"
        if microsoft else settings.oidc_discovery_url
    )
    client_id = settings.ms_client_id if microsoft else settings.oidc_client_id
    client_secret = settings.ms_client_secret if microsoft else settings.oidc_client_secret
    scope = "openid profile email https://graph.microsoft.com/User.Read" if microsoft else settings.oidc_scopes
    config = (metadata_url, client_id, client_secret, scope)
    if _oauth is not None and _oauth_config == config:
        return _oauth
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="sso",
        server_metadata_url=metadata_url,
        client_id=client_id,
        client_secret=client_secret,
        client_kwargs={"scope": scope, "code_challenge_method": "S256", "timeout": 15},
    )
    _oauth = oauth
    _oauth_config = config
    return _oauth


def reset_oauth() -> None:
    """Test hook - drop the cached registry."""
    global _oauth, _oauth_config
    _oauth = None
    _oauth_config = None


def email_is_allowed(email: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    domains = settings.allowed_email_domains
    if not domains:
        return True
    email = (email or "").lower()
    return any(email.endswith("@" + domain) for domain in domains)


def user_from_claims(claims: Dict[str, Any]) -> Dict[str, str]:
    email = (
        claims.get("email")
        or claims.get("preferred_username")
        or claims.get("upn")
        or ""
    )
    name = claims.get("name") or email.split("@")[0] or "Firstsource user"
    return {"email": email, "name": name}


def current_user(request: Request) -> Optional[Dict[str, str]]:
    """The logged-in user, or a synthetic one when auth is disabled."""
    settings = get_settings()
    if settings.auth_mode == "disabled":
        return {"email": "anonymous@localhost", "name": "Firstsource user"}
    # Starlette's request.session property raises AssertionError when
    # SessionMiddleware is absent, so probe the scope instead of the attribute.
    session = request.scope.get("session")
    if not session:
        return None
    user = session.get(SESSION_USER_KEY)
    if settings.auth_mode == "microsoft" and isinstance(user, dict):
        issued = session.get("authenticated_at")
        if (
            session.get("provider") != "microsoft"
            or session.get("tenant_id") != settings.ms_tenant_id
            or not session.get("object_id")
            or not isinstance(issued, (float, int))
            or not 0 <= time.time() - issued < settings.session_max_age_seconds
        ):
            session.clear()
            return None
        if not _live_session(session, settings):
            revoke_session(session.get("sid"))
            session.clear()
            return None
        if settings.managed_provisioning:
            return user
        try:
            permitted = user.get("email", "").casefold() in allowed_emails(settings)
        except AccessPolicyError:
            log.error("Microsoft access policy is unavailable; refusing access.")
            session.clear()
            raise HTTPException(status_code=503, detail="Access verification is unavailable. Contact your administrator.")
        if not permitted:
            log.warning("Microsoft session rejected: account removed from the allowlist.")
            session.clear()
            return None
    return user if isinstance(user, dict) else None


def require_user(request: Request) -> Dict[str, str]:
    """FastAPI dependency guarding every API route."""
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not signed in.",
            headers={"X-Login-Url": "/auth/login"},
        )
    return user


UserDep = Depends(require_user)
