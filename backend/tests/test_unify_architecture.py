from __future__ import annotations

import time
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api import routes
from app.db import Base, get_db
from app.main import app
from app import models
from app.schemas import OrderLine, UnifyOrder, UnifyOrderPreview
from app.services.unify_service import UnifyService
from app.services.zoho_service import ZohoService


@pytest.fixture()
def test_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    db = SessionLocal()
    db.add(models.Setting(key="UNIFY_BASE_URL", value="https://api.unifyordering.com"))
    db.add(models.Setting(key="UNIFY_CLIENT_ID", value="client"))
    db.add(models.Setting(key="UNIFY_CLIENT_SECRET", value="secret"))
    db.add(models.Setting(key="ZOHO_BASE_URL", value="https://www.zohoapis.eu"))
    db.add(models.Setting(key="ZOHO_CLIENT_ID", value="zoho-client"))
    db.add(models.Setting(key="ZOHO_CLIENT_SECRET", value="zoho-secret"))
    db.add(models.Setting(key="ZOHO_REFRESH_TOKEN", value="refresh"))
    db.add(models.Setting(key="ZOHO_ORG_ID", value="org"))
    db.commit()

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield db
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()


@pytest.fixture()
def client(test_db):
    return TestClient(app)


@pytest.mark.asyncio
async def test_preview_fetch_skips_item_hydration(monkeypatch):
    preview_calls = {"items": 0, "detail": 0}
    raw_orders = [
        {"order_id": "O-1", "buyerId": "B-1", "deliveryDate": "2026-04-02", "buyerName": "Buyer One", "totalNetAmount": 10},
        {"order_id": "O-2", "buyerId": "B-2", "deliveryDate": "2026-04-02", "buyerName": "Buyer Two", "totalNetAmount": 20},
    ]

    async def fake_request(self, method, path, *, params=None, force_refresh=False):
        class DummyResponse:
            def json(self_nonlocal):
                return raw_orders

            text = ""

        assert path == "/v1/orders"
        return DummyResponse()

    async def fake_fetch_order_detail(self, order_id):
        preview_calls["detail"] += 1
        return {"deliveryDate": "2026-04-02"}

    async def fake_fetch_order_items(self, order_id):
        preview_calls["items"] += 1
        raise AssertionError("fetch_order_items should not run during preview fetch")

    async def fake_fetch_buyer_organisation(self, buyer_id):
        return {}

    monkeypatch.setattr(UnifyService, "_request", fake_request)
    monkeypatch.setattr(UnifyService, "fetch_order_detail", fake_fetch_order_detail)
    monkeypatch.setattr(UnifyService, "fetch_order_items", fake_fetch_order_items)
    monkeypatch.setattr(UnifyService, "fetch_buyer_organisation", fake_fetch_buyer_organisation)

    svc = UnifyService("https://api.unifyordering.com", "client", "secret")
    started = time.perf_counter()
    orders = await svc.fetch_orders_preview("2026-04-02", "2026-04-02")
    duration = time.perf_counter() - started

    assert duration < 1.0
    assert len(orders) == 2
    assert orders[0].order_id == "O-1"
    assert orders[1].order_id == "O-2"
    assert svc.last_fetch_debug["previewOrdersCount"] == 2
    assert svc.last_fetch_debug["rawOrdersCount"] == 2
    assert preview_calls["detail"] == 0
    assert preview_calls["items"] == 0


