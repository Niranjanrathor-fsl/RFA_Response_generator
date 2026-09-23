# SharePoint Delta Sync — Design Spec

**Date:** 2026-09-23
**Status:** Approved for planning
**Branch:** `feature/rag-sharepoint-grounding`

---

## 1. Goal

Replace the local-folder knowledge base with the live SharePoint folder, and keep
the vector database automatically fresh.

Two requirements, stated by the user:

1. **Recursive access.** Read *every* file under the configured SharePoint folder,
   at any nesting depth — including folders that do not exist yet. Files sitting
   loose in the top folder count too.
2. **Automatic freshness.** When someone uploads, edits or removes a file in
   SharePoint, the vector database must reflect that without anyone running a
   command.

Success means: a user drops a file into any subfolder of the Analyst folder, and
within ~15 minutes it is retrievable through RAG, with no human action.

---

## 2. Context

### 2.1 What exists today

`app/rag/sources.py` has a `SharePointSource` that authenticates via Microsoft
Graph app-only auth and lists one folder level. It contains the line:

```python
if "file" not in item:
    continue  # skip subfolders; extend here for recursive walk
```

So it is flat-only by construction, and `RAG_SOURCE` is currently set to `local`.
Ingestion is a manual `python -m app.rag.ingest`, which re-embeds every document
on every run.

### 2.2 Live probe results (2026-09-23, read-only)

Run against the real folder using the credentials already in `.env`:

- Folder-scoped delta (`GET /drives/{driveId}/items/{folderId}/delta`) — **supported**.
- **117 files across 39 folders**, nested up to 5 levels
  (`Analyst / Avasant / 2026 / Healthcare / ...`). Analyst-firm folders present:
  Avasant, Everest, Forrester, HFS, ISG, Nelson Hall, plus `old and new RFP`.
- Extension histogram: `.pdf` 34, `.pptx` 27, `.docx` 24, `.xlsx` 18, `.html` 4,
  `.mp4` 3, `.md` 3, `.txt` 1, `.xls` 1, `.png` 1, `.zip` 1.
- `cTag` present on every file. `parentReference.path` **is** populated on this
  tenant (the API reference warns it may not be — we therefore use it for display
  only, never for identity).
- `file.hashes.quickXorHash` present — usable for duplicate detection at no cost.
- Exact-duplicate pairs exist, e.g. `..._GCOO_RFI.pdf` and `..._GCOO_RFI (1).pdf`
  at identical sizes.
- Individual files reach ~26 MB (PowerPoint decks).

This is roughly 7x the 16-file sample corpus currently indexed.

### 2.3 Deployment reality

The app runs locally only; an Azure App Service deployment is planned. Microsoft
Graph **webhooks are therefore out of scope** — they require a publicly reachable
HTTPS endpoint. Polling is the trigger mechanism. This is not a compromise:
Microsoft's own scale guidance recommends a periodic delta poll *even when*
webhooks are in use.

---

## 3. Approach

Use the Microsoft Graph **delta query** as the single mechanism serving both
requirements, following Microsoft's documented pattern
(*Discover → Crawl → Notify → Process changes*), with polling in place of
webhook notification.

`GET /drives/{driveId}/items/{folderId}/delta` enumerates the entire nested
hierarchy in one paged call and returns an `@odata.deltaLink`. Replaying that
link later returns **only** what changed since, including deletions.

Delta is not merely convenient. The API reference states that paging the
`children` collection is **not guaranteed to return every item** if writes occur
during enumeration, and that delta must be used to maintain a full local
representation. A hand-rolled recursive crawl would be the one design capable of
silently losing a file — precisely the failure this work exists to prevent.

---

## 4. Design

### 4.1 Document identity

Going recursive makes filenames unsafe as identity: two files in different
subfolders can share a name and would overwrite each other's chunks. Identity
moves to the Graph `driveItem` id, which also survives renames and moves.

