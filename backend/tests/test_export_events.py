"""Tests for the append-only export_events audit table and migration 0002.

Isolated SQLite files only (PostgreSQL variants live in test_postgres_concurrency.py).
No production database or Zoho organisation is ever contacted.
"""

from __future__ import annotations

import asyncio
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import sessionmaker

from app import models
from app.api import routes
from app.config import settings
from app.db import Base, get_db
from app.schemas import ResetSelectedRunsRequest
from app.services.export_service import AuditContext, list_export_events, soft_reset_export_records
from tests.test_invoice_idempotency import (
    FakeZoho,
    StubbedExportService,
    details_by_id,
    make_order,
    record,
    run,
    seed_record,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'events.db').as_posix()}", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    yield factory
    engine.dispose()


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


@pytest.fixture()
def zoho():
    return FakeZoho()


@pytest.fixture()
def service(zoho):
    svc = StubbedExportService(None, zoho)
    svc.audit = AuditContext(tenant_id="tenant-a", actor_id="user-1")
    return svc


def events(db, tenant="tenant-a", order_id=None):
    db.expire_all()
    return list_export_events(db, tenant, order_id)


def summary(db, order_id, tenant="tenant-a"):
    return [(e.event_type, e.previous_status, e.new_status) for e in events(db, tenant, order_id)]


def fake_request(actor=None):
    return SimpleNamespace(headers={"x-zconnect-actor": actor} if actor else {})


# ---------------------------------------------------------------------------
# Event creation and ordering
# ---------------------------------------------------------------------------


def test_successful_sync_records_claim_and_creation(service, zoho, db):
    result = run(service, db, make_order("O-1"))

    rows = events(db, order_id="O-1")
    assert [(e.event_type, e.previous_status, e.new_status) for e in rows] == [
        ("claimed", None, "in_progress"),
        ("invoice_created", "in_progress", "exported"),
    ]
    created = rows[1]
    assert created.zoho_invoice_id == "INV-1"
    assert created.sync_run_id == result["sync_run_id"]
    assert created.export_record_id == record(db, "O-1").id
    assert created.tenant_id == "tenant-a" and created.actor_id == "user-1"
    assert created.created_at is not None


def test_event_timestamps_are_utc(service, db):
    run(service, db, make_order("O-1"))
    stamp = events(db, order_id="O-1")[0].created_at
    if stamp.tzinfo is not None:  # PostgreSQL returns aware values; SQLite returns naive UTC
        assert stamp.utcoffset() == timezone.utc.utcoffset(None)


def test_full_lifecycle_is_ordered_and_append_only(service, zoho, db):
    db.add(models.Setting(key="TENANT_ID", value="tenant-a"))  # routes derive the tenant from configuration
    db.commit()
    run(service, db, make_order("O-1"))
    asyncio.run(routes.reset_order("O-1", db, fake_request("user-2")))
    run(service, db, make_order("O-1"))
    run(service, db, make_order("O-1"))

    rows = events(db, order_id="O-1")
    assert [e.event_type for e in rows] == ["claimed", "invoice_created", "reset", "claimed", "invoice_linked", "sync_blocked"]
    assert [e.id for e in rows] == sorted(e.id for e in rows)
    assert all(a.created_at <= b.created_at for a, b in zip(rows, rows[1:]))
    assert rows[2].actor_id == "user-2" and rows[2].previous_status == "exported" and rows[2].new_status == "reset"
    assert rows[4].zoho_invoice_id == "INV-1"  # linked, not a new invoice
    assert len(zoho.invoices_for("O-1")) == 1


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_events_are_tenant_scoped(zoho, db):
    service_a = StubbedExportService(None, zoho)
    service_a.audit = AuditContext(tenant_id="tenant-a")
    service_b = StubbedExportService(None, zoho)
    service_b.audit = AuditContext(tenant_id="tenant-b")

    run(service_a, db, make_order("O-1"))
    run(service_b, db, make_order("O-2", "B2"))

    assert {e.unify_order_id for e in events(db, "tenant-a")} == {"O-1"}
    assert {e.unify_order_id for e in events(db, "tenant-b")} == {"O-2"}
    assert events(db, "tenant-a", "O-2") == []
    assert events(db, "tenant-unknown") == []