@pytest.mark.usefixtures("client")
def test_preview_route_returns_fast_summary_and_logs(client, monkeypatch, caplog):
    preview_calls = {"items": 0}

    async def fake_request(self, method, path, *, params=None, force_refresh=False):
        class DummyResponse:
            def json(self_nonlocal):
                return [
                    {"order_id": "O-1", "buyerId": "B-1", "deliveryDate": "2026-04-02", "buyerName": "Buyer One", "totalNetAmount": 10},
                    {"order_id": "O-2", "buyerId": "B-2", "deliveryDate": "2026-04-02", "buyerName": "Buyer Two", "totalNetAmount": 20},
                ]

            text = ""

        return DummyResponse()

    async def fake_fetch_order_items(self, order_id):
        preview_calls["items"] += 1
        raise AssertionError("fetch_order_items should not run during preview fetch")

    monkeypatch.setattr(UnifyService, "_request", fake_request)
    monkeypatch.setattr(UnifyService, "fetch_order_items", fake_fetch_order_items)

    caplog.set_level("INFO")
    started = time.perf_counter()
    response = client.post("/api/unify/fetch-orders", json={"date_from": "2026-04-02", "date_to": "2026-04-02"})
    duration = time.perf_counter() - started

    assert response.status_code == 200
    payload = response.json()
    assert duration < 1.0
    assert payload["total_orders"] == 2
    assert payload["debug_summary"]["previewOrdersCount"] == 2
    assert payload["debug_summary"]["rawOrdersCount"] == 2
    assert preview_calls["items"] == 0
    assert any("TEMP unify preview fetch success" in record.message for record in caplog.records)


@pytest.mark.usefixtures("client")
def test_preview_route_returns_partial_content_when_truncated(client, monkeypatch):
    async def fake_fetch_orders_preview(self, date_from, date_to, max_pages=5):
        self.last_fetch_debug = {
            "previewTruncated": True,
            "previewTruncationReason": "max_pages_reached",
            "rawOrdersCount": 1,
            "rawPageCount": 1,
            "previewOrdersCount": 1,
        }
        return [
            UnifyOrderPreview(
                order_id="O-1",
                customer_name="Buyer One",
                buyer_id="B-1",
                delivery_date="2026-04-02",
                total=10,
                status="confirmed",
                preview_status="ready",
                preview_reason="",
            )
        ]

    monkeypatch.setattr(UnifyService, "fetch_orders_preview", fake_fetch_orders_preview)

    response = client.post("/api/unify/fetch-orders", json={"date_from": "2026-04-02", "date_to": "2026-04-02"})

    assert response.status_code == 206
    payload = response.json()
    assert payload["debug_summary"]["previewTruncated"] is True
    assert payload["debug_summary"]["previewTruncationReason"] == "max_pages_reached"


@pytest.mark.usefixtures("client")
def test_lazy_order_detail_route_returns_lines(client, monkeypatch, caplog):
    async def fake_fetch_order_detail(self, order_id):
        return {
            "orderId": order_id,
            "buyerId": "B-9",
            "deliveryDate": "2026-04-02",
            "orderDate": "2026-04-01",
            "buyerName": "Buyer Nine",
            "totalNetAmount": 60,
        }

    async def fake_fetch_order_items(self, order_id):
        return [
            {
                "productModificationId": "PM-1",
                "displayName": "Widget",
                "quantity": 2,
                "totalNetAmount": {"amount": 40},
                "status": "confirmed",
            },
            {
                "productModificationId": "PM-2",
                "displayName": "Shipping",
                "quantity": 1,
                "totalNetAmount": {"amount": 20},
                "status": "confirmed",
            },
        ]

    monkeypatch.setattr(UnifyService, "fetch_order_detail", fake_fetch_order_detail)
    monkeypatch.setattr(UnifyService, "fetch_order_items", fake_fetch_order_items)

    caplog.set_level("INFO")
    response = client.get("/api/unify/order-preview/O-9")

    assert response.status_code == 200
    payload = response.json()
    assert payload["order_id"] == "O-9"
    assert len(payload["lines"]) == 2
    assert payload["lines"][0]["item_name"] == "Widget"
    assert payload["lines"][1]["item_name"] == "Shipping"
    assert any("TEMP unify order detail lazy-load success" in record.message for record in caplog.records)


