"""Historical Zoho invoice snapshot: validation, deduplication and company isolation."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.db import Base
from app.services.zoho_history_service import (
    HistoricalImportError, _normalise_invoice, persist_snapshot,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all([
        models.Company(id="default", name="Default"),
        models.Company(id="other", name="Other"),
    ])
    session.commit()
    yield session
    session.close()
    engine.dispose()


def invoice(invoice_id="123", amount="20.00"):
    return {
        "invoice_id": invoice_id,
        "date": "2026-10-10",
        "invoice_number": "INV-123",
        "customer_id": "customer-1",
        "customer_name": "Cafe A",
        "status": "sent",
        "currency_code": "EUR",
        "sub_total": amount,
        "line_items": [
            {"line_item_id": "line-1", "item_id": "item-7", "name": "Brownie", "quantity": 2, "item_total": amount}
        ],
    }


def test_invoices_store_once_and_reimport_updates(db):
    snapshot = [_normalise_invoice(invoice())]
    result = persist_snapshot(db, company_id="default", zoho_org_id="org-1", snapshot=snapshot)
    assert result == {"created": 1, "updated": 0, "total": 1}

    changed = invoice(amount="24.00")
    changed["status"] = "paid"
    result = persist_snapshot(db, company_id="default", zoho_org_id="org-1", snapshot=[_normalise_invoice(changed)])
    assert result == {"created": 0, "updated": 1, "total": 1}
    rows = db.scalars(select(models.HistoricalInvoice)).all()
    assert len(rows) == 1
    assert rows[0].status == "paid"
    assert rows[0].net_amount == Decimal("24.00")
    assert len(rows[0].lines) == 1
    assert rows[0].lines[0].product_name == "Brownie"


def test_same_zoho_id_is_company_scoped(db):
    snapshot = [_normalise_invoice(invoice())]
    persist_snapshot(db, company_id="default", zoho_org_id="org-1", snapshot=snapshot)
    persist_snapshot(db, company_id="other", zoho_org_id="org-1", snapshot=snapshot)
    assert len(db.scalars(select(models.HistoricalInvoice)).all()) == 2


def test_missing_total_cannot_become_fake_zero():
    bad = invoice()
    del bad["sub_total"]
    with pytest.raises(HistoricalImportError, match="sub_total"):
        _normalise_invoice(bad)


def test_duplicate_ids_are_rejected(db):
    snapshot = [_normalise_invoice(invoice())]
    with pytest.raises(HistoricalImportError, match="Duplicate"):
        persist_snapshot(db, company_id="default", zoho_org_id="org-1", snapshot=snapshot + snapshot)
    assert not db.scalars(select(models.HistoricalInvoice)).all()


def test_invoice_does_not_accept_nonfinite_money():
    bad = invoice(amount="NaN")
    with pytest.raises(HistoricalImportError, match="Non-finite"):
        _normalise_invoice(bad)


def test_invoice_level_discount_requires_reconciliation():
    record = invoice(amount="20.00")
    record["sub_total"] = "18.00"
    with pytest.raises(HistoricalImportError, match="subtotal differs"):
        _normalise_invoice(record)


def test_tiny_rounding_difference_is_accepted():
    record = invoice(amount="20.00")
    record["sub_total"] = "19.99"
    assert _normalise_invoice(record)["net_amount"] == Decimal("19.99")


def test_historical_invoice_date_change_never_double_counts(db):
    from app.services import analytics_service
    original = models.OrderFact(
        company_id="default", unify_order_id="UNIFY-1", zoho_invoice_id="123",
        customer_key="customer-1", customer_name="Cafe A",
        invoice_date=date(2026, 10, 9), currency="EUR",
        net_amount=Decimal("20.00"), line_count=1, source="invoice_created"
    )
    db.add(original)
    db.flush()
    db.add(models.OrderLineFact(
        order_fact_id=original.id, company_id="default",
        product_key="zoho:item-7", product_name="Brownie",
        quantity=Decimal("2"), unit_price=Decimal("10"),
        net_amount=Decimal("20.00")
    ))
    db.commit()
    persist_snapshot(db, company_id="default", zoho_org_id="org-1", snapshot=[_normalise_invoice(invoice())])
    def summary(start, end):
        return analytics_service.compute_summary(
            db, analytics_service.AnalyticsFilters(
                company_id="default", date_from=start, date_to=end, granularity="day"
            )
        )
    old_date = summary(date(2026, 10, 9), date(2026, 10, 9))
    new_date = summary(date(2026, 10, 10), date(2026, 10, 10))
    assert old_date["kpis"]["invoiced_value"] is None
    assert new_date["kpis"]["invoiced_value"] == "20.00"
    assert new_date["top_customers"][0]["name"] == "Cafe A"



def test_dry_run_preview_summarises_without_customer_data():
    from app.services.zoho_history_service import preview_snapshot
    first = _normalise_invoice(invoice(invoice_id="inv1", amount="20.00"))
    second_data = invoice(invoice_id="inv2", amount="12.00")
    second_data["status"] = "paid"
    second_data["date"] = "2026-10-12"
    second = _normalise_invoice(second_data)
    preview = preview_snapshot([first, second])
    assert preview == {
        "validated_invoices": 2,
        "status_counts": {"paid": 1, "sent": 1},
        "net_by_currency": {"EUR": "32.00"},
        "first_invoice_date": "2026-10-10",
        "last_invoice_date": "2026-10-12",
    }
    assert "Cafe A" not in str(preview)
    assert "inv1" not in str(preview)

class FakeResponse:
    status_code = 200
    def __init__(self, record):
        self.record = record
    def json(self):
        return {"invoice": self.record}


class FakeZoho:
    org_id = "org-1"
    def __init__(self, records):
        self.records = records
        self.detail_calls = []
    async def list_all_invoices_for_duplicate_check(self):
        return [{"invoice_id": x["invoice_id"], "date": x["date"]} for x in self.records]
    async def _request(self, method, path):
        assert method == "GET"
        invoice_id = path.rsplit("/", 1)[-1]
        self.detail_calls.append(invoice_id)
        return FakeResponse(next(x for x in self.records if x["invoice_id"] == invoice_id))


@pytest.mark.asyncio
async def test_incremental_batch_resumes_without_duplicate_detail_calls(db):
    from app.services.zoho_history_service import import_next_batch
    records = []
    for i, day in enumerate(["2026-10-12", "2026-10-12", "2026-10-11"]):
        row = invoice(invoice_id=str(i))
        row["date"] = day
        records.append(row)
    zoho = FakeZoho(records)
    first = await import_next_batch(zoho, db, company_id="default", max_details=2)
    assert first["imported"] == 2
    assert first["remaining_estimate"] == 1
    second = await import_next_batch(zoho, db, company_id="default", max_details=2)
    assert second["imported"] == 1
    assert zoho.detail_calls == ["0", "1", "2"]
    assert len(db.scalars(select(models.HistoricalInvoice)).all()) == 3


@pytest.mark.asyncio
async def test_batch_does_not_split_an_invoice_date(db):
    from app.services.zoho_history_service import import_next_batch
    records = []
    for i in range(3):
        row = invoice(invoice_id=str(i))
        records.append(row)
    zoho = FakeZoho(records)
    result = await import_next_batch(zoho, db, company_id="default", max_details=2)
    assert result["imported"] == 0
    assert result["blocked_large_day"] is True
    assert zoho.detail_calls == []


@pytest.mark.asyncio
async def test_cheap_preview_never_requests_invoice_details(db):
    from app.services.zoho_history_service import preview_next_batch
    zoho = FakeZoho([invoice(invoice_id="1"), invoice(invoice_id="2")])
    result = await preview_next_batch(zoho, db, company_id="default")
    assert result["remaining_estimate"] == 2
    assert zoho.detail_calls == []
