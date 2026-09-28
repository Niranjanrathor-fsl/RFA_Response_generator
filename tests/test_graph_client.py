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
