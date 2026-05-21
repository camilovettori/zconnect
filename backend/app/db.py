import logging
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from .config import get_database_url_log_details, settings

logger = logging.getLogger(__name__)

normalized_database_url = settings.normalized_database_url
try:
    database_url_details = get_database_url_log_details(normalized_database_url)
except Exception:
    database_url_details = {
        "scheme": "unknown",
        "host": "unparseable",
        "port": None,
        "database": None,
    }

logger.info(
    "Using DATABASE_URL: scheme=%s host=%s port=%s database=%s",
    database_url_details["scheme"],
    database_url_details["host"],
    database_url_details["port"],
    database_url_details["database"],
)

def _create_engine_for_url(database_url: str):
    return create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
        future=True,
    )


def _validate_engine(engine):
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def _fallback_sqlite_url() -> str:
    fallback_path = (Path(__file__).resolve().parents[1] / "unify_zoho.db").resolve()
    return f"sqlite:///{fallback_path.as_posix()}"


engine = _create_engine_for_url(normalized_database_url)
try:
    _validate_engine(engine)
except OperationalError as exc:
    if normalized_database_url.startswith("sqlite"):
        raise
    fallback_url = _fallback_sqlite_url()
    logger.warning(
        "Primary DATABASE_URL is unreachable; falling back to local SQLite database=%s reason=%s",
        fallback_url,
        exc,
    )
    engine = _create_engine_for_url(fallback_url)
    _validate_engine(engine)
    logger.info("SQLite fallback database is ready")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
