"""Manual ingestion entry point.

    python -m app.rag.ingest              incremental (only what changed)
    python -m app.rag.ingest --full       full enumeration, ignore the delta link
    python -m app.rag.ingest --reset      forget all state, then full re-ingest

Use --reset after wiping the Qdrant collection. Without it the saved content tags
make the next sync conclude 'nothing changed', leaving the collection empty.

The real work lives in app/rag/sync.py, shared with the background scheduler.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..config import get_settings
from . import store
from .sync import sync_once

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
log = logging.getLogger("app.rag.ingest")


def run(full: bool = False, reset: bool = False) -> None:
    settings = get_settings()
    if not settings.rag_enabled:
        log.error("RAG_ENABLED is false. Set RAG_ENABLED=true in .env before ingesting.")
        sys.exit(1)

    if reset:
        store.ensure_schema(settings)
        store.reset_sync_state(settings.rag_source, settings)
        full = True

    report = sync_once(settings, full=full, trigger="manual")
    log.info("Ingestion finished: %s.", report.summary())
    if report.error:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest source documents into the vector index.")
    parser.add_argument("--full", action="store_true",
                        help="Enumerate everything, ignoring the saved delta link.")
    parser.add_argument("--reset", action="store_true",
                        help="Forget all sync state first. Use after wiping the Qdrant collection.")
    args = parser.parse_args()
    run(full=args.full, reset=args.reset)
