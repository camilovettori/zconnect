import os
from pathlib import Path
from typing import Mapping, Optional

from sqlalchemy.engine import make_url
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DATABASE_URL_INVALID_MESSAGE = (
    "Invalid DATABASE_URL format. Expected postgresql+psycopg://... or sqlite:///..."
)
ENVIRONMENTS = {"", "development", "test", "production"}


class DatabaseConfigurationError(RuntimeError):
    """DATABASE_URL is missing, malformed or not allowed in this environment. Never contains credentials."""


def _clean_database_url(raw_url: str) -> str:
    cleaned = raw_url.replace("\r", "").replace("\n", "").strip()
    while len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def normalize_database_url(raw_url: str) -> str:
    url = _clean_database_url(raw_url)

    if not url:
        raise ValueError(DATABASE_URL_INVALID_MESSAGE)

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)

    if url.startswith("postgresql+psycopg://"):
        return url

    if url.startswith("sqlite:///./"):
        relative_path = url.replace("sqlite:///./", "", 1)
        db_path = (Path(__file__).resolve().parents[1] / relative_path).resolve()
        return f"sqlite:///{db_path.as_posix()}"

    if url.startswith("sqlite:///"):
        return url

    raise ValueError(DATABASE_URL_INVALID_MESSAGE)


def get_database_url_log_details(normalized_url: str) -> dict[str, str | int | None]:
    fallback = {
        "scheme": "unknown",
        "host": "unparseable",
        "port": None,
        "database": None,
    }

    try:
        parsed = make_url(normalized_url)
        backend_name = getattr(parsed, "drivername", "") or ""

        if backend_name.startswith("sqlite"):
            database = getattr(parsed, "database", None) or ""
            return {
                "scheme": "sqlite",
                "host": None,
                "port": None,
                "database": database,
            }

        database = getattr(parsed, "database", None)
        return {
            "scheme": backend_name or "unknown",
            "host": getattr(parsed, "host", None) or "unparseable",
            "port": getattr(parsed, "port", None),
            "database": database,
        }
    except Exception:
        return fallback


def is_production(environment: str, environ: Optional[Mapping[str, str]] = None) -> bool:
    """Production when ZCONNECT_ENVIRONMENT=production or when running on Render (RENDER=true)."""
    environ = os.environ if environ is None else environ
    value = (environment or "").strip().lower()
    if value not in ENVIRONMENTS:
        raise DatabaseConfigurationError(
            f"ZCONNECT_ENVIRONMENT={value!r} is not recognised; use production, development or test."
        )
    return value == "production" or (environ.get("RENDER") or "").strip().lower() == "true"


def resolve_database_url(raw_url: str, environment: str = "", environ: Optional[Mapping[str, str]] = None) -> str:
    """The only database URL the application may use. There is no implicit default and no fallback:
    DATABASE_URL must be configured explicitly, and production accepts PostgreSQL only.
    SQLite stays available for local development and tests when configured explicitly."""
    if not _clean_database_url(raw_url or ""):
        raise DatabaseConfigurationError(
            "DATABASE_URL is not set. Production requires a PostgreSQL URL; for local development set "
            "DATABASE_URL=sqlite:///./unify_zoho.db explicitly."
        )
    try:
        normalized = normalize_database_url(raw_url)
    except ValueError:
        raise DatabaseConfigurationError(DATABASE_URL_INVALID_MESSAGE) from None
    if is_production(environment, environ) and not normalized.startswith("postgresql+psycopg://"):
        scheme = get_database_url_log_details(normalized)["scheme"]
        raise DatabaseConfigurationError(
            f"Production requires a PostgreSQL DATABASE_URL (configured scheme: {scheme}). Refusing to start."
        )
    return normalized


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # No default: an unset DATABASE_URL must never silently select a local database.
    DATABASE_URL: str = Field(default="")
    # "production" enables the PostgreSQL-only rule (also implied on Render by RENDER=true).
    ZCONNECT_ENVIRONMENT: str = Field(default="")
    UNIFY_BASE_URL: str = Field(default="https://api.unifyordering.com")
    UNIFY_CLIENT_ID: str = Field(default="")
    UNIFY_CLIENT_SECRET: str = Field(default="")

    ZOHO_BASE_URL: str = Field(default="https://www.zohoapis.eu")
    ZOHO_CLIENT_ID: str = Field(default="")
    ZOHO_CLIENT_SECRET: str = Field(default="")
    ZOHO_REFRESH_TOKEN: str = Field(default="")
    ZOHO_ORG_ID: str = Field(default="")

    UNIFY_DEBUG_SHAPES: bool = Field(default=False)
    UNIFY_DEBUG_MONEY: bool = Field(default=False)
    UNIFY_DEBUG_SAMPLES: bool = Field(default=False)
    # Shared server-to-server secret (Next.js server <-> this API). Never sent to browsers.
    ZCONNECT_BACKEND_SECRET: str = Field(default="")
    ZCONNECT_BACKEND_SECRET_PREVIOUS: str = Field(default="")
    ZCONNECT_ENABLE_API_DOCS: bool = Field(default=False)

    @property
    def normalized_database_url(self) -> str:
        return resolve_database_url(self.DATABASE_URL, self.ZCONNECT_ENVIRONMENT)


settings = Settings()
