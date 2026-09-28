"""Standalone diagnostics for the pieces the RAG pipeline depends on.

This is not part of pytest: it makes real network calls and is intended to be
run manually. To check only Microsoft Graph / SharePoint (without spending any
Azure OpenAI tokens), use::

    python test.py --sharepoint

Add ``--sharepoint-download-one`` only when you also want to download one file
into memory and discard it. Secrets are read from ``.env`` and never printed.
"""

from __future__ import annotations

import argparse
from urllib.parse import quote

from app.config import get_settings


def _ok(label: str) -> None:
    print(f"[OK]   {label}")


def _fail(label: str, exc: Exception) -> None:
    print(f"[FAIL] {label}: {exc}")


def check_settings() -> None:
    s = get_settings()
    print("--- Config ---")
    print(f"AZURE_OPENAI_ENDPOINT           = {s.azure_openai_endpoint or '(not set)'}")
    print(f"AZURE_OPENAI_DEPLOYMENT_NAME    = {s.azure_openai_deployment_name or '(not set)'}")
    print(f"AZURE_OPENAI_EMBEDDING_DEPLOYMENT = {s.azure_openai_embedding_deployment or '(not set)'}")
    print(f"QDRANT_URL                      = {s.qdrant_url or '(not set)'}")
    print(f"RAG_ENABLED                     = {s.rag_enabled}")
    print(f"RAG_SOURCE                      = {s.rag_source}")
    print(f"PG_ENABLED                      = {s.pg_enabled}")
    print(f"PG_HOST                         = {s.pg_host or '(not set)'}")
    print(f"PG_DBNAME                       = {s.pg_dbname or '(not set)'}")
    print(f"PG_SCHEMA                       = {s.pg_schema}")


def check_chat_deployment() -> None:
    from app.llm import LLMClient

    try:
        client = LLMClient()
        result = client.generate_document(
            "Reply with strict JSON only.",
            'Return exactly: {"title": "ping", "tabs": [{"name": "Overview"}]}',
        )
        assert result.get("title") == "ping"
        _ok(f"Azure OpenAI chat deployment ({client.model}) responded correctly")
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic script
        _fail("Azure OpenAI chat deployment", exc)


def check_embedding_deployment() -> None:
    from app.rag.embeddings import DenseEmbedder

    try:
        embedder = DenseEmbedder()
        vector = embedder.embed_one("liability cap and insurance coverage")
        _ok(f"Azure OpenAI embedding deployment ({embedder.settings.azure_openai_embedding_deployment}) "
            f"returned a {len(vector)}-dim vector")
    except Exception as exc:  # noqa: BLE001
        _fail("Azure OpenAI embedding deployment", exc)


def check_sparse_embedder() -> None:
    from app.rag.embeddings import SparseEmbedder

    try:
        vec = SparseEmbedder().embed_one("liability cap and insurance coverage")
        _ok(f"BM25 sparse embedder (local, no network) produced {len(vec.indices)} terms")
    except Exception as exc:  # noqa: BLE001
        _fail("BM25 sparse embedder", exc)


def check_qdrant() -> None:
    from app.rag.index import get_client

    try:
        client = get_client()
        collections = client.get_collections().collections
        names = [c.name for c in collections]
        _ok(f"Qdrant reachable, {len(names)} collection(s): {names}")
    except Exception as exc:  # noqa: BLE001
        _fail("Qdrant connection", exc)


def check_postgres() -> None:
    from app.rag import store
    from app.config import get_settings as _gs

    s = _gs()
    if not s.pg_enabled:
        print("[SKIP] Postgres check - PG_ENABLED is false")
        return
    try:
        store.ensure_schema(s)
        run_id = store.start_ingestion_run(s)
        store.finish_ingestion_run(run_id, 0, 0, "", s)
        _ok(f"Postgres reachable (db={s.pg_dbname}, schema={s.pg_schema}), "
            f"schema ensured, test ingestion_runs row id={run_id}")
    except Exception as exc:  # noqa: BLE001
        _fail("Postgres connection", exc)


