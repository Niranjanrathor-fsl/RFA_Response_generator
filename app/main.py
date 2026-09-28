"""FastAPI application entry point.

Run locally:   uvicorn app.main:app --reload
In Docker:     uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .config import get_settings
from .knowledge import KnowledgeBaseError, get_knowledge_base
from .routes import auth_routes, generate, health

log = logging.getLogger("app")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("%s v%s starting (environment=%s)", settings.app_name, __version__, settings.environment)

    for problem in settings.startup_problems():
        log.warning("CONFIG: %s", problem)

    try:
        knowledge = get_knowledge_base()
        log.info("Knowledge base ready (%d chars).", knowledge.total_chars)
    except KnowledgeBaseError as exc:
        # Fail loudly rather than silently answering without the knowledge base.
        log.error("FATAL: %s", exc)
        raise

    # Background delta sync. No-ops unless RAG_SYNC_ENABLED=true; the Postgres
    # advisory lock inside sync_once keeps multiple workers/instances from
    # ingesting simultaneously.
    from .rag import scheduler

    scheduler.start(settings)

    yield

    await scheduler.stop()
    log.info("Shutting down.")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Turns RFIs, questionnaires and source documents into on-brand "
            "Firstsource deliverables. Self-hosted; the Anthropic API key and the "
            "knowledge base stay server-side."
        ),
        lifespan=lifespan,
    )

    # Session cookie backs the SSO login. Signed with SESSION_SECRET.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie=settings.session_cookie_name,
        max_age=settings.session_max_age_seconds,
        https_only=settings.session_https_only,
        same_site="lax",
    )

    if settings.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(health.router)
    app.include_router(generate.router)
    # Every mode mounts the same router; the routes themselves branch on
    # auth_mode. /auth/me is useful to the frontend regardless.
    app.include_router(auth_routes.router, include_in_schema=settings.auth_mode != "disabled")

    static_dir: Path = settings.static_dir
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(str(static_dir / "index.html"))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        # Keep the standard `detail` shape so the frontend can display it.
        return JSONResponse(
            status_code=422,
            content={
                "detail": "The request could not be understood.",
                "errors": exc.errors(),
            },
        )

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.environment == "local",
    )