```python
@dataclass
class SourceDocument:
    item_id: str       # stable identity: Graph driveItem id (relative path for local source)
    name: str          # display/citation only
    folder_path: str   # relative to the configured root, for display/citation
    content_tag: str   # cTag (SharePoint) / sha256 (local) - "did the CONTENT change?"
    content_hash: str  # quickXorHash (SharePoint) / sha256 (local) - duplicate detection
    modified_at: datetime
    web_url: str
    size: int
    fetch: Callable[[], bytes]   # lazy: called only if we decide to ingest


@dataclass
class SourceDeletion:
    item_id: str
```

Three properties this buys:

- **`item_id`** eliminates the same-filename collision and survives renames/moves.
- **`content_tag`** uses `cTag`, which changes only on *content* edits. Renaming a
  file or editing a SharePoint metadata column costs zero LLM calls.
- **`fetch` is lazy.** A sync that finds nothing new downloads nothing. The current
  `list_documents` eagerly downloads every file in the folder merely to inspect it.

`LocalFolderSource` implements the same interface (relative path as `item_id`,
sha256 as both tags), so the engine stays testable offline and the two sources
remain interchangeable by config, as today.

### 4.2 Graph transport — `app/rag/graph_client.py` (new)

Auth and HTTP move out of `sources.py`, which then holds only source semantics.
This fixes two live defects:

- The access token is cached in `self._token` and **never refreshed**. Any
  ingestion running past the token lifetime (~1 hour) fails with 401 — a certainty
  with a 117-file corpus.
- There is **no 429/503 handling**. SharePoint throttles per-tenant, and
  Microsoft's guidance is explicit that ignoring `Retry-After` leads to apps being
  blocked for abusive calling patterns.

Responsibilities:

- token acquisition with expiry-aware caching and refresh
- `get_json(url)` honouring `Retry-After` on 429/503, with bounded exponential backoff
- `paginate(url)` following `@odata.nextLink`
- `download(url) -> bytes`
- site / drive / folder-item resolution, cached per process

### 4.3 File-type policy

Per the user's instruction: read everything we reasonably can; reject only `.mp4`
and `.zip`. The current fixed allow-list is replaced by a three-tier policy.

**Tier 1 — explicit rejects** (skipped, logged, never retried): audio, video,
archive and executable binaries — `.mp4`, `.zip`, `.mov`, `.avi`, `.wmv`, `.mp3`,
`.wav`, `.7z`, `.rar`, `.tar`, `.gz`, `.exe`, `.dll`, `.msi`, `.iso`.

**Tier 2 — explicit handlers:** all current handlers (`.pdf`, `.docx`, `.pptx`,
`.xlsx`, `.csv`, `.tsv`, `.json`, text, images) **plus two new ones**:

- **`.html` / `.htm`** — new `extract_html_sections()`. Strips `<script>` and
  `<style>` entirely, then extracts visible text, via `beautifulsoup4` using
  Python's built-in `html.parser` (pure Python, no C toolchain).
- **`.xls`** — new `extract_xls_sections()` via `xlrd` (v2.x reads `.xls` only,
  which is exactly the gap). `.xls` is removed from `LEGACY_EXTENSIONS`.

`.doc` and `.ppt` remain unsupported — no reliable pure-Python reader exists and
neither appears in the corpus. In the *ingestion* path they are skipped with a log;
in the *interactive upload* path they keep today's helpful "save as .docx" error,
which is the right UX there.

**Tier 3 — unknown extensions:** sniff the bytes. If the content decodes as
predominantly printable text, treat it as plain text; otherwise skip and log. This
makes future file types (`.xml`, `.yaml`, `.rst`, …) work without a code change
instead of being silently dropped.

### 4.4 Duplicate handling

SharePoint supplies `quickXorHash` free of charge. Two files with identical content
hashes are indexed once; later copies are recorded in Postgres as duplicates of the
first and skipped. This stops the retriever seeing the same evidence twice and
inflating its apparent corroboration.

Controlled by `RAG_SKIP_DUPLICATE_CONTENT` (default `true`) so it is reversible.

### 4.5 Sync engine — `app/rag/sync.py` (new)

One public entry point:

```python
def sync_once(settings: Settings, *, full: bool = False) -> SyncReport
```

1. Acquire a **Postgres advisory lock** on a dedicated held-open connection. If
   already held, another worker or instance is syncing — return immediately.
