# SharePoint Delta Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the local-folder knowledge base with live, recursive SharePoint ingestion that keeps the Qdrant vector database automatically fresh within ~15 minutes of any upload, edit or deletion.

**Architecture:** A single Microsoft Graph delta query (`GET /drives/{driveId}/items/{folderId}/delta`) both enumerates the entire nested folder tree and reports incremental changes. A `sync_once()` engine consumes those changes, keyed on the Graph `driveItem` id rather than filename, skipping documents whose `cTag` is unchanged. An asyncio scheduler in the FastAPI lifespan calls it every 15 minutes, guarded by a Postgres advisory lock.

**Tech Stack:** Python 3.11, FastAPI, httpx, msal, qdrant-client, psycopg2, beautifulsoup4 (new), xlrd (new), pytest.

**Spec:** [docs/superpowers/specs/2026-09-23-sharepoint-delta-sync-design.md](../specs/2026-09-23-sharepoint-delta-sync-design.md)

## Global Constraints

- Every RAG failure is caught and logged, never raised. One bad file must not abort a 117-file run. This is the established `app/rag/` convention — follow it in all new code.
- Identity is **always** the Graph `driveItem` id. `parentReference.path` is for display only; the API reference warns it may be absent.
- Postgres calls are best-effort: `try/except → log.warning`, never raise. Exception: `advisory_lock` must correctly report failure to acquire.
- Rejected file extensions: **only** `.mp4`, `.zip` and other audio/video/archive/executable binaries. `.html` and `.xls` are explicitly **in** scope.
- New dependencies pinned in the existing style: `beautifulsoup4==4.12.3`, `xlrd==2.0.1`.
- Qdrant collection name stays `firstsource_analyst_kb_v2` — wiped and rebuilt in place, not renamed.
- Tests never touch live Qdrant, Postgres, SharePoint or Azure OpenAI. `tests/conftest.py` already forces `RAG_ENABLED=false`, `PG_ENABLED=false`, `RAG_VISION_ENABLED=false`.
- Default sync interval 15 minutes; full reconcile every 24 hours.

## Review Focus

Five failure modes the spec implies but that no task's happy path exercises. Each has a test assigned to the task owning the code.

1. **Wiped collection + retained document state** — if Qdrant v2 is wiped but Postgres still holds each document's `content_tag`, the next sync sees "nothing changed" and the collection stays permanently empty. Needs an explicit reset path. *(Task 3 provides `reset_sync_state`; Task 6 tests the `--reset` CLI path)*
2. **Delta item that is both a file and deleted** — the `deleted` facet must win over the `file` facet, or a deleted document gets re-ingested. *(Task 4)*
3. **File with no `@microsoft.graph.downloadUrl`** — some driveItems (OneNote sections, shortcuts) have no download URL. Must skip with a log, not crash the run. *(Task 4)*
4. **Document that extracts to zero chunks** — an empty or image-free scanned PDF produces no chunks. Must still record state, or it is retried on every single sync forever. *(Task 6)*
5. **Non-ASCII names and paths** — the live probe found folders like `Intelligent Process Automation (IPA) Solutions PEAK Matrix® Assessment`. Path derivation and URL quoting must handle these without raising. *(Task 4)*

---

## Task 1: File-type policy, HTML and legacy XLS extraction

**Files:**
- Modify: `app/document_sections.py` (add rejects, `extract_html_sections`, `extract_xls_sections`, text sniffing, dispatch)
- Modify: `app/extract.py:33-47` (extension sets), `app/extract.py:100-130` (parsers + dispatch)
- Modify: `requirements.txt`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `document_sections.REJECTED_EXTENSIONS: set[str]`, `document_sections.extract_html_sections(data: bytes, settings: Settings) -> List[Tuple[str, str]]`, `document_sections.extract_xls_sections(data: bytes, settings: Settings) -> List[Tuple[str, str]]`, `document_sections._looks_like_text(data: bytes) -> bool`. `extract_sections()` keeps its signature but now returns `[]` for rejected/unreadable files instead of raising.

- [ ] **Step 1: Add the new dependencies**

In `requirements.txt`, after the `pymupdf`/`pillow` block:

```
# Extraction: HTML pages and legacy .xls workbooks found in the SharePoint corpus
beautifulsoup4==4.12.3
xlrd==2.0.1
```

Then run: `./.venv/Scripts/python.exe -m pip install beautifulsoup4==4.12.3 xlrd==2.0.1`

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_extract.py`:

```python
def test_html_extracts_visible_text_and_drops_script_and_style():
    html = (
        b"<html><head><style>.a{color:red}</style>"
        b"<script>var secret = 'do not index me';</script></head>"
        b"<body><h1>Kairos proposal studio</h1><p>Governed Autonomy applies.</p></body></html>"
    )
    doc = extract_text("proposal-studio.html", html)
    assert "Kairos proposal studio" in doc.text
    assert "Governed Autonomy applies." in doc.text
    assert "do not index me" not in doc.text
    assert "color:red" not in doc.text


def test_html_tables_become_pipe_delimited_rows():
    html = (
        b"<html><body><table>"
        b"<tr><th>Area</th><th>Detail</th></tr>"
        b"<tr><td>Generative AI</td><td>32-38 engagements</td></tr>"
        b"</table></body></html>"
    )
    doc = extract_text("capabilities.html", html)
    assert "Area | Detail" in doc.text
    assert "Generative AI | 32-38 engagements" in doc.text


def test_legacy_xls_is_now_readable():
    import xlwt  # test-only writer for the legacy format

    book = xlwt.Workbook()
    sheet = book.add_sheet("Pricing")
    sheet.write(0, 0, "Tier")
    sheet.write(0, 1, "Monthly Fee")
    sheet.write(1, 0, "Starter")
    sheet.write(1, 1, "15000")
    buffer = io.BytesIO()
    book.save(buffer)

    doc = extract_text("pricing.xls", buffer.getvalue())
    assert "Tier | Monthly Fee" in doc.text
    assert "Starter" in doc.text


def test_rejected_binary_formats_yield_no_sections():
    from app.config import get_settings
    from app.document_sections import extract_sections

    assert extract_sections("clip.mp4", b"\x00\x00\x00\x18ftypmp42", get_settings()) == []
    assert extract_sections("bundle.zip", b"PK\x03\x04rubbish", get_settings()) == []


def test_unknown_extension_that_looks_like_text_is_read():
    from app.config import get_settings
    from app.document_sections import extract_sections

    sections = extract_sections("config.xml", b"<root><note>Kairos</note></root>", get_settings())
    assert sections
    assert "Kairos" in sections[0][1]


def test_unknown_extension_that_is_binary_is_skipped():
    from app.config import get_settings
    from app.document_sections import extract_sections

    assert extract_sections("thing.bin", b"\x00\x01\x02\x03\xff\xfe", get_settings()) == []
```

Add `xlwt==1.3.0` to `requirements.txt` under the `# Tests` heading.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_extract.py -k "html or xls or rejected or unknown" -v`
Expected: FAIL — `ExtractionError: Unsupported file type '.html'` and `ImportError` / `KeyError` for the rest.

- [ ] **Step 4: Add the extractors to `app/document_sections.py`**

Add near `IMAGE_EXTENSIONS`:

```python
# Binary formats with no text to recover. Skipped silently (logged), never retried.
REJECTED_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".wmv", ".mkv", ".webm",
    ".mp3", ".wav", ".m4a",
    ".zip", ".7z", ".rar", ".tar", ".gz",
    ".exe", ".dll", ".msi", ".iso",
}

# Legacy Office binaries with no reliable pure-Python reader. .xls is NOT here:
# xlrd 2.x reads it. .doc/.ppt would need antiword/LibreOffice.
UNREADABLE_LEGACY_EXTENSIONS = {".doc", ".ppt"}
```

Add the two extractors after `extract_text_sections`:

```python
def extract_html_sections(data: bytes, settings: Settings) -> List[Tuple[str, str]]:
    """Visible text from an HTML page. Scripts and styles are removed entirely -
    an app-generated page is mostly JavaScript, and indexing that would bury the
    real content. Tables become pipe-delimited rows, matching the docx/xlsx
    convention so downstream table detection works identically."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_decode(data), "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    table_rows: List[str] = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if any(cells):
                table_rows.append(" | ".join(cells))
        # Remove so the body text below does not repeat the table content.
        table.decompose()

    parts: List[str] = []
    body_text = soup.get_text("\n", strip=True)
    if body_text:
        parts.append(body_text)
    parts.extend(table_rows)
    joined = "\n".join(parts).strip()
    return [("", joined)] if joined else []


def extract_xls_sections(data: bytes, settings: Settings) -> List[Tuple[str, str]]:
    """Legacy Excel 97-2003 workbooks via xlrd (v2.x reads .xls only, which is
    exactly the gap openpyxl leaves). Same 'Sheet: name' / pipe-row shape as the
    modern .xlsx extractor so both look identical to the chunker."""
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    sections: List[Tuple[str, str]] = []
    for sheet in book.sheets():
        rows: List[str] = []
        for row_index in range(sheet.nrows):
            cells = [str(sheet.cell_value(row_index, c)).strip() for c in range(sheet.ncols)]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            sections.append((f"Sheet: {sheet.name}", "\n".join(rows)))
    return sections


def _looks_like_text(data: bytes, sample_bytes: int = 4096) -> bool:
    """Is this plausibly a text file under an extension we do not recognise?
    Lets future formats (.xml, .yaml, .rst) work without a code change, while
    stopping binary junk being indexed as gibberish."""
    chunk = data[:sample_bytes]
    if not chunk or b"\x00" in chunk:
        return False
    for encoding in ("utf-8", "cp1252"):
        try:
            decoded = chunk.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return False
    if not decoded:
        return False
    printable = sum(1 for ch in decoded if ch.isprintable() or ch in "\r\n\t")
    return printable / len(decoded) >= 0.9
```

- [ ] **Step 5: Register the handlers and rewrite the dispatcher**

In `_SECTION_EXTRACTORS`, add:

```python
    ".html": extract_html_sections,
    ".htm": extract_html_sections,
    ".xls": extract_xls_sections,
```

Replace `extract_sections` entirely:

```python
def extract_sections(
    filename: str, data: bytes, settings: Settings | None = None
) -> List[Tuple[str, str]]:
    """Split a document into (location_label, text) sections, one per page/slide/sheet,
    with embedded images/charts/scanned pages folded in as ordinary text.

    Returns [] for anything unreadable rather than raising: one unusable file in a
    117-file SharePoint corpus must not abort the run.
    """
    settings = settings or get_settings()
    suffix = Path(filename).suffix.lower()

    if suffix in REJECTED_EXTENSIONS:
        log.info("Skipping %s: %s is a binary format with no extractable text.", filename, suffix)
        return []
    if suffix in UNREADABLE_LEGACY_EXTENSIONS:
        log.info("Skipping %s: %s is a legacy binary format with no reliable reader.", filename, suffix)
        return []

    extractor = _SECTION_EXTRACTORS.get(suffix)
    if extractor is not None:
        return extractor(data, settings)

    if _looks_like_text(data):
        log.info("Reading %s as plain text (unrecognised extension %s).", filename, suffix or "(none)")
        return extract_text_sections(data, settings)

    log.info("Skipping %s: unrecognised extension %s and content is not text.", filename, suffix or "(none)")
    return []
```

