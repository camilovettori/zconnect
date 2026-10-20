"""Release tooling: tools/schema_preflight.py (read-only) and tools/run_migration.py (guarded).

The refusal rules run everywhere with the database inspection stubbed out. The PostgreSQL
tests are skipped unless ZCONNECT_TEST_POSTGRES_URL points to a disposable local server
(see test_postgres_concurrency.py); each creates and drops its own scratch database.
"""
from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import settings
from app.db import Base
from app.main import MIGRATION_ONLY_TABLES

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
BACKEND_DIR = TOOLS_DIR.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = _load("schema_preflight")
runner = _load("run_migration")

POSTGRES_URL = os.environ.get("ZCONNECT_TEST_POSTGRES_URL", "")
needs_postgres = pytest.mark.skipif(not POSTGRES_URL, reason="set ZCONNECT_TEST_POSTGRES_URL to a disposable local PostgreSQL")
TARGET = "postgresql+psycopg://operator:s3cret-password@db.example.internal:5432/postgres"
UNVERSIONED = {"alembic_version": None, "export_events_exists": False, "row_counts": {"settings": 1}}


# ---------------------------------------------------------------------------
# Target resolution and refusal rules (no database needed)
# ---------------------------------------------------------------------------


def test_target_comes_only_from_the_environment(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(preflight.PreflightError, match="not set"):
        preflight.target_from_env()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./unify_zoho.db")
    with pytest.raises(preflight.PreflightError, match="PostgreSQL"):
        preflight.target_from_env()
    monkeypatch.setenv("DATABASE_URL", TARGET)
    url, target = preflight.target_from_env()
    assert url.startswith("postgresql://")
    assert target == {"host": "db.example.internal", "port": 5432, "database": "postgres", "user": "operator"}
    assert "s3cret" not in repr(target)


def test_preflight_cli_never_prints_the_password(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", TARGET)
    assert preflight.main(["target"]) == 0
    out = capsys.readouterr()
    assert "db.example.internal" in out.err
    assert "s3cret" not in out.out + out.err


@pytest.fixture()
def stub_runner(monkeypatch):
    """Runner with inspection and Alembic stubbed: records what would have run."""
    calls = {"inspect": 0, "stamp": [], "upgrade": []}
    state = {"info": dict(UNVERSIONED), "matched": "reference_createall"}

    def fake_inspect(url):
        calls["inspect"] += 1
        return dict(state["info"]), state["matched"]

    monkeypatch.setenv("DATABASE_URL", TARGET)
    monkeypatch.setattr(settings, "DATABASE_URL", TARGET)
    monkeypatch.setattr(runner, "_inspect", fake_inspect)
    monkeypatch.setattr("alembic.command.stamp", lambda cfg, rev: calls["stamp"].append(rev))
    monkeypatch.setattr("alembic.command.upgrade", lambda cfg, rev: calls["upgrade"].append(rev))
    return calls, state


def args(*extra):
    return ["--expected-host", "db.example.internal", "--expected-database", "postgres", *extra]


def test_runner_refuses_without_explicit_database_url(stub_runner, monkeypatch, capsys):
    calls, _state = stub_runner
    monkeypatch.delenv("DATABASE_URL")
    assert runner.run(args("--backup-verified", "stamp")) == 2
    assert calls == {"inspect": 0, "stamp": [], "upgrade": []}
    assert "REFUSED" in capsys.readouterr().err


@pytest.mark.parametrize(
    "override",
    [["--expected-host", "aws-0-other.pooler.supabase.com"], ["--expected-database", "staging"], ["--expected-user", "postgres.otherproject"]],
)
def test_runner_refuses_a_different_target_before_connecting(stub_runner, override):
    calls, _state = stub_runner
    argv = args("--backup-verified")
    if override[0] in argv:
        argv[argv.index(override[0]) + 1] = override[1]
    else:
        argv += override
    assert runner.run(argv + ["stamp"]) == 2
    assert calls == {"inspect": 0, "stamp": [], "upgrade": []}


def test_runner_status_is_read_only(stub_runner):
    calls, _state = stub_runner
    assert runner.run(args("status")) == 0
    assert calls["stamp"] == [] and calls["upgrade"] == []


@pytest.mark.parametrize(
    "action, info, matched, backup, reason",
    [
        ("stamp", UNVERSIONED, "reference_createall", False, "--backup-verified"),
        ("stamp", UNVERSIONED, None, True, "does not match"),
        ("stamp", {**UNVERSIONED, "alembic_version": ["0001_initial"]}, "reference_createall", True, "already versioned"),
        ("stamp", {**UNVERSIONED, "export_events_exists": True}, "reference_createall", True, "export_events already exists"),
        ("upgrade", UNVERSIONED, "reference_createall", True, "requires alembic_version 0001_initial"),
        ("upgrade", {**UNVERSIONED, "alembic_version": ["0002_export_events"]}, "reference_createall", True, "requires alembic_version"),
        ("upgrade", {**UNVERSIONED, "alembic_version": ["0001_initial"]}, None, True, "does not match"),
        ("upgrade", {**UNVERSIONED, "alembic_version": ["0001_initial"]}, "reference_createall", False, "--backup-verified"),
    ],
)
def test_runner_refusals(stub_runner, capsys, action, info, matched, backup, reason):
    calls, state = stub_runner
    state.update(info=info, matched=matched)
    argv = args(*(["--backup-verified"] if backup else [])) + [action]
    assert runner.run(argv) == 2
    assert calls["stamp"] == [] and calls["upgrade"] == []
    assert reason in capsys.readouterr().err


def test_runner_stamps_and_upgrades_to_the_release_revision_only(stub_runner):
    calls, state = stub_runner
    assert runner.run(args("--backup-verified", "stamp")) == 0
    state["info"] = {**UNVERSIONED, "alembic_version": ["0001_initial"]}
    assert runner.run(args("--backup-verified", "upgrade")) == 0
    assert calls["stamp"] == ["0001_initial"]
    assert calls["upgrade"] == ["0002_export_events"]  # never "head"


def test_runner_refuses_when_app_settings_resolve_elsewhere(stub_runner, monkeypatch, capsys):
    calls, _state = stub_runner
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///./unify_zoho.db")  # e.g. a stray .env
    assert runner.run(args("--backup-verified", "stamp")) == 2
    assert calls["stamp"] == []
    assert "different DATABASE_URL" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Real PostgreSQL: read-only session, reproducible references, end-to-end runner
# ---------------------------------------------------------------------------


@pytest.fixture()
def scratch_db():
    url = make_url(POSTGRES_URL)
    if url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost"} or "test" not in (url.database or ""):
        pytest.fail("Refusing to run: ZCONNECT_TEST_POSTGRES_URL must be a local *test* PostgreSQL database")
    name = f"zconnect_test_preflight_{uuid.uuid4().hex[:8]}"
    admin = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield url.set(database=name).render_as_string(hide_password=False)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def create_like_startup(url: str) -> None:
    """What app/main.py does on boot: create_all except the migration-only tables."""
    engine = create_engine(url, future=True)
    Base.metadata.create_all(bind=engine, tables=[t for t in Base.metadata.sorted_tables if t.name not in MIGRATION_ONLY_TABLES])
    engine.dispose()


def execute(url: str, *statements: str) -> None:
    engine = create_engine(url, future=True)
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    engine.dispose()


def inspect(url: str):
    with preflight.connect_read_only(url.replace("postgresql+psycopg://", "postgresql://")) as conn:
        schema, info = preflight.snapshot(conn)
    return preflight.compare(schema, preflight.load_references())[0], info


@needs_postgres
def test_preflight_session_refuses_writes(scratch_db):
    create_like_startup(scratch_db)
    with preflight.connect_read_only(scratch_db.replace("postgresql+psycopg://", "postgresql://")) as conn:
        for statement in ("CREATE TABLE should_not_exist (id int)", "INSERT INTO settings (key, value) VALUES ('k', 'v')", "DELETE FROM settings"):
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute(statement)
            conn.rollback()


@needs_postgres
def test_startup_schema_matches_createall_reference_and_drift_is_detected(scratch_db):
    create_like_startup(scratch_db)
    assert inspect(scratch_db)[0] == "reference_createall"
    execute(scratch_db, "ALTER TABLE exported_orders ADD COLUMN legacy_note text")
    assert inspect(scratch_db)[0] is None


@needs_postgres
def test_alembic_0001_then_startup_matches_its_reference(scratch_db, monkeypatch):
    from alembic import command
    from alembic.config import Config

    monkeypatch.setattr(settings, "DATABASE_URL", scratch_db)
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(config, "0001_initial")
    create_like_startup(scratch_db)
    matched, info = inspect(scratch_db)
    assert matched == "reference_0001_alembic"
    assert info["alembic_version"] == ["0001_initial"]


@needs_postgres
def test_guarded_runner_end_to_end_on_a_production_like_database(scratch_db, monkeypatch, capsys):
    create_like_startup(scratch_db)
    execute(
        scratch_db,
        "INSERT INTO settings (key, value) VALUES ('ZOHO_ORG_ID', 'x'), ('ZOHO_BASE_URL', 'https://www.zohoapis.eu')",
        "INSERT INTO sync_runs (status, date_from, date_to) VALUES ('success', '2026-10-01', '2026-10-01')",
        "INSERT INTO exported_orders (sync_run_id, unify_order_id, status) VALUES (1, 'O-1', 'exported'), (1, 'O-2', 'failed')",
    )
    monkeypatch.setenv("DATABASE_URL", scratch_db)
    monkeypatch.setattr(settings, "DATABASE_URL", scratch_db)
    database = make_url(scratch_db).database
    base = ["--expected-host", "127.0.0.1", "--expected-database", database]

    _matched, pre_info = inspect(scratch_db)
    assert pre_info["settings_keys"] == [["ZOHO_BASE_URL", True], ["ZOHO_ORG_ID", True]]
    assert runner.run(base + ["status"]) == 0
    assert runner.run(base + ["--backup-verified", "upgrade"]) == 2  # unversioned: must stamp first
    assert runner.run(base + ["stamp"]) == 2  # backup not confirmed
    assert inspect(scratch_db)[1]["alembic_version"] is None

    assert runner.run(base + ["--backup-verified", "stamp"]) == 0
    assert runner.run(base + ["--backup-verified", "stamp"]) == 2  # already versioned
    assert runner.run(base + ["--backup-verified", "upgrade"]) == 0
    assert runner.run(base + ["--backup-verified", "upgrade"]) == 2  # already migrated

    with preflight.connect_read_only(scratch_db.replace("postgresql+psycopg://", "postgresql://")) as conn:
        _schema, info = preflight.snapshot(conn)
        checks = preflight.postcheck(conn, info, pre_info)
    assert all(checks.values()), checks
    execute(scratch_db, "INSERT INTO export_events (tenant_id, unify_order_id, event_type) VALUES ('t', 'O-1', 'test')")
    with pytest.raises(Exception, match="append-only"):
        execute(scratch_db, "UPDATE export_events SET detail = 'changed'")
    assert "s3cret" not in capsys.readouterr().out


@needs_postgres
def test_guarded_runner_refuses_to_stamp_a_drifted_schema(scratch_db, monkeypatch):
    create_like_startup(scratch_db)
    execute(scratch_db, "ALTER TABLE settings ADD COLUMN extra text")
    monkeypatch.setenv("DATABASE_URL", scratch_db)
    monkeypatch.setattr(settings, "DATABASE_URL", scratch_db)
    argv = ["--expected-host", "127.0.0.1", "--expected-database", make_url(scratch_db).database, "--backup-verified", "stamp"]
    assert runner.run(argv) == 2
    assert inspect(scratch_db)[1]["alembic_version"] is None
