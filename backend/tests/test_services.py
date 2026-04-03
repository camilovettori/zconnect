import pytest
import asyncio
import base64
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db import Base
import app.services.unify_service as unify_module
from app.services.unify_service import UnifyService, UnifyServiceError
from app.services import zoho_service as zoho_module
from app.services.zoho_service import ZohoService, ZohoServiceError, ZOHO_TOKEN_URL
from app.services.export_service import ExportService
from app.api import routes
from app.schemas import UnifyOrder, OrderLine


class DummyAsyncClient:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.handler(method, url, **kwargs)

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)


class DummyResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data


@pytest.mark.asyncio
async def test_unify_fetch_orders_parses_response(monkeypatch):
    unify_module._TOKEN_CACHE.clear()
    unify_module._BUYERS_CACHE.clear()
    list_payload = [
        {
            "order_id": "123",
            "buyerId": "B-1",
            "deliveryDate": "2026-03-15",
            "buyer": {"id": "B-1"},
            "buyerName": "Acme Corp LTD",
            "totalNetAmount": {"amount": 200},
        }
    ]
    calls = []

    def handler(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url == "https://oauth.unifyordering.com/oauth2/token":
            assert kwargs["headers"]["Authorization"] == "Basic Y2xpZW50OnNlY3JldA=="
            assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
            assert kwargs["data"] == {"grant_type": "client_credentials"}
            return DummyResponse(status_code=200, json_data={"access_token": "token-1", "expires_in": 3600})
        if url.endswith("/v1/orders"):
            return DummyResponse(status_code=200, json_data=list_payload)
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=10: DummyAsyncClient(handler))

    svc = UnifyService("https://api.unify.example", "client", "secret")
    orders = await svc.fetch_orders_preview("2026-03-01", "2026-03-31")

    assert len(orders) == 1
    assert orders[0].order_id == "123"
    assert orders[0].customer_name == "Acme Corp LTD"
    assert orders[0].buyer_id == "B-1"
    assert orders[0].status == "confirmed"
    assert orders[0].total == 2.0
    assert orders[0].preview_status == "ready"
    assert calls[0][1] == "https://oauth.unifyordering.com/oauth2/token"
    assert calls[1][1].endswith("/v1/orders")
    assert len(calls) == 2
    assert calls[1][2]["headers"]["Authorization"] == "Bearer token-1"


@pytest.mark.asyncio
async def test_unify_fetch_orders_handles_paginated_items(monkeypatch):
    unify_module._TOKEN_CACHE.clear()
    unify_module._BUYERS_CACHE.clear()
    list_payload = [{"order_id": "ABC-1", "buyerId": "B-2", "deliveryDate": "2026-03-15", "buyer": {"id": "B-2"}}]
    calls = []

    def handler(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url == "https://oauth.unifyordering.com/oauth2/token":
            return DummyResponse(status_code=200, json_data={"access_token": "token-2", "expires_in": 3600})
        if url.endswith("/v1/orders"):
            return DummyResponse(status_code=200, json_data=list_payload)
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=10: DummyAsyncClient(handler))

    svc = UnifyService("https://api.unify.example", "client", "secret")
    orders = await svc.fetch_orders_preview("2026-03-01", "2026-03-31")

    assert len(orders) == 1
    assert orders[0].order_id == "ABC-1"
    assert orders[0].preview_status == "ready"
    assert orders[0].status == "confirmed"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unify_fetch_orders_preview_truncates_at_max_pages_and_uses_restricted_statuses(monkeypatch):
    unify_module._TOKEN_CACHE.clear()
    unify_module._BUYERS_CACHE.clear()

    calls = []
    page1 = {
        "orders": [
            {
                "order_id": "P-1",
                "deliveryDate": "2026-04-18",
                "createTime": "2026-04-18T12:00:00Z",
            },
            {
                "order_id": "P-2",
                "deliveryDate": "2026-04-17",
                "createTime": "2026-04-18T11:00:00Z",
            },
        ],
        "nextPageToken": "page-2",
    }

    def handler(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url == "https://oauth.unifyordering.com/oauth2/token":
            return DummyResponse(status_code=200, json_data={"access_token": "token-3", "expires_in": 3600})
        if url.endswith("/v1/orders"):
            assert kwargs["params"]["statuses"] == ["confirmed", "received", "checked"]
            return DummyResponse(status_code=200, json_data=page1)
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=10: DummyAsyncClient(handler))

    svc = UnifyService("https://api.unify.example", "client", "secret")
    orders = await svc.fetch_orders_preview("2026-04-01", "2026-04-30", max_pages=1)

    assert len(orders) == 2
    assert svc.last_fetch_debug["rawPageCount"] == 1
    assert svc.last_fetch_debug["rawOrdersCount"] == 2
    assert svc.last_fetch_debug["previewTruncated"] is True
    assert svc.last_fetch_debug["previewTruncationReason"] == "max_pages_reached"
    assert svc.last_fetch_debug["earlyStopReason"] is None
    assert len([call for call in calls if call[1].endswith("/v1/orders")]) == 1


