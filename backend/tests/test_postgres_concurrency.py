"""Concurrency checks against a real, disposable PostgreSQL server.

Skipped unless ZCONNECT_TEST_POSTGRES_URL points to a throwaway database on localhost, e.g.
    postgresql+psycopg://zctest@127.0.0.1:55432/zconnect_test
Never point this at a shared or production database: every test drops and recreates the
tables. Threads use separate connections and are released together through a barrier so
claims, conditional updates and reconciliations genuinely race inside PostgreSQL.
"""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import models
from app.api import routes
from app.db import Base
from app.services.export_service import ReconcileError, soft_reset_export_records
from tests.test_invoice_idempotency import FakeZoho, StubbedExportService, make_order

POSTGRES_URL = os.environ.get("ZCONNECT_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="set ZCONNECT_TEST_POSTGRES_URL to a disposable local PostgreSQL")

THREADS = 8


@pytest.fixture()
def pg_factory():
    url = make_url(POSTGRES_URL)
    if url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost"} or "test" not in (url.database or ""):
        pytest.fail("Refusing to run: ZCONNECT_TEST_POSTGRES_URL must be a local *test* PostgreSQL database")
    engine = create_engine(POSTGRES_URL, future=True, pool_size=THREADS + 4, max_overflow=4)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    yield factory
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


class ThreadSafeFakeZoho(FakeZoho):
    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()

    async def create_draft_invoice_from_payload(self, payload):
        await asyncio.sleep(0.2)  # hold the claim while other threads try to take it
        with self.lock:
            self.create_calls.append(payload["reference_number"])
            invoice = self.add_invoice(payload["reference_number"], payload["customer_id"], customer_name=payload.get("customer_name"))
        return invoice["invoice_id"]

    async def find_invoices_by_reference_number(self, reference):
        with self.lock:
            self.lookup_calls.append(reference)
            return [inv for inv in self.invoices if reference in inv["reference_number"]]


def race(worker, count: int = THREADS) -> List[Any]:
    barrier = threading.Barrier(count)
    results: List[Any] = [None] * count

    def target(index: int) -> None:
        barrier.wait()
        try:
            results[index] = worker(index)
        except BaseException as exc:  # surface errors to the assertion
            results[index] = exc

    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return results


def sync_in_thread(factory, service, order_id: str):
    def worker(_index: int):
        session = factory()
        try:
            result = asyncio.run(service.run_export(session, "2026-10-01", "2026-10-02", [make_order(order_id)]))
            return result["details"][0]["status"]
        finally:
            session.close()

    return worker


def test_postgres_concurrent_new_order_claims_create_one_invoice(pg_factory):
    zoho = ThreadSafeFakeZoho()
    service = StubbedExportService(None, zoho)
    statuses = race(sync_in_thread(pg_factory, service, "PG-1"))

    assert not [s for s in statuses if isinstance(s, BaseException)], statuses
    assert statuses.count("created") == 1, statuses
    assert set(statuses) <= {"created", "in_progress", "already_exported"}, statuses
    assert zoho.create_calls == ["PG-1"]
    session = pg_factory()
    assert session.query(models.ExportedOrder).count() == 1
    assert session.query(models.ExportedInvoice).count() == 1
    session.close()


@pytest.mark.parametrize("prior_status", ["failed", "reset"])
def test_postgres_concurrent_retry_claims_create_one_invoice(pg_factory, prior_status):
    session = pg_factory()
    sync_run = models.SyncRun(date_from="2026-10-01", date_to="2026-10-01", status="failed")
    session.add(sync_run)
    session.commit()
    session.add(models.ExportedOrder(sync_run_id=sync_run.id, unify_order_id="PG-2", customer_name="Customer B1", status=prior_status, message="seed"))
    session.commit()
    session.close()

    zoho = ThreadSafeFakeZoho()
    service = StubbedExportService(None, zoho)
    statuses = race(sync_in_thread(pg_factory, service, "PG-2"))

    assert not [s for s in statuses if isinstance(s, BaseException)], statuses
    assert statuses.count("created") == 1, statuses
    assert zoho.create_calls == ["PG-2"]
    session = pg_factory()
    assert session.query(models.ExportedOrder).one().status == "exported"
    session.close()


def test_postgres_concurrent_reconciliations_apply_once(pg_factory):
    session = pg_factory()
    sync_run = models.SyncRun(date_from="2026-10-01", date_to="2026-10-01", status="running")
    session.add(sync_run)
    session.commit()
    stamp = (datetime.utcnow() - timedelta(hours=2)).isoformat(timespec="seconds")
    session.add(
        models.ExportedOrder(
            sync_run_id=sync_run.id,
            unify_order_id="PG-3",
            customer_name="Customer B1",
            status="in_progress",
            message=f"Export attempt started (claim abc) [run {sync_run.id}, {stamp}Z]",
        )
    )
    session.commit()
    session.close()

    zoho = ThreadSafeFakeZoho()
    zoho.add_invoice("PG-3", "C-B1")
    service = StubbedExportService(None, zoho)

    def worker(_index: int):
        local = pg_factory()
        try:
            return asyncio.run(service.reconcile_order(local, "PG-3"))["result"]
        except ReconcileError as exc:
            return exc.status_code
        finally:
            local.close()

    outcomes = race(worker)
    assert outcomes.count("linked") == 1, outcomes
    assert all(o in ("linked", 409) for o in outcomes), outcomes
    session = pg_factory()
    assert session.query(models.ExportedOrder).one().status == "exported"
    assert session.query(models.ExportedInvoice).count() == 1
    session.close()
    assert zoho.create_calls == []


def test_postgres_soft_reset_racing_with_sync_never_creates_twice(pg_factory):
    zoho = ThreadSafeFakeZoho()
    service = StubbedExportService(None, zoho)
    first = pg_factory()
    asyncio.run(service.run_export(first, "2026-10-01", "2026-10-02", [make_order("PG-4")]))
    first.close()

    def worker(index: int):
        local = pg_factory()
        try:
            if index % 2 == 0:
                records = local.query(models.ExportedOrder).filter(models.ExportedOrder.unify_order_id == "PG-4").all()
                return soft_reset_export_records(local, records, source="race test")
            return asyncio.run(service.run_export(local, "2026-10-01", "2026-10-02", [make_order("PG-4")]))["details"][0]["status"]
        finally:
            local.close()

    outcomes = race(worker)
    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    assert zoho.create_calls == ["PG-4"]  # only the very first sync ever created an invoice
    assert len(zoho.invoices_for("PG-4")) == 1
    session = pg_factory()
    assert session.query(models.ExportedOrder).count() == 1
    assert session.query(models.ExportedInvoice).count() == 1
    session.close()


def test_postgres_history_reset_keeps_everything(pg_factory):
    zoho = ThreadSafeFakeZoho()
    service = StubbedExportService(None, zoho)
    session = pg_factory()
    result = asyncio.run(service.run_export(session, "2026-10-01", "2026-10-02", [make_order("PG-5"), make_order("PG-6", "B2")]))
    before = (session.query(models.ExportedOrder).count(), session.query(models.ExportedInvoice).count(), session.query(models.SyncRun).count())
    from app.schemas import ResetSelectedRunsRequest

    response = asyncio.run(routes.reset_selected_runs(ResetSelectedRunsRequest(run_ids=[result["sync_run_id"]]), session))
    after = (session.query(models.ExportedOrder).count(), session.query(models.ExportedInvoice).count(), session.query(models.SyncRun).count())
    assert response.data["reset_exported_orders"] == 2 and before == after
    again = asyncio.run(service.run_export(session, "2026-10-01", "2026-10-02", [make_order("PG-5"), make_order("PG-6", "B2")]))
    assert {d["status"] for d in again["details"]} == {"already_exported"}
    assert zoho.create_calls == ["PG-5", "PG-6"]
    session.close()


# ---------------------------------------------------------------------------
# export_events on PostgreSQL: concurrent events, migration and append-only triggers
# ---------------------------------------------------------------------------


def test_postgres_concurrent_syncs_record_one_creation_event(pg_factory):
    from app.services.export_service import AuditContext, list_export_events

    zoho = ThreadSafeFakeZoho()
    service = StubbedExportService(None, zoho)
    service.audit = AuditContext(tenant_id="tenant-pg")
    statuses = race(sync_in_thread(pg_factory, service, "PG-EV"))
    assert statuses.count("created") == 1, statuses

    session = pg_factory()
    rows = list_export_events(session, "tenant-pg", "PG-EV")
    session.close()
    types = [e.event_type for e in rows]
    assert types.count("invoice_created") == 1
    assert types.count("claimed") == 1
    assert types.count("sync_blocked") == THREADS - 1
    assert [e.id for e in rows] == sorted(e.id for e in rows)
    # timestamptz stores an absolute instant; PostgreSQL returns it in the session time zone
    # (here BST, +01:00). What matters is that it is aware and is the correct UTC instant.
    from datetime import timezone as _tz

    now_utc = datetime.now(_tz.utc)
    assert all(e.created_at.tzinfo is not None for e in rows)
    assert all(abs((now_utc - e.created_at.astimezone(_tz.utc)).total_seconds()) < 300 for e in rows)


def _pg_alembic_config(monkeypatch):
    from pathlib import Path

    from alembic.config import Config

    from app.config import settings

    backend = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(settings, "DATABASE_URL", POSTGRES_URL)
    config = Config()
    config.set_main_option("script_location", str(backend / "alembic"))
    config.set_main_option("sqlalchemy.url", POSTGRES_URL)
    return config


def test_postgres_migration_upgrade_downgrade_and_append_only(pg_factory, monkeypatch):
    from alembic import command
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import DatabaseError

    engine = pg_factory.kw["bind"]
    # Production-like start: tables from create_all (no export_events, no alembic_version), with data.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine, tables=[t for t in Base.metadata.sorted_tables if t.name != "export_events"])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sync_runs (id, status, date_from, date_to, total_orders, total_customers, total_invoices) VALUES (1, 'success', '2026-09-01', '2026-09-01', 1, 1, 1)"))
        conn.execute(text("INSERT INTO exported_orders (id, sync_run_id, unify_order_id, customer_name, status, message) VALUES (1, 1, 'L-1', 'Customer', 'exported', 'Invoice ZINV-1 created')"))
        before = [tuple(r) for r in conn.execute(text("SELECT * FROM exported_orders ORDER BY id"))]

    config = _pg_alembic_config(monkeypatch)
    command.stamp(config, "0001_initial")
    command.upgrade(config, "head")
    assert "export_events" in inspect(engine).get_table_names()
    with engine.begin() as conn:
        assert [tuple(r) for r in conn.execute(text("SELECT * FROM exported_orders ORDER BY id"))] == before
        conn.execute(text("INSERT INTO export_events (tenant_id, unify_order_id, export_record_id, sync_run_id, event_type, new_status) VALUES ('t', 'L-1', 1, 1, 'claimed', 'in_progress')"))
    for statement in ("UPDATE export_events SET event_type = 'tampered'", "DELETE FROM export_events"):
        with pytest.raises(DatabaseError, match="append-only"):
            with engine.begin() as conn:
                conn.execute(text(statement))
    with engine.begin() as conn:
        assert conn.execute(text("SELECT event_type FROM export_events")).scalar() == "claimed"
        # Foreign keys: an event cannot point to a missing record or run.
        with pytest.raises(DatabaseError):
            with conn.begin_nested():
                conn.execute(text("INSERT INTO export_events (tenant_id, unify_order_id, export_record_id, event_type) VALUES ('t', 'X', 999, 'claimed')"))

    command.downgrade(config, "0001_initial")
    assert "export_events" not in inspect(engine).get_table_names()
    with engine.begin() as conn:
        assert [tuple(r) for r in conn.execute(text("SELECT * FROM exported_orders ORDER BY id"))] == before
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