2. Load the saved `delta_link` from `sync_state` (skipped when `full=True`).
3. Call delta. On `410 Gone` / resync-required, discard the token and restart a
   full enumeration from the response's `Location` header.
4. Page through `@odata.nextLink`, collecting upserts and deletions. Folder items
   are recorded for path reconstruction but never ingested.
5. **Deletions** → delete that item's points from Qdrant; mark deleted in Postgres.
6. **Upserts** → if the stored `content_tag` matches, **skip entirely** (no
   download, no chunking, no embedding, no LLM). Otherwise: `fetch()` →
   `chunk_document` → **delete this item's existing points** → `index_document` →
   record the new `content_tag` and `content_hash`.
7. Persist the new `delta_link`.
8. Write an `ingestion_runs` row, extended with a `trigger` column
   (`manual` / `scheduled` / `reconcile`).

Step 6's delete-before-index closes backlog gap 3.5: a document shrinking from 40
chunks to 20 no longer strands 20 orphaned chunks in Qdrant.

**Failure handling — the retry mechanism is the freshness check.** A document that
fails to ingest never records its `content_tag`, so the next run naturally sees it
as changed and retries it. No dead-letter queue, no retry table. `attempt_count`
bounds this so one permanently-broken file cannot retry forever; on exceeding
`RAG_SYNC_MAX_ATTEMPTS` it is marked `failed` and skipped until its content changes.

This follows the established `app/rag/` convention: every failure caught and
logged, never raised — one bad file must not abort a 117-file run.

### 4.6 Scheduler — `app/rag/scheduler.py` (new)

An asyncio task started from the existing `main.py` lifespan when
`RAG_SYNC_ENABLED=true`:

- every `RAG_SYNC_INTERVAL_MINUTES` (default **15**) → `sync_once(full=False)`,
  executed in a worker thread so the event loop stays free
- every `RAG_SYNC_FULL_RECONCILE_HOURS` (default **24**) → `sync_once(full=True)` —
  Microsoft's recommended safety net; catches anything delta missed and sweeps up
  previously-failed documents
- never propagates an exception into the application
- the advisory lock makes it correct under the 2 gunicorn workers in `startup.sh`
  and under App Service scale-out

`python -m app.rag.ingest` remains the manual entry point, delegating to
`sync_once(full=True)`.

**Deployment prerequisite:** an in-process scheduler requires App Service
*Always On*; without it the app unloads when idle and stops polling. Not an issue
locally. If it becomes one, `sync_once()` is trigger-agnostic and moves to a
separate worker process or WebJob with no engine change.

### 4.7 Data model changes

**Qdrant** (`firstsource_analyst_kb_v2`, wiped and rebuilt in place per user decision):

- point id: `uuid5(f"{item_id}::{chunk_index}")` (was `{name}::{chunk_index}`)
- payload gains `item_id` and `folder_path`
- `source` payload keeps the **filename**, so citations, `format_grounding_block`
  and `_match_known_source` are unaffected
- **new payload indexes** on `item_id` and `source` — required for efficient
  delete-by-filter, and they also remove a full scan from the existing
  `_match_known_source` document filter in `retrieve.py`

**PostgreSQL** — `documents` gains `item_id`, `folder_path`, `content_tag`,
`content_hash`, `sync_status`, `attempt_count`, `last_error`, `deleted_at`. The
unique key moves from `(name, source_type)` to `(item_id, source_type)`. New table
`sync_state(source_key, delta_link, last_synced_at, last_full_sync_at)`.

`ensure_schema()` uses `CREATE TABLE IF NOT EXISTS`, which will not alter the
existing `documents` table. Migration uses idempotent
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `DROP CONSTRAINT IF EXISTS` and a new
unique constraint, all inside the existing best-effort wrapper.

**Semantic cache** — `RetrievedChunk` gains `item_id`, and cache invalidation keys
on `item_id` rather than filename. Without this, duplicate filenames across folders
make invalidation ambiguous.

---

## 5. Configuration

```
RAG_SOURCE=sharepoint
RAG_SYNC_ENABLED=true
RAG_SYNC_INTERVAL_MINUTES=15
RAG_SYNC_FULL_RECONCILE_HOURS=24
RAG_SYNC_MAX_ATTEMPTS=3
RAG_SKIP_DUPLICATE_CONTENT=true
```