@pytest.mark.asyncio
async def test_unify_fetch_orders_preview_early_stops_when_delivery_dates_descend_past_range(monkeypatch):
    unify_module._TOKEN_CACHE.clear()
    unify_module._BUYERS_CACHE.clear()

    calls = []
    pages = [
        {
            "orders": [
                {
                    "order_id": "E-1",
                    "deliveryDate": "2026-04-12",
                    "createTime": "2026-04-12T12:00:00Z",
                },
                {
                    "order_id": "E-2",
                    "deliveryDate": "2026-04-11",
                    "createTime": "2026-04-12T11:00:00Z",
                },
            ],
            "nextPageToken": "page-2",
        },
        {
            "orders": [
                {
                    "order_id": "E-3",
                    "deliveryDate": "2026-04-09",
                    "createTime": "2026-04-10T12:00:00Z",
                },
                {
                    "order_id": "E-4",
                    "deliveryDate": "2026-04-08",
                    "createTime": "2026-04-10T11:00:00Z",
                },
            ],
            "nextPageToken": "page-3",
        },
    ]

    def handler(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url == "https://oauth.unifyordering.com/oauth2/token":
            return DummyResponse(status_code=200, json_data={"access_token": "token-4", "expires_in": 3600})
        if url.endswith("/v1/orders"):
            order_requests = [call for call in calls if call[1].endswith("/v1/orders")]
            if len(order_requests) == 1:
                return DummyResponse(status_code=200, json_data=pages[0])
            if len(order_requests) == 2:
                return DummyResponse(status_code=200, json_data=pages[1])
            raise AssertionError("Unexpected third /v1/orders request")
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=10: DummyAsyncClient(handler))

    svc = UnifyService("https://api.unify.example", "client", "secret")
    orders = await svc.fetch_orders_preview("2026-04-10", "2026-04-30", max_pages=None)

    assert len(orders) == 2
    assert svc.last_fetch_debug["rawPageCount"] == 2
    assert svc.last_fetch_debug["previewTruncated"] is False
    assert svc.last_fetch_debug["earlyStopReason"] == "older_than_selected_range"
    assert svc.last_fetch_debug["preview_count"] == 2
    assert svc.last_fetch_debug["total_in_range_orders"] == 2
    assert svc.last_fetch_debug["dropped_out_of_range_count"] == 2
    assert svc.last_fetch_debug["orderingAssessment"]["deliveryDateDescendingObserved"] is True
    assert svc.last_fetch_debug["orderingAssessment"]["createTimeDescendingObserved"] is True
    assert svc.last_fetch_debug["orderingAssessment"]["earlyStopEligible"] is True
    assert len([call for call in calls if call[1].endswith("/v1/orders")]) == 2


@pytest.mark.asyncio
async def test_zoho_service_methods_raise_on_missing_refresh_token():
    svc = ZohoService("https://www.zohoapis.eu", "client", "secret", "", "org")
    with pytest.raises(ZohoServiceError, match="Zoho refresh token is missing"):
        await svc._headers()


