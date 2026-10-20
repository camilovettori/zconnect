"""Release tooling: tools/schema_preflight.py (read-only) and tools/run_migration.py (guarded).

The production database is shared with other applications (ZRecipe): Zconnect's migration
objects are zconnect_export_events and zconnect_alembic_version, and objects with generic
names (public.export_events, public.alembic_version) belong to someone else.

The classification and refusal rules run everywhere with the database stubbed out. The
PostgreSQL tests are skipped unless ZCONNECT_TEST_POSTGRES_URL points to a disposable local
server (see test_postgres_concurrency.py); each creates and drops its own scratch database.
"""
from __future__ import annotations

import importlib.util
import os
import re
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import models
from app.config import settings
from app.db import ALEMBIC_VERSION_TABLE, Base
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
NO_FOREIGN = {"public.export_events": {"exists": False}, "public.alembic_version": {"exists": False}}
FOREIGN_PRESENT = {
    "public.export_events": {"exists": True, "columns": 13, "rows": 0, "indexes": 5, "custom_triggers": 0},
    "public.alembic_version": {"exists": True, "columns": 1, "rows": 1, "indexes": 1, "custom_triggers": 0},
}


def info(version=None, table=False, function=False, foreign=NO_FOREIGN):
    return {
        "zconnect_alembic_version": version,
        "zconnect_export_events_exists": table,
        "zconnect_append_only_function_exists": function,
        "foreign_objects": foreign,
        "row_counts": {"settings": 1},
    }


# ---------------------------------------------------------------------------
# Names: the application, migration and tools agree on Zconnect-specific objects
# ---------------------------------------------------------------------------