- [ ] **Step 6: Wire the interactive upload path in `app/extract.py`**

Add after `JSON_EXTENSIONS`:

```python
HTML_EXTENSIONS = {".html", ".htm"}
LEGACY_EXCEL_EXTENSIONS = {".xls"}
```

Add `HTML_EXTENSIONS` and `LEGACY_EXCEL_EXTENSIONS` to the `SUPPORTED_EXTENSIONS` union.

Change `LEGACY_EXTENSIONS` to drop `.xls`:

```python
# Legacy binary formats the modern parsers cannot open. (.xls is handled by xlrd.)
LEGACY_EXTENSIONS = {".doc", ".ppt"}
```

Import the two new section functions from `.document_sections` and add the parsers:

```python
def _extract_html(data: bytes) -> str:
    return _join_plain(extract_html_sections(data, get_settings()))


def _extract_xls(data: bytes) -> str:
    return _join_with_markers(extract_xls_sections(data, get_settings()))
```

In `extract_text`, add these branches immediately after the `EXCEL_EXTENSIONS` branch:

```python
    elif suffix in LEGACY_EXCEL_EXTENSIONS:
        text = _extract_xls(data)
    elif suffix in HTML_EXTENSIONS:
        text = _extract_html(data)
```

Update the unsupported-type message to mention the new formats:

```python
        raise ExtractionError(
            f"Unsupported file type '{suffix}'. Supported: PDF, Word (.docx), "
            "PowerPoint (.pptx), Excel (.xlsx/.xls), HTML, and "
            "text/CSV/TSV/JSON/Markdown."
        )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_extract.py -v`
Expected: PASS, including all pre-existing tests.

- [ ] **Step 8: Commit**

```bash
git add requirements.txt app/document_sections.py app/extract.py tests/test_extract.py
git commit -m "feat(extract): read HTML and legacy .xls; reject only true binaries

The SharePoint corpus contains 4 .html and 1 .xls file that the fixed
allow-list silently dropped. Replaces that allow-list with a three-tier
policy: explicit binary rejects, explicit handlers, then a text sniff for
unrecognised extensions so future formats work without a code change.

extract_sections now returns [] for unreadable files instead of raising -
one bad file must not abort a 117-file ingestion run.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 2: Microsoft Graph transport client

**Files:**
- Create: `app/rag/graph_client.py`
- Test: `tests/test_graph_client.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `GraphClient(settings: Settings, http: httpx.Client | None = None)` with methods `headers() -> dict`, `get_json(url: str, *, params: dict | None = None) -> dict`, `paginate(url: str) -> Iterator[dict]`, `download(download_url: str) -> bytes`, `resolve_site_id() -> str`, `resolve_drive_id(site_id: str) -> str`, `resolve_folder_item(drive_id: str, folder_path: str) -> dict`. Module constants `GRAPH_BASE: str`. Exception `GraphError(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_graph_client.py`:

```python
"""Graph transport: token refresh, throttling, pagination. No network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import get_settings
from app.rag.graph_client import GRAPH_BASE, GraphClient, GraphError


def _client(handler, **token_kwargs) -> GraphClient:
    """GraphClient wired to a fake transport, with token acquisition stubbed."""
    transport = httpx.MockTransport(handler)
    client = GraphClient(get_settings(), http=httpx.Client(transport=transport))
    client._token = token_kwargs.get("token", "fake-token")
    client._token_expires_at = token_kwargs.get(
        "expires_at", datetime.now(timezone.utc) + timedelta(hours=1)
    )
    return client


def test_get_json_returns_parsed_body():
    def handler(request):
        return httpx.Response(200, json={"value": [{"id": "abc"}]})

    body = _client(handler).get_json(f"{GRAPH_BASE}/drives/d1/root/delta")
    assert body["value"][0]["id"] == "abc"


def test_authorization_header_is_sent():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    _client(handler).get_json(f"{GRAPH_BASE}/sites/root")
    assert seen["auth"] == "Bearer fake-token"


def test_429_honours_retry_after_then_succeeds(monkeypatch):
    slept = []
    monkeypatch.setattr("app.rag.graph_client.time.sleep", slept.append)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json={"ok": True})

    body = _client(handler).get_json(f"{GRAPH_BASE}/sites/root")
    assert body == {"ok": True}
    assert slept == [7.0]


def test_503_without_retry_after_uses_exponential_backoff(monkeypatch):
    slept = []
    monkeypatch.setattr("app.rag.graph_client.time.sleep", slept.append)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"ok": True})

    _client(handler).get_json(f"{GRAPH_BASE}/sites/root")
    assert slept == [2.0, 4.0]


def test_expired_token_triggers_reacquisition(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={})

    client = _client(handler, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    refreshed = {"n": 0}

    def fake_acquire():
        refreshed["n"] += 1
        client._token = "refreshed-token"
        client._token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    monkeypatch.setattr(client, "_acquire_token", fake_acquire)
    assert client.headers()["Authorization"] == "Bearer refreshed-token"
    assert refreshed["n"] == 1


def test_paginate_follows_next_link_and_stops():
    pages = {
        f"{GRAPH_BASE}/p1": {"value": [{"id": "a"}], "@odata.nextLink": f"{GRAPH_BASE}/p2"},
        f"{GRAPH_BASE}/p2": {"value": [{"id": "b"}], "@odata.deltaLink": f"{GRAPH_BASE}/done"},
    }

    def handler(request):
        return httpx.Response(200, json=pages[str(request.url)])

    collected = list(_client(handler).paginate(f"{GRAPH_BASE}/p1"))
    assert [p["value"][0]["id"] for p in collected] == ["a", "b"]
    assert collected[-1]["@odata.deltaLink"] == f"{GRAPH_BASE}/done"


def test_download_sends_no_authorization_header():
    """Graph pre-signed download URLs reject an Authorization header."""
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=b"file-bytes")

    assert _client(handler).download("https://signed.example/file") == b"file-bytes"
    assert seen["auth"] is None


def test_persistent_throttling_raises_graph_error(monkeypatch):
    monkeypatch.setattr("app.rag.graph_client.time.sleep", lambda _: None)

    def handler(request):
        return httpx.Response(429, json={})

    with pytest.raises(GraphError, match="throttled"):
        _client(handler).get_json(f"{GRAPH_BASE}/sites/root")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_graph_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.rag.graph_client'`

- [ ] **Step 3: Create `app/rag/graph_client.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_graph_client.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add app/rag/graph_client.py tests/test_graph_client.py
git commit -m "feat(rag): add throttle-aware Microsoft Graph transport client

Fixes two defects in the inline implementation in sources.py: the access
token was never refreshed (guaranteed 401 on any ingestion past ~1 hour),
and 429/503 responses were unhandled despite Microsoft's guidance that
ignoring Retry-After gets an application blocked.

Download deliberately omits the Authorization header - Graph download URLs
are pre-signed and can reject one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 3: Postgres sync state, document state, advisory lock

**Files:**
- Modify: `app/rag/store.py` (schema, migration, new functions)
- Test: `tests/test_store_sql.py` (new — SQL-shape tests that run with `PG_ENABLED=false`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `store.DocumentState` dataclass: `item_id: str`, `name: str`, `content_tag: str`, `content_hash: str`, `sync_status: str`, `attempt_count: int`
  - `store.get_document_states(settings) -> Dict[str, DocumentState]` keyed by `item_id`
  - `store.get_known_document_names(settings) -> List[str]`
  - `store.get_content_hash_owners(settings) -> Dict[str, str]` mapping `content_hash -> item_id`
  - `store.record_document(item_id, name, folder_path, source_type, web_url, content_tag, content_hash, chunk_count, settings) -> None`
  - `store.record_document_failure(item_id, name, source_type, error, settings) -> None`
  - `store.mark_document_deleted(item_id, settings) -> None`
  - `store.get_sync_state(source_key, settings) -> Optional[str]` (the delta link)
  - `store.save_sync_state(source_key, delta_link, full, settings) -> None`
  - `store.reset_sync_state(source_key, settings) -> None`
  - `store.advisory_lock(settings)` — context manager yielding `bool`
  - `store.start_ingestion_run(settings, trigger="manual")` — `trigger` parameter added

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store_sql.py`:

```python
"""Store schema and no-Postgres behaviour. PG_ENABLED is false in conftest,
so every call must degrade safely rather than attempt a connection."""

from __future__ import annotations

from app.config import get_settings
from app.rag import store


def test_schema_declares_sync_state_table():
    assert "CREATE TABLE IF NOT EXISTS sync_state" in store.SCHEMA_SQL
    assert "delta_link" in store.SCHEMA_SQL
    assert "last_full_sync_at" in store.SCHEMA_SQL


def test_migration_adds_item_id_and_moves_the_unique_key():
    sql = store.MIGRATION_SQL
    assert "ADD COLUMN IF NOT EXISTS item_id" in sql
    assert "ADD COLUMN IF NOT EXISTS content_tag" in sql
    assert "ADD COLUMN IF NOT EXISTS content_hash" in sql
    assert "ADD COLUMN IF NOT EXISTS folder_path" in sql
    assert "ADD COLUMN IF NOT EXISTS sync_status" in sql
    assert "ADD COLUMN IF NOT EXISTS attempt_count" in sql
    # The old filename-based key must go, or two same-named files in different
    # subfolders still collide.
    assert "DROP CONSTRAINT IF EXISTS documents_name_source_type_key" in sql
    assert "documents_item_id_source_type_key" in sql


def test_ingestion_runs_records_what_triggered_it():
    assert "trigger" in store.SCHEMA_SQL
    assert "ADD COLUMN IF NOT EXISTS trigger" in store.MIGRATION_SQL


def test_reads_return_empty_when_postgres_is_disabled():
    settings = get_settings()
    assert settings.pg_enabled is False
    assert store.get_document_states(settings) == {}
    assert store.get_known_document_names(settings) == []
    assert store.get_content_hash_owners(settings) == {}
    assert store.get_sync_state("sharepoint", settings) is None


def test_writes_are_no_ops_when_postgres_is_disabled():
    settings = get_settings()
    store.save_sync_state("sharepoint", "https://delta", False, settings)
    store.mark_document_deleted("item-1", settings)
    store.record_document_failure("item-1", "a.docx", "sharepoint", "boom", settings)
    store.reset_sync_state("sharepoint", settings)


def test_advisory_lock_grants_without_postgres():
    """No Postgres means a single local process; it must not block itself."""
    with store.advisory_lock(get_settings()) as acquired:
        assert acquired is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_store_sql.py -v`
Expected: FAIL with `AttributeError: module 'app.rag.store' has no attribute 'MIGRATION_SQL'`

- [ ] **Step 3: Extend the schema in `app/rag/store.py`**

Replace the `documents` block inside `SCHEMA_SQL` and append the new table:

```sql
CREATE TABLE IF NOT EXISTS documents (
    id SERIAL PRIMARY KEY,
    item_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    folder_path TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL,
    web_url TEXT DEFAULT '',
    latest_hash TEXT NOT NULL,
    content_tag TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    sync_status TEXT NOT NULL DEFAULT 'ok',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT DEFAULT '',
    deleted_at TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sync_state (
    source_key TEXT PRIMARY KEY,
    delta_link TEXT NOT NULL DEFAULT '',
    last_synced_at TIMESTAMPTZ,
    last_full_sync_at TIMESTAMPTZ
);
```