@pytest.mark.asyncio
async def test_unify_service_uses_oauth_for_connection_check(monkeypatch):
    unify_module._TOKEN_CACHE.clear()
    calls = []

    def handler(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url == "https://oauth.unifyordering.com/oauth2/token":
            return DummyResponse(status_code=200, json_data={"access_token": "token-123", "expires_in": 3600})
        if url.endswith("/v1/orders"):
            return DummyResponse(status_code=200, json_data=[{"order_id": "1", "customer_name": "Test", "line_items": []}])
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=10: DummyAsyncClient(handler))

    svc = UnifyService("https://api.unifyordering.com", "client-id", "client-secret")
    assert await svc.test_connection() is True
    assert calls[0][1] == "https://oauth.unifyordering.com/oauth2/token"
    assert calls[1][1].endswith("/v1/orders")
    assert calls[1][2]["headers"]["Authorization"] == "Bearer token-123"
    assert calls[1][2]["params"] == {"pageSize": 1}


@pytest.mark.asyncio
async def test_zoho_create_draft_invoice_payload(monkeypatch):
    zoho_module._TOKEN_CACHE.clear()
    captured = {}

    def handler(method, url, **kwargs):
        if url == ZOHO_TOKEN_URL:
            return DummyResponse(status_code=200, json_data={"access_token": "token-1", "expires_in_sec": 3600})
        if method == "POST" and url.endswith("/invoice/v3/invoices"):
            captured.update(kwargs["json"])
            return DummyResponse(status_code=201, json_data={"invoice": {"invoice_id": "INV-1"}})
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=10: DummyAsyncClient(handler))

    svc = ZohoService("https://www.zohoapis.eu", "client", "secret", "refresh", "org")
    invoice_id = await svc.create_draft_invoice("C1", [{"item_id": "I1", "name": "Product", "quantity": 1, "rate": 100}], "REF-1")

    assert invoice_id == "INV-1"
    assert captured["is_inclusive_tax"] is False


@pytest.mark.asyncio
async def test_zoho_refreshes_token_before_connection_check(monkeypatch):
    zoho_module._TOKEN_CACHE.clear()

    seen = []

    def handler(method, url, **kwargs):
        seen.append((method, url))
        if url == ZOHO_TOKEN_URL:
            return DummyResponse(status_code=200, json_data={"access_token": "token-1", "expires_in_sec": 3600})
        if method == "GET" and url.endswith("/invoice/v3/contacts"):
            return DummyResponse(status_code=200, json_data={"contacts": []})
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=10: DummyAsyncClient(handler))

    svc = ZohoService("https://www.zohoapis.eu", "client", "secret", "refresh", "org")
    assert await svc.test_connection() is True
    assert seen[0][1] == ZOHO_TOKEN_URL
    assert seen[1][1].endswith("/invoice/v3/contacts")


@pytest.mark.asyncio
async def test_zoho_contact_lookup_and_update_use_contact_id_endpoint(monkeypatch):
    zoho_module._TOKEN_CACHE.clear()

    seen = []

    def handler(method, url, **kwargs):
        seen.append((method, url, kwargs))
        if url == ZOHO_TOKEN_URL:
            return DummyResponse(status_code=200, json_data={"access_token": "token-2", "expires_in_sec": 3600})
        if method == "GET" and url.endswith("/invoice/v3/contacts/legacy-contact-1"):
            return DummyResponse(status_code=200, json_data={"contact": {"contact_id": "legacy-contact-1", "contact_name": "317"}})
        if method == "PUT" and url.endswith("/invoice/v3/contacts/legacy-contact-1"):
            assert kwargs["json"] == {"contact_name": "Gotham Cafe City"}
            return DummyResponse(status_code=200, json_data={"contact": {"contact_id": "legacy-contact-1", "contact_name": "Gotham Cafe City"}})
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=10: DummyAsyncClient(handler))

    svc = ZohoService("https://www.zohoapis.eu", "client", "secret", "refresh", "org")
    contact = await svc.get_contact_by_id("legacy-contact-1")
    assert contact["contact_name"] == "317"
    assert await svc.update_contact_name("legacy-contact-1", "Gotham Cafe City") is True
    assert seen[0][1] == ZOHO_TOKEN_URL
    assert seen[1][1].endswith("/invoice/v3/contacts/legacy-contact-1")
    assert seen[2][1].endswith("/invoice/v3/contacts/legacy-contact-1")


def make_in_memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    return session