def test_tenant_comes_from_configuration(db):
    assert routes._audit_context(db, None).tenant_id == "default"
    db.add(models.Setting(key="ZOHO_ORG_ID", value="20060001"))
    db.commit()
    assert routes._audit_context(db, None).tenant_id == "20060001"
    db.add(models.Setting(key="TENANT_ID", value="acme-bakery"))
    db.commit()
    context = routes._audit_context(db, fake_request("user-9"))
    assert (context.tenant_id, context.actor_id) == ("acme-bakery", "user-9")


# ---------------------------------------------------------------------------
# Failed, ambiguous and blocked operations
# ---------------------------------------------------------------------------


def test_explicit_failure_event_has_no_response_body(service, zoho, db):
    zoho.create_behavior["O-1"] = "fail"
    run(service, db, make_order("O-1"))

    failed = events(db, order_id="O-1")[-1]
    assert (failed.event_type, failed.previous_status, failed.new_status) == ("failed", "in_progress", "failed")
    assert failed.detail == "Zoho API failed HTTP 400"


def test_timeout_after_creation_records_needs_verification(service, zoho, db):
    zoho.create_behavior["O-1"] = "timeout_after_accept"
    run(service, db, make_order("O-1"))
    run(service, db, make_order("O-1"))

    assert summary(db, "O-1") == [
        ("claimed", None, "in_progress"),
        ("needs_verification", "in_progress", "needs_verification"),
        ("sync_blocked", "needs_verification", "needs_verification"),
    ]


def test_ambiguous_lookup_and_lookup_failure_are_recorded(service, zoho, db):
    zoho.add_invoice("O-1", "C-OTHER")
    run(service, db, make_order("O-1"))
    zoho.lookup_behavior = "error"
    run(service, db, make_order("O-2", "B2"))

    assert summary(db, "O-1")[-1] == ("needs_verification", "in_progress", "needs_verification")
    assert "could not be matched safely" in events(db, order_id="O-1")[-1].detail
    assert summary(db, "O-2")[-1] == ("failed", "in_progress", "failed")
    assert zoho.create_calls == []


def test_concurrent_attempt_is_recorded_as_blocked(session_factory, zoho):
    service = StubbedExportService(None, zoho)
    service.audit = AuditContext(tenant_id="tenant-a")
    first, second = session_factory(), session_factory()

    async def both():
        return await asyncio.gather(
            service.run_export(first, "2026-10-01", "2026-10-02", [make_order("O-1")]),
            service.run_export(second, "2026-10-01", "2026-10-02", [make_order("O-1")]),
        )

    try:
        asyncio.run(both())
    finally:
        first.close()
        second.close()
    check = session_factory()
    types = sorted(e.event_type for e in list_export_events(check, "tenant-a", "O-1"))
    check.close()
    assert types == ["claimed", "invoice_created", "sync_blocked"]
    assert zoho.create_calls == ["O-1"]


# ---------------------------------------------------------------------------
# Unsync / History reset / reset-all keep audit history and protection
# ---------------------------------------------------------------------------


