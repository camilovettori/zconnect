import logging

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from .config import get_database_url_log_details, settings

logger = logging.getLogger(__name__)

# The production database is shared with other applications, so Zconnect's Alembic history
# lives in its own version table instead of the generic alembic_version.
ALEMBIC_VERSION_TABLE = "zconnect_alembic_version"

# PostgreSQL connections fail fast instead of hanging startup on an unreachable host.
POSTGRES_CONNECT_TIMEOUT_SECONDS = 10


class DatabaseUnavailableError(RuntimeError):
    """The configured database cannot be reached. Never contains credentials."""


def describe_database_url(database_url: str) -> str:
    """Scheme, host, port and database only: never the user name or password."""
    try:
        details = get_database_url_log_details(database_url)
    except Exception:
        return "scheme=unknown host=unparseable port=None database=None"
    return f"scheme={details['scheme']} host={details['host']} port={details['port']} database={details['database']}"


def _redacted_reason(exc: BaseException, database_url: str) -> str:
    original = getattr(exc, "orig", None) or exc
    reason = (str(original).strip().splitlines() or [type(original).__name__])[0]
    try:
        password = make_url(database_url).password
    except Exception:
        password = None
    if password:
        reason = reason.replace(str(password), "***")
    return f"{type(original).__name__}: {reason}"


def create_validated_engine(database_url: str):
    """Create the engine for the configured database and prove it is reachable.

    There is deliberately no fallback: if the configured database cannot be reached the
    application must not start (a different, empty database would hide existing exports
    and break invoice idempotency)."""
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    else:
        connect_args = {"connect_timeout": POSTGRES_CONNECT_TIMEOUT_SECONDS}
    engine = create_engine(database_url, connect_args=connect_args, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        engine.dispose()
        raise DatabaseUnavailableError(
            f"Configured database is unavailable ({describe_database_url(database_url)}): "
            f"{_redacted_reason(exc, database_url)}. Startup aborted; there is no fallback database."
        ) from None
    return engine


normalized_database_url = settings.normalized_database_url  # raises DatabaseConfigurationError when not allowed
logger.info("Using DATABASE_URL: %s", describe_database_url(normalized_database_url))
engine = create_validated_engine(normalized_database_url)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