def test_zconnect_object_names_are_consistent():
    assert models.ExportEvent.__tablename__ == preflight.AUDIT_TABLE == "zconnect_export_events"
    assert ALEMBIC_VERSION_TABLE == preflight.VERSION_TABLE == "zconnect_alembic_version"
    assert MIGRATION_ONLY_TABLES == {"zconnect_export_events"}
    reference = preflight.load_audit_reference()
    model_indexes = {ix.name for ix in models.ExportEvent.__table__.indexes}
    assert {row[0] for row in reference["indexes"]} == model_indexes | {"zconnect_export_events_pkey"}
    assert {row[0] for row in reference["columns"]} == {c.name for c in models.ExportEvent.__table__.columns}
    assert [row[0] for row in reference["triggers"]] == ["zconnect_export_events_no_update_delete"]
    # Nothing Zconnect creates or reads uses the generic names another application may own.
    assert "export_events" not in Base.metadata.tables and "alembic_version" not in Base.metadata.tables
    source = (BACKEND_DIR / "alembic" / "versions" / "0002_export_events.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]  # the module docstring explains the foreign table; check only the code
    assert not re.search(r"(?<![\w])export_events(?![\w])", code)
    assert not re.search(r"(?<![\w])alembic_version(?![\w])", code)


# ---------------------------------------------------------------------------
# Classification (no database needed)
# ---------------------------------------------------------------------------


AUDIT_REFERENCE = preflight.load_audit_reference()
EXTRA_COLUMN = {**AUDIT_REFERENCE, "columns": AUDIT_REFERENCE["columns"] + [["unknown_extra", "text", None, "YES", "", "NO"]]}
NO_TRIGGER = {**AUDIT_REFERENCE, "triggers": []}


@pytest.mark.parametrize(
    "matched, state_info, definition, expected",
    [
        ("reference_createall", info(), None, "ready_to_stamp"),
        ("reference_createall", info(foreign=FOREIGN_PRESENT), None, "ready_to_stamp"),  # foreign objects never count
        ("reference_0001_alembic", info(version=["0001_initial"]), None, "ready_to_upgrade"),
        ("reference_createall", info(version=["0001_initial"], foreign=FOREIGN_PRESENT), None, "ready_to_upgrade"),
        ("reference_createall", info(version=["0002_export_events"], table=True, function=True), AUDIT_REFERENCE, "migrated"),
        ("reference_createall", info(table=True), NO_TRIGGER, "incompatible"),  # e.g. created by create_all, never migrated
        ("reference_createall", info(function=True), None, "incompatible"),
        ("reference_createall", info(version=["0001_initial"], table=True, function=True), AUDIT_REFERENCE, "incompatible"),
        ("reference_createall", info(version=["0002_export_events"], table=True, function=True), EXTRA_COLUMN, "incompatible"),
        ("reference_createall", info(version=["0002_export_events"], table=True), NO_TRIGGER, "incompatible"),
        ("reference_createall", info(version=["0002_export_events"]), None, "incompatible"),
        ("reference_createall", info(version=["9999_unknown"]), None, "incompatible"),
        (None, info(), None, "schema_differs"),
    ],
)
def test_assess_states(matched, state_info, definition, expected):
    assessment = preflight.assess(matched, state_info, definition, AUDIT_REFERENCE)
    assert assessment["state"] == expected
    assert assessment["verdict"].startswith("SCHEMA MATCHES") == (expected in preflight.READY_STATES)
    if expected == "incompatible":
        assert assessment["problems"]


def test_incompatible_audit_table_reports_exact_differences():
    assessment = preflight.assess("reference_createall", info(version=["0002_export_events"], table=True, function=True), EXTRA_COLUMN, AUDIT_REFERENCE)
    assert any("unknown_extra" in problem for problem in assessment["problems"])
    assert "SCHEMA MATCHES" not in assessment["verdict"]


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
    state = {"info": info(foreign=FOREIGN_PRESENT), "state": "ready_to_stamp"}

    def fake_inspect(url):
        calls["inspect"] += 1
        return dict(state["info"]), {"state": state["state"], "matched": "reference_createall", "problems": [], "verdict": state["state"]}

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


def test_runner_status_is_read_only_and_reports_foreign_objects(stub_runner, capsys):
    calls, _state = stub_runner
    assert runner.run(args("status")) == 0
    assert calls["stamp"] == [] and calls["upgrade"] == []
    out = capsys.readouterr().out
    assert "public.export_events exists (13 columns, 0 rows): not a Zconnect object, ignored" in out


@pytest.mark.parametrize(
    "action, state, backup, reason",
    [
        ("stamp", "ready_to_stamp", False, "--backup-verified"),
        ("stamp", "ready_to_upgrade", True, "stamp requires state ready_to_stamp"),
        ("stamp", "incompatible", True, "stamp requires state ready_to_stamp"),
        ("stamp", "schema_differs", True, "stamp requires state ready_to_stamp"),
        ("stamp", "migrated", True, "stamp requires state ready_to_stamp"),
        ("upgrade", "ready_to_stamp", True, "upgrade requires state ready_to_upgrade"),
        ("upgrade", "incompatible", True, "upgrade requires state ready_to_upgrade"),
        ("upgrade", "migrated", True, "upgrade requires state ready_to_upgrade"),
        ("upgrade", "ready_to_upgrade", False, "--backup-verified"),
    ],
)
def test_runner_refusals(stub_runner, capsys, action, state, backup, reason):
    calls, stub_state = stub_runner
    stub_state["state"] = state
    argv = args(*(["--backup-verified"] if backup else [])) + [action]
    assert runner.run(argv) == 2
    assert calls["stamp"] == [] and calls["upgrade"] == []
    assert reason in capsys.readouterr().err


def test_runner_stamps_and_upgrades_to_the_release_revision_only(stub_runner):
    calls, state = stub_runner
    assert runner.run(args("--backup-verified", "stamp")) == 0
    state["state"] = "ready_to_upgrade"
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
# Real PostgreSQL: read-only session, references, shared database, end-to-end runner
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


def libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def inspect(url: str):
    with preflight.connect_read_only(libpq(url)) as conn:
        _schema, info_, _differences, assessment = preflight.inspect_database(conn)
    return assessment, info_


def add_foreign_application_objects(url: str) -> None:
    """Objects another application (e.g. ZRecipe) owns in the shared project, mirroring the
    production findings: a 13-column export_events with 5 indexes, no custom triggers and no
    rows, a foreign alembic_version, and an RLS-protected table of its own."""
    columns = ", ".join(f"c{i} text" for i in range(5, 14))
    execute(
        url,
        f"CREATE TABLE export_events (id bigserial PRIMARY KEY, tenant_id uuid, recipe_id text, kind text, {columns})",
        "CREATE INDEX ix_foreign_tenant ON export_events (tenant_id)",
        "CREATE INDEX ix_foreign_recipe ON export_events (recipe_id)",
        "CREATE INDEX ix_foreign_kind ON export_events (kind)",
        "CREATE INDEX ix_foreign_c5 ON export_events (c5)",
        "CREATE TABLE alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY)",
        "INSERT INTO alembic_version VALUES ('other_app_0042')",
        "CREATE TABLE recipes (id uuid PRIMARY KEY, name text)",
        "ALTER TABLE recipes ENABLE ROW LEVEL SECURITY",
    )


def foreign_definition(url: str) -> dict:
    with psycopg.connect(libpq(url)) as conn:
        q = lambda sql: [tuple(r) for r in conn.execute(sql).fetchall()]
        return {
            "columns": q("select column_name, data_type, is_nullable, column_default from information_schema.columns where table_name = 'export_events' order by ordinal_position"),
            "indexes": q("select indexname, indexdef from pg_indexes where tablename = 'export_events' order by 1"),
            "triggers": q("select tgname from pg_trigger where tgrelid = 'public.export_events'::regclass and not tgisinternal"),
            "rows": q("select count(*) from export_events"),
            "alembic_version": q("select version_num from alembic_version"),
            "recipes_rls": q("select relrowsecurity from pg_class where oid = 'public.recipes'::regclass"),
        }


def run_status(url: str, *action: str) -> int:
    database = make_url(url).database
    return runner.run(["--expected-host", "127.0.0.1", "--expected-database", database, *action])


@pytest.fixture()
def target_env(monkeypatch):
    def point_at(url: str) -> None:
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setattr(settings, "DATABASE_URL", url)

    return point_at


@needs_postgres
def test_preflight_session_refuses_writes(scratch_db):
    create_like_startup(scratch_db)
    with preflight.connect_read_only(libpq(scratch_db)) as conn:
        for statement in ("CREATE TABLE should_not_exist (id int)", "INSERT INTO settings (key, value) VALUES ('k', 'v')", "DELETE FROM settings"):
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute(statement)
            conn.rollback()


@needs_postgres
def test_startup_schema_matches_createall_reference_and_drift_is_detected(scratch_db):
    create_like_startup(scratch_db)
    assessment, _info = inspect(scratch_db)
    assert (assessment["state"], assessment["matched"]) == ("ready_to_stamp", "reference_createall")
    execute(scratch_db, "ALTER TABLE exported_orders ADD COLUMN legacy_note text")
    assert inspect(scratch_db)[0]["state"] == "schema_differs"


@needs_postgres
def test_alembic_0001_then_startup_matches_its_reference_and_uses_zconnect_version_table(scratch_db, monkeypatch):
    from alembic import command
    from alembic.config import Config

    monkeypatch.setattr(settings, "DATABASE_URL", scratch_db)
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(config, "0001_initial")
    create_like_startup(scratch_db)
    assessment, info_ = inspect(scratch_db)
    assert (assessment["state"], assessment["matched"]) == ("ready_to_upgrade", "reference_0001_alembic")
    assert info_["zconnect_alembic_version"] == ["0001_initial"]
    assert info_["foreign_objects"]["public.alembic_version"]["exists"] is False


@needs_postgres
def test_shared_database_with_foreign_export_events_migrates_without_touching_it(scratch_db, target_env, capsys, tmp_path):
    """Production-like: Zconnect's tables plus another application's export_events and
    alembic_version. The foreign objects are informational only and stay byte-for-byte the same."""
    create_like_startup(scratch_db)
    execute(
        scratch_db,
        "INSERT INTO settings (key, value) VALUES ('ZOHO_ORG_ID', 'x')",
        "INSERT INTO sync_runs (status, date_from, date_to) VALUES ('success', '2026-10-01', '2026-10-01')",
        "INSERT INTO exported_orders (sync_run_id, unify_order_id, status) VALUES (1, 'O-1', 'exported')",
    )
    add_foreign_application_objects(scratch_db)
    before = foreign_definition(scratch_db)
    assert len(before["columns"]) == 13 and len(before["indexes"]) == 5 and before["triggers"] == []
    target_env(scratch_db)

    assert preflight.main(["compare"]) == 0
    out = capsys.readouterr().out
    assert "INFO: public.export_events exists (13 columns, 0 rows, 5 indexes, 0 custom triggers)" in out
    assert "INFO: public.alembic_version exists" in out
    assert "SCHEMA MATCHES 0001_initial (reference_createall): unversioned, ready to stamp" in out

    snapshot_file = tmp_path / "pre_migration_snapshot.json"
    assert preflight.main(["dump", str(snapshot_file)]) == 0
    assert run_status(scratch_db, "--backup-verified", "stamp") == 0
    assert run_status(scratch_db, "--backup-verified", "upgrade") == 0
    assert preflight.main(["postcheck", str(snapshot_file)]) == 0
    assert "POST-MIGRATION CHECKS PASSED" in capsys.readouterr().out

    assessment, info_ = inspect(scratch_db)
    assert assessment["state"] == "migrated"
    assert info_["zconnect_alembic_version"] == ["0002_export_events"]
    assert foreign_definition(scratch_db) == before  # same columns, indexes, no triggers, no rows, foreign version, RLS
    execute(scratch_db, "INSERT INTO zconnect_export_events (tenant_id, unify_order_id, event_type) VALUES ('t', 'O-1', 'test')")
    with pytest.raises(Exception, match="append-only"):
        execute(scratch_db, "UPDATE zconnect_export_events SET detail = 'changed'")
    execute(scratch_db, "UPDATE export_events SET kind = 'still writable by its owner'")  # no Zconnect trigger was attached


@needs_postgres
def test_preexisting_incompatible_zconnect_audit_table_blocks_stamp_and_upgrade(scratch_db, target_env, capsys):
    """A zconnect_export_events created outside the migration (e.g. by create_all, so without
    the append-only trigger) must never be reported as a match nor adopted."""
    engine = create_engine(scratch_db, future=True)
    Base.metadata.create_all(bind=engine)  # includes the audit table, without trigger or version
    engine.dispose()
    add_foreign_application_objects(scratch_db)
    target_env(scratch_db)

    assert preflight.main(["compare"]) == 1
    out = capsys.readouterr().out
    assert "SCHEMA MATCHES" not in out
    assert "INCOMPATIBLE ZCONNECT OBJECTS PRESENT" in out
    assert "zconnect_export_events MISSING triggers" in out
    assert run_status(scratch_db, "--backup-verified", "stamp") == 2
    assert run_status(scratch_db, "--backup-verified", "upgrade") == 2
    assessment, info_ = inspect(scratch_db)
    assert assessment["state"] == "incompatible" and info_["zconnect_alembic_version"] is None


@needs_postgres
def test_migrated_audit_table_altered_later_is_incompatible(scratch_db, target_env, capsys):
    create_like_startup(scratch_db)
    target_env(scratch_db)
    assert run_status(scratch_db, "--backup-verified", "stamp") == 0
    assert run_status(scratch_db, "--backup-verified", "upgrade") == 0
    assert inspect(scratch_db)[0]["state"] == "migrated"
    execute(scratch_db, "ALTER TABLE zconnect_export_events ADD COLUMN unexpected text")
    assessment, _info = inspect(scratch_db)
    assert assessment["state"] == "incompatible"
    assert any("unexpected" in p for p in assessment["problems"])
    assert run_status(scratch_db, "--backup-verified", "upgrade") == 2
    capsys.readouterr()


@needs_postgres
def test_guarded_runner_refusal_order_on_a_production_like_database(scratch_db, target_env):
    create_like_startup(scratch_db)
    target_env(scratch_db)
    assert run_status(scratch_db, "status") == 0
    assert run_status(scratch_db, "--backup-verified", "upgrade") == 2  # unversioned: must stamp first
    assert run_status(scratch_db, "stamp") == 2  # backup not confirmed
    assert inspect(scratch_db)[1]["zconnect_alembic_version"] is None
    assert run_status(scratch_db, "--backup-verified", "stamp") == 0
    assert run_status(scratch_db, "--backup-verified", "stamp") == 2  # already versioned
    assert run_status(scratch_db, "--backup-verified", "upgrade") == 0
    assert run_status(scratch_db, "--backup-verified", "upgrade") == 2  # already migrated


@needs_postgres
def test_guarded_runner_refuses_to_stamp_a_drifted_schema(scratch_db, target_env):
    create_like_startup(scratch_db)
    execute(scratch_db, "ALTER TABLE settings ADD COLUMN extra text")
    target_env(scratch_db)
    assert run_status(scratch_db, "--backup-verified", "stamp") == 2
    assert inspect(scratch_db)[1]["zconnect_alembic_version"] is None