Add `trigger TEXT NOT NULL DEFAULT 'manual'` to the `ingestion_runs` table definition.

- [ ] **Step 4: Add the migration**

`CREATE TABLE IF NOT EXISTS` will not alter the already-deployed `documents` table, so add a separate idempotent migration below `SCHEMA_SQL`:

```python
# CREATE TABLE IF NOT EXISTS never alters an existing table, so these run
# separately and idempotently against the already-deployed Phase 2 schema.
MIGRATION_SQL = """
ALTER TABLE documents ADD COLUMN IF NOT EXISTS item_id TEXT NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS folder_path TEXT NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_tag TEXT NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS sync_status TEXT NOT NULL DEFAULT 'ok';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_error TEXT DEFAULT '';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- Existing rows were keyed on filename. Backfill item_id from the name so the
-- new unique key can be created, then retire the filename-based constraint:
-- two files with the same name in different subfolders must not collide.
UPDATE documents SET item_id = name WHERE item_id = '';
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_name_source_type_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'documents_item_id_source_type_key'
    ) THEN
        ALTER TABLE documents
            ADD CONSTRAINT documents_item_id_source_type_key UNIQUE (item_id, source_type);
    END IF;
END $$;

ALTER TABLE ingestion_runs ADD COLUMN IF NOT EXISTS trigger TEXT NOT NULL DEFAULT 'manual';
"""
```

In `ensure_schema`, execute the migration after the schema:

```python
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(MIGRATION_SQL)
```

- [ ] **Step 5: Add the state accessors**

Add near the top of `store.py`:

```python
from dataclasses import dataclass


@dataclass
class DocumentState:
    """What we already know about one source document, for change detection."""

    item_id: str
    name: str
    content_tag: str
    content_hash: str
    sync_status: str
    attempt_count: int
```

Add the functions (each follows the existing best-effort pattern):

```python
def get_document_states(settings: Settings | None = None) -> Dict[str, DocumentState]:
    """Current per-document sync state keyed by item_id - the basis for deciding
    whether a document needs re-ingesting."""
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return {}
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT item_id, name, content_tag, content_hash, sync_status, attempt_count
                FROM documents WHERE deleted_at IS NULL
                """
            )
            return {
                row[0]: DocumentState(
                    item_id=row[0], name=row[1], content_tag=row[2],
                    content_hash=row[3], sync_status=row[4], attempt_count=row[5],
                )
                for row in cur.fetchall()
            }
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch document states: %s", exc)
        return {}


def get_known_document_names(settings: Settings | None = None) -> List[str]:
    """Distinct filenames of live documents - used by retrieval's document-name filter."""
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return []
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT name FROM documents WHERE deleted_at IS NULL")
            return [row[0] for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch document names: %s", exc)
        return []


def get_content_hash_owners(settings: Settings | None = None) -> Dict[str, str]:
    """content_hash -> the item_id that owns it, for duplicate suppression."""
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return {}
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT content_hash, MIN(item_id) FROM documents
                WHERE deleted_at IS NULL AND content_hash <> '' AND sync_status = 'ok'
                GROUP BY content_hash
                """
            )
            return dict(cur.fetchall())
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch content hash owners: %s", exc)
        return {}
```

Replace `record_document` with the item-id-keyed version:

```python
def record_document(
    item_id: str,
    name: str,
    folder_path: str,
    source_type: str,
    web_url: str,
    content_tag: str,
    content_hash: str,
    chunk_count: int,
    settings: Settings | None = None,
) -> None:
    """Record a successful ingestion. Clears any previous failure state."""
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return
    now = datetime.now(timezone.utc)
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents (item_id, name, folder_path, source_type, web_url,
                                       latest_hash, content_tag, content_hash,
                                       sync_status, attempt_count, last_error,
                                       deleted_at, first_seen_at, last_ingested_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ok', 0, '', NULL, %s, %s)
                ON CONFLICT (item_id, source_type) DO UPDATE
                    SET name = EXCLUDED.name,
                        folder_path = EXCLUDED.folder_path,
                        web_url = EXCLUDED.web_url,
                        latest_hash = EXCLUDED.latest_hash,
                        content_tag = EXCLUDED.content_tag,
                        content_hash = EXCLUDED.content_hash,
                        sync_status = 'ok',
                        attempt_count = 0,
                        last_error = '',
                        deleted_at = NULL,
                        last_ingested_at = EXCLUDED.last_ingested_at
                RETURNING id
                """,
                (item_id, name, folder_path, source_type, web_url, content_hash,
                 content_tag, content_hash, now, now),
            )
            doc_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO document_versions (document_id, content_hash, chunk_count)
                SELECT %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM document_versions
                    WHERE document_id = %s AND content_hash = %s
                )
                """,
                (doc_id, content_hash, chunk_count, doc_id, content_hash),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not record document metadata for %s: %s", name, exc)


def record_document_failure(
    item_id: str, name: str, source_type: str, error: str, settings: Settings | None = None
) -> None:
    """Record a failed ingestion WITHOUT recording content_tag - that omission is
    what makes the next sync retry this document automatically."""
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents (item_id, name, source_type, latest_hash,
                                       sync_status, attempt_count, last_error)
                VALUES (%s, %s, %s, '', 'failed', 1, %s)
                ON CONFLICT (item_id, source_type) DO UPDATE
                    SET sync_status = 'failed',
                        attempt_count = documents.attempt_count + 1,
                        last_error = EXCLUDED.last_error
                """,
                (item_id, name, source_type, error[:2000]),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not record ingestion failure for %s: %s", name, exc)


def mark_document_deleted(item_id: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET deleted_at = now() WHERE item_id = %s", (item_id,)
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not mark document %s deleted: %s", item_id, exc)
```

- [ ] **Step 6: Add sync-state accessors and the advisory lock**

```python
def get_sync_state(source_key: str, settings: Settings | None = None) -> Optional[str]:
    """The saved delta link for this source, or None to force a full enumeration."""
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return None
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute("SELECT delta_link FROM sync_state WHERE source_key = %s", (source_key,))
            row = cur.fetchone()
            return (row[0] or None) if row else None
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read sync state: %s", exc)
        return None


def save_sync_state(
    source_key: str, delta_link: str, full: bool = False, settings: Settings | None = None
) -> None:
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_state (source_key, delta_link, last_synced_at, last_full_sync_at)
                VALUES (%s, %s, now(), CASE WHEN %s THEN now() ELSE NULL END)
                ON CONFLICT (source_key) DO UPDATE
                    SET delta_link = EXCLUDED.delta_link,
                        last_synced_at = now(),
                        last_full_sync_at = CASE WHEN %s THEN now()
                                                 ELSE sync_state.last_full_sync_at END
                """,
                (source_key, delta_link, full, full),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not save sync state: %s", exc)


def reset_sync_state(source_key: str, settings: Settings | None = None) -> None:
    """Forget the delta link AND every document's content tag.

    Required after wiping the Qdrant collection: without this the next sync sees
    'nothing changed' and the collection stays permanently empty.
    """
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sync_state WHERE source_key = %s", (source_key,))
            cur.execute(
                "UPDATE documents SET content_tag = '', attempt_count = 0, sync_status = 'ok' "
                "WHERE source_type = %s",
                (source_key,),
            )
        log.info("Reset sync state for '%s' - the next run will re-ingest everything.", source_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not reset sync state: %s", exc)


# Arbitrary but stable key; shared by every instance so only one syncs at a time.
_SYNC_ADVISORY_LOCK_KEY = 728411


@contextmanager
def advisory_lock(
    settings: Settings | None = None, key: int = _SYNC_ADVISORY_LOCK_KEY
) -> Iterator[bool]:
    """Cluster-wide mutex for ingestion, yielding whether it was acquired.

    Held on a dedicated connection for the caller's whole duration - this is what
    stops the 2 gunicorn workers (and any scaled-out instance) ingesting at once.
    Without Postgres there is a single local process, so it always grants.
    """
    settings = settings or get_settings()
    if not settings.pg_enabled:
        yield True
        return

    import psycopg2

    conn = None
    acquired = False
    try:
        conn = psycopg2.connect(
            host=settings.pg_host, port=settings.pg_port, dbname=settings.pg_dbname,
            user=settings.pg_user, password=settings.pg_password, sslmode=settings.pg_sslmode,
            options=f"-c search_path={settings.pg_schema}",
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            acquired = bool(cur.fetchone()[0])
        yield acquired
    except Exception as exc:  # noqa: BLE001 - never break the caller over the lock
        log.warning("Could not obtain the sync advisory lock: %s", exc)
        yield False
    finally:
        if conn is not None:
            try:
                if acquired:
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            except Exception:  # noqa: BLE001
                pass  # closing the connection releases it anyway
            conn.close()
```

Add `trigger` to `start_ingestion_run`:

```python
def start_ingestion_run(
    settings: Settings | None = None, trigger: str = "manual"
) -> Optional[int]:
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return None
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ingestion_runs (trigger) VALUES (%s) RETURNING id", (trigger,)
            )
            return cur.fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not start an ingestion run record: %s", exc)
        return None
```

Ensure `List` is in the `typing` import line.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_store_sql.py -v`
Expected: PASS (6 tests)

- [ ] **Step 8: Commit**

```bash
git add app/rag/store.py tests/test_store_sql.py
git commit -m "feat(rag): item-id document state, sync state, advisory lock

Moves the documents unique key from (name, source_type) to
(item_id, source_type) so two files with the same name in different
SharePoint subfolders no longer overwrite each other. CREATE TABLE IF NOT
EXISTS cannot alter a deployed table, so an idempotent MIGRATION_SQL runs
alongside it and backfills item_id from name.

reset_sync_state exists for a specific trap: wiping the Qdrant collection
while retaining content tags would make the next sync conclude 'nothing
changed' and leave the collection permanently empty.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: Delta-based document sources

**Files:**
- Rewrite: `app/rag/sources.py`
- Test: `tests/test_sources.py` (new)

**Interfaces:**
- Consumes: `GraphClient`, `GraphError`, `GRAPH_BASE` (Task 2); `REJECTED_EXTENSIONS` (Task 1).
- Produces:
  - `SourceDocument` dataclass: `item_id`, `name`, `folder_path`, `content_tag`, `content_hash`, `modified_at`, `web_url`, `size`, `fetch: Callable[[], bytes]`
  - `SourceDeletion` dataclass: `item_id`
  - `SourceBatch` dataclass: `documents: List[SourceDocument]`, `deletions: List[SourceDeletion]`, `delta_link: str`
  - `DocumentSource` protocol with `fetch_changes(delta_link: Optional[str]) -> SourceBatch`
  - `LocalFolderSource(directory: Path)`, `SharePointSource(settings: Settings, graph: GraphClient | None = None)`
  - `get_document_source(settings) -> DocumentSource`
  - `SHAREPOINT_SOURCE_KEY = "sharepoint"`, `LOCAL_SOURCE_KEY = "local"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sources.py`:

```python
"""Delta parsing and local-source equivalence. No network."""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.rag.graph_client import GRAPH_BASE, GraphClient
from app.rag.sources import LocalFolderSource, SharePointSource

ROOT_PATH = "/drives/d1/root:/GenAI Content/relAI/relAI Latest Pitch Deck/Analyst"


def _item(name, item_id, *, folder=False, deleted=False, path=ROOT_PATH, **extra):
    item = {
        "id": item_id,
        "name": name,
        "lastModifiedDateTime": "2026-04-13T06:55:23Z",
        "webUrl": f"https://sp.example/{name}",
        "size": 1234,
        "parentReference": {"path": path, "driveId": "d1"},
    }
    if folder:
        item["folder"] = {"childCount": 0}
    else:
        item["file"] = {"hashes": {"quickXorHash": f"hash-{item_id}"}}
        item["cTag"] = f"ctag-{item_id}"
        item["@microsoft.graph.downloadUrl"] = f"https://signed.example/{item_id}"
    if deleted:
        item["deleted"] = {"state": "deleted"}
    item.update(extra)
    return item


def _source(pages) -> SharePointSource:
    """SharePointSource whose Graph calls are served from `pages` by URL."""

    def handler(request):
        url = str(request.url)
        if url in pages:
            return httpx.Response(200, json=pages[url])
        if url.startswith("https://signed.example/"):
            return httpx.Response(200, content=b"file-bytes")
        raise AssertionError(f"unexpected URL: {url}")

    graph = GraphClient(get_settings(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    graph._token = "fake"
    from datetime import datetime, timedelta, timezone
    graph._token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    source = SharePointSource(get_settings(), graph=graph)
    source._resolved = ("site1", "d1", "folder1")  # skip live resolution
    return source


DELTA_URL = f"{GRAPH_BASE}/drives/d1/items/folder1/delta"


def test_folders_are_not_returned_as_documents():
    batch = _source({
        DELTA_URL: {
            "value": [_item("ISG", "f1", folder=True), _item("Q1.docx", "i1")],
            "@odata.deltaLink": f"{GRAPH_BASE}/next",
        }
    }).fetch_changes(None)
    assert [d.name for d in batch.documents] == ["Q1.docx"]
    assert batch.delta_link == f"{GRAPH_BASE}/next"


def test_pagination_collects_every_page():
    batch = _source({
        DELTA_URL: {"value": [_item("a.docx", "i1")], "@odata.nextLink": f"{GRAPH_BASE}/p2"},
        f"{GRAPH_BASE}/p2": {"value": [_item("b.docx", "i2")], "@odata.deltaLink": f"{GRAPH_BASE}/end"},
    }).fetch_changes(None)
    assert sorted(d.item_id for d in batch.documents) == ["i1", "i2"]


def test_deleted_facet_wins_over_file_facet():
    """A delta item can carry BOTH file and deleted facets. Treating it as an
    upsert would silently re-ingest a document the user deleted."""
    batch = _source({
        DELTA_URL: {
            "value": [_item("gone.docx", "i9", deleted=True)],
            "@odata.deltaLink": f"{GRAPH_BASE}/end",
        }
    }).fetch_changes(None)
    assert batch.documents == []
    assert [d.item_id for d in batch.deletions] == ["i9"]


def test_file_without_download_url_is_skipped_not_crashed():
    item = _item("weird.docx", "i5")
    del item["@microsoft.graph.downloadUrl"]
    batch = _source({
        DELTA_URL: {"value": [item], "@odata.deltaLink": f"{GRAPH_BASE}/end"}
    }).fetch_changes(None)
    assert batch.documents == []


def test_rejected_extensions_never_reach_the_pipeline():
    batch = _source({
        DELTA_URL: {
            "value": [_item("clip.mp4", "i1"), _item("bundle.zip", "i2"), _item("real.pdf", "i3")],
            "@odata.deltaLink": f"{GRAPH_BASE}/end",
        }
    }).fetch_changes(None)
    assert [d.name for d in batch.documents] == ["real.pdf"]


def test_folder_path_is_relative_to_the_configured_root():
    nested = f"{ROOT_PATH}/Avasant/2026/Healthcare"
    batch = _source({
        DELTA_URL: {"value": [_item("x.docx", "i1", path=nested)], "@odata.deltaLink": "x"}
    }).fetch_changes(None)
    assert batch.documents[0].folder_path == "Avasant/2026/Healthcare"


def test_non_ascii_folder_path_does_not_raise():
    odd = f"{ROOT_PATH}/Everest/2025/IPA Solutions PEAK Matrix® Assessment"
    batch = _source({
        DELTA_URL: {"value": [_item("y.docx", "i1", path=odd)], "@odata.deltaLink": "x"}
    }).fetch_changes(None)
    assert "PEAK Matrix" in batch.documents[0].folder_path


def test_content_tag_and_hash_are_carried_through():
    batch = _source({
        DELTA_URL: {"value": [_item("a.docx", "i1")], "@odata.deltaLink": "x"}
    }).fetch_changes(None)
    doc = batch.documents[0]
    assert doc.content_tag == "ctag-i1"
    assert doc.content_hash == "hash-i1"


def test_fetch_is_lazy_and_returns_bytes():
    batch = _source({
        DELTA_URL: {"value": [_item("a.docx", "i1")], "@odata.deltaLink": "x"}
    }).fetch_changes(None)
    assert batch.documents[0].fetch() == b"file-bytes"


def test_saved_delta_link_is_used_instead_of_a_full_enumeration():
    saved = f"{GRAPH_BASE}/drives/d1/items/folder1/delta(token='abc')"
    batch = _source({saved: {"value": [], "@odata.deltaLink": saved}}).fetch_changes(saved)
    assert batch.documents == []
    assert batch.delta_link == saved


def test_local_source_walks_subdirectories(tmp_path):
    (tmp_path / "sub" / "deep").mkdir(parents=True)
    (tmp_path / "top.txt").write_bytes(b"top")
    (tmp_path / "sub" / "deep" / "nested.txt").write_bytes(b"nested")
    (tmp_path / "sub" / "clip.mp4").write_bytes(b"\x00binary")

    batch = LocalFolderSource(tmp_path).fetch_changes(None)
    names = sorted(d.name for d in batch.documents)
    assert names == ["nested.txt", "top.txt"]
    nested = next(d for d in batch.documents if d.name == "nested.txt")
    assert nested.folder_path == "sub/deep"
    assert nested.fetch() == b"nested"


def test_local_source_content_tag_changes_with_content(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"one")
    first = LocalFolderSource(tmp_path).fetch_changes(None).documents[0].content_tag
    path.write_bytes(b"two")
    second = LocalFolderSource(tmp_path).fetch_changes(None).documents[0].content_tag
    assert first != second
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_sources.py -v`
Expected: FAIL with `ImportError: cannot import name 'SourceDeletion'` / `LocalFolderSource has no attribute 'fetch_changes'`

- [ ] **Step 3: Rewrite `app/rag/sources.py`**

```python
"""Document sources for ingestion, expressed as CHANGE BATCHES rather than full listings.

Two interchangeable sources, selected by RAG_SOURCE:

- "local"      reads files recursively from a folder on disk. No Azure AD needed.
               Used to exercise the whole sync engine offline.
- "sharepoint" reads a SharePoint document library subtree via the Microsoft Graph
               DELTA query, using app-only (client credentials) auth. Read-only.

Why delta and not a recursive children walk: the Graph API reference states that
paging the `children` collection is NOT guaranteed to return every item if any
writes happen during enumeration, and that delta must be used to maintain a full
local representation. Delta also walks the entire nested hierarchy in one paged
call and reports deletions, which a children walk cannot do at all.

Identity is ALWAYS item_id, never the filename: two files in different subfolders
can share a name, and files get renamed and moved.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Protocol

from ..config import Settings, get_settings
from ..document_sections import REJECTED_EXTENSIONS
from .graph_client import GRAPH_BASE, GraphClient, GraphError

log = logging.getLogger(__name__)

SHAREPOINT_SOURCE_KEY = "sharepoint"
LOCAL_SOURCE_KEY = "local"


@dataclass
class SourceDocument:
    """One file to consider ingesting, wherever it came from."""

    item_id: str          # stable identity (Graph driveItem id / relative path)
    name: str             # display and citation only
    folder_path: str      # relative to the configured root, for display
    content_tag: str      # changes ONLY when content changes (cTag / sha256)
    content_hash: str     # for duplicate detection (quickXorHash / sha256)
    modified_at: datetime
    web_url: str
    size: int
    # Lazy: called only once we have decided this document actually needs work.
    fetch: Callable[[], bytes] = field(repr=False, default=lambda: b"")


@dataclass
class SourceDeletion:
    item_id: str


@dataclass
class SourceBatch:
    documents: List[SourceDocument]
    deletions: List[SourceDeletion]
    delta_link: str


class DocumentSource(Protocol):
    def fetch_changes(self, delta_link: Optional[str]) -> SourceBatch: ...


def _is_rejected(name: str) -> bool:
    return Path(name).suffix.lower() in REJECTED_EXTENSIONS


# ------------------------------------------------------------------- local
class LocalFolderSource:
    """Every file under a directory, recursively. Content hash stands in for cTag,
    so the engine's change detection behaves identically to SharePoint's."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def fetch_changes(self, delta_link: Optional[str]) -> SourceBatch:
        documents: List[SourceDocument] = []
        if not self.directory.is_dir():
            log.warning("RAG local source dir %s does not exist.", self.directory)
            return SourceBatch([], [], "")

        for path in sorted(self.directory.rglob("*")):
            if not path.is_file() or _is_rejected(path.name):
                continue
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            relative = path.relative_to(self.directory)
            folder_path = str(relative.parent).replace("\\", "/")
            documents.append(
                SourceDocument(
                    item_id=str(relative).replace("\\", "/"),
                    name=path.name,
                    folder_path="" if folder_path == "." else folder_path,
                    content_tag=digest,
                    content_hash=digest,
                    modified_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
                    web_url=str(path),
                    size=len(data),
                    fetch=(lambda p=path: p.read_bytes()),
                )
            )
        # A local scan is always complete, so there is no incremental token.
        return SourceBatch(documents, [], "")


# -------------------------------------------------------------- sharepoint
class SharePointSource:
    """A SharePoint folder subtree, read through the Graph delta query."""

    def __init__(self, settings: Settings, graph: Optional[GraphClient] = None) -> None:
        self.settings = settings
        self.graph = graph or GraphClient(settings)
        self._resolved: Optional[tuple[str, str, str]] = None
        self._root_path_prefix = ""

    def _resolve(self) -> tuple[str, str, str]:
        if self._resolved is None:
            site_id = self.graph.resolve_site_id()
            drive_id = self.graph.resolve_drive_id(site_id)
            folder = self.graph.resolve_folder_item(drive_id, self.settings.sharepoint_folder)
            self._resolved = (site_id, drive_id, folder["id"])
            log.info("Resolved SharePoint folder '%s' to item %s.",
                     self.settings.sharepoint_folder, folder["id"])
        return self._resolved

    def _relative_folder_path(self, item: dict) -> str:
        """Path under the configured root, for display. parentReference.path may be
        absent (the API reference warns of this), so this must never be load-bearing."""
        raw = (item.get("parentReference") or {}).get("path") or ""
        if not raw:
            return ""
        root_leaf = self.settings.sharepoint_folder.strip("/").rsplit("/", 1)[-1]
        marker = f"/{root_leaf}"
        index = raw.find(marker)
        if index < 0:
            return ""
        return raw[index + len(marker):].strip("/")

    def _to_document(self, item: dict) -> Optional[SourceDocument]:
        name = item.get("name", "")
        if _is_rejected(name):
            log.info("Skipping %s: rejected binary format.", name)
            return None

        download_url = item.get("@microsoft.graph.downloadUrl")
        if not download_url:
            # OneNote sections, shortcuts and some special items have no content URL.
            log.info("Skipping %s: no download URL on this driveItem.", name)
            return None

        hashes = (item.get("file") or {}).get("hashes") or {}
        modified_raw = item.get("lastModifiedDateTime", "")
        try:
            modified_at = datetime.fromisoformat(modified_raw.replace("Z", "+00:00"))
        except ValueError:
            modified_at = datetime.now(timezone.utc)

        return SourceDocument(
            item_id=item["id"],
            name=name,
            folder_path=self._relative_folder_path(item),
            content_tag=item.get("cTag", "") or item.get("eTag", ""),
            content_hash=hashes.get("quickXorHash", "") or hashes.get("sha256Hash", ""),
            modified_at=modified_at,
            web_url=item.get("webUrl", ""),
            size=int(item.get("size", 0) or 0),
            fetch=(lambda url=download_url: self.graph.download(url)),
        )

    def fetch_changes(self, delta_link: Optional[str]) -> SourceBatch:
        _, drive_id, folder_id = self._resolve()
        url = delta_link or f"{GRAPH_BASE}/drives/{drive_id}/items/{folder_id}/delta"

        documents: List[SourceDocument] = []
        deletions: List[SourceDeletion] = []
        next_delta_link = ""

        for page in self.graph.paginate(url):
            for item in page.get("value", []):
                # The deleted facet can appear alongside file/folder. It must win,
                # or a document the user deleted gets silently re-ingested.
                if "deleted" in item:
                    deletions.append(SourceDeletion(item_id=item["id"]))
                    continue
                if "folder" in item:
                    continue  # containers carry no content of their own
                document = self._to_document(item)
                if document is not None:
                    documents.append(document)
            next_delta_link = page.get("@odata.deltaLink", next_delta_link)

        log.info("Delta returned %d document(s) and %d deletion(s).", len(documents), len(deletions))
        return SourceBatch(documents, deletions, next_delta_link)


def get_document_source(settings: Settings | None = None) -> DocumentSource:
    settings = settings or get_settings()
    if settings.rag_source == "sharepoint":
        return SharePointSource(settings)
    return LocalFolderSource(settings.base_dir / settings.rag_local_source_dir)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_sources.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add app/rag/sources.py tests/test_sources.py
git commit -m "feat(rag): delta-based recursive SharePoint source

Replaces the flat single-folder listing (which explicitly skipped
subfolders) with the Graph delta query. Delta walks the whole nested
hierarchy in one paged call, reports deletions, and returns a token for
incremental follow-ups.

Sources now return change BATCHES rather than full listings, and file
content is fetched lazily so a sync that finds nothing new downloads
nothing. LocalFolderSource implements the same interface and recurses too,
keeping the engine fully testable offline.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: Item-id identity through Qdrant, retrieval and cache

**Files:**
- Modify: `app/rag/index.py` (point id, payload, payload indexes, `delete_document`)
- Modify: `app/rag/retrieve.py:30-45` (`RetrievedChunk`), `:57-66` (`_match_known_source`), `:175-186` (chunk construction)
- Modify: `app/rag/cache.py:96-104` (invalidation keying)
- Test: `tests/test_index_identity.py` (new)

**Interfaces:**
- Consumes: `SourceDocument` (Task 4); `store.get_known_document_names`, `store.get_document_states` (Task 3).
- Produces: `index._point_id(item_id: str, chunk_index: int) -> str`, `index.delete_document(client, collection, item_id) -> None`, `index.ensure_collection(client, collection, dense_dim)` now also creates payload indexes. `RetrievedChunk` gains `item_id: str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_index_identity.py`:

```python
"""Document identity and deletion. No live Qdrant."""

