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