@pytest.mark.asyncio
async def test_export_service_run_export_success(monkeypatch):
    class ProductZohoService:
        def __init__(self):
            self.contact_lookups = 0
            self.created_contacts = 0
            self.created_invoices = 0
            self.last_payload = None

        async def find_contact_by_name(self, name):
            self.contact_lookups += 1
            return None

        async def create_contact(self, name):
            self.created_contacts += 1
            return "ZC-1"

        async def get_contact_by_id(self, contact_id):
            return {"contact": {"contact_id": contact_id, "contact_name": "Customer A"}}

        async def update_contact_name(self, contact_id, name):
            return True

        async def find_item_by_sku(self, sku):
            return None

        async def find_item_by_name(self, name):
            return "ZI-1"

        async def create_item(self, sku, name, price):
            return "ZI-1"

        async def create_draft_invoice_from_payload(self, payload):
            self.created_invoices += 1
            self.last_payload = payload
            return "INV-1"

    zoho_svc = ProductZohoService()
    # unify service not used in this export path
    export_svc = ExportService(None, zoho_svc)

    db = make_in_memory_db()

    orders = [
        UnifyOrder(order_id="O1", customer_name="Customer A", buyer_id="Customer A", order_date="2026-03-01", lines=[OrderLine(item_sku="SKU1", item_name="Item 1", quantity=1, price=10.0)], total=10.0),
        UnifyOrder(order_id="O2", customer_name="Customer A", buyer_id="Customer A", order_date="2026-03-01", lines=[OrderLine(item_sku="SKU2", item_name="Item 2", quantity=2, price=20.0)], total=40.0),
    ]

    result = await export_svc.run_export(db, "2026-03-01", "2026-03-31", orders)

    assert result["status"] == "success"
    assert result["total_orders"] == 2
    assert result["total_customers"] == 1
    assert result["total_invoices"] == 2
    assert result["created"] == 2
    assert result["skipped"] == 0
    assert result["failed"] == 0
    assert len(result["details"]) == 2
    assert result["created"] == 2
    assert result["skipped"] == 0
    assert result["failed"] == 0

    from app import models
    exported_orders = db.query(models.ExportedOrder).all()
    assert len(exported_orders) == 2

    exported_invoices = db.query(models.ExportedInvoice).all()
    assert len(exported_invoices) == 2
    assert exported_invoices[0].zoho_invoice_id == "INV-1"
    assert zoho_svc.last_payload is not None
    assert all("item_id" in line for line in zoho_svc.last_payload["line_items"])


@pytest.mark.asyncio
async def test_export_service_uses_item_id_when_zoho_item_is_found(monkeypatch):
    class ItemZohoService:
        def __init__(self):
            self.last_payload = None

        async def find_contact_by_name(self, name):
            return None

        async def create_contact(self, name):
            return "contact-1"

        async def find_item_by_name(self, name):
            return "item-123"

        async def create_item(self, name, rate, tax_id):
            raise AssertionError("create_item should not be called when Zoho item lookup succeeds")

        async def get_tax_by_rate(self, rate):
            return "tax-1"

        async def create_draft_invoice_from_payload(self, payload):
            self.last_payload = payload
            return "invoice-1"

    export_svc = ExportService(None, ItemZohoService())
    db = make_in_memory_db()
    order = UnifyOrder(
        order_id="O-ITEM",
        customer_name="Gotham Cafe City",
        buyer_name="Gotham Cafe City",
        order_date="2026-04-01",
        lines=[OrderLine(item_sku="SKU-1", item_name="Brownie", quantity=2, price=5.0)],
        total=10.0,
        buyer_id="B-1",
    )

    result = await export_svc.run_export(db, "2026-04-01", "2026-04-30", [order])

    assert result["status"] == "success"
    assert export_svc.zoho_service.last_payload is not None
    assert export_svc.zoho_service.last_payload["line_items"][0]["item_id"] == "item-123"
    assert export_svc.zoho_service.last_payload["line_items"][0]["name"] == "Brownie"


def test_settings_mask_secret_values_and_preserve_sentinal():
    assert routes._mask_setting_value("UNIFY_CLIENT_SECRET", "secret-value") == routes.MASKED_SECRET_VALUE
    assert routes._mask_setting_value("UNIFY_CLIENT_ID", "client-value") == routes.MASKED_SECRET_VALUE
    assert routes._is_secret_key("UNIFY_CLIENT_SECRET") is True
    assert routes._is_secret_key("ZOHO_ORG_ID") is True
    assert routes._is_secret_key("ZOHO_BASE_URL") is False