from __future__ import annotations

from app.rag.index import _point_id, delete_document


def test_point_id_is_stable_for_the_same_item():
    assert _point_id("item-abc", 3) == _point_id("item-abc", 3)


def test_same_filename_in_different_folders_gets_different_ids():
    """The collision this whole identity change exists to prevent."""
    assert _point_id("item-in-isg", 0) != _point_id("item-in-hfs", 0)


def test_chunk_index_separates_points_within_a_document():
    assert _point_id("item-abc", 0) != _point_id("item-abc", 1)


class _FakeClient:
    def __init__(self):
        self.deleted = []

    def delete(self, collection_name, points_selector):
        self.deleted.append((collection_name, points_selector))


def test_delete_document_filters_on_item_id():
    client = _FakeClient()
    delete_document(client, "kb", "item-abc")
    collection, selector = client.deleted[0]
    assert collection == "kb"
    condition = selector.filter.must[0]
    assert condition.key == "item_id"
    assert condition.match.value == "item-abc"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_index_identity.py -v`
Expected: FAIL with `ImportError: cannot import name 'delete_document'`

- [ ] **Step 3: Update `app/rag/index.py`**

Change `_point_id` to key on item id:

```python
def _point_id(item_id: str, chunk_index: int) -> str:
    # Stable, deterministic UUID so re-ingesting the same document updates the same
    # points in place instead of duplicating them. Keyed on the SOURCE ITEM ID, not
    # the filename: two files in different SharePoint subfolders can share a name,
    # and files get renamed and moved. Qdrant point IDs must be an unsigned integer
    # or a UUID - a raw hash string is rejected.
    return str(uuid.uuid5(_ID_NAMESPACE, f"{item_id}::{chunk_index}"))
```

Add payload indexes to `ensure_collection`, after `create_collection`:

```python
    # Payload indexes make delete-by-item_id cheap, and remove a full scan from
    # the document-name filter already used in retrieve.py.
    for field_name in ("item_id", "source"):
        try:
            client.create_payload_index(
                collection_name=collection,
                field_name=field_name,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        except Exception as exc:  # noqa: BLE001 - index is an optimisation, not required
            log.warning("Could not create the '%s' payload index: %s", field_name, exc)
```

Add the deletion function after `_point_id`:

```python
def delete_document(client: QdrantClient, collection: str, item_id: str) -> None:
    """Remove every chunk belonging to one source document.

    Called both when a document is deleted at source and immediately before
    re-indexing a changed document - without the latter, a document that shrinks
    from 40 chunks to 20 strands 20 orphans that keep surfacing in retrieval.
    """
    try:
        client.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(key="item_id", match=models.MatchValue(value=item_id))]
                )
            ),
        )
    except Exception as exc:  # noqa: BLE001 - never abort a run over one deletion
        log.warning("Could not delete chunks for item %s: %s", item_id, exc)
```

In `index_document`, update the point id call and the payload:

```python
                id=_point_id(document.item_id, chunk.chunk_index),
```

```python
                payload={
                    "item_id": document.item_id,
                    "source": document.name,
                    "folder_path": document.folder_path,
                    "location": chunk.location,
                    "text": chunk.text,
                    "web_url": document.web_url,
                    "modified_at": document.modified_at.isoformat(),
                    "chunk_index": chunk.chunk_index,
                    "content_type": content_type,
                    "document_type": document_type,
                },
```

- [ ] **Step 4: Propagate item_id through retrieval**

In `app/rag/retrieve.py`, add the field to `RetrievedChunk`:

```python
@dataclass
class RetrievedChunk:
    text: str
    source: str
    location: str
    web_url: str
    score: float
    item_id: str = ""
```

Fix `_match_known_source` — it currently reads `store.get_document_hashes(settings).keys()`, which no longer returns filenames:

```python
def _match_known_source(query: str, settings: Settings) -> Optional[str]:
    """If the query names a specific ingested document, return its exact filename
    so the search can be hard-filtered to it - the most reliable way to satisfy a
    document/client-specific question, independent of vector similarity."""
    known_names = store.get_known_document_names(settings)
    query_lower = query.lower()
    for name in known_names:
        stem = name.rsplit(".", 1)[0].lower()
        if len(stem) >= 6 and stem in query_lower:
            return name
    return None
```

Add `item_id` to the returned chunks:

```python
            RetrievedChunk(
                text=payload.get("text", ""),
                source=payload.get("source", ""),
                location=payload.get("location", ""),
                web_url=payload.get("web_url", ""),
                score=float(score),
                item_id=payload.get("item_id", ""),
            )
```

- [ ] **Step 5: Key cache invalidation on item_id**

In `app/rag/cache.py`, replace the staleness check in `lookup`:

```python
        cached_sources: dict = payload.get("source_hashes", {})
        current_states = store.get_document_states(settings)
        for item_id, cached_tag in cached_sources.items():
            state = current_states.get(item_id)
            if state is None or state.content_tag != cached_tag:
                log.info("Semantic cache entry stale (item '%s' changed or gone), treating as a miss.", item_id)
                return None
```

And in `store_result`:

```python
        current_states = store.get_document_states(settings)
        source_items = {c.item_id for c in chunks if c.item_id}
        source_hashes = {
            item_id: current_states[item_id].content_tag
            for item_id in source_items
            if item_id in current_states
        }
```

Update `record_cache_write(cache_id, query, source_items, settings)` accordingly — it already takes a `Set[str]`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_index_identity.py tests/ -v`
Expected: PASS — the whole suite, confirming no regression in retrieval or cache imports.

- [ ] **Step 7: Commit**

