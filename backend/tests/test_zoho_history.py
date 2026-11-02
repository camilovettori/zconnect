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
