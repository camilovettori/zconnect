import logging

from sqlalchemy import create_engine
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

engine = create_engine(
    normalized_database_url,
    connect_args={"check_same_thread": False} if normalized_database_url.startswith("sqlite") else {},
    future=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