@pytest.mark.usefixtures("client")
def test_single_order_sync_hydrates_only_selected_order_and_creates_invoice(client, monkeypatch, test_db, caplog):
    hydration_calls = []
    preview_calls = {"count": 0}
    preview_orders = [
        UnifyOrderPreview(
            order_id="O-1",
            customer_name="Buyer One",
            buyer_id="B-1",
            delivery_date="2026-04-02",
            total=10,
            status="confirmed",
            preview_status="ready",
            preview_reason="",
        ),
        UnifyOrderPreview(
            order_id="O-2",
            customer_name="Buyer Two",
            buyer_id="B-2",
            delivery_date="2026-04-02",
            total=20,
            status="confirmed",
            preview_status="ready",
            preview_reason="",
        ),
    ]

    async def fake_fetch_orders_preview(self, date_from, date_to):
        preview_calls["count"] += 1
        self.last_fetch_debug = {"previewOrdersCount": 2, "rawOrdersCount": 2}
        return preview_orders

    async def fake_fetch_order_with_items(self, order_id):
        hydration_calls.append(order_id)
        return UnifyOrder(
            order_id=order_id,
            customer_name=f"Buyer {order_id}",
            order_date="2026-04-02",
            delivery_date="2026-04-02",
            lines=[
                OrderLine(item_sku="SKU-1", item_name="Widget", quantity=1, price=10.0),
            ],
            total=10.0,
            buyer_id=f"B-{order_id}",
            status="confirmed",
            preview_status="ready",
            preview_reason="",
            total_net_amount=10.0,
            total_vat_amount=0.0,
            total_delivery_fee=0.0,
        )

    async def fake_find_contact_by_name(self, name):
        return None

    async def fake_create_contact(self, name):
        return "C-1"

    async def fake_find_item_by_sku(self, sku):
        return None

    async def fake_create_item(self, sku, name, price):
        return "I-1"

    async def fake_get_tax_by_rate(self, rate):
        return ""

    async def fake_create_draft_invoice(self, contact_id, lines, reference_number):
        return "INV-1"

    monkeypatch.setattr(UnifyService, "fetch_orders_preview", fake_fetch_orders_preview)
    monkeypatch.setattr(UnifyService, "fetch_order_with_items", fake_fetch_order_with_items)
    monkeypatch.setattr(ZohoService, "find_contact_by_name", fake_find_contact_by_name)
    monkeypatch.setattr(ZohoService, "create_contact", fake_create_contact)
    monkeypatch.setattr(ZohoService, "find_item_by_sku", fake_find_item_by_sku)
    monkeypatch.setattr(ZohoService, "create_item", fake_create_item)
    monkeypatch.setattr(ZohoService, "get_tax_by_rate", fake_get_tax_by_rate)
    monkeypatch.setattr(ZohoService, "create_draft_invoice", fake_create_draft_invoice)

    caplog.set_level("INFO")
    started = time.perf_counter()
    response = client.post(
        "/api/export/run",
        json={"date_from": "2026-04-02", "date_to": "2026-04-02", "order_ids": ["O-1"]},
    )
    duration = time.perf_counter() - started

    assert response.status_code == 200
    payload = response.json()
    assert duration < 1.0
    assert payload["status"] == "success"
    assert payload["created"] == 1
    assert hydration_calls == ["O-1"]
    assert preview_calls["count"] == 0
    exported_orders = test_db.query(models.ExportedOrder).all()
    assert len(exported_orders) == 1
    assert exported_orders[0].unify_order_id == "O-1"
    assert any("TEMP unify single-order sync completed" in record.message for record in caplog.records)