```bash
git add app/rag/index.py app/rag/retrieve.py app/rag/cache.py tests/test_index_identity.py
git commit -m "feat(rag): key chunks on item_id and add delete_document

Point IDs move from filename to source item id, so two same-named files in
different SharePoint subfolders no longer overwrite each other's chunks.

delete_document closes a long-standing gap: there was no way to remove a
document's chunks, so a deleted document lingered forever and a document
that shrank stranded orphan chunks. It is called both on deletion and
before re-indexing a changed document.

Adds keyword payload indexes on item_id and source - required for cheap
delete-by-filter, and it also removes a full scan from the existing
document-name retrieval filter.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 6: The sync engine

**Files:**
- Create: `app/rag/sync.py`
- Rewrite: `app/rag/ingest.py`
- Test: `tests/test_sync.py` (new)

**Interfaces:**
- Consumes: `SourceBatch`/`SourceDocument`/`SourceDeletion`/`get_document_source` (Task 4); `index.delete_document`/`index_document`/`get_client` (Task 5); all `store` functions (Task 3); `chunk_document` (existing).
- Produces: `sync.SyncReport` dataclass (`documents_indexed`, `documents_skipped`, `documents_failed`, `documents_deleted`, `duplicates_skipped`, `chunks_indexed`, `error`), `sync.sync_once(settings, *, full=False, trigger="manual") -> SyncReport`. Settings `rag_sync_max_attempts: int = 3` and `rag_skip_duplicate_content: bool = True` (the scheduler-specific settings arrive in Task 7).

- [ ] **Step 1: Add the two settings the engine needs**

The engine reads these, so they must exist before its tests run. In `app/config.py`, after the Phase 4 semantic cache block:

```python
    # ------------------------------------------- SharePoint delta sync
    # Give up on a document after this many consecutive ingestion failures. It is
    # retried again only once its content changes.
    rag_sync_max_attempts: int = 3
    # SharePoint accumulates "file (1).pdf" copies. Index identical content once so
    # the retriever cannot see the same evidence twice and over-weight it.
    rag_skip_duplicate_content: bool = True
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_sync.py`:

```python
"""Sync engine decision logic. Every collaborator is faked - no network, no LLM."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import get_settings
from app.rag import sync as sync_module
from app.rag.sources import SourceBatch, SourceDeletion, SourceDocument
from app.rag.store import DocumentState


def _doc(item_id="i1", name="a.docx", tag="ctag-1", content_hash="hash-1", payload=b"bytes"):
    return SourceDocument(
        item_id=item_id, name=name, folder_path="ISG/2026", content_tag=tag,
        content_hash=content_hash, modified_at=datetime.now(timezone.utc),
        web_url="https://sp/a.docx", size=10, fetch=lambda: payload,
    )


@pytest.fixture
def harness(monkeypatch):
    """Fakes every boundary the engine touches, and records what it did."""
    calls = {
        "indexed": [], "deleted_qdrant": [], "recorded": [], "failures": [],
        "deleted_pg": [], "saved_link": [], "chunks": None,
    }

    monkeypatch.setattr(sync_module.store, "ensure_schema", lambda s: None)
    monkeypatch.setattr(sync_module.store, "start_ingestion_run", lambda s, trigger="manual": 1)
    monkeypatch.setattr(sync_module.store, "finish_ingestion_run", lambda *a, **k: None)
    monkeypatch.setattr(sync_module.store, "get_sync_state", lambda k, s: None)
    monkeypatch.setattr(sync_module.store, "get_content_hash_owners", lambda s: {})
    monkeypatch.setattr(sync_module.store, "save_sync_state",
                        lambda k, link, full, s: calls["saved_link"].append((link, full)))
    monkeypatch.setattr(sync_module.store, "mark_document_deleted",
                        lambda i, s: calls["deleted_pg"].append(i))
    monkeypatch.setattr(sync_module.store, "record_document",
                        lambda **kw: calls["recorded"].append(kw))
    monkeypatch.setattr(sync_module.store, "record_document_failure",
                        lambda *a, **k: calls["failures"].append(a[0]))
    monkeypatch.setattr(sync_module.index, "get_client", lambda s: object())
    monkeypatch.setattr(sync_module.index, "delete_document",
                        lambda c, coll, i: calls["deleted_qdrant"].append(i))
    monkeypatch.setattr(sync_module.index, "index_document",
                        lambda *a, **k: (calls["indexed"].append(a[2].item_id), 3)[1])
    monkeypatch.setattr(sync_module, "DenseEmbedder", lambda s: object())
    monkeypatch.setattr(sync_module, "SparseEmbedder", lambda: object())
    monkeypatch.setattr(sync_module, "generate_document_identity_summary", lambda *a, **k: "")
    monkeypatch.setattr(sync_module, "chunk_document",
                        lambda *a, **k: calls["chunks"] if calls["chunks"] is not None else ["c1"])
    return calls


def _run(monkeypatch, harness, batch, states=None, **kwargs):
    monkeypatch.setattr(sync_module.store, "get_document_states", lambda s: states or {})
    monkeypatch.setattr(sync_module, "get_document_source", lambda s: _FakeSource(batch))
    return sync_module.sync_once(get_settings(), **kwargs)


class _FakeSource:
    def __init__(self, batch):
        self.batch = batch

    def fetch_changes(self, delta_link):
        return self.batch


def test_new_document_is_indexed(monkeypatch, harness):
    report = _run(monkeypatch, harness, SourceBatch([_doc()], [], "link-1"))
    assert harness["indexed"] == ["i1"]
    assert report.documents_indexed == 1


def test_unchanged_content_tag_does_no_work(monkeypatch, harness):
    """The core cost control: a rename or metadata edit must cost nothing."""
    states = {"i1": DocumentState("i1", "a.docx", "ctag-1", "hash-1", "ok", 0)}
    report = _run(monkeypatch, harness, SourceBatch([_doc(tag="ctag-1")], [], "l"), states)
    assert harness["indexed"] == []
    assert report.documents_skipped == 1


def test_changed_content_tag_reindexes(monkeypatch, harness):
    states = {"i1": DocumentState("i1", "a.docx", "OLD-ctag", "hash-1", "ok", 0)}
    _run(monkeypatch, harness, SourceBatch([_doc(tag="NEW-ctag")], [], "l"), states)
    assert harness["indexed"] == ["i1"]


def test_reindex_deletes_old_chunks_first(monkeypatch, harness):
    """Otherwise a document that shrinks leaves orphan chunks behind."""
    states = {"i1": DocumentState("i1", "a.docx", "OLD", "hash-1", "ok", 0)}
    _run(monkeypatch, harness, SourceBatch([_doc(tag="NEW")], [], "l"), states)
    assert harness["deleted_qdrant"] == ["i1"]


def test_deletion_removes_chunks_and_marks_the_row(monkeypatch, harness):
    report = _run(monkeypatch, harness, SourceBatch([], [SourceDeletion("i9")], "l"))
    assert harness["deleted_qdrant"] == ["i9"]
    assert harness["deleted_pg"] == ["i9"]
    assert report.documents_deleted == 1


def test_delta_link_is_saved_after_processing(monkeypatch, harness):
    _run(monkeypatch, harness, SourceBatch([_doc()], [], "link-42"))
    assert harness["saved_link"] == [("link-42", False)]


def test_failure_is_recorded_and_does_not_abort_the_run(monkeypatch, harness):
    def explode(*a, **k):
        raise RuntimeError("parser blew up")

    monkeypatch.setattr(sync_module, "chunk_document", explode)
    report = _run(monkeypatch, harness, SourceBatch([_doc("i1"), _doc("i2", "b.docx")], [], "l"))
    assert report.documents_failed == 2
    assert harness["failures"] == ["i1", "i2"]


def test_document_with_zero_chunks_still_records_state(monkeypatch, harness):
    """An empty PDF must not be retried on every single sync forever."""
    harness["chunks"] = []
    report = _run(monkeypatch, harness, SourceBatch([_doc()], [], "l"))
    assert report.documents_skipped == 1
    assert [r["item_id"] for r in harness["recorded"]] == ["i1"]


def test_duplicate_content_is_indexed_only_once(monkeypatch, harness):
    monkeypatch.setattr(sync_module.store, "get_content_hash_owners", lambda s: {"hash-1": "i1"})
    report = _run(
        monkeypatch, harness,
        SourceBatch([_doc("i2", "a (1).docx", tag="t2", content_hash="hash-1")], [], "l"),
    )
    assert harness["indexed"] == []
    assert report.duplicates_skipped == 1


def test_permanently_failing_document_is_abandoned_after_max_attempts(monkeypatch, harness):
    settings = get_settings()
    states = {"i1": DocumentState("i1", "a.docx", "", "", "failed", settings.rag_sync_max_attempts)}
    report = _run(monkeypatch, harness, SourceBatch([_doc()], [], "l"), states)
    assert harness["indexed"] == []
    assert report.documents_skipped == 1


def test_lock_not_acquired_means_no_work(monkeypatch, harness):
    from contextlib import contextmanager

    @contextmanager
    def busy(settings, key=None):
        yield False

    monkeypatch.setattr(sync_module.store, "advisory_lock", busy)
    report = _run(monkeypatch, harness, SourceBatch([_doc()], [], "l"))
    assert harness["indexed"] == []
    assert report.documents_indexed == 0


def test_reset_flag_clears_state_before_a_full_run(monkeypatch):
    """The wiped-collection trap: without clearing the stored content tags, the
    next sync concludes 'nothing changed' and a freshly wiped collection stays
    empty forever."""
    from app.config import Settings
    from app.rag import ingest as ingest_module
    from app.rag.sync import SyncReport

    settings = Settings(
        azure_openai_api_key="k", azure_openai_endpoint="https://e/",
        azure_openai_deployment_name="gpt-5.4", rag_enabled=True, rag_source="sharepoint",
    )
    calls = {"reset": [], "full": []}
    monkeypatch.setattr(ingest_module, "get_settings", lambda: settings)
    monkeypatch.setattr(ingest_module.store, "ensure_schema", lambda s: None)
    monkeypatch.setattr(ingest_module.store, "reset_sync_state",
                        lambda key, s: calls["reset"].append(key))
    monkeypatch.setattr(
        ingest_module, "sync_once",
        lambda s, full=False, trigger="manual": (calls["full"].append(full), SyncReport())[1],
    )

    ingest_module.run(full=False, reset=True)

    assert calls["reset"] == ["sharepoint"]
    assert calls["full"] == [True]  # --reset must imply a full re-enumeration
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_sync.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.rag.sync'`

- [ ] **Step 4: Create `app/rag/sync.py`**

```python
"""Incremental ingestion: source changes -> chunk -> embed -> index.

One entry point, sync_once(), used by both the manual CLI and the background
scheduler. It is deliberately trigger-agnostic: if a Graph webhook receiver is
added once the app is publicly hosted, it calls this same function.

The freshness check IS the retry mechanism. A document that fails to ingest never
records its content_tag, so the next run sees it as changed and retries it - no
dead-letter queue, no retry table. attempt_count bounds that so one permanently
broken file cannot retry forever.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..config import Settings, get_settings
from . import index, store
from .chunking import chunk_document
from .embeddings import DenseEmbedder, SparseEmbedder, generate_document_identity_summary
from .sources import SourceDocument, get_document_source

log = logging.getLogger(__name__)


@dataclass
class SyncReport:
    documents_indexed: int = 0
    documents_skipped: int = 0
    documents_failed: int = 0
    documents_deleted: int = 0
    duplicates_skipped: int = 0
    chunks_indexed: int = 0
    error: str = ""

    def summary(self) -> str:
        return (
            f"{self.documents_indexed} indexed ({self.chunks_indexed} chunks), "
            f"{self.documents_skipped} unchanged, {self.duplicates_skipped} duplicates, "
            f"{self.documents_deleted} deleted, {self.documents_failed} failed"
        )


def _needs_ingestion(
    document: SourceDocument, state: Optional[store.DocumentState], settings: Settings
) -> bool:
    if state is None:
        return True
    if state.sync_status == "failed" and state.attempt_count >= settings.rag_sync_max_attempts:
        log.warning(
            "Giving up on %s after %d failed attempts; it will be retried only if its content changes.",
            document.name, state.attempt_count,
        )
        return False
    # An empty stored tag means "never successfully ingested" - always retry.
    if not state.content_tag:
        return True
    return state.content_tag != document.content_tag


