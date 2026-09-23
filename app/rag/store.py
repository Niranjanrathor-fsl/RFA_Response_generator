"""PostgreSQL metadata store (Phase 2): what's indexed, when, and how well it retrieves.

This is deliberately separate from Qdrant: Qdrant holds vectors for search, this
holds structured bookkeeping - document versions, ingestion run history, and
DeepEval scorecards - for governance and quality tracking.

All calls are best-effort: a Postgres hiccup must never break ingestion or
generation, so every public function catches and logs instead of raising.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Set

from ..config import Settings, get_settings

log = logging.getLogger(__name__)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class DocumentState:
    """What we already know about one source document, for change detection."""

    item_id: str
    name: str
    content_tag: str
    content_hash: str
    sync_status: str
    attempt_count: int


@contextmanager
def _connection(settings: Settings) -> Iterator[Any]:
    import psycopg2

    conn = psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_dbname,
        user=settings.pg_user,
        password=settings.pg_password,
        sslmode=settings.pg_sslmode,
        options=f"-c search_path={settings.pg_schema}",
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQL = """
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

CREATE TABLE IF NOT EXISTS document_versions (
    id SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    document_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    trigger TEXT NOT NULL DEFAULT 'manual',
    error TEXT
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS eval_results (
    id SERIAL PRIMARY KEY,
    eval_run_id INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    metric TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    passed BOOLEAN NOT NULL,
    reason TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS semantic_cache_entries (
    id SERIAL PRIMARY KEY,
    cache_id TEXT NOT NULL UNIQUE,
    query_text TEXT NOT NULL,
    source_documents TEXT DEFAULT '',
    hit_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_hit_at TIMESTAMPTZ
);
"""


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


def ensure_schema(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(MIGRATION_SQL)
        log.info("Postgres schema ready (schema=%s, db=%s).", settings.pg_schema, settings.pg_dbname)
    except Exception as exc:  # noqa: BLE001 - metadata tracking must not break ingestion
        log.warning("Could not ensure Postgres schema, continuing without it: %s", exc)


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


def finish_ingestion_run(
    run_id: Optional[int],
    document_count: int,
    chunk_count: int,
    error: str = "",
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    if not settings.pg_enabled or run_id is None:
        return
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ingestion_runs
                SET finished_at = now(), document_count = %s, chunk_count = %s,
                    status = %s, error = %s
                WHERE id = %s
                """,
                (document_count, chunk_count, "failed" if error else "ok", error, run_id),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not finish ingestion run record: %s", exc)


def start_eval_run(notes: str = "", settings: Settings | None = None) -> Optional[int]:
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return None
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO eval_runs (notes) VALUES (%s) RETURNING id", (notes,))
            return cur.fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not start an eval run record: %s", exc)
        return None


def finish_eval_run(run_id: Optional[int], settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not settings.pg_enabled or run_id is None:
        return
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute("UPDATE eval_runs SET finished_at = now() WHERE id = %s", (run_id,))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not finish eval run record: %s", exc)


def record_eval_result(
    eval_run_id: Optional[int],
    question: str,
    metric: str,
    score: float,
    passed: bool,
    reason: str = "",
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    if not settings.pg_enabled or eval_run_id is None:
        return
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO eval_results (eval_run_id, question, metric, score, passed, reason)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (eval_run_id, question, metric, score, passed, reason),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not record eval result: %s", exc)


def fetch_eval_result_metrics(
    eval_run_id: int, settings: Settings | None = None
) -> Dict[str, Set[str]]:
    """Return saved metric names per question for resuming an eval run safely."""
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return {}
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT question, metric FROM eval_results WHERE eval_run_id = %s",
                (eval_run_id,),
            )
            results: Dict[str, Set[str]] = {}
            for question, metric in cur.fetchall():
                results.setdefault(question, set()).add(metric)
            return results
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch persisted eval results: %s", exc)
        return {}


def record_cache_write(
    cache_id: str, query_text: str, source_documents: Set[str], settings: Settings | None = None
) -> None:
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO semantic_cache_entries (cache_id, query_text, source_documents)
                VALUES (%s, %s, %s)
                ON CONFLICT (cache_id) DO UPDATE
                    SET query_text = EXCLUDED.query_text, source_documents = EXCLUDED.source_documents
                """,
                (cache_id, query_text, ",".join(sorted(source_documents))),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not record cache write: %s", exc)


def record_cache_hit(cache_id: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE semantic_cache_entries
                SET hit_count = hit_count + 1, last_hit_at = now()
                WHERE cache_id = %s
                """,
                (cache_id,),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not record cache hit: %s", exc)


def fetch_eval_scorecard(eval_run_id: int, settings: Settings | None = None) -> Dict[str, Dict[str, float]]:
    """Average score per metric for one eval run - used to print/display a scorecard."""
    settings = settings or get_settings()
    if not settings.pg_enabled:
        return {}
    try:
        with _connection(settings) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT metric, AVG(score), AVG(CASE WHEN passed THEN 1.0 ELSE 0.0 END)
                FROM eval_results WHERE eval_run_id = %s GROUP BY metric
                """,
                (eval_run_id,),
            )
            return {
                metric: {"avg_score": float(avg_score), "pass_rate": float(pass_rate)}
                for metric, avg_score, pass_rate in cur.fetchall()
            }
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch eval scorecard: %s", exc)
        return {}


# ------------------------------------------------------------ sync state
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
    "nothing changed" and the collection stays permanently empty.
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
    # Acquisition is guarded; the yield deliberately is NOT. A yield inside this
    # try would let contextmanager throw the caller's own exception in here, where
    # the except below would mislabel it as a lock failure and the second yield
    # would raise "generator didn't stop after throw()", destroying the traceback.
    try:
        conn = psycopg2.connect(
            host=settings.pg_host, port=settings.pg_port, dbname=settings.pg_dbname,
            user=settings.pg_user, password=settings.pg_password, sslmode=settings.pg_sslmode,
            options=f"-c search_path={settings.pg_schema}",
            # An advisory lock lives as long as its session. If the process is
            # killed mid-ingest, the backend goes idle STILL HOLDING the lock and
            # Postgres will not reap it until TCP gives up - observed live at
            # 1h35m, during which every scheduled sync silently no-ops with
            # "Another instance is already syncing". Keepalives cut that to about
            # two minutes (30s idle, then 3 probes 10s apart).
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            acquired = bool(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001 - never break the caller over the lock
        log.warning("Could not obtain the sync advisory lock: %s", exc)
        acquired = False

    try:
        yield acquired
    finally:
        if conn is not None:
            try:
                if acquired:
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            except Exception:  # noqa: BLE001
                pass  # closing the connection releases it anyway
            conn.close()
