"""Authentication.

Two modes, chosen with the AUTH_MODE environment variable:

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
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request, status

from .config import Settings, get_settings

log = logging.getLogger(__name__)

SESSION_USER_KEY = "user"

_oauth: Any = None


def get_oauth(settings: Settings | None = None) -> Any:
    """Lazily build the Authlib OAuth registry for the configured provider."""
    global _oauth
    settings = settings or get_settings()
    if settings.auth_mode != "oidc":
        return None
    if _oauth is not None:
        return _oauth
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="sso",
        server_metadata_url=settings.oidc_discovery_url,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        client_kwargs={"scope": settings.oidc_scopes},
    )
    _oauth = oauth
    return _oauth


def reset_oauth() -> None:
    """Test hook - drop the cached registry."""
    global _oauth
    _oauth = None


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
