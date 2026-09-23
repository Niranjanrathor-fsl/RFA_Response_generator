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
from typing import Callable, List, Optional, Protocol, Tuple

from ..config import Settings, get_settings
from ..document_sections import REJECTED_EXTENSIONS
from .graph_client import GRAPH_BASE, GraphClient

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
        self._resolved: Optional[Tuple[str, str, str]] = None

    def _resolve(self) -> Tuple[str, str, str]:
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

    def _to_document(self, item: dict, drive_id: str) -> Optional[SourceDocument]:
        name = item.get("name", "")
        if _is_rejected(name):
            log.info("Skipping %s: rejected binary format.", name)
            return None

        if "file" not in item:
            # OneNote sections, shortcuts and bundles carry no downloadable content.
            log.info("Skipping %s: driveItem has no file facet.", name)
            return None

        # Delta responses do NOT include @microsoft.graph.downloadUrl (only the
        # /children collection does), so the authenticated /content endpoint is the
        # real path here; a pre-signed URL is just a fast path when one is offered.
        download_url = item.get("@microsoft.graph.downloadUrl")
        item_id = item["id"]
        if download_url:
            fetch = (lambda url=download_url: self.graph.download(url))
        else:
            fetch = (lambda i=item_id: self.graph.download_item(drive_id, i))

        hashes = (item.get("file") or {}).get("hashes") or {}
        modified_raw = item.get("lastModifiedDateTime", "")
        try:
            modified_at = datetime.fromisoformat(modified_raw.replace("Z", "+00:00"))
        except ValueError:
            modified_at = datetime.now(timezone.utc)

        return SourceDocument(
            item_id=item_id,
            name=name,
            folder_path=self._relative_folder_path(item),
            content_tag=item.get("cTag", "") or item.get("eTag", ""),
            content_hash=hashes.get("quickXorHash", "") or hashes.get("sha256Hash", ""),
            modified_at=modified_at,
            web_url=item.get("webUrl", ""),
            size=int(item.get("size", 0) or 0),
            fetch=fetch,
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
                document = self._to_document(item, drive_id)
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
