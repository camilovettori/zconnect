from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        url = self.DATABASE_URL.strip()
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)

        if url.startswith("postgresql+psycopg://"):
            return url

        if not url.startswith("sqlite:///./"):
            return url

        relative_path = url.replace("sqlite:///./", "", 1)
        db_path = (Path(__file__).resolve().parents[1] / relative_path).resolve()
        return f"sqlite:///{db_path.as_posix()}"


settings = Settings()
