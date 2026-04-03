from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DATABASE_URL_INVALID_MESSAGE = (
    "Invalid DATABASE_URL format. Expected postgresql+psycopg://... or sqlite:///..."
)


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
    parsed = urlparse(normalized_url)
    if normalized_url.startswith("sqlite"):
        database = parsed.path or ""
        return {
            "scheme": "sqlite",
            "host": None,
            "port": None,
            "database": database.lstrip("/"),
        }

    database = parsed.path.lstrip("/") if parsed.path else None
    return {
        "scheme": parsed.scheme or None,
        "host": parsed.hostname,
        "port": parsed.port,
        "database": database,
    }


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DATABASE_URL: str = Field(default="sqlite:///./unify_zoho.db")
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

    @property
    def normalized_database_url(self) -> str:
        return normalize_database_url(self.DATABASE_URL)


settings = Settings()
