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
    settings: Settings,
    ticks: int,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
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
