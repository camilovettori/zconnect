"""Fail-closed database configuration: no implicit default, no SQLite fallback, PostgreSQL only
in production, clear startup failures that never reveal credentials.

Startup is exercised in a separate Python process run from an empty directory (so no .env
file can be picked up). The "unreachable" PostgreSQL is a refused local port: nothing leaves
this machine.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app import db as app_db
from app.config import DatabaseConfigurationError, is_production, resolve_database_url

BACKEND_DIR = Path(__file__).resolve().parents[1]
PASSWORD = "Sup3r-Secret-Pw"
UNREACHABLE = f"postgresql+psycopg://zc_operator:{PASSWORD}@127.0.0.1:1/zconnect"


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, environment, environ, expected_scheme",
    [
        ("sqlite:///./local.db", "", {}, "sqlite"),
        ("sqlite:///./local.db", "development", {}, "sqlite"),
        ("sqlite:///./local.db", "test", {"RENDER": "false"}, "sqlite"),
        (f"postgresql://u:{PASSWORD}@db.example.internal:5432/postgres", "production", {}, "postgresql+psycopg"),
        (f"postgres://u:{PASSWORD}@db.example.internal:5432/postgres", "", {"RENDER": "true"}, "postgresql+psycopg"),
    ],
)
def test_explicit_urls_are_accepted_where_allowed(raw, environment, environ, expected_scheme):
    assert resolve_database_url(raw, environment, environ).startswith(expected_scheme)


@pytest.mark.parametrize(
    "raw, environment, environ, message",
    [
        ("", "", {}, "DATABASE_URL is not set"),
        ("   ", "development", {}, "DATABASE_URL is not set"),
        (f"mysql://u:{PASSWORD}@db.example.internal/x", "", {}, "Invalid DATABASE_URL format"),
        ("sqlite:///./local.db", "production", {}, "Production requires a PostgreSQL DATABASE_URL (configured scheme: sqlite)"),
        ("sqlite:///./local.db", "", {"RENDER": "true"}, "Production requires a PostgreSQL DATABASE_URL"),
        ("sqlite:///./local.db", "Production", {}, "Production requires a PostgreSQL DATABASE_URL"),
        ("", "production", {}, "DATABASE_URL is not set"),
        ("sqlite:///./local.db", "prod", {}, "not recognised"),  # a typo must not relax the rules
    ],
)
def test_missing_invalid_or_disallowed_urls_are_refused(raw, environment, environ, message):
    with pytest.raises(DatabaseConfigurationError) as excinfo:
        resolve_database_url(raw, environment, environ)
    assert message in str(excinfo.value)
    assert PASSWORD not in str(excinfo.value)


def test_render_implies_production():
    assert is_production("", {"RENDER": "true"})
    assert is_production("production", {})
    assert not is_production("", {})
    assert not is_production("development", {"RENDER": "false"})


# ---------------------------------------------------------------------------
# Engine creation: no fallback, redacted errors
# ---------------------------------------------------------------------------


def test_no_fallback_helper_remains():
    assert not hasattr(app_db, "_fallback_sqlite_url")


def test_unreachable_postgres_fails_clearly_without_credentials():
    with pytest.raises(app_db.DatabaseUnavailableError) as excinfo:
        app_db.create_validated_engine(UNREACHABLE)
    message = str(excinfo.value)
    assert "Configured database is unavailable" in message
    assert "host=127.0.0.1 port=1 database=zconnect" in message
    assert "no fallback database" in message
    assert PASSWORD not in message and "zc_operator" not in message
    assert excinfo.value.__cause__ is None  # the driver exception (which may echo the DSN) is not chained


def test_driver_messages_are_redacted():
    exc = OperationalError("SELECT 1", {}, Exception(f'connection to server failed: password "{PASSWORD}" rejected'))
    reason = app_db._redacted_reason(exc, UNREACHABLE)
    assert PASSWORD not in reason and "***" in reason


def test_explicit_sqlite_still_works_for_local_development(tmp_path):
    database = tmp_path / "dev.db"
    engine = app_db.create_validated_engine(f"sqlite:///{database.as_posix()}")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar() == 1
    engine.dispose()
    assert database.exists()


# ---------------------------------------------------------------------------
# Real startup and Alembic in a separate process (empty cwd: no .env can be read)
# ---------------------------------------------------------------------------


def _run(code: str, cwd: Path, **env_overrides: str) -> subprocess.CompletedProcess:
    keep = ("PATH", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env.update(env_overrides)
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


START_APP = "import app.main"
ALEMBIC_CURRENT = (
    "from alembic.config import Config\n"
    "from alembic import command\n"
    "c = Config()\n"
    f"c.set_main_option('script_location', {str(BACKEND_DIR / 'alembic')!r})\n"
    "command.current(c)\n"
)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, "DATABASE_URL is not set"),
        ({"DATABASE_URL": "sqlite:///./would-be-created.db", "RENDER": "true"}, "Production requires a PostgreSQL DATABASE_URL"),
        ({"DATABASE_URL": "sqlite:///./would-be-created.db", "ZCONNECT_ENVIRONMENT": "production"}, "Production requires a PostgreSQL DATABASE_URL"),
        ({"DATABASE_URL": UNREACHABLE}, "Configured database is unavailable"),
        ({"DATABASE_URL": UNREACHABLE, "RENDER": "true"}, "no fallback database"),
    ],
)
@pytest.mark.parametrize("program", [START_APP, ALEMBIC_CURRENT], ids=["app-startup", "alembic"])
def test_startup_and_migrations_fail_closed(tmp_path, env, expected, program):
    result = _run(program, tmp_path, **env)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert expected in output
    assert PASSWORD not in output and "zc_operator" not in output
    assert not list(tmp_path.glob("*.db"))  # no SQLite database was created as a substitute


def test_startup_with_explicit_sqlite_succeeds(tmp_path):
    database = tmp_path / "local-dev.db"
    result = _run(START_APP, tmp_path, DATABASE_URL=f"sqlite:///{database.as_posix()}", ZCONNECT_ENVIRONMENT="development")
    assert result.returncode == 0, result.stderr
    assert database.exists()