def test_reset_routes_record_events_and_never_delete_them(service, zoho, db, monkeypatch):
    monkeypatch.setattr(routes, "_audit_context", lambda _db, req: AuditContext("tenant-a", req.headers.get("x-zconnect-actor") if req else None))
    first = run(service, db, make_order("O-1"), make_order("O-2", "B2"))
    seed_record(db, "O-3", "needs_verification", "evidence")
    before = len(events(db))

    asyncio.run(routes.reset_order("O-1", db, fake_request("user-7")))
    with pytest.raises(Exception):
        asyncio.run(routes.reset_order("O-3", db, fake_request("user-7")))
    asyncio.run(routes.reset_selected_runs(ResetSelectedRunsRequest(run_ids=[first["sync_run_id"]]), db, fake_request("user-8")))
    asyncio.run(routes.reset_sync_state(db, fake_request("user-9")))

    rows = events(db)
    assert len(rows) > before
    new = [(e.unify_order_id, e.event_type, e.actor_id) for e in rows[before:]]
    assert ("O-1", "reset", "user-7") in new
    assert ("O-3", "reset_refused", "user-7") in new
    assert ("O-2", "reset", "user-8") in new  # O-1 was already reset -> no duplicate reset event
    assert ("O-3", "reset_refused", "user-9") in new
    assert record(db, "O-3").status == "needs_verification"

    again = run(service, db, make_order("O-1"), make_order("O-2", "B2"))
    assert {d["status"] for d in again["details"]} == {"already_exported"}  # protection kept
    assert zoho.create_calls == ["O-1", "O-2"]


def test_actor_header_reaches_events_through_http(service, zoho, db, monkeypatch):
    from app.main import app

    run(service, db, make_order("O-1"))
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            response = client.post("/api/export/reset-order/O-1", headers={"X-Zconnect-Actor": "user-http"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    reset_event = [e for e in list_export_events(db, "default", "O-1") if e.event_type == "reset"]
    assert len(reset_event) == 1 and reset_event[0].actor_id == "user-http"


def test_soft_reset_without_context_uses_default_tenant(service, db):
    run(service, db, make_order("O-1"))
    soft_reset_export_records(db, [record(db, "O-1")], source="script")
    assert [e.event_type for e in events(db, "default", "O-1")] == ["reset"]


# ---------------------------------------------------------------------------
# Fail closed when the audit table is missing
# ---------------------------------------------------------------------------


def test_missing_export_events_table_fails_closed(tmp_path, zoho):
    engine = create_engine(f"sqlite:///{(tmp_path / 'no_events.db').as_posix()}", future=True)
    Base.metadata.create_all(bind=engine, tables=[t for t in Base.metadata.sorted_tables if t.name != "export_events"])
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    service = StubbedExportService(None, zoho)
    try:
        result = asyncio.run(service.run_export(session, "2026-10-01", "2026-10-02", [make_order("O-1")]))
    finally:
        session.close()
        engine.dispose()
    assert details_by_id(result)["O-1"]["status"] == "failed"
    assert zoho.create_calls == [] and zoho.lookup_calls == []


# ---------------------------------------------------------------------------
# Migration 0002: upgrade / downgrade on disposable SQLite databases
# ---------------------------------------------------------------------------


def alembic_config(monkeypatch, url: str) -> Config:
    assert url.startswith("sqlite:///"), "migration tests only run on disposable SQLite here"
    monkeypatch.setattr(settings, "DATABASE_URL", url)  # alembic env.py reads app settings
    # No ini file: env.py then skips logging.config.fileConfig, which would disable the
    # application's loggers for every later test in the session.
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_migration_upgrade_and_downgrade_fresh_database(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}"
    config = alembic_config(monkeypatch, url)
    command.upgrade(config, "head")

    engine = create_engine(url, future=True)
    inspector = inspect(engine)
    assert "export_events" in inspector.get_table_names()
    assert {ix["name"] for ix in inspector.get_indexes("export_events")} >= {
        "ix_export_events_tenant_order",
        "ix_export_events_tenant_created",
        "ix_export_events_sync_run",
        "ix_export_events_export_record",
    }
    fks = {fk["referred_table"] for fk in inspector.get_foreign_keys("export_events")}
    assert fks == {"exported_orders", "sync_runs"}
    with engine.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002_export_events"
        triggers = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    assert triggers == {"export_events_no_update", "export_events_no_delete"}
    engine.dispose()

    command.downgrade(config, "0001_initial")
    engine = create_engine(url, future=True)
    assert "export_events" not in inspect(engine).get_table_names()
    assert {"settings", "sync_runs", "exported_orders", "exported_invoices"} <= set(inspect(engine).get_table_names())
    engine.dispose()

    command.upgrade(config, "head")  # re-upgrade works after a downgrade
    engine = create_engine(url, future=True)
    assert "export_events" in inspect(engine).get_table_names()
    engine.dispose()


def test_migration_on_production_like_database_preserves_existing_rows(tmp_path, monkeypatch):
    """Production tables were created by create_all (no alembic_version). Plan: stamp
    0001_initial, then upgrade. Existing rows must be untouched both ways."""
    url = f"sqlite:///{(tmp_path / 'prodlike.db').as_posix()}"
    engine = create_engine(url, future=True)
    Base.metadata.create_all(bind=engine, tables=[t for t in Base.metadata.sorted_tables if t.name != "export_events"])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sync_runs (id, status, date_from, date_to, total_orders, total_customers, total_invoices) VALUES (1, 'success', '2026-09-01', '2026-09-01', 1, 1, 1)"))
        conn.execute(text("INSERT INTO exported_orders (id, sync_run_id, unify_order_id, customer_name, status, message) VALUES (1, 1, 'L-1', 'Customer', 'exported', 'Invoice ZINV-1 created')"))
        conn.execute(text("INSERT INTO exported_invoices (id, sync_run_id, unify_customer_name, unify_order_ids, zoho_invoice_id, status, message) VALUES (1, 1, 'B1', '[\"L-1\"]', 'ZINV-1', 'success', 'Draft invoice created')"))
    def snapshot():
        with engine.begin() as conn:
            return {
                table: [tuple(row) for row in conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))]
                for table in ("sync_runs", "exported_orders", "exported_invoices")
            }

    before = snapshot()
    config = alembic_config(monkeypatch, url)
    command.stamp(config, "0001_initial")
    command.upgrade(config, "head")
    assert snapshot() == before
    assert "export_events" in inspect(engine).get_table_names()
    command.downgrade(config, "0001_initial")
    assert snapshot() == before
    engine.dispose()