def test_normalize_request_date_accepts_iso_and_dd_mm_yyyy():
    assert routes._normalize_request_date("2026-04-02", "date_from") == "2026-04-02"
    assert routes._normalize_request_date("02/04/2026", "date_from") == "2026-04-02"


def test_database_url_is_normalized_to_backend_dir():
    from app.config import settings

    assert settings.normalized_database_url.endswith("/backend/unify_zoho.db")


def test_customer_name_resolution_prefers_readable_labels_over_numeric_ids():
    svc = UnifyService("https://api.unify.example", "client", "secret")
    raw = {
        "buyerName": "317",
        "customerName": "317",
    }
    buyer_name, customer_name, _ = svc._resolve_buyer_details(
        raw,
        "317",
        {"317": "Gotham Cafe City"},
        {"displayName": "Gotham Cafe City"},
    )

    assert buyer_name == "Gotham Cafe City"
    assert customer_name == "Gotham Cafe City"
    assert svc._extract_readable_buyer_name({"buyerName": "317"}) is None
    assert svc._extract_customer_name({"buyerName": "317", "customerName": "317"}, "317", {"317": "Gotham Cafe City"}) == "Gotham Cafe City"


def test_customer_name_resolution_uses_readable_customer_name_when_buyer_name_is_numeric():
    svc = UnifyService("https://api.unify.example", "client", "secret")
    buyer_name, customer_name, _ = svc._resolve_buyer_details(
        {"buyerName": "317", "customerName": "Gotham Cafe City"},
        "317",
        None,
        None,
    )

    assert buyer_name == "Gotham Cafe City"
    assert customer_name == "Gotham Cafe City"


def test_customer_name_resolution_falls_back_to_customer_label_when_no_readable_name_exists():
    svc = UnifyService("https://api.unify.example", "client", "secret")
    buyer_name, customer_name, _ = svc._resolve_buyer_details(
        {"buyerName": "317", "customerName": "317"},
        "317",
        None,
        None,
    )

    assert buyer_name == "Customer 317"
    assert customer_name == "Customer 317"
    assert svc._is_meaningful_customer_label("317") is False
    assert svc._is_meaningful_customer_label("Gotham Cafe City") is True


def test_export_service_resolves_customer_name_for_zoho_contact_and_invoice():
    class DummyZohoService:
        async def find_contact_by_name(self, name):
            return None

        async def create_contact(self, name):
            return "zoho-contact-1"

        async def find_item_by_name(self, name):
            return None

        async def create_item(self, name, rate, tax_id):
            return "item-1"

        async def get_tax_by_rate(self, rate):
            return "tax-1"

        async def create_draft_invoice_from_payload(self, payload):
            self.payload = payload
            return "invoice-1"

    export_svc = ExportService(None, DummyZohoService())
    order = UnifyOrder(
        order_id="O-1",
        customer_name="317",
        buyer_name="Gotham Cafe City",
        order_date="2026-04-01",
        lines=[OrderLine(item_sku="SKU1", item_name="Item 1", quantity=1, price=10.0)],
        total=10.0,
        buyer_id="317",
    )

    assert export_svc._resolve_invoice_contact_name(order) == "Gotham Cafe City"
    assert export_svc._resolve_customer_display_name(order) == "Gotham Cafe City"
    assert export_svc._pick_meaningful_customer_label("317", "Gotham Cafe City") == "Gotham Cafe City"
    assert export_svc._pick_meaningful_customer_label("317", "   ") is None


def test_build_invoice_notes_uses_standard_client_facing_format():
    export_svc = ExportService(None, object())

    with_storage = SimpleNamespace(order_id="O-1", storage_note="Keep refrigerated")
    without_storage = SimpleNamespace(order_id="O-2")

    assert export_svc.build_invoice_notes(with_storage) == (
        "Storage: Keep refrigerated\n\n"
        "This is your delivery receipt and invoice. A weekly statement will be e-mailed to you. "
        "Thank you for loving our cake! Caryna and the Lovin' from the Oven team\n"
        "VAT IE9802711J"
    )
    assert export_svc.build_invoice_notes(without_storage) == (
        "This is your delivery receipt and invoice. A weekly statement will be e-mailed to you. "
        "Thank you for loving our cake! Caryna and the Lovin' from the Oven team\n"
        "VAT IE9802711J"
    )


