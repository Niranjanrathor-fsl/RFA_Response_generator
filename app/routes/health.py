"""Health and metadata endpoints (used by load balancers and the frontend)."""

from __future__ import annotations

from fastapi import APIRouter

from .. import __version__
from ..config import get_settings
from ..extract import SUPPORTED_EXTENSIONS
from ..generators import available_formats
from ..knowledge import get_knowledge_base

router = APIRouter(tags=["meta"])

# Human-readable groups, most common first. Kept in sync with
# app.extract.SUPPORTED_EXTENSIONS by test_config_display_matches_supported.
ACCEPTED_DISPLAY = [
    "PDF",
    "Word (.docx)",
    "PowerPoint (.pptx)",
    "Excel (.xlsx, .xls)",
    "Images (.png, .jpg, .gif, .webp, .bmp)",
    "Web pages (.html)",
    "Text (.txt, .md, .csv, .tsv, .json)",
]


@router.get("/healthz", response_model=None)
def healthz() -> dict:
    """Liveness probe. Never touches the Anthropic API."""
    settings = get_settings()
    try:
        kb_chars = get_knowledge_base().total_chars
        kb_ok = kb_chars > 0
    except Exception:  # noqa: BLE001 - health must not raise
        kb_chars, kb_ok = 0, False
    return {
        "status": "ok" if kb_ok else "degraded",
        "version": __version__,
        "environment": settings.environment,
        "knowledge_base_loaded": kb_ok,
        "knowledge_base_chars": kb_chars,
        "model_configured": bool(settings.azure_openai_api_key and settings.azure_openai_endpoint),
    }


@router.get("/api/config", response_model=None)
def client_config() -> dict:
    """Non-sensitive settings the browser needs. No keys, no knowledge base."""
    settings = get_settings()
    return {
        "app_name": settings.app_name,
        "version": __version__,
        "auth_mode": settings.auth_mode,
        "formats": available_formats(),
        "max_files": settings.max_files,
        "max_upload_mb": settings.max_upload_mb,
        "accepted_extensions": sorted(SUPPORTED_EXTENSIONS),
        # Grouped for display. The UI used to show the alphabetically-first eight
        # extensions, which told a user nothing useful.
        "accepted_display": ACCEPTED_DISPLAY,
    }