`startup_problems()` gains validation: `RAG_SYNC_ENABLED=true` requires
`RAG_ENABLED=true` and, for the SharePoint source, `PG_ENABLED=true` — the delta
link and per-document state have nowhere else to live.

New dependencies: `beautifulsoup4` (HTML) and `xlrd` (legacy `.xls`). Both pure
Python, pinned in the existing style.

---

## 6. Testing

**Unit** (no network, no tokens, no Azure spend) — against recorded Graph JSON
fixtures captured from the live probe:

- delta page parsing: files vs folders, `@odata.nextLink` pagination, `deleted` facet
- `content_tag` skip logic (an unchanged file performs zero work)
- `410 Gone` resync path
- `Retry-After` backoff on 429/503
- token refresh on expiry
- duplicate detection by content hash
- file-type policy: rejects, new `.html` / `.xls` handlers, unknown-extension sniffing
- chunk deletion on document delete and on document shrink

**Offline integration** — `LocalFolderSource` on the same interface means the whole
engine runs against `knowledge/rag_sample_docs/` with files added, edited and
deleted, exercising every delta path without touching SharePoint.

**Live** — extend `test.py` with a `--delta` check performing one read-only
enumeration that prints the tree without ingesting (the probe, made permanent).

---

## 7. Migration and rollout

1. Merge the code with `RAG_SYNC_ENABLED=false`, `RAG_SOURCE=local` — no behaviour change.
2. Apply Postgres migrations via `ensure_schema()`.
3. Run `test.py --delta` to confirm live access and the file inventory.
4. Wipe `firstsource_analyst_kb_v2`.
5. Switch `RAG_SOURCE=sharepoint` and run `python -m app.rag.ingest` manually for the
   full first ingestion. The user has accepted its cost and duration.
6. Spot-check retrieval, then set `RAG_SYNC_ENABLED=true`.
7. Verify freshness end to end: upload a file to a SharePoint subfolder and confirm
   it becomes retrievable within ~15 minutes.

---

## 8. Non-goals

- **Graph webhooks / change notifications** — require a public HTTPS endpoint the
  deployment does not yet have. The engine is trigger-agnostic, so adding a webhook
  receiver later is additive, not a rewrite.
- **Ingestion cost controls** (per-chunk summary skipping, vision caps) — the user
  explicitly chose full quality over cost for the first run. Tracked as backlog item 3.4.
- **Per-question retrieval, structured citations, template-aware output** — separate
  backlog items, unrelated to this work.
- **`.doc` / `.ppt` support** — no reliable pure-Python reader; absent from the corpus.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| First ingestion is long and expensive (117 files, 26 MB decks, per-chunk LLM calls) | Accepted by the user. Progress logged per document. Interruptible and resumable: `content_tag` state means a restart skips everything already done. |
| Large file exhausts memory (26 MB PPTX held in memory plus per-image vision) | Documents processed strictly one at a time; bytes released after chunking. |
| App Service *Always On* not enabled | Documented as a deployment prerequisite; `sync_once()` can move to a separate worker with no engine change. |
| Graph throttling during the first bulk run | `Retry-After` honoured with bounded backoff; run recommended off-peak per Microsoft guidance. |
| `parentReference.path` absent on some items | Used for display only; identity is always `item_id`. |
| Postgres unavailable mid-sync | Advisory lock unobtainable → sync no-ops and logs; generation and retrieval are unaffected. |

---

## 10. Success criteria

1. Every readable file under the Analyst folder, at any depth, is indexed — **113 of
   117**. The only rejects are the 3 `.mp4` and 1 `.zip`, logged by name and reason.
   (`.html` ×4 and `.xls` ×1 are explicitly in scope per the user's instruction.)
2. Two files with the same name in different subfolders are both retrievable and do
   not overwrite each other.
3. A file uploaded to any subfolder is retrievable within ~15 minutes, unattended.
4. A file deleted in SharePoint stops appearing in retrieval results.
5. A sync where nothing changed makes one cheap Graph call: no downloads, no
   embeddings, no LLM calls.
6. Renaming a file, or editing a SharePoint metadata column, triggers no re-ingestion.
