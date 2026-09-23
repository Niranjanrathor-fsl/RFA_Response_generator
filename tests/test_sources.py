"""Delta parsing and local-source equivalence. No network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

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
