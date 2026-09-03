"""OIDC login / logout routes.

Only mounted when AUTH_MODE=oidc. The provider is configured entirely through
environment variables - see README section "Single sign-on".
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import SESSION_USER_KEY, current_user, email_is_allowed, get_oauth, user_from_claims
from ..config import get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login")
async def login(request: Request):
    settings = get_settings()
    if settings.auth_mode == "disabled":
        return RedirectResponse("/")
    oauth = get_oauth(settings)
    if oauth is None:  # pragma: no cover - guarded by auth_mode above
        raise HTTPException(status_code=500, detail="SSO is not configured.")
    return await oauth.sso.authorize_redirect(request, settings.oidc_redirect_uri)


@router.get("/callback")
async def callback(request: Request):
    settings = get_settings()
    if settings.auth_mode == "disabled":
        # Reachable without auth; must not surface a 500.
        return RedirectResponse("/")
    oauth = get_oauth(settings)
    if oauth is None:
        raise HTTPException(status_code=500, detail="SSO is not configured.")
    try:
        token = await oauth.sso.authorize_access_token(request)
    except Exception as exc:  # noqa: BLE001 - surface a readable error, log the detail
        log.warning("OIDC callback failed: %s", exc)
        return HTMLResponse(
            "<h1>Sign-in failed</h1><p>Could not complete single sign-on. "
            '<a href="/auth/login">Try again</a>.</p>',
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    claims = token.get("userinfo") or {}
    if not claims:
        try:
            claims = await oauth.sso.userinfo(token=token)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not fetch userinfo: %s", exc)
            claims = {}

    user = user_from_claims(dict(claims))
    if not user["email"]:
        return HTMLResponse(
            "<h1>Sign-in failed</h1><p>The identity provider did not return an "
            "email address. Ask IT to add the <code>email</code> claim to the app "
            "registration.</p>",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    if not email_is_allowed(user["email"], settings):
        log.info("Rejected login for %s (domain not allowed).", user["email"])
        return HTMLResponse(
            "<h1>Access denied</h1><p>Your account is not permitted to use this "
            "application.</p>",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    request.session[SESSION_USER_KEY] = user
    log.info("Signed in: %s", user["email"])
    return RedirectResponse("/")


@router.get("/logout")
def logout(request: Request):
    if "session" in request.scope:
        request.session.clear()
    return RedirectResponse("/")


@router.get("/me", response_model=None)
def me(request: Request) -> dict:
    user = current_user(request)
    return {"authenticated": user is not None, "user": user}
