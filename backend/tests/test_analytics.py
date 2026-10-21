"""Analytics V1: calculations, filters, missing-data handling, value capture, authorization
and tenant isolation. Temporary in-memory databases and fakes only."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.db import Base, get_db
from app.main import MIGRATION_ONLY_TABLES, app
from app.services import analytics_service as svc
from app.services.export_service import AuditContext, analytics_lines, delivery_line_flags
from tests.test_invoice_idempotency import FakeZoho, StubbedExportService, details_by_id, make_order

TODAY = date(2026, 10, 21)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    session.add_all([models.Company(id="default", name="Default company"), models.Company(id="acme", name="Acme Bakery"), models.Company(id="other", name="Other Ltd")])
    session.commit()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def captured(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)


def add_fact(db, company, order_id, invoice_date, customer="B1", lines=(("zoho:1", "Sourdough", "2", "10.00"),), captured_on=None, currency="EUR", invoice="INV"):
    line_rows = analytics_lines([{"name": name, "quantity": qty, "rate": price, **({"item_id": key[5:]} if key.startswith("zoho:") else {})} for key, name, qty, price in lines], company)
    fact = models.OrderFact(
        company_id=company,
        unify_order_id=order_id,
        zoho_invoice_id=f"{invoice}-{order_id}",
        customer_key=customer,
        customer_name=f"Customer {customer}",
        invoice_date=invoice_date,
        currency=currency,
        net_amount=sum((line.net_amount for line in line_rows), Decimal("0")),
        line_count=len(line_rows),
        source="invoice_created",
        captured_at=captured(captured_on or invoice_date),
        lines=line_rows,
    )
    db.add(fact)
    db.commit()
    return fact


def add_run(db, company, status="success", started=datetime(2026, 10, 10, 9), exported=(), invoices=()):
    run = models.SyncRun(date_from="2026-10-01", date_to="2026-10-01", status=status, started_at=started)
    db.add(run)
    db.commit()
    if company is not None:
        db.add(models.SyncRunCompany(sync_run_id=run.id, company_id=company))
    for order_id, customer in exported:
        db.add(models.ExportedOrder(sync_run_id=run.id, unify_order_id=order_id, customer_name=customer, status="exported", created_at=started))
    for order_id, customer in invoices:
        db.add(models.ExportedInvoice(sync_run_id=run.id, unify_customer_name=customer, unify_order_ids=[order_id], zoho_invoice_id=f"Z-{order_id}", status="success", created_at=started))
    db.commit()
    return run


def summary(db, company="default", date_from="2026-10-01", date_to="2026-10-21", granularity=None, customer=None, product=None):
    return svc.compute_summary(db, svc.build_filters(company, date_from, date_to, granularity, customer, product, today=TODAY))


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------


def test_line_values_use_exact_decimal_rounding():
    lines = analytics_lines([{"name": "Rolls", "quantity": 3, "rate": 0.335}, {"name": "Delivery charge", "quantity": 1, "rate": 4.5}], "acme")
    assert [(line.product_key, line.net_amount) for line in lines] == [("name:rolls", Decimal("1.01")), ("delivery-charge", Decimal("4.50"))]


def test_totals_average_and_top_lists(db):
    add_fact(db, "acme", "O-1", date(2026, 10, 5), "B1", (("zoho:1", "Sourdough", "2", "10.00"), ("zoho:2", "Croissant", "10", "1.25")))
    add_fact(db, "acme", "O-2", date(2026, 10, 6), "B2", (("zoho:1", "Sourdough", "1", "10.00"), ("delivery-charge", "Delivery charge", "1", "5.00")))
    add_fact(db, "acme", "O-3", date(2026, 10, 6), "B1", (("zoho:2", "Croissant", "4", "1.25"),))
    result = summary(db, "acme")

    kpis = result["kpis"]
    # O-1 = 2x10.00 + 10x1.25 = 32.50; O-2 = 10.00 + 5.00 delivery = 15.00; O-3 = 4x1.25 = 5.00
    assert (kpis["invoiced_value"], kpis["orders_with_value"], kpis["average_order_value"]) == ("52.50", 3, "17.50")
    assert result["currency"] == "EUR"
    assert [(c["key"], c["value"], c["orders"]) for c in result["top_customers"]] == [("B1", "37.50", 2), ("B2", "15.00", 1)]
    # Delivery counts in the invoiced value but is not a product.
    assert [(p["name"], p["value"], p["quantity"], p["orders"]) for p in result["top_products"]] == [("Sourdough", "30.00", "3", 2), ("Croissant", "17.50", "14", 2)]


def test_series_buckets_by_day_week_and_month(db):
    add_fact(db, "acme", "O-1", date(2026, 10, 5), captured_on=date(2026, 10, 5))  # Monday
    add_fact(db, "acme", "O-2", date(2026, 10, 12))
    day = summary(db, "acme", "2026-10-03", "2026-10-06", "day")["series"]
    assert [(s["period_start"], s["value"], s["captured"]) for s in day] == [
        ("2026-10-03", None, False),  # before value capture started: unknown, not zero
        ("2026-10-04", None, False),
        ("2026-10-05", "20.00", True),
        ("2026-10-06", "0.00", True),
    ]
    week = summary(db, "acme", "2026-10-01", "2026-10-18", "week")["series"]
    assert [(s["period_start"], s["value"], s["partial"]) for s in week] == [("2026-09-28", None, False), ("2026-10-05", "20.00", False), ("2026-10-12", "20.00", False)]
    month = summary(db, "acme", "2026-09-15", "2026-10-21", "month")["series"]
    assert [(s["period_start"], s["period_end"], s["value"], s["partial"]) for s in month] == [
        ("2026-09-01", "2026-09-30", None, False),
        ("2026-10-01", "2026-10-21", "40.00", True),  # capture started inside this month
    ]


def test_counts_come_from_runs_and_are_never_money(db):
    add_run(db, "acme", "success", exported=[("O-1", "B1"), ("O-2", "B2")], invoices=[("O-1", "B1"), ("O-2", "B2")])
    add_run(db, "acme", "partial", started=datetime(2026, 10, 11, 9), exported=[("O-3", "B1")], invoices=[("O-3", "B1")])
    add_run(db, "acme", "failed", started=datetime(2026, 10, 12, 9))
    add_run(db, "acme", "skipped", started=datetime(2026, 9, 1, 9))  # outside the range
    add_fact(db, "acme", "O-1", date(2026, 10, 10))
    result = summary(db, "acme")

    kpis = result["kpis"]
    assert kpis["invoices_created"] == 3 and kpis["exported_orders"] == 3
    assert kpis["sync_runs"] == {"success": 1, "partial": 1, "failed": 1, "skipped": 0, "running": 0, "total": 3}
    assert kpis["invoiced_value"] == "20.00"  # only the captured order carries a value
    assert result["coverage"]["exported_orders_without_value"] == 2


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_customer_filter(db):
    add_run(db, "acme", invoices=[("O-1", "B1"), ("O-2", "B2")])
    add_fact(db, "acme", "O-1", date(2026, 10, 5), "B1")
    add_fact(db, "acme", "O-2", date(2026, 10, 5), "B2", (("zoho:9", "Cake", "1", "99.00"),))
    result = summary(db, "acme", customer="B2")

    assert (result["kpis"]["invoiced_value"], result["kpis"]["orders_with_value"], result["kpis"]["invoices_created"]) == ("99.00", 1, 1)
    assert result["kpis"]["exported_orders"] is None  # exported orders are not stored per customer key
    assert result["coverage"]["sync_runs_ignore_customer_and_product_filters"] is True


def test_product_filter_counts_only_that_product(db):
    add_fact(db, "acme", "O-1", date(2026, 10, 5), "B1", (("zoho:1", "Sourdough", "2", "10.00"), ("zoho:2", "Croissant", "10", "1.25")))
    add_fact(db, "acme", "O-2", date(2026, 10, 6), "B2", (("zoho:2", "Croissant", "2", "1.25"),))
    add_fact(db, "acme", "O-3", date(2026, 10, 6), "B3", (("zoho:3", "Cake", "1", "30.00"),))
    result = summary(db, "acme", product="zoho:2")

    assert (result["kpis"]["invoiced_value"], result["kpis"]["orders_with_value"], result["kpis"]["invoices_created"]) == ("15.00", 2, 2)
    assert [p["key"] for p in result["top_products"]] == ["zoho:2"]


@pytest.mark.parametrize(
    "args, message",
    [
        (("2026-10-21", "2026-10-01", None), "on or before"),
        (("21/10/2026", None, None), "YYYY-MM-DD"),
        (("2020-01-01", "2026-10-21", None), "three years"),
        (("2026-10-01", "2026-10-21", "hourly"), "Granularity"),
    ],
)
def test_invalid_filters_are_rejected(args, message):
    with pytest.raises(svc.AnalyticsError, match=message):
        svc.build_filters("acme", *args, None, None, today=TODAY)


def test_default_range_and_granularity():
    f = svc.build_filters("acme", None, None, None, None, None, today=TODAY)
    assert (f.date_from.isoformat(), f.date_to.isoformat(), f.granularity) == ("2026-07-24", "2026-10-21", "week")
    assert svc.default_granularity(date(2026, 10, 1), date(2026, 10, 21)) == "day"
    assert svc.default_granularity(date(2025, 1, 1), date(2026, 1, 1)) == "month"


# ---------------------------------------------------------------------------
# Missing historical data
# ---------------------------------------------------------------------------


def test_no_captured_values_is_unknown_not_zero(db):
    add_run(db, None, exported=[("L-1", "B1")], invoices=[("L-1", "B1")])  # a legacy run (before Analytics)
    result = summary(db, "default")

    assert result["kpis"]["invoiced_value"] is None and result["kpis"]["average_order_value"] is None
    assert result["kpis"]["orders_with_value"] is None
    assert all(point["value"] is None and not point["captured"] for point in result["series"])
    assert result["coverage"]["value_capture_started"] is None
    assert result["kpis"]["invoices_created"] == 1  # counts are still reported, as counts


def test_range_before_capture_is_unknown_and_after_capture_is_zero(db):
    add_fact(db, "acme", "O-1", date(2026, 10, 10))
    before = summary(db, "acme", "2026-09-01", "2026-09-30")
    after = summary(db, "acme", "2026-10-15", "2026-10-21")
    assert before["kpis"]["invoiced_value"] is None and before["coverage"]["range_starts_before_capture"] is True
    assert after["kpis"]["invoiced_value"] == "0.00" and after["kpis"]["orders_with_value"] == 0


def test_mixed_currencies_are_never_added(db):
    add_fact(db, "acme", "O-1", date(2026, 10, 5))
    add_fact(db, "acme", "O-2", date(2026, 10, 5), currency="GBP")
    result = summary(db, "acme")
    assert result["currency"] == "MIXED" and result["kpis"]["invoiced_value"] is None


# ---------------------------------------------------------------------------
# Value capture during sync
# ---------------------------------------------------------------------------


def sync(db, zoho, *orders, company="acme"):
    service = StubbedExportService(None, zoho)
    service.audit = AuditContext(tenant_id="t", actor_id="u", company_id=company)
    return asyncio.run(service.run_export(db, "2026-10-01", "2026-10-02", list(orders))), service


def test_sync_captures_order_value_and_run_company(db):
    zoho = FakeZoho()
    result, _service = sync(db, zoho, make_order("O-1"))

    fact = db.query(models.OrderFact).one()
    assert (fact.company_id, fact.unify_order_id, fact.zoho_invoice_id, fact.net_amount, fact.source) == ("acme", "O-1", "INV-1", Decimal("10.00"), "invoice_created")
    assert [(line.product_name, line.quantity, line.net_amount) for line in fact.lines] == [("Widget", Decimal("1"), Decimal("10.00"))]
    assert db.get(models.SyncRunCompany, result["sync_run_id"]).company_id == "acme"


def test_delivery_lines_from_unify_are_never_counted_as_products():
    from types import SimpleNamespace as NS

    order = NS(lines=[NS(quantity=2, price=4.0, line_type="product"), NS(quantity=0, price=1.0, line_type="product"), NS(quantity=1, price=7.5, line_type="delivery")])
    payload = [{"name": "Sourdough", "item_id": "11", "quantity": 2, "rate": 4.0}, {"name": "Delivery", "quantity": 1, "rate": 7.5}]
    flags = delivery_line_flags(order, payload)
    assert flags == [False, True]  # the zero-quantity line was skipped by the payload builder
    lines = analytics_lines(payload, "acme", flags)
    assert [line.product_key for line in lines] == ["zoho:11", "delivery-charge"]
    # Synthetic "Delivery charge" line appended by the builder
    synthetic = delivery_line_flags(NS(lines=[NS(quantity=1, price=3.0, line_type="product")]), [{"name": "Bread"}, {"name": "Delivery charge"}])
    assert synthetic == [False, True]
    # Counts that do not line up: fall back to name matching only, never guess
    assert delivery_line_flags(NS(lines=[]), [{"name": "Bread"}]) is None
    assert [line.product_key for line in analytics_lines([{"name": "Delivery charge", "quantity": 1, "rate": 5}], "acme", None)] == ["delivery-charge"]


def test_resync_and_linked_invoices_never_duplicate_values(db):
    zoho = FakeZoho()
    sync(db, zoho, make_order("O-1"))
    sync(db, zoho, make_order("O-1"))  # already exported
    zoho.add_invoice("O-2", "C-B1")  # exists in Zoho already: linked, not created
    sync(db, zoho, make_order("O-2"))

    facts = {f.unify_order_id: f.source for f in db.query(models.OrderFact)}
    assert facts == {"O-1": "invoice_created", "O-2": "invoice_linked"}


def test_failed_sync_captures_no_value(db):
    zoho = FakeZoho()
    zoho.create_behavior["O-1"] = "fail"
    sync(db, zoho, make_order("O-1"))
    assert db.query(models.OrderFact).count() == 0


def test_capture_failure_never_changes_the_sync_outcome(db, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("analytics storage unavailable")

    monkeypatch.setattr("app.services.export_service.analytics_lines", broken)
    zoho = FakeZoho()
    result, _service = sync(db, zoho, make_order("O-1"))

    assert details_by_id(result)["O-1"]["status"] == "created" and result["status"] == "success"
    assert db.query(models.ExportedOrder).one().status == "exported"
    assert db.query(models.OrderFact).count() == 0


def test_nondefault_company_fails_closed_without_ownership_table(tmp_path):
    from app.services.export_service import ExportServiceError
    engine = create_engine(f"sqlite:///{(tmp_path / 'isolated.db').as_posix()}", future=True)
    Base.metadata.create_all(bind=engine, tables=[t for t in Base.metadata.sorted_tables if t.name not in MIGRATION_ONLY_TABLES or t.name == "zconnect_export_events"])
    session = sessionmaker(bind=engine, future=True)()
    zoho = FakeZoho()
    try:
        with pytest.raises(ExportServiceError, match="ownership"):
            sync(session, zoho, make_order("O-1"), company="acme")
        assert zoho.create_calls == []
        assert session.query(models.SyncRun).count() == 0
    finally:
        session.close()
        engine.dispose()


def test_sync_still_works_before_the_analytics_migration(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'pre0003.db').as_posix()}", future=True)
    Base.metadata.create_all(bind=engine, tables=[t for t in Base.metadata.sorted_tables if t.name not in MIGRATION_ONLY_TABLES or t.name == "zconnect_export_events"])
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    try:
        result, _service = sync(session, FakeZoho(), make_order("O-1"), company="default")
        assert details_by_id(result)["O-1"]["status"] == "created"
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Authorization and tenant isolation (through the signed API)
# ---------------------------------------------------------------------------


def get(client, sign_request, path, query="", company="acme", role="user"):
    return client.get(path + query, headers=sign_request("GET", path, query=query, role=role, company=company))


def seed_two_companies(db):
    add_fact(db, "acme", "A-1", date(2026, 10, 5), "B1", (("zoho:1", "Acme bread", "1", "100.00"),))
    add_fact(db, "other", "X-1", date(2026, 10, 5), "B9", (("zoho:9", "Other cake", "1", "900.00"),))
    add_run(db, "acme", invoices=[("A-1", "B1")])
    add_run(db, "other", invoices=[("X-1", "B9")])
    add_run(db, None, invoices=[("L-1", "B5")])  # legacy run: default company


QUERY = "?date_from=2026-10-01&date_to=2026-10-21"


def test_each_company_sees_only_its_own_data(client, sign_request, db):
    seed_two_companies(db)
    acme = get(client, sign_request, "/api/analytics/summary", QUERY, company="acme").json()
    other = get(client, sign_request, "/api/analytics/summary", QUERY, company="other").json()
    default = get(client, sign_request, "/api/analytics/summary", QUERY, company="default").json()

    assert (acme["kpis"]["invoiced_value"], acme["kpis"]["invoices_created"], [p["name"] for p in acme["top_products"]]) == ("100.00", 1, ["Acme bread"])
    assert (other["kpis"]["invoiced_value"], other["kpis"]["invoices_created"]) == ("900.00", 1)
    assert default["kpis"]["invoiced_value"] is None and default["kpis"]["invoices_created"] == 1  # legacy run only
    filters = get(client, sign_request, "/api/analytics/filters", company="acme").json()
    assert [c["key"] for c in filters["customers"]] == ["B1"] and [p["name"] for p in filters["products"]] == ["Acme bread"]


@pytest.mark.parametrize("role", ["user", "admin"])
def test_other_company_is_forbidden_without_platform_role(client, sign_request, db, role):
    seed_two_companies(db)
    response = get(client, sign_request, "/api/analytics/summary", QUERY + "&company_id=other", company="acme", role=role)
    assert response.status_code == 403
    assert "900" not in response.text
    assert get(client, sign_request, "/api/analytics/filters", "?company_id=other", company="acme", role=role).status_code == 403


def test_platform_admin_can_choose_a_company(client, sign_request, db):
    seed_two_companies(db)
    response = get(client, sign_request, "/api/analytics/summary", QUERY + "&company_id=other", company="acme", role="platform_admin")
    assert response.status_code == 200 and response.json()["kpis"]["invoiced_value"] == "900.00"
    companies = get(client, sign_request, "/api/analytics/companies", company="acme", role="platform_admin").json()
    assert companies["can_switch"] is True and {c["id"] for c in companies["companies"]} == {"default", "acme", "other"}


def test_companies_endpoint_shows_only_own_company_to_others(client, sign_request, db):
    companies = get(client, sign_request, "/api/analytics/companies", company="acme").json()
    assert companies == {"companies": [{"id": "acme", "name": "Acme Bakery"}], "can_switch": False, "current": "acme"}


def test_company_is_part_of_the_signature(client, sign_request, db):
    seed_two_companies(db)
    headers = sign_request("GET", "/api/analytics/summary", query=QUERY, role="user", company="acme")
    headers["X-Zconnect-Company"] = "other"  # tampered after signing
    assert client.get("/api/analytics/summary" + QUERY, headers=headers).status_code == 401
    headers = sign_request("GET", "/api/analytics/summary", query=QUERY, role="user", company="acme")
    del headers["X-Zconnect-Company"]
    assert client.get("/api/analytics/summary" + QUERY, headers=headers).status_code == 401
    bad = sign_request("GET", "/api/analytics/summary", query=QUERY, role="user", company="Not A Company!")
    assert client.get("/api/analytics/summary" + QUERY, headers=bad).status_code == 401


def test_invalid_filters_return_400(client, sign_request, db):
    response = get(client, sign_request, "/api/analytics/summary", "?date_from=2026-10-21&date_to=2026-10-01")
    assert response.status_code == 400 and "on or before" in response.json()["detail"]


def test_missing_analytics_tables_return_503(sign_request):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(bind=engine, tables=[t for t in Base.metadata.sorted_tables if t.name not in models.ANALYTICS_TABLES])
    session = sessionmaker(bind=engine, future=True)()
    app.dependency_overrides[get_db] = lambda: session
    try:
        with TestClient(app) as client:
            response = get(client, sign_request, "/api/analytics/summary", QUERY)
        assert response.status_code == 503 and "0003_analytics" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_db, None)
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Migration 0003_analytics
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import inspect as sa_inspect, text  # noqa: E402

from app.config import settings  # noqa: E402
from tests.test_schema_preflight import needs_postgres, scratch_db  # noqa: E402,F401  (fixture)

BACKEND_DIR = Path(__file__).resolve().parents[1]


def migration_config(monkeypatch, url):
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return config


def test_migration_0003_adds_only_analytics_tables(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'm3.db').as_posix()}"
    config = migration_config(monkeypatch, url)
    command.upgrade(config, "0002_export_events")
    engine = create_engine(url, future=True)
    before = set(sa_inspect(engine).get_table_names())
    command.upgrade(config, "0003_analytics")
    after = set(sa_inspect(engine).get_table_names())
    assert after - before == set(models.ANALYTICS_TABLES)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT id, name FROM zconnect_companies")).all() == [("default", "Default company")]
        assert conn.execute(text("SELECT version_num FROM zconnect_alembic_version")).scalar() == "0003_analytics"
    command.downgrade(config, "0002_export_events")
    assert set(sa_inspect(engine).get_table_names()) == before
    engine.dispose()


@needs_postgres
def test_migration_0003_locks_down_supabase_api_roles(scratch_db, monkeypatch):
    import psycopg

    libpq = scratch_db.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(libpq, autocommit=True) as conn:
        for role in ("anon", "authenticated"):
            if not conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
                conn.execute(f"CREATE ROLE {role} NOLOGIN")
        conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated")
        conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated")
    command.upgrade(migration_config(monkeypatch, scratch_db), "0003_analytics")
    with psycopg.connect(libpq) as conn:
        for table in models.ANALYTICS_TABLES:
            for role in ("anon", "authenticated"):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert conn.execute("select has_table_privilege(%s, %s, %s)", (role, f"public.{table}", privilege)).fetchone()[0] is False
            assert conn.execute("select relrowsecurity from pg_class where oid = %s::regclass", (f"public.{table}",)).fetchone()[0] is True
        for sequence in ("zconnect_order_facts_id_seq", "zconnect_order_lines_id_seq"):
            assert conn.execute("select has_sequence_privilege('anon', %s, 'USAGE')", (f"public.{sequence}",)).fetchone()[0] is False