def sync_once(
    settings: Settings | None = None, *, full: bool = False, trigger: str = "manual"
) -> SyncReport:
    """Pull changes from the configured source and bring the index up to date."""
    settings = settings or get_settings()
    report = SyncReport()

    if not settings.rag_enabled:
        report.error = "RAG_ENABLED is false."
        log.error(report.error)
        return report

    with store.advisory_lock(settings) as acquired:
        if not acquired:
            log.info("Another instance is already syncing; skipping this run.")
            return report

        store.ensure_schema(settings)
        source_key = settings.rag_source
        run_id = store.start_ingestion_run(settings, trigger=trigger)

        try:
            delta_link = None if full else store.get_sync_state(source_key, settings)
            if full:
                log.info("Running a FULL enumeration (ignoring any saved delta link).")

            source = get_document_source(settings)
            batch = source.fetch_changes(delta_link)

            client = index.get_client(settings)
            collection = settings.qdrant_collection_v2
            dense = DenseEmbedder(settings)
            sparse = SparseEmbedder()

            # Deletions first: a delete-then-recreate in the same batch must end indexed.
            for deletion in batch.deletions:
                index.delete_document(client, collection, deletion.item_id)
                store.mark_document_deleted(deletion.item_id, settings)
                report.documents_deleted += 1

            states = store.get_document_states(settings)
            hash_owners = store.get_content_hash_owners(settings)

            for document in batch.documents:
                if not _needs_ingestion(document, states.get(document.item_id), settings):
                    report.documents_skipped += 1
                    continue

                if (
                    settings.rag_skip_duplicate_content
                    and document.content_hash
                    and hash_owners.get(document.content_hash) not in (None, document.item_id)
                ):
                    log.info(
                        "Skipping %s: identical content already indexed as item %s.",
                        document.name, hash_owners[document.content_hash],
                    )
                    report.duplicates_skipped += 1
                    continue

                try:
                    written = _ingest_one(document, client, collection, dense, sparse, settings)
                except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
                    log.warning("Failed to ingest %s: %s: %s", document.name, type(exc).__name__, exc)
                    store.record_document_failure(
                        document.item_id, document.name, source_key, f"{type(exc).__name__}: {exc}", settings
                    )
                    report.documents_failed += 1
                    continue

                if written == 0:
                    # Recorded anyway, so an unreadable file is not retried every sync.
                    report.documents_skipped += 1
                else:
                    report.documents_indexed += 1
                    report.chunks_indexed += written
                    if document.content_hash:
                        hash_owners.setdefault(document.content_hash, document.item_id)

            if batch.delta_link:
                store.save_sync_state(source_key, batch.delta_link, full, settings)

        except Exception as exc:  # noqa: BLE001 - a sync failure must never reach the app
            report.error = f"{type(exc).__name__}: {exc}"
            log.warning("Sync run failed: %s", report.error)
        finally:
            store.finish_ingestion_run(
                run_id, report.documents_indexed, report.chunks_indexed, report.error, settings
            )

    log.info("Sync complete: %s.", report.summary())
    return report


def _ingest_one(
    document: SourceDocument, client, collection: str, dense, sparse, settings: Settings
) -> int:
    """Chunk, embed and index one document. Returns the number of points written."""
    data = document.fetch()
    chunks = chunk_document(
        document.name,
        data,
        max_tokens=settings.rag_chunk_tokens,
        overlap_tokens=settings.rag_chunk_overlap_tokens,
        settings=settings,
    )

    # Always clear first: a document that shrinks would otherwise strand the
    # chunks beyond its new length, and they would keep surfacing in retrieval.
    index.delete_document(client, collection, document.item_id)

    if not chunks:
        log.info("No text extracted from %s; recording it so it is not retried every run.", document.name)
        store.record_document(
            item_id=document.item_id, name=document.name, folder_path=document.folder_path,
            source_type=settings.rag_source, web_url=document.web_url,
            content_tag=document.content_tag, content_hash=document.content_hash,
            chunk_count=0, settings=settings,
        )
        return 0

    document_identity_vector = None
    try:
        sample_text = "\n\n".join(c.text for c in chunks[:3])
        identity_summary = generate_document_identity_summary(document.name, sample_text, settings)
        if identity_summary:
            document_identity_vector = dense.embed_one(identity_summary)
    except Exception as exc:  # noqa: BLE001 - identity vector is an enhancement, not required
        log.warning("Could not generate a document identity summary for %s: %s", document.name, exc)

    written = index.index_document(
        client, collection, document, chunks, dense, sparse, settings, document_identity_vector
    )
    log.info("Indexed %s (%s): %d chunks.", document.name, document.folder_path or "root", written)

    store.record_document(
        item_id=document.item_id, name=document.name, folder_path=document.folder_path,
        source_type=settings.rag_source, web_url=document.web_url,
        content_tag=document.content_tag, content_hash=document.content_hash,
        chunk_count=written, settings=settings,
    )
    return written
```

- [ ] **Step 5: Rewrite `app/rag/ingest.py` as a thin CLI**

```python
"""Manual ingestion entry point.

    python -m app.rag.ingest              incremental (only what changed)
    python -m app.rag.ingest --full       full enumeration, ignore the delta link
    python -m app.rag.ingest --reset      forget all state, then full re-ingest

Use --reset after wiping the Qdrant collection. Without it the saved content tags
make the next sync conclude 'nothing changed', leaving the collection empty.

The real work lives in app/rag/sync.py, shared with the background scheduler.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..config import get_settings
from . import store
from .sync import sync_once

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
log = logging.getLogger("app.rag.ingest")


def run(full: bool = False, reset: bool = False) -> None:
    settings = get_settings()
    if not settings.rag_enabled:
        log.error("RAG_ENABLED is false. Set RAG_ENABLED=true in .env before ingesting.")
        sys.exit(1)

    if reset:
        store.ensure_schema(settings)
        store.reset_sync_state(settings.rag_source, settings)
        full = True

    report = sync_once(settings, full=full, trigger="manual")
    log.info("Ingestion finished: %s.", report.summary())
    if report.error:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest source documents into the vector index.")
    parser.add_argument("--full", action="store_true",
                        help="Enumerate everything, ignoring the saved delta link.")
    parser.add_argument("--reset", action="store_true",
                        help="Forget all sync state first. Use after wiping the Qdrant collection.")
    args = parser.parse_args()
    run(full=args.full, reset=args.reset)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_sync.py -v`
Expected: PASS (12 tests)

- [ ] **Step 7: Commit**

```bash
git add app/rag/sync.py app/rag/ingest.py tests/test_sync.py
git commit -m "feat(rag): incremental sync engine

sync_once() is the single ingestion path, shared by the CLI and (next task)
the background scheduler, and deliberately trigger-agnostic so a webhook
receiver can call it unchanged once the app is publicly hosted.

Change detection is by content tag, so an unchanged document costs nothing:
no download, no chunking, no embedding, no LLM call. Failures record no
content tag, which makes the next run retry them automatically - the
freshness check doubles as the retry queue, bounded by attempt_count.

ingest.py gains --reset for the wipe-and-rebuild path, without which
retained content tags would leave a freshly wiped collection empty.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 7: Scheduler, configuration and app wiring

**Files:**
- Create: `app/rag/scheduler.py`
- Modify: `app/config.py` (settings + `startup_problems`)
- Modify: `app/main.py:31-55` (lifespan)
- Modify: `.env.example`
- Test: `tests/test_scheduler.py` (new), `tests/test_config_validation.py` (new)

**Interfaces:**
- Consumes: `sync.sync_once` (Task 6).
- Produces: `scheduler.start(app) -> None`, `scheduler.stop() -> None`, `scheduler._run_loop(settings, sleeper, now) -> None` (injectable clock for tests). Settings: `rag_sync_enabled`, `rag_sync_interval_minutes`, `rag_sync_full_reconcile_hours`, `rag_sync_max_attempts`, `rag_skip_duplicate_content`.

- [ ] **Step 1: Add the settings to `app/config.py`**

After the Phase 4 semantic cache block:

```python
    # Background polling that keeps the index fresh. The Graph delta query both
    # enumerates the whole nested folder tree and reports incremental changes;
    # webhooks are deferred until the app has a public HTTPS endpoint.
    # (rag_sync_max_attempts / rag_skip_duplicate_content were added in Task 6.)
    rag_sync_enabled: bool = False
    rag_sync_interval_minutes: int = 15
    # Safety net recommended by Microsoft's scale guidance: re-enumerate
    # periodically so nothing is permanently missed, and retry failed documents.
    rag_sync_full_reconcile_hours: int = 24
```

In `startup_problems()`, after the `rag_enabled` block:

```python
        if self.rag_sync_enabled:
            if not self.rag_enabled:
                problems.append("RAG_SYNC_ENABLED=true but RAG_ENABLED is false - nothing will sync.")
            if self.rag_source == "sharepoint" and not self.pg_enabled:
                problems.append(
                    "RAG_SYNC_ENABLED=true with RAG_SOURCE=sharepoint requires PG_ENABLED=true: "
                    "the delta link and per-document state have nowhere else to live."
                )
            if self.rag_sync_interval_minutes < 1:
                problems.append("RAG_SYNC_INTERVAL_MINUTES must be at least 1.")
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_config_validation.py`:

```python
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
```

Create `tests/test_scheduler.py`:

```python
"""Scheduler cadence. The clock and sync call are injected - no real waiting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.rag import scheduler as scheduler_module


def _settings(**overrides) -> Settings:
    base = dict(
        azure_openai_api_key="k", azure_openai_endpoint="https://e/",
        azure_openai_deployment_name="gpt-5.4", rag_enabled=True, rag_sync_enabled=True,
        rag_sync_interval_minutes=15, rag_sync_full_reconcile_hours=24,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduler_module, "sync_once",
        lambda settings, full=False, trigger="scheduled": calls.append((full, trigger)),
    )
    return calls


def test_first_tick_is_a_full_reconcile(recorded):
    """Nothing is known at startup, so start by enumerating everything."""
    scheduler_module.run_ticks(_settings(), ticks=1, clock=_clock())
    assert recorded == [(True, "reconcile")]


def test_subsequent_ticks_are_incremental(recorded):
    scheduler_module.run_ticks(_settings(), ticks=3, clock=_clock())
    assert recorded[1:] == [(False, "scheduled"), (False, "scheduled")]


def test_reconcile_recurs_after_the_configured_interval(recorded):
    # 15-minute ticks, 1-hour reconcile window -> tick 5 is the next reconcile.
    scheduler_module.run_ticks(
        _settings(rag_sync_full_reconcile_hours=1), ticks=6, clock=_clock()
    )
    assert [t for t, (full, _) in enumerate(recorded) if full] == [0, 5]


def test_a_failing_sync_does_not_stop_the_loop(monkeypatch):
    calls = []

    def explode(settings, full=False, trigger="scheduled"):
        calls.append(trigger)
        raise RuntimeError("qdrant unreachable")

    monkeypatch.setattr(scheduler_module, "sync_once", explode)
    scheduler_module.run_ticks(_settings(), ticks=3, clock=_clock())
    assert len(calls) == 3


