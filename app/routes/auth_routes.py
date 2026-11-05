"""Sign-in / sign-out routes.

Serves whichever mode AUTH_MODE selects:

* ``password`` - a plain username/password form checked against the JSON user
  store in app/users.py. Repeated failures are locked out per username.
* ``oidc``     - OpenID Connect, configured entirely through environment
  variables. See README section "Single sign-on".
"""

from __future__ import annotations

import logging
import secrets
import time

from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple
from urllib.parse import urlencode

import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.jose.errors import JoseError
from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..auth import (
    SESSION_USER_KEY, current_user, email_is_allowed, get_oauth, register_session,
    revoke_session, user_from_claims,
)
from ..access import AccessPolicyError, allowed_emails
from ..config import BASE_DIR, get_settings
from ..users import verify as verify_password

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def microsoft_login_page(request: Request, message: str = "", status_code: int = 200):
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"message": message}, status_code=status_code,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


class MfaRequired(Exception):
    """The sign-in did not satisfy multi-factor authentication."""


def mfa_satisfied(claims: dict) -> bool:
    amr = claims.get("amr")
    return isinstance(amr, list) and "mfa" in amr


def managed_access_granted(claims: dict, settings) -> bool:
    """Entra-managed provisioning: group membership or app-role assignment."""
    if "groups" in (claims.get("_claim_names") or {}):
        log.warning("Group claim overage; cannot evaluate membership. Use app roles or fewer groups.")
        return False
    groups = {g.lower() for g in claims.get("groups") or [] if isinstance(g, str)}
    roles = {r for r in claims.get("roles") or [] if isinstance(r, str)}
    return bool(groups & set(settings.ms_group_ids)) or bool(roles & set(settings.ms_app_roles))


async def microsoft_user(token: dict, settings) -> dict[str, str] | None:
    claims = token.get("userinfo") or {}
    if claims.get("tid") != settings.ms_tenant_id or not claims.get("oid") or not token.get("access_token"):
        return None
    if settings.ms_require_mfa and not mfa_satisfied(claims):
        raise MfaRequired()
    if settings.managed_provisioning and not managed_access_granted(claims, settings):
        return None
    # Do not authorize using mutable email/UPN claims in the ID token. Ask Graph
    # for the signed-in directory account and bind its ID to the verified token.
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            params={"$select": "id,displayName,userPrincipalName,userType"},
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
        response.raise_for_status()
        profile = response.json()
    if not isinstance(profile, dict) or profile.get("id") != claims["oid"] or profile.get("userType") != "Member":
        return None
    upn = profile.get("userPrincipalName")
    if not isinstance(upn, str):
        return None
    if not settings.managed_provisioning and upn.strip().casefold() not in allowed_emails(settings):
        return None
    return {
        "email": upn.strip().casefold(),
        "name": profile.get("displayName") or "Firstsource user",
    }


# username -> (consecutive failures, when the lockout expires). In-process only:
# a single App Service instance, and a restart clearing it is an acceptable
# trade for not adding a store just to throttle a form.
_failures: Dict[str, Tuple[int, datetime]] = {}


def _locked_out(username: str, settings) -> bool:
    count, until = _failures.get(username, (0, datetime.min.replace(tzinfo=timezone.utc)))
    if count < settings.auth_max_failed_logins:
        return False
    if datetime.now(timezone.utc) >= until:
        _failures.pop(username, None)  # cool-off served
        return False
    return True


def _record_failure(username: str, settings) -> None:
    count, _ = _failures.get(username, (0, datetime.min.replace(tzinfo=timezone.utc)))
    until = datetime.now(timezone.utc) + timedelta(minutes=settings.auth_lockout_minutes)
    _failures[username] = (count + 1, until)


def reset_login_throttle() -> None:
    """Test hook - forget every recorded failure."""
    _failures.clear()