def check_sharepoint(download_one: bool = False) -> None:
    """Verify the exact Microsoft Graph access chain used by RAG ingestion.

    This checks client-credential authentication, site resolution, library
    discovery and access to the configured folder. It lists metadata only by
    default, so the test is safe to run against a production document library.
    """
    from app.rag.sources import GRAPH_BASE, SharePointSource

    s = get_settings()
    required = {
        "MS_TENANT_ID": s.ms_tenant_id,
        "MS_CLIENT_ID": s.ms_client_id,
        "MS_CLIENT_SECRET": s.ms_client_secret,
        "SHAREPOINT_SITE_URL": s.sharepoint_site_url,
        "SHAREPOINT_LIBRARY": s.sharepoint_library,
        "SHAREPOINT_FOLDER": s.sharepoint_folder,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        print("[SKIP] SharePoint check - configure " + ", ".join(missing) + " in .env")
        return

    print("--- SharePoint / Microsoft Graph ---")
    print(f"Site    = {s.sharepoint_site_url}")
    print(f"Library = {s.sharepoint_library}")
    print(f"Folder  = {s.sharepoint_folder}")

    source = SharePointSource(s)
    try:
        # Token acquisition validates tenant, client ID, secret and consent.
        source._get_token()
        _ok("Microsoft Graph app-only token acquired")

        import httpx

        with httpx.Client(timeout=30.0) as client:
            site_id = source._resolve_site_id(client)
            _ok(f"SharePoint site resolved ({site_id})")

            drive_id = source._resolve_drive_id(client, site_id)
            _ok(f"Document library '{s.sharepoint_library}' resolved ({drive_id})")

            folder = s.sharepoint_folder.strip("/")
            # Quote spaces and other path characters while deliberately preserving
            # path separators: this is the same Graph folder addressed by ingestion.
            folder_path = quote(folder, safe="/")
            response = client.get(
                f"{GRAPH_BASE}/drives/{drive_id}/root:/{folder_path}:/children",
                headers=source._headers(),
                params={"$select": "id,name,file,folder,size,lastModifiedDateTime,webUrl,@microsoft.graph.downloadUrl"},
            )
            response.raise_for_status()
            items = response.json().get("value", [])
            files = [item for item in items if "file" in item]
            folders = [item for item in items if "folder" in item]
            _ok(f"Configured folder is accessible: {len(files)} file(s), {len(folders)} subfolder(s)")
            for item in files[:10]:
                print(
                    f"       file: {item.get('name', '(unnamed)')} "
                    f"({item.get('size', 0)} bytes; modified {item.get('lastModifiedDateTime', 'unknown')})"
                )
            if len(files) > 10:
                print(f"       ... {len(files) - 10} additional file(s) not shown")

            if download_one:
                candidate = next(
                    (item for item in files if item.get("@microsoft.graph.downloadUrl")),
                    None,
                )
                if candidate is None:
                    raise RuntimeError("The folder has no downloadable file to test.")
                # Deliberately retain nothing: this validates the signed download
                # URL used by SharePointSource.list_documents without persisting data.
                file_response = client.get(candidate["@microsoft.graph.downloadUrl"])
                file_response.raise_for_status()
                _ok(
                    f"Downloaded '{candidate.get('name', '(unnamed)')}' into memory "
                    f"({len(file_response.content)} bytes; discarded)"
                )
    except Exception as exc:  # noqa: BLE001 - diagnostic output should stay actionable
        _fail("SharePoint / Microsoft Graph access", exc)


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check external RAG dependencies.")
    parser.add_argument(
        "--delta",
        action="store_true",
        help="Enumerate the SharePoint folder tree via delta (read-only, no downloads).",
    )
    parser.add_argument(
        "--sharepoint",
        action="store_true",
        help="Run only the Microsoft Graph / SharePoint access check.",
    )
    parser.add_argument(
        "--sharepoint-download-one",
        action="store_true",
        help="With --sharepoint, download one file into memory and discard it.",
    )
    args = parser.parse_args()

    if args.delta:
        check_delta()
    elif args.sharepoint:
        check_sharepoint(download_one=args.sharepoint_download_one)
    else:
        check_settings()
        print("\n--- Checks ---")
        check_chat_deployment()
        check_embedding_deployment()
        check_sparse_embedder()
        check_qdrant()
        check_postgres()
        check_sharepoint()
