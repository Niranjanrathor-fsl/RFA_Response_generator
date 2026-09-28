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


def test_advisory_lock_does_not_swallow_errors_from_its_body(monkeypatch):
    """The yield must sit OUTSIDE the try that handles lock acquisition, or a
    failure inside the `with` block is caught by that except, reported as a bogus
    lock error, and turned into RuntimeError('generator didn't stop after
    throw()') - destroying the original traceback.

    Needs PG_ENABLED=true to reach that code path at all; the disabled path
    returns before the try.
    """
    import pytest

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a):
            pass

        def fetchone(self):
            return (True,)  # lock acquired

    class _Conn:
        autocommit = False

        def cursor(self):
            return _Cursor()

        def close(self):
            pass

    monkeypatch.setattr("psycopg2.connect", lambda **kw: _Conn())
    settings = get_settings().model_copy(update={"pg_enabled": True})

    with pytest.raises(ValueError, match="failure inside the body"):
        with store.advisory_lock(settings) as acquired:
            assert acquired is True
            raise ValueError("failure inside the body")


def test_advisory_lock_connection_enables_tcp_keepalives(monkeypatch):
    """A killed ingest leaves its Postgres backend idle and STILL HOLDING the
    lock until TCP reaps it - observed live at 1h35m, during which every
    scheduled sync silently no-ops. Keepalives let Postgres notice a genuinely
    dead client, while staying slack enough not to tear down a healthy but idle
    connection (the first attempt at 30s/10s/3 was too aggressive and the lock
    was lost mid-ingest). The heartbeat below is what actually keeps it alive.
    """
    captured = {}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a):
            pass

        def fetchone(self):
            return (True,)

    class _Conn:
        autocommit = False

        def cursor(self):
            return _Cursor()

        def close(self):
            pass

    def _connect(**kwargs):
        captured.update(kwargs)
        return _Conn()

    monkeypatch.setattr("psycopg2.connect", _connect)
    settings = get_settings().model_copy(update={"pg_enabled": True})

    with store.advisory_lock(settings):
        pass

    assert captured.get("keepalives") == 1
    assert captured.get("keepalives_idle") == 60
    assert captured.get("keepalives_interval") == 15
    assert captured.get("keepalives_count") == 5


def test_advisory_lock_heartbeats_so_the_session_is_never_idle(monkeypatch):
    """Keepalives alone were not enough. Observed live: during a multi-hour
    ingest the lock connection sat idle and was dropped, so pg_locks showed ZERO
    holders while the ingest was still running - leaving nothing to stop a second
    sync starting. A periodic query keeps the session genuinely active.
    """
    import threading

    queries = []
    seen_during_body = threading.Event()

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, *a):
            queries.append(sql)
            if "SELECT 1" in sql:
                seen_during_body.set()

        def fetchone(self):
            return (True,)

    class _Conn:
        autocommit = False

        def cursor(self):
            return _Cursor()

        def close(self):
            pass

    monkeypatch.setattr("psycopg2.connect", lambda **kw: _Conn())
    monkeypatch.setattr(store, "_LOCK_HEARTBEAT_SECONDS", 0.05)
    settings = get_settings().model_copy(update={"pg_enabled": True})

    with store.advisory_lock(settings) as acquired:
        assert acquired is True
        assert seen_during_body.wait(timeout=5), "no heartbeat query ran while the lock was held"

    assert any("SELECT 1" in q for q in queries)


def test_a_lock_held_by_a_dead_session_is_reclaimed(monkeypatch):
    """Observed live: a network blip left the finished ingest's lock session
    idle on the server for 100 minutes, and every scheduled sync skipped. A live
    holder heartbeats every minute, so a holder idle far longer than that is dead:
    terminate it and take the lock."""
    queries = []
    attempts = {"n": 0}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, *a):
            queries.append(sql)

        def fetchone(self):
            if "pg_try_advisory_lock" in queries[-1]:
                attempts["n"] += 1
                return (attempts["n"] > 1,)  # held on the first try, free after the reap
            return (True,)

        def fetchall(self):
            return [(True,)]

    class _Conn:
        autocommit = False

        def cursor(self):
            return _Cursor()

        def close(self):
            pass

    monkeypatch.setattr("psycopg2.connect", lambda **kw: _Conn())
    settings = get_settings().model_copy(update={"pg_enabled": True})

    with store.advisory_lock(settings) as acquired:
        assert acquired is True

    reap = next(q for q in queries if "pg_terminate_backend" in q)
    assert "idle" in reap and "state_change" in reap
    assert attempts["n"] == 2