def _login_page(message: str = "", status_code: int = 200) -> HTMLResponse:
    note = f'<p class="err">{message}</p>' if message else ""
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in</title><style>
:root{{color-scheme:light dark}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;
font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f6f9;color:#16181d}}
form{{background:#fff;padding:2rem;border-radius:12px;width:min(92vw,360px);
box-shadow:0 10px 30px rgba(16,24,40,.10)}}
h1{{font-size:1.25rem;margin:0 0 1.25rem}}
label{{display:block;font-size:.85rem;font-weight:600;margin:0 0 .35rem}}
input{{width:100%;padding:.6rem .7rem;margin:0 0 1rem;border:1px solid #d3d8e0;
border-radius:8px;font:inherit;box-sizing:border-box;background:#fff;color:inherit}}
button{{width:100%;padding:.65rem;border:0;border-radius:8px;background:#16181d;
color:#fff;font:inherit;font-weight:600;cursor:pointer}}
.err{{background:#fdecec;color:#96222b;padding:.6rem .75rem;border-radius:8px;
margin:0 0 1rem;font-size:.9rem}}
@media(prefers-color-scheme:dark){{body{{background:#14161a;color:#e8eaed}}
form{{background:#1d2025}}input{{background:#14161a;border-color:#333}}
button{{background:#e8eaed;color:#14161a}}}}
</style></head><body>
<form method="post" action="/auth/login">
<h1>Firstsource RFP Response Generator</h1>
{note}
<label for="username">Username</label>
<input id="username" name="username" type="text" autocomplete="username" autofocus required>
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button>
</form></body></html>""",
        status_code=status_code,
    )


@router.post("/login")
def password_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    settings = get_settings()
    if settings.auth_mode != "password":
        raise HTTPException(status_code=404, detail="Not found.")

    name = (username or "").strip().lower()
    if _locked_out(name, settings):
        log.warning("Sign-in blocked for %s (too many failed attempts).", name)
        return _login_page(
            f"Too many failed attempts. Try again in {settings.auth_lockout_minutes} minutes.",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    if not verify_password(name, password, settings):
        _record_failure(name, settings)
        log.info("Failed sign-in for %s.", name or "(blank)")
        # Deliberately identical whether or not the account exists, so the form
        # cannot be used to discover who has one.
        return _login_page("Username or password is incorrect.", status.HTTP_401_UNAUTHORIZED)

    _failures.pop(name, None)
    request.session[SESSION_USER_KEY] = {
        "email": name,
        "name": name.split("@")[0] or "Firstsource user",
    }
    log.info("Signed in: %s", name)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/login")
async def login(request: Request):
    settings = get_settings()
    if settings.auth_mode == "disabled":
        return RedirectResponse("/")
    if settings.auth_mode == "password":
        return _login_page()
    if settings.auth_mode == "microsoft":
        if current_user(request):
            return RedirectResponse("/", status_code=303)
        return microsoft_login_page(request)
    oauth = get_oauth(settings)
    if oauth is None:  # pragma: no cover - guarded by auth_mode above
        raise HTTPException(status_code=500, detail="SSO is not configured.")
    return await oauth.sso.authorize_redirect(request, settings.oidc_redirect_uri)


@router.get("/microsoft")
async def microsoft_login(request: Request):
    settings = get_settings()
    if settings.auth_mode != "microsoft":
        raise HTTPException(status_code=404, detail="Not found.")
    request.session.clear()
    try:
        if not settings.managed_provisioning:
            allowed_emails(settings)
        oauth = get_oauth(settings)
        return await oauth.sso.authorize_redirect(
            request, settings.oidc_redirect_uri, response_mode="query", prompt="select_account"
        )
    except AccessPolicyError:
        log.error("Microsoft access policy is unavailable; refusing sign-in.")
    except (OAuthError, httpx.HTTPError, ValueError) as exc:
        log.warning("Microsoft sign-in initialization failed (%s).", type(exc).__name__)
    return microsoft_login_page(request, "Sign-in is unavailable. Please contact your administrator.", 503)


@router.get("/callback")
async def callback(request: Request):
    settings = get_settings()
    if settings.auth_mode == "disabled":
        # Reachable without auth; must not surface a 500.
        return RedirectResponse("/")
    oauth = get_oauth(settings)
    if oauth is None:
        raise HTTPException(status_code=404, detail="Not found.")
    try:
        token = await oauth.sso.authorize_access_token(request)
    except (OAuthError, JoseError, httpx.HTTPError, ValueError) as exc:
        request.session.clear()
        log.warning("OIDC callback validation failed (%s).", type(exc).__name__)
        if settings.auth_mode == "microsoft":
            return microsoft_login_page(
                request, "Sign-in was cancelled, expired, or could not be verified. Please try again.", 401
            )
        return HTMLResponse(
            "<h1>Sign-in failed</h1><p>Could not complete single sign-on. "
            '<a href="/auth/login">Try again</a>.</p>',
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if settings.auth_mode == "microsoft":
        request.session.clear()
        try:
            user = await microsoft_user(token, settings)
        except MfaRequired:
            log.warning("Microsoft sign-in refused: multi-factor authentication not satisfied.")
            return microsoft_login_page(
                request, "Multi-factor authentication is required. Complete MFA with your work account and try again.", 403
            )
        except (httpx.HTTPError, ValueError, AccessPolicyError) as exc:
            log.warning("Microsoft directory/access verification failed (%s).", type(exc).__name__)
            return microsoft_login_page(
                request, "Unable to verify your access. Please contact your administrator.", 503
            )
        if user is None:
            log.warning("Microsoft sign-in refused by the tenant/user access policy.")
            return microsoft_login_page(
                request, "Access denied. This application is available only to approved users. "
                "Choose an approved work account or contact your administrator.", 403
            )
        request.session.update({
            SESSION_USER_KEY: user,
            "provider": "microsoft",
            "tenant_id": settings.ms_tenant_id,
            "object_id": token["userinfo"]["oid"],
            "sid": register_session(token["userinfo"]["oid"], settings),
            "authenticated_at": time.time(),
            "csrf": secrets.token_urlsafe(32),
        })
        return RedirectResponse("/", status_code=303)

    claims = token.get("userinfo") or {}
    if not claims:
        try:
            claims = await oauth.sso.userinfo(token=token)
        except (OAuthError, httpx.HTTPError, ValueError) as exc:
            log.warning("Could not fetch userinfo (%s).", type(exc).__name__)
            raise HTTPException(status_code=503, detail="The identity provider is unavailable.") from None

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
    if get_settings().auth_mode == "microsoft":
        raise HTTPException(status_code=405, detail="Use the sign-out button.", headers={"Allow": "POST"})
    if "session" in request.scope:
        request.session.clear()
    return RedirectResponse("/")


@router.post("/logout")
def microsoft_logout(request: Request, csrf: str = Form(...)):
    settings = get_settings()
    expected = request.session.get("csrf")
    if (
        settings.auth_mode != "microsoft" or not isinstance(expected, str)
        or not secrets.compare_digest(csrf.encode(), expected.encode())
        or request.headers.get("origin", settings.public_base_url.rstrip("/")) != settings.public_base_url.rstrip("/")
    ):
        log.warning("Rejected sign-out request with invalid CSRF protection.")
        raise HTTPException(status_code=403, detail="Invalid sign-out request. Reload and try again.")
    revoke_session(request.session.get("sid"))
    request.session.clear()
    return RedirectResponse(
        settings.microsoft_authority + "/oauth2/v2.0/logout?"
        + urlencode({"post_logout_redirect_uri": settings.public_base_url.rstrip("/") + "/"}),
        status_code=303,
    )


@router.get("/me", response_model=None)
def me(request: Request) -> dict:
    user = current_user(request)
    return {
        "authenticated": user is not None, "user": user,
        "csrf": request.session.get("csrf") if user else None,
    }
