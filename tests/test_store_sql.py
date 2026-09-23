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