@pytest.mark.asyncio
async def test_export_service_repairs_numeric_legacy_contact_before_invoice(monkeypatch):
    class DummyZohoService:
        def __init__(self):
            self.lookup_queries = []
            self.updated_contacts = []
            self.created_contacts = []
            self.payload = None

        async def find_contact_by_name(self, name):
            self.lookup_queries.append(name)
            return None

        async def get_contact_by_id(self, contact_id):
            return {"contact": {"contact_id": contact_id, "contact_name": "317"}}

        async def update_contact_name(self, contact_id, name):
            self.updated_contacts.append((contact_id, name))
            return True

        async def find_item_by_name(self, name):
            return None

        async def create_item(self, name, rate, tax_id):
            return "item-1"

        async def get_tax_by_rate(self, rate):
            return "tax-1"

        async def create_draft_invoice_from_payload(self, payload):
            self.payload = payload
            return "invoice-1"

    from app import models

    zoho_svc = DummyZohoService()
    export_svc = ExportService(None, zoho_svc)
    db = make_in_memory_db()
    db.add(models.CustomerMapping(unify_customer_name="317", zoho_contact_id="legacy-contact-1"))
    db.commit()

    order = UnifyOrder(
        order_id="O-1",
        customer_name="317",
        buyer_name="Gotham Cafe City",
        order_date="2026-04-01",
        lines=[OrderLine(item_sku="SKU1", item_name="Item 1", quantity=1, price=10.0)],
        total=10.0,
        buyer_id="317",
    )

    result = await export_svc.run_export(db, "2026-04-01", "2026-04-30", [order])

    assert result["status"] == "success"
    assert zoho_svc.lookup_queries == []
    assert zoho_svc.updated_contacts == [("legacy-contact-1", "Gotham Cafe City")]
    assert zoho_svc.created_contacts == []
    assert zoho_svc.payload["customer_id"] == "legacy-contact-1"
    assert zoho_svc.payload["notes"] == (
        "This is your delivery receipt and invoice. A weekly statement will be e-mailed to you. "
        "Thank you for loving our cake! Caryna and the Lovin' from the Oven team\n"
        "VAT IE9802711J"
    )


@pytest.mark.asyncio
async def test_settings_get_returns_flags_without_secrets():
    db = make_in_memory_db()
    from app import models

    db.add(models.Setting(key="ZOHO_BASE_URL", value="https://example.zohoapis.eu"))
    db.add(models.Setting(key="UNIFY_CLIENT_ID", value="unify-client"))
    db.add(models.Setting(key="UNIFY_CLIENT_SECRET", value="unify-secret"))
    db.add(models.Setting(key="ZOHO_ORG_ID", value="org-123"))
    db.commit()

    response = await routes.get_settings(db)

    assert response["ZOHO_BASE_URL"] == "https://example.zohoapis.eu"
    assert response["has_unify_client_id"] is True
    assert response["has_unify_client_secret"] is True
    assert response["has_zoho_organization_id"] is True
    assert "UNIFY_CLIENT_ID" not in response
    assert "UNIFY_CLIENT_SECRET" not in response
    assert "ZOHO_ORG_ID" not in response


@pytest.mark.asyncio
async def test_settings_post_preserves_blank_secret_values():
    db = make_in_memory_db()
    from app import models

    db.add(models.Setting(key="UNIFY_CLIENT_ID", value="existing-client"))
    db.add(models.Setting(key="UNIFY_CLIENT_SECRET", value="existing-secret"))
    db.add(models.Setting(key="ZOHO_BASE_URL", value="https://old.example"))
    db.commit()

    await routes.post_settings(
        {
            "UNIFY_CLIENT_ID": "",
            "UNIFY_CLIENT_SECRET": "   ",
            "ZOHO_BASE_URL": "https://new.example",
            "ZOHO_ORG_ID": "org-456",
        },
        db,
    )

    refreshed = {row.key: row.value for row in db.query(models.Setting).all()}
    assert refreshed["UNIFY_CLIENT_ID"] == "existing-client"
    assert refreshed["UNIFY_CLIENT_SECRET"] == "existing-secret"
    assert refreshed["ZOHO_BASE_URL"] == "https://new.example"
    assert refreshed["ZOHO_ORG_ID"] == "org-456"