@pytest.mark.usefixtures("client")
def test_csv_preview_route_groups_orders_and_returns_full_lines(client):
    csv_text = "\n".join(
        [
            "order_id,customer_name,buyer_id,order_date,delivery_date,delivery_address,item_name,item_sku,quantity,price,total,tax_percentage",
            "CSV-1,Unify Customer,B-1,2026-04-02,2026-04-03,1 Main St,Widget,SKU-1,2,5.00,10.00,23",
            "CSV-1,Unify Customer,B-1,2026-04-02,2026-04-03,1 Main St,Shipping,DELIVERY,1,2.50,2.50,23",
        ]
    )

    response = client.post(
        "/api/unify/preview-csv",
        content=csv_text.encode("utf-8"),
        headers={"Content-Type": "text/csv; charset=utf-8"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_orders"] == 1
    assert payload["total_customers"] == 1
    assert payload["debug_summary"]["source"] == "csv"
    assert payload["orders"][0]["order_id"] == "CSV-1"
    assert len(payload["orders"][0]["lines"]) == 2
    assert payload["orders"][0]["lines"][0]["item_name"] == "Widget"
    assert payload["orders"][0]["lines"][1]["line_type"] == "delivery"


@pytest.mark.usefixtures("client")
def test_csv_preview_route_accepts_portuguese_order_headers(client):
    csv_text = "\n".join(
        [
            "Número do pedido,Nome do cliente,Buyer ID,Data do pedido,Data de entrega,Endereço de entrega,Nome do item,SKU,Quantidade,Preço,Total,IVA %",
            "PED-1,Cliente Exemplo,B-1,2026-04-02,2026-04-03,Rua 1,Produto Exemplo,SKU-1,2,5.00,10.00,23",
        ]
    )

    response = client.post(
        "/api/unify/preview-csv",
        content=csv_text.encode("utf-8"),
        headers={"Content-Type": "text/csv; charset=utf-8"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_orders"] == 1
    assert payload["orders"][0]["order_id"] == "PED-1"


@pytest.mark.usefixtures("client")
def test_csv_export_route_reuses_export_pipeline(client, monkeypatch, test_db):
    csv_text = "\n".join(
        [
            "order_id,customer_name,buyer_id,order_date,delivery_date,delivery_address,item_name,item_sku,quantity,price,total,tax_percentage",
            "CSV-2,Unify Customer,B-2,2026-04-02,2026-04-03,2 Main St,Widget,SKU-2,1,12.00,12.00,23",
        ]
    )

    preview_response = client.post(
        "/api/unify/preview-csv",
        content=csv_text.encode("utf-8"),
        headers={"Content-Type": "text/csv; charset=utf-8"},
    )
    assert preview_response.status_code == 200
    preview_payload = preview_response.json()

    class CsvZohoService:
        async def find_contact_by_name(self, name):
            return None

        async def create_contact(self, name):
            return "CONTACT-CSV-1"

        async def get_contact_by_id(self, contact_id):
            return {"contact": {"contact_id": contact_id, "contact_name": "Unify Customer"}}

        async def update_contact_name(self, contact_id, name):
            return True

        async def find_item_by_name(self, name):
            return None

        async def create_item(self, name, rate, tax_id):
            return "ITEM-CSV-1"

        async def create_draft_invoice_from_payload(self, payload):
            self.last_payload = payload
            return "INV-CSV-1"

    monkeypatch.setattr(ZohoService, "find_contact_by_name", CsvZohoService.find_contact_by_name)
    monkeypatch.setattr(ZohoService, "create_contact", CsvZohoService.create_contact)
    monkeypatch.setattr(ZohoService, "get_contact_by_id", CsvZohoService.get_contact_by_id)
    monkeypatch.setattr(ZohoService, "update_contact_name", CsvZohoService.update_contact_name)
    monkeypatch.setattr(ZohoService, "find_item_by_name", CsvZohoService.find_item_by_name)
    monkeypatch.setattr(ZohoService, "create_item", CsvZohoService.create_item)
    monkeypatch.setattr(ZohoService, "create_draft_invoice_from_payload", CsvZohoService.create_draft_invoice_from_payload)

    response = client.post(
        "/api/export/run-csv",
        json={
            "orders": preview_payload["orders"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["created"] == 1
    assert payload["details"][0]["status"] == "created"
    exported_orders = test_db.query(models.ExportedOrder).all()
    assert len(exported_orders) == 1