def _clock():
    """A clock that advances 15 minutes per call."""
    state = {"now": datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)}

    def clock():
        current = state["now"]
        state["now"] = current + timedelta(minutes=15)
        return current

    return clock
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_config_validation.py tests/test_scheduler.py -v`
Expected: `test_config_validation` FAILs on unknown settings; `test_scheduler` FAILs with `ModuleNotFoundError: No module named 'app.rag.scheduler'`

- [ ] **Step 4: Create `app/rag/scheduler.py`**

```python
"""Background poller that keeps the vector index fresh.

Runs as an asyncio task inside the FastAPI lifespan. Every tick calls sync_once()
in a worker thread so the event loop stays free for requests; the Postgres
advisory lock inside sync_once makes this correct under the 2 gunicorn workers in
startup.sh and under App Service scale-out.

Deployment note: on Azure App Service this needs "Always On", or the app unloads
when idle and stops polling. sync_once() is trigger-agnostic, so moving this to a
separate worker process or WebJob needs no engine change.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from ..config import Settings, get_settings
from .sync import sync_once

log = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None


def _tick(settings: Settings, last_full: Optional[datetime], now: datetime) -> datetime:
    """Run one sync and return the new 'last full reconcile' timestamp."""
    reconcile_after = timedelta(hours=settings.rag_sync_full_reconcile_hours)
    due_for_full = last_full is None or (now - last_full) >= reconcile_after
    try:
        if due_for_full:
            sync_once(settings, full=True, trigger="reconcile")
            return now
        sync_once(settings, full=False, trigger="scheduled")
    except Exception as exc:  # noqa: BLE001 - the loop must outlive any single failure
        log.warning("Scheduled sync failed, continuing: %s: %s", type(exc).__name__, exc)
        if due_for_full:
            # Do not record a successful reconcile that did not happen.
            return last_full if last_full is not None else now - reconcile_after
    return last_full if last_full is not None else now - reconcile_after


def run_ticks(
    settings: Settings, ticks: int, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
) -> None:
    """Run a fixed number of ticks with no waiting. Used by the tests."""
    last_full: Optional[datetime] = None
    for _ in range(ticks):
        last_full = _tick(settings, last_full, clock())


async def _loop(settings: Settings) -> None:
    interval_seconds = settings.rag_sync_interval_minutes * 60
    last_full: Optional[datetime] = None
    log.info(
        "Sync scheduler started: every %d min, full reconcile every %d h.",
        settings.rag_sync_interval_minutes, settings.rag_sync_full_reconcile_hours,
    )
    while True:
        try:
            now = datetime.now(timezone.utc)
            last_full = await asyncio.to_thread(_tick, settings, last_full, now)
        except asyncio.CancelledError:
            log.info("Sync scheduler stopping.")
            raise
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            log.warning("Sync scheduler tick failed: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(interval_seconds)


def start(settings: Settings | None = None) -> None:
    global _task
    settings = settings or get_settings()
    if not settings.rag_sync_enabled:
        log.info("RAG_SYNC_ENABLED is false; the index will only update on manual ingestion.")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(settings))


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
```

- [ ] **Step 5: Wire it into `app/main.py`**

In the `lifespan` function, replace `yield` and the shutdown log with:

```python
    from .rag import scheduler

    scheduler.start(settings)

    yield

    await scheduler.stop()
    log.info("Shutting down.")
```

- [ ] **Step 6: Document the settings in `.env.example`**

Append:

```
# ---------------------------------------------------------- SharePoint sync
# Background polling that keeps the vector index fresh. One Microsoft Graph
# delta query both walks the whole nested folder tree and reports incremental
# changes, so new files, edits and deletions are all picked up automatically.
#
# With RAG_SOURCE=sharepoint this REQUIRES PG_ENABLED=true - the delta link and
# per-document state have nowhere else to live.
#
# On Azure App Service, enable "Always On", or the app unloads when idle and
# stops polling.
RAG_SYNC_ENABLED=false
RAG_SYNC_INTERVAL_MINUTES=15
# Periodic full re-enumeration: catches anything a delta missed and retries
# previously-failed documents. Microsoft recommends no more than once a day.
RAG_SYNC_FULL_RECONCILE_HOURS=24
# Give up on a document after this many consecutive ingestion failures. It is
# retried again only if its content changes.
RAG_SYNC_MAX_ATTEMPTS=3
# SharePoint accumulates "file (1).pdf" copies. Index identical content once so
# the retriever cannot see the same evidence twice.
RAG_SKIP_DUPLICATE_CONTENT=true
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: PASS — the full suite, including the `client` fixture, which now exercises scheduler start/stop through the lifespan.

- [ ] **Step 8: Commit**

```bash
git add app/rag/scheduler.py app/config.py app/main.py .env.example tests/test_scheduler.py tests/test_config_validation.py
git commit -m "feat(rag): background sync scheduler

Polls every 15 minutes with a full reconcile every 24 hours - the safety net
Microsoft's scale guidance recommends even when webhooks are in use. Each
tick runs in a worker thread so the event loop stays free, and the advisory
lock inside sync_once keeps two gunicorn workers from ingesting at once.

Startup validation refuses the silently-broken combinations: sync without
RAG, and SharePoint sync without Postgres to hold the delta link.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 8: Live diagnostics and documentation

**Files:**
- Modify: `test.py` (add `--delta`)
- Modify: `README.md` (SharePoint sync section)
- Modify: `docs/known-gaps-and-backlog.md` (close gap 3.5)

**Interfaces:**
- Consumes: `GraphClient` (Task 2), `SharePointSource` (Task 4).
- Produces: `test.py --delta` CLI flag. No importable API.

- [ ] **Step 1: Add the `--delta` diagnostic to `test.py`**

Add this function before `if __name__ == "__main__":`

```python
def check_delta() -> None:
    """Enumerate the configured SharePoint folder tree via delta, read-only.

    Downloads no file content and spends no Azure OpenAI tokens. Use this to
    confirm access and see exactly what ingestion would pick up.
    """
    from collections import Counter
    from pathlib import Path

    from app.rag.sources import SharePointSource

    s = get_settings()
    if s.rag_source != "sharepoint":
        print(f"[SKIP] RAG_SOURCE is '{s.rag_source}'. Set RAG_SOURCE=sharepoint to check delta.")
        return

    print("--- SharePoint delta enumeration (read-only) ---")
    try:
        source = SharePointSource(s)
        batch = source.fetch_changes(None)
    except Exception as exc:  # noqa: BLE001 - diagnostic output should stay actionable
        _fail("SharePoint delta enumeration", exc)
        return

    by_folder: dict = {}
    for document in batch.documents:
        by_folder.setdefault(document.folder_path or "(root)", []).append(document)

    for folder in sorted(by_folder):
        print(f"\n  {folder}/")
        for document in sorted(by_folder[folder], key=lambda d: d.name):
            print(f"      {document.name}  ({document.size:,} bytes)")

    extensions = Counter(Path(d.name).suffix.lower() or "(none)" for d in batch.documents)
    print(f"\n  Folders: {len(by_folder)}   Ingestable files: {len(batch.documents)}")
    print(f"  By type: {dict(sorted(extensions.items()))}")
    print(f"  Deletions reported: {len(batch.deletions)}")
    _ok(f"Delta enumeration succeeded; deltaLink {'received' if batch.delta_link else 'MISSING'}")
```

Register the flag in the argument parser:

```python
    parser.add_argument(
        "--delta",
        action="store_true",
        help="Enumerate the SharePoint folder tree via delta (read-only, no downloads).",
    )
```

And dispatch it before the SharePoint branch:

```python
    if args.delta:
        check_delta()
    elif args.sharepoint:
        check_sharepoint(download_one=args.sharepoint_download_one)
    else:
        ...
```

- [ ] **Step 2: Run the diagnostic against the live folder**

Run: `./.venv/Scripts/python.exe test.py --delta`
Expected: the nested tree printed, ~113 ingestable files across ~39 folders, `deltaLink received`. `.mp4` and `.zip` must be absent from the type breakdown; `.html` and `.xls` must be present.

- [ ] **Step 3: Document the sync in `README.md`**

Add a `## SharePoint knowledge base` section after `## Updating the knowledge base`:

````markdown
## SharePoint knowledge base

The analyst knowledge base can be read directly from a SharePoint folder instead
of a local directory. Everything under the configured folder is indexed, at any
nesting depth, and the index keeps itself up to date.

### How it stays fresh

A single Microsoft Graph **delta query** does both jobs. The first call walks the
entire nested folder tree and returns a `deltaLink`; that link is stored in
Postgres. Every 15 minutes the app replays it and gets back only what changed —
files added, edited or deleted. When nothing has changed, that is one cheap HTTP
call and no further work.

Costs are avoided aggressively: a document is only re-processed when its content
actually changes (checked via SharePoint's `cTag`), so renaming a file or editing
a metadata column is free. Identical copies — SharePoint's `file (1).pdf` habit —
are indexed once.

### Configuration

```
RAG_SOURCE=sharepoint
RAG_SYNC_ENABLED=true
PG_ENABLED=true          # required: the delta link lives here
```

See `.env.example` for the full list and the SharePoint credentials.

### Commands

```bash
python test.py --delta            # read-only: show exactly what would be indexed
python -m app.rag.ingest          # incremental: only what changed
python -m app.rag.ingest --full   # re-enumerate everything
python -m app.rag.ingest --reset  # forget all state, then re-ingest everything
```

**Use `--reset` after wiping the Qdrant collection.** Without it, the stored
content tags make the next sync conclude "nothing changed" and the collection
stays empty.

### Limits

- Files are read wherever possible. Only audio, video, archives and executables
  are skipped (`.mp4`, `.zip`, …), plus `.doc`/`.ppt`, which have no reliable
  pure-Python reader — save those as `.docx`/`.pptx`.
- Change notifications (webhooks) are not used. They need a publicly reachable
  HTTPS endpoint; polling works from anywhere and is the safety net Microsoft
  recommends regardless.
- On Azure App Service, enable **Always On** or the app unloads when idle and
  stops polling.
````

- [ ] **Step 4: Close gap 3.5 in the backlog**

In `docs/known-gaps-and-backlog.md`, replace the `### 3.5 No chunk deletion path` section body with:

```markdown
### 3.5 No chunk deletion path — RESOLVED 2026-09-23
Closed by the SharePoint delta sync work. `index.delete_document()` removes every
chunk for a source item by payload filter, and `sync.py` calls it both when a
document is deleted at source and immediately before re-indexing a changed one —
so a document that shrinks no longer strands orphan chunks. Identity also moved
from filename to source item id, so same-named files in different folders no
longer overwrite each other.
```

Also remove item 1 from the "Suggested priority" list and renumber.

- [ ] **Step 5: Run the full test suite**

Run: `./.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add test.py README.md docs/known-gaps-and-backlog.md
git commit -m "docs: SharePoint sync guide and read-only delta diagnostic

test.py --delta enumerates the configured folder tree and prints exactly
what ingestion would pick up, without downloading content or spending any
Azure OpenAI tokens.

Closes backlog gap 3.5 (no chunk deletion path), resolved by this work.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Rollout (after all tasks land)

These are live operations, not code. Run in order.

- [ ] **1. Confirm access and inventory:** `python test.py --delta` — expect ~113 files across ~39 folders.
- [ ] **2. Apply the Postgres migration:** `python -c "from app.config import get_settings; from app.rag import store; store.ensure_schema(get_settings())"`
- [ ] **3. Wipe the Qdrant collection:**
```python
from app.config import get_settings
from app.rag.index import get_client
s = get_settings()
get_client(s).delete_collection(s.qdrant_collection_v2)
```
- [ ] **4. Switch the source:** set `RAG_SOURCE=sharepoint` in `.env` (leave `RAG_SYNC_ENABLED=false` for now).
- [ ] **5. Run the first full ingestion:** `python -m app.rag.ingest --reset`
      This is long and makes many LLM calls, as agreed. It is safe to interrupt — restarting skips everything already done.
- [ ] **6. Spot-check retrieval** through the app on a question answerable only from a nested subfolder document.
- [ ] **7. Enable automatic sync:** set `RAG_SYNC_ENABLED=true`, restart.
- [ ] **8. Verify freshness end to end:** upload a small `.docx` to any SharePoint subfolder, wait ~15 minutes, confirm it is retrievable and that the logs show it indexed.
- [ ] **9. Verify deletion:** remove that file in SharePoint, wait ~15 minutes, confirm it stops appearing.