def test_migrated_table_rejects_update_and_delete(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'appendonly.db').as_posix()}"
    command.upgrade(alembic_config(monkeypatch, url), "head")
    engine = create_engine(url, future=True)
    session = sessionmaker(bind=engine, future=True)()
    session.add(models.ExportEvent(tenant_id="t", unify_order_id="O-1", event_type="claimed", new_status="in_progress"))
    session.commit()
    with pytest.raises(DatabaseError, match="append-only"):
        session.execute(text("UPDATE export_events SET event_type = 'tampered'"))
        session.commit()
    session.rollback()
    with pytest.raises(DatabaseError, match="append-only"):
        session.execute(text("DELETE FROM export_events"))
        session.commit()
    session.rollback()
    assert session.execute(text("SELECT event_type FROM export_events")).scalar() == "claimed"
    session.close()
    engine.dispose()


def test_model_matches_migration(tmp_path, monkeypatch):
    """The ORM model and migration 0002 define the same columns."""
    url = f"sqlite:///{(tmp_path / 'compare.db').as_posix()}"
    command.upgrade(alembic_config(monkeypatch, url), "head")
    engine = create_engine(url, future=True)
    migrated = {c["name"]: c["nullable"] for c in inspect(engine).get_columns("export_events")}
    engine.dispose()
    modelled = {c.name: c.nullable for c in models.ExportEvent.__table__.columns}
    assert migrated == modelled


def test_app_startup_never_creates_migration_only_tables():
    """Deploying code must not implicitly run the schema change: only migration 0002 creates export_events."""
    from app import db as app_db
    import app.main  # noqa: F401  (runs the startup create_all)

    tables = inspect(app_db.engine).get_table_names()
    assert "sync_runs" in tables and "exported_orders" in tables
    assert "export_events" not in tables
