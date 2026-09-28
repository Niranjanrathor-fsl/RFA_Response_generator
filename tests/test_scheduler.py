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


def _clock():
    """A clock that advances 15 minutes per call."""
    state = {"now": datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)}

    def clock():
        current = state["now"]
        state["now"] = current + timedelta(minutes=15)
        return current

    return clock


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
    # 15-minute ticks from 09:00, 1-hour reconcile window. Tick 0 reconciles at
    # 09:00; tick 4 is 10:00, exactly one hour later, so that is the next one -
    # "every hour" should fire AT the hour, not a tick past it.
    scheduler_module.run_ticks(
        _settings(rag_sync_full_reconcile_hours=1), ticks=6, clock=_clock()
    )
    assert [t for t, (full, _) in enumerate(recorded) if full] == [0, 4]


def test_a_failing_sync_does_not_stop_the_loop(monkeypatch):
    calls = []

    def explode(settings, full=False, trigger="scheduled"):
        calls.append(trigger)
        raise RuntimeError("qdrant unreachable")

    monkeypatch.setattr(scheduler_module, "sync_once", explode)
    scheduler_module.run_ticks(_settings(), ticks=3, clock=_clock())
    assert len(calls) == 3


def test_a_failed_reconcile_is_not_recorded_as_a_successful_one(monkeypatch):
    """sync_once never raises - it returns a report carrying .error. A reconcile
    that failed must not advance last_full, or the next 24h are incremental-only
    while the logs claim the scheduler is healthy."""
    from app.rag.sync import SyncReport

    calls = []

    def failing(settings, full=False, trigger="scheduled"):
        calls.append((full, trigger))
        return SyncReport(error="GraphError: unreachable")

    monkeypatch.setattr(scheduler_module, "sync_once", failing)
    scheduler_module.run_ticks(_settings(), ticks=2, clock=_clock())
    # Tick 0 reconcile failed, so tick 1 must retry the reconcile, not go incremental.
    assert calls == [(True, "reconcile"), (True, "reconcile")]


def test_scheduler_refuses_sharepoint_sync_without_postgres(caplog):
    """Without Postgres there is no delta link and no content tags, so every tick
    re-downloads and re-embeds the entire corpus - an unbounded Azure OpenAI bill
    from one misconfigured variable."""
    scheduler_module.start(
        _settings(rag_source="sharepoint", pg_enabled=False)
    )
    assert scheduler_module._task is None


def test_a_reconcile_skipped_for_the_lock_is_retried(monkeypatch):
    """When another sync (e.g. a manual ingest) holds the lock, the scheduled
    reconcile does nothing. It must not count as done, or the next full
    check - and its sweep of deleted files - waits a whole day."""
    from app.rag.sync import SyncReport

    calls = []

    def locked_out(settings, full=False, trigger="scheduled"):
        calls.append((full, trigger))
        return SyncReport(skipped=True)

    monkeypatch.setattr(scheduler_module, "sync_once", locked_out)
    scheduler_module.run_ticks(_settings(), ticks=2, clock=_clock())
    assert calls == [(True, "reconcile"), (True, "reconcile")]
