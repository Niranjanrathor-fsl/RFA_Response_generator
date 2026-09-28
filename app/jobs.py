"""Background generation jobs.

Azure App Service's front end drops any request that stays open longer than
~230 seconds, and a full RFI takes 10+ minutes. So the browser starts a job,
gets an id back at once, and polls until the result is ready.

Job state lives in small JSON files rather than in memory: gunicorn runs several
worker processes, and a poll can land on a different worker than the one
running the job. All workers share the container's filesystem.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from .config import get_settings

log = logging.getLogger(__name__)

# A running job touches its file this often. When the touches stop, the worker
# running it is gone (a crash, an out-of-memory kill, a restart) - so the browser
# is told within minutes instead of waiting on a job that will never finish.
HEARTBEAT_SECONDS = 20
STALE_AFTER_SECONDS = 120
# Finished jobs are swept after a day.
KEEP_SECONDS = 24 * 60 * 60

# Strong references, or asyncio may garbage-collect a task mid-run.
_tasks: set[asyncio.Task] = set()


def _dir() -> Path:
    configured = get_settings().job_dir
    path = Path(configured) if configured else Path(tempfile.gettempdir()) / "fs-rfp-jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(job_id: str) -> Path:
    return _dir() / f"{job_id}.json"


def _write(job_id: str, record: Dict[str, Any]) -> None:
    target = _path(job_id)
    partial = target.with_name(f"{job_id}.{os.getpid()}.tmp")
    partial.write_text(json.dumps(record), encoding="utf-8")
    os.replace(partial, target)  # atomic: a poll never sees a half-written file


def _sweep() -> None:
    cutoff = time.time() - KEEP_SECONDS
    for path in _dir().glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def start(owner: str, work: Callable[[], Awaitable[Any]]) -> str:
    """Run `work` in the background and return the job id to poll.

    An HTTPException raised by `work` is recorded with its status code and
    detail, so the browser can show the same message a direct request would.
    """
    _sweep()
    job_id = secrets.token_urlsafe(16)
    started = time.time()
    _write(job_id, {"status": "running", "owner": owner, "started": started})

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                os.utime(_path(job_id))
            except OSError:
                pass

    async def run() -> None:
        beat = asyncio.create_task(heartbeat())
        try:
            record = {"status": "done", "result": jsonable_encoder(await work())}
        except HTTPException as exc:
            record = {"status": "error", "status_code": exc.status_code, "detail": exc.detail}
        except Exception:
            log.exception("Generation job %s failed", job_id)
            record = {
                "status": "error",
                "status_code": 500,
                "detail": "Generation failed unexpectedly. Please try again.",
            }
        finally:
            beat.cancel()
        _write(job_id, {**record, "owner": owner, "started": started})

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job_id


def get(job_id: str, owner: str) -> Optional[Dict[str, Any]]:
    """The job's state as the browser sees it, or None if this user has no such job."""
    # The id becomes a filename - accept only what token_urlsafe produces.
    if not job_id or not all(c.isalnum() or c in "-_" for c in job_id):
        return None
    path = _path(job_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        last_beat = path.stat().st_mtime
    except (OSError, ValueError):
        return None
    if record.pop("owner", None) != owner:
        return None

    elapsed = time.time() - record.pop("started", time.time())
    if record["status"] == "running" and time.time() - last_beat > STALE_AFTER_SECONDS:
        return {
            "status": "error",
            "status_code": 500,
            "detail": "This generation was interrupted - the server stopped while working on it. Please run it again.",
        }
    record["elapsed_seconds"] = int(elapsed)
    return record
