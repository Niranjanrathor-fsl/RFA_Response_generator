"""Microsoft Graph transport: authentication, throttling, pagination, download.

Split out of sources.py so that module holds only source semantics. Fixes two
defects in the original inline implementation:

  - The access token was cached once and never refreshed. Any ingestion running
    past the token lifetime (~1 hour) failed with 401 - a certainty for a corpus
    of this size.
  - There was no 429/503 handling. SharePoint throttles per-tenant, and Microsoft's
    scale guidance is explicit that ignoring Retry-After gets an application blocked
    for abusive calling patterns.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, Optional
from urllib.parse import quote

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Refresh this far before actual expiry so a long request cannot straddle it.
_TOKEN_REFRESH_MARGIN = timedelta(seconds=300)
_MAX_ATTEMPTS = 5
_BASE_BACKOFF_SECONDS = 2.0


class GraphError(RuntimeError):
    """Microsoft Graph could not be reached, authenticated to, or satisfied."""


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    """Honour the server's Retry-After when present; otherwise back off exponentially."""
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass  # Retry-After may be an HTTP-date; fall through to backoff.
    return _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))


class GraphClient:
    """Authenticated, throttle-aware Microsoft Graph client (app-only auth)."""

    def __init__(self, settings: Settings, http: Optional[httpx.Client] = None) -> None:
        self.settings = settings
        self._http = http or httpx.Client(timeout=180.0)
        self._token: Optional[str] = None
        self._token_expires_at = datetime.min.replace(tzinfo=timezone.utc)

    # ------------------------------------------------------------------ auth
    def _acquire_token(self) -> None:
        import msal

        s = self.settings
        app = msal.ConfidentialClientApplication(
            client_id=s.ms_client_id,
            client_credential=s.ms_client_secret,
            authority=f"https://login.microsoftonline.com/{s.ms_tenant_id}",
        )
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise GraphError(
                f"Could not authenticate to Microsoft Graph: "
                f"{result.get('error')}: {result.get('error_description')}"
            )
        self._token = result["access_token"]
        self._token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(result.get("expires_in", 3600))
        )
        log.info("Acquired a Microsoft Graph token, valid until %s.", self._token_expires_at)

    def headers(self) -> Dict[str, str]:
        if self._token is None or datetime.now(timezone.utc) >= self._token_expires_at - _TOKEN_REFRESH_MARGIN:
            self._acquire_token()
        return {"Authorization": f"Bearer {self._token}"}

    # --------------------------------------------------------------- request
    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_error = ""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = self._http.request(method, url, headers=self.headers(), **kwargs)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == _MAX_ATTEMPTS:
                    raise GraphError(f"Microsoft Graph was unreachable: {last_error}") from exc
                time.sleep(_BASE_BACKOFF_SECONDS * attempt)
                continue

            if response.status_code in (429, 503):
                delay = _retry_after_seconds(response, attempt)
                last_error = f"HTTP {response.status_code}"
                if attempt == _MAX_ATTEMPTS:
                    raise GraphError(
                        f"Microsoft Graph kept us throttled after {_MAX_ATTEMPTS} attempts ({last_error})."
                    )
                log.warning(
                    "Graph returned %s; waiting %.0fs before retry %d/%d.",
                    response.status_code, delay, attempt + 1, _MAX_ATTEMPTS,
                )
                time.sleep(delay)
                continue

            if response.status_code == 401 and attempt < _MAX_ATTEMPTS:
                log.warning("Graph returned 401; discarding the token and retrying.")
                self._token = None
                continue

            return response

        raise GraphError(f"Microsoft Graph request failed: {last_error}")

    def get_json(self, url: str, *, params: Optional[dict] = None) -> dict:
        response = self._request("GET", url, params=params)
        if response.status_code >= 400:
            raise GraphError(f"Graph GET {url} returned {response.status_code}: {response.text[:400]}")
        return response.json()

    def paginate(self, url: str) -> Iterator[dict]:
        """Yield each page body, following @odata.nextLink until exhausted."""
        while url:
            body = self.get_json(url)
            yield body
            url = body.get("@odata.nextLink", "")

    def download(self, download_url: str) -> bytes:
        """Fetch file content from a Graph pre-signed download URL.

        Deliberately sends NO Authorization header: the URL is already signed and
        adding one can cause the request to be rejected.
        """
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            response = self._http.get(download_url, follow_redirects=True)
            if response.status_code in (429, 503):
                if attempt == _MAX_ATTEMPTS:
                    raise GraphError(f"Download stayed throttled after {_MAX_ATTEMPTS} attempts.")
                time.sleep(_retry_after_seconds(response, attempt))
                continue
            if response.status_code >= 400:
                raise GraphError(f"Download returned {response.status_code}.")
            return response.content
        raise GraphError("Download failed.")

    def download_item(self, drive_id: str, item_id: str) -> bytes:
        """Fetch file content through the authenticated /content endpoint.

        This is the path the delta pipeline uses. Delta responses do NOT carry
        @microsoft.graph.downloadUrl - only the /children collection does - so a
        pre-signed URL is a fast path we take when offered, never something to
        depend on.
        """
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
        response = self._request("GET", url, follow_redirects=True)
        if response.status_code >= 400:
            raise GraphError(f"Content download for item {item_id} returned {response.status_code}.")
        return response.content

    # -------------------------------------------------------------- resolve
    def resolve_site_id(self) -> str:
        url = self.settings.sharepoint_site_url.rstrip("/")
        hostname, _, site_path = url.partition("://")[2].partition("/")
        return self.get_json(f"{GRAPH_BASE}/sites/{hostname}:/{site_path}")["id"]

    def resolve_drive_id(self, site_id: str) -> str:
        body = self.get_json(f"{GRAPH_BASE}/sites/{site_id}/drives")
        for drive in body.get("value", []):
            if drive.get("name") == self.settings.sharepoint_library:
                return drive["id"]
        raise GraphError(
            f"Library '{self.settings.sharepoint_library}' not found on the SharePoint site."
        )

    def resolve_folder_item(self, drive_id: str, folder_path: str) -> dict:
        """The driveItem for the configured root folder. Path segments are quoted
        (spaces, ampersands, non-ASCII) while '/' separators are preserved."""
        folder = folder_path.strip("/")
        if not folder:
            return self.get_json(f"{GRAPH_BASE}/drives/{drive_id}/root")
        quoted = quote(folder, safe="/")
        return self.get_json(f"{GRAPH_BASE}/drives/{drive_id}/root:/{quoted}")
