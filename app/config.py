"""Application configuration, loaded entirely from environment variables.

Nothing secret is ever hard-coded or shipped to the browser. See .env.example
for the full list of variables and which are required in which auth mode.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Anchored to the project directory: resolving ".env" against the process
    # CWD means starting uvicorn from elsewhere silently loses every setting.
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------------------------------------------------------------- app
    app_name: str = "Firstsource RFP Response Generator"
    environment: Literal["local", "staging", "production"] = "local"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    # Public base URL of the deployment. Used to build the OIDC redirect URI.
    # Local: http://localhost:8000   Server: https://rfp.firstsource.com
    public_base_url: str = "http://localhost:8000"

    # -------------------------------------------------------- azure openai
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2024-02-01"
    azure_openai_deployment_name: str = ""
    azure_openai_max_tokens: int = 16000
    azure_openai_timeout_seconds: float = 300.0

    # ------------------------------------------------------------- limits
    max_files: int = 20
    max_upload_mb: int = 25
    max_chars_per_document: int = 60_000
    max_total_corpus_chars: int = 140_000
    max_prompt_corpus_chars: int = 48_000
    max_knowledge_chars_per_file: int = 60_000

    # ------------------------------------------------------- RAG (Phase 1)
    # Master switch. Off by default so the app runs unchanged until configured.
    rag_enabled: bool = False
    # "local"     -> read documents from rag_local_source_dir (no Azure AD needed, for testing)
    # "sharepoint" -> read documents from Microsoft Graph
    rag_source: Literal["local", "sharepoint"] = "local"
    rag_local_source_dir: str = "knowledge/rag_sample_docs"

    # Microsoft Graph app-only auth (client credentials flow)
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    sharepoint_site_url: str = ""
    sharepoint_library: str = "Documents"
    sharepoint_folder: str = ""

    # Azure OpenAI embedding deployment (separate from the chat deployment)
    azure_openai_embedding_api_key: str = ""
    azure_openai_embedding_endpoint: str = ""
    azure_openai_embedding_deployment: str = ""
    azure_openai_embedding_api_version: str = "2024-02-01"

    # Qdrant vector database
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    # qdrant_collection: str = "firstsource_analyst_kb"

    # Chunking + retrieval tuning
    rag_chunk_tokens: int = 200
    rag_chunk_overlap_tokens: int = 80
    rag_retrieve_top_k: int = 100
    rag_rerank_top_k: int = 8
    # Reranker scores are raw, unbounded cross-encoder logits (calibrated live:
    # a truly relevant chunk vs. a clearly irrelevant one differ by ~4-5 points).
    # Drop any chunk trailing more than this far behind the best match for the
    # query, in addition to the top_k cap - keeps context tight even within top_k.
    rag_rerank_score_margin: float = 6.0

    # ------------------------------------------------- Phase 4: multi-vector
    # Decouples the vector used for MATCHING from the chunk text passed to the
    # LLM. Each chunk gets several named vectors in Qdrant (see app/rag/index.py):
    # dense_content (full text), dense_summary (short retrieval-optimized proxy),
    # dense_metadata (per-document identity: source/topic/entities), dense_table
    # (conditional, table-heavy chunks only - a natural-language paraphrase).
    qdrant_collection_v2: str = "firstsource_analyst_kb_v2"
    rag_table_chunk_threshold: float = 0.4

    # ------------------------------------------------- Phase 4: semantic cache
    # Caches retrieval results keyed by query-embedding similarity, in a separate
    # small Qdrant collection (avoids depending on a Postgres vector extension on
    # the shared RDS instance). Postgres tracks hit/write audit history only.
    rag_cache_enabled: bool = False
    qdrant_cache_collection: str = "rag_semantic_cache"
    rag_cache_similarity_threshold: float = 0.95
    rag_cache_ttl_hours: int = 168

    # ------------------------------------------- SharePoint delta sync
    # Background polling that keeps the index fresh. The Graph delta query both
    # enumerates the whole nested folder tree and reports incremental changes;
    # webhooks are deferred until the app has a public HTTPS endpoint.
    rag_sync_enabled: bool = False
    rag_sync_interval_minutes: int = 15
    # Safety net recommended by Microsoft's scale guidance: re-enumerate
    # periodically so nothing is permanently missed, and retry failed documents.
    rag_sync_full_reconcile_hours: int = 24
    # Give up on a document after this many consecutive ingestion failures. It is
    # retried again only once its content changes.
    rag_sync_max_attempts: int = 3
    # SharePoint accumulates "file (1).pdf" copies. Index identical content once so
    # the retriever cannot see the same evidence twice and over-weight it.
    rag_skip_duplicate_content: bool = True

    # ------------------------------------------------- Phase 2: PostgreSQL
    # Metadata (documents, ingestion runs) + DeepEval scorecards. Independent
    # of RAG_ENABLED: failures here are logged, never block ingestion or generation.
    pg_enabled: bool = False
    pg_host: str = ""
    pg_port: int = 5432
    pg_dbname: str = ""
    pg_schema: str = "public"
    pg_user: str = ""
    pg_password: str = ""
    pg_sslmode: str = "require"

    # ------------------------------------------------- Phase 3: vision/OCR
    # Describes images, charts and scanned pages as text via the existing
    # gpt-5.4 vision-capable chat deployment - no separate OCR engine, no GPU.
    rag_vision_enabled: bool = True
    # Near-zero sanity floor only (skips literal 1px tracking pixels). Real
    # decorative-vs-meaningful judgment is made by the model itself, not pixel size.
    rag_vision_min_image_px: int = 16
    # Pages with less extracted text than this are treated as likely-scanned and
    # rendered to a whole-page image for vision description instead.
    rag_vision_min_page_chars: int = 30

    # --------------------------------------------------------------- auth
    # "disabled" -> open to anyone who can reach the URL (intranet/VPN only!)
    # "oidc"     -> Azure AD / any OpenID Connect provider (see README)
    auth_mode: Literal["disabled", "oidc"] = "disabled"
    session_secret: str = "change-me-in-production"
    session_cookie_name: str = "fs_rfp_session"
    session_max_age_seconds: int = 60 * 60 * 8
    session_https_only: bool = False

    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid email profile"
    # Optional allow-list, e.g. "firstsource.com". Empty means any successful login.
    oidc_allowed_email_domains: str = ""

    # --------------------------------------------------------------- misc
    cors_allow_origins: str = ""
    embed_fonts_in_html: bool = True

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # -------------------------------------------------------------- paths
    @property
    def base_dir(self) -> Path:
        return BASE_DIR

    @property
    def knowledge_dir(self) -> Path:
        return BASE_DIR / "knowledge"

    @property
    def static_dir(self) -> Path:
        return BASE_DIR / "static"

    @property
    def logo_dir(self) -> Path:
        return self.static_dir / "logos"

    @property
    def font_dir(self) -> Path:
        return self.static_dir / "fonts"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def allowed_email_domains(self) -> List[str]:
        raw = self.oidc_allowed_email_domains or ""
        return [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]

    @property
    def cors_origins(self) -> List[str]:
        raw = self.cors_allow_origins or ""
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def oidc_redirect_uri(self) -> str:
        return self.public_base_url.rstrip("/") + "/auth/callback"

    def startup_problems(self) -> List[str]:
        """Configuration errors worth refusing to boot over (or loudly warning)."""
        problems: List[str] = []
        if not self.azure_openai_api_key:
            problems.append(
                "AZURE_OPENAI_API_KEY is not set - generation requests will fail."
            )
        if not self.azure_openai_endpoint:
            problems.append(
                "AZURE_OPENAI_ENDPOINT is not set - generation requests will fail."
            )
        if not self.azure_openai_deployment_name:
            problems.append(
                "AZURE_OPENAI_DEPLOYMENT_NAME is not set - generation requests will fail."
            )
        if self.rag_enabled:
            if not self.azure_openai_embedding_api_key:
                problems.append(
                    "RAG_ENABLED=true but AZURE_OPENAI_EMBEDDING_API_KEY is not set."
                )
            if not self.azure_openai_embedding_endpoint:
                problems.append(
                    "RAG_ENABLED=true but AZURE_OPENAI_EMBEDDING_ENDPOINT is not set."
                )
            if not self.azure_openai_embedding_deployment:
                problems.append(
                    "RAG_ENABLED=true but AZURE_OPENAI_EMBEDDING_DEPLOYMENT is not set."
                )
            if not self.qdrant_url:
                problems.append("RAG_ENABLED=true but QDRANT_URL is not set.")
            if self.rag_source == "sharepoint":
                for name, value in (
                    ("MS_TENANT_ID", self.ms_tenant_id),
                    ("MS_CLIENT_ID", self.ms_client_id),
                    ("MS_CLIENT_SECRET", self.ms_client_secret),
                    ("SHAREPOINT_SITE_URL", self.sharepoint_site_url),
                ):
                    if not value:
                        problems.append(f"RAG_SOURCE=sharepoint but {name} is not set.")
        if self.rag_sync_enabled:
            if not self.rag_enabled:
                problems.append("RAG_SYNC_ENABLED=true but RAG_ENABLED is false - nothing will sync.")
            if self.rag_source == "sharepoint" and not self.pg_enabled:
                problems.append(
                    "RAG_SYNC_ENABLED=true with RAG_SOURCE=sharepoint requires PG_ENABLED=true: "
                    "the delta link and per-document state have nowhere else to live."
                )
            if self.rag_sync_interval_minutes < 1:
                problems.append("RAG_SYNC_INTERVAL_MINUTES must be at least 1.")
        if self.pg_enabled:
            for name, value in (
                ("PG_HOST", self.pg_host),
                ("PG_DBNAME", self.pg_dbname),
                ("PG_USER", self.pg_user),
            ):
                if not value:
                    problems.append(f"PG_ENABLED=true but {name} is not set.")
        if self.auth_mode == "oidc":
            for name, value in (
                ("OIDC_DISCOVERY_URL", self.oidc_discovery_url),
                ("OIDC_CLIENT_ID", self.oidc_client_id),
                ("OIDC_CLIENT_SECRET", self.oidc_client_secret),
            ):
                if not value:
                    problems.append(f"AUTH_MODE=oidc but {name} is not set.")
        if self.environment == "production":
            if self.session_secret == "change-me-in-production":
                problems.append("SESSION_SECRET must be changed in production.")
            if not self.session_https_only:
                problems.append(
                    "SESSION_HTTPS_ONLY should be true in production (HTTPS cookies)."
                )
            if self.auth_mode == "disabled":
                problems.append(
                    "AUTH_MODE=disabled in production - the app is unauthenticated."
                )
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
