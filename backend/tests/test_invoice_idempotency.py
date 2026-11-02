"""Invoice-safety tests for the export pipeline.

Everything runs against a temporary SQLite file and a fake Zoho service; no real Zoho
organisation, Unify account or production database is ever contacted (see conftest.py).
The fake records every invoice it "creates", so duplicates can be counted exactly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app import models
from app.api import routes
from app.db import Base
from app.schemas import OrderLine, ResetSelectedRunsRequest, UnifyOrder, UnifyOrderPreview
from fastapi import HTTPException

from app.services.export_service import ExportService, ExportServiceError, ReconcileError, latest_audit_time
from app.services.zoho_service import (
    ZohoAmbiguousResponseError,
    ZohoLookupError,
    ZohoService,
    ZohoServiceError,
)


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'idempotency.db').as_posix()}", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    yield factory
    engine.dispose()


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


class FakeZoho:
    """Stands in for ZohoService. Invoices it creates are kept in `self.invoices`."""

    def __init__(self) -> None:
        self.invoices: List[Dict[str, Any]] = []
        self.create_calls: List[str] = []
        self.lookup_calls: List[str] = []
        self.create_behavior: Dict[str, str] = {}
        self.lookup_behavior: str | None = None
        self.extra_lookup_results: List[Dict[str, Any]] = []
        self.on_create = None
        self.during_create = None  # async hook run after Zoho stored the invoice
        self.on_lookup = None  # async hook run inside a lookup

    def add_invoice(self, reference: str, customer_id: str, status: str = "draft", customer_name: str | None = None) -> Dict[str, Any]:
        invoice = {
            "invoice_id": f"INV-{len(self.invoices) + 1}",
            "invoice_number": f"N-{len(self.invoices) + 1}",
            "reference_number": reference,
            "customer_id": customer_id,
            "customer_name": customer_name if customer_name is not None else "Customer " + customer_id.removeprefix("C-"),
            "status": status,
        }
        self.invoices.append(invoice)
        return invoice

    def invoices_for(self, reference: str) -> List[Dict[str, Any]]:
        return [inv for inv in self.invoices if inv["reference_number"] == reference]

    async def create_draft_invoice_from_payload(self, payload: Dict[str, Any]) -> str:
        reference = payload["reference_number"]
        self.create_calls.append(reference)
        await asyncio.sleep(0)  # yield, so concurrent runs interleave here
        behavior = self.create_behavior.get(reference, "ok")
        if behavior == "fail":
            raise ZohoServiceError('{"error": "Zoho API failed", "status_code": 400}')
        invoice = self.add_invoice(reference, payload["customer_id"], customer_name=payload.get("customer_name"))
        if self.on_create:
            self.on_create(invoice)
        if self.during_create:
            await self.during_create(invoice)
        if behavior == "timeout_after_accept":
            raise ZohoAmbiguousResponseError("ReadTimeout after Zoho accepted the request")
        return invoice["invoice_id"]

    async def find_invoices_by_reference_number(self, reference: str) -> List[Dict[str, Any]]:
        self.lookup_calls.append(reference)
        if self.on_lookup:
            await self.on_lookup(reference)
        if self.lookup_behavior == "error":
            raise ZohoLookupError("Zoho invoice lookup failed 500")
        results = [inv for inv in self.invoices if reference in inv["reference_number"]]
        return results + list(self.extra_lookup_results)


class StubbedExportService(ExportService):
    """Real per-order orchestration; only line/contact/payload building is stubbed."""

    async def _build_line_items(self, db, order):
        await asyncio.sleep(0)
        return [{"name": "Widget", "quantity": 1, "rate": 10.0, "tax_id": "TAX-1"}], []

    async def _resolve_contact_for_order(self, db, order, contact_name):
        return f"C-{order.buyer_id}"

    async def _build_draft_invoice_payload(self, order, contact_id, line_items, invoice_date, contact_name):
        return {"reference_number": order.order_id, "customer_id": contact_id, "customer_name": contact_name, "line_items": line_items}


def make_order(order_id: str, buyer_id: str = "B1", preview_status: str = "ready") -> UnifyOrder:
    return UnifyOrder(
        order_id=order_id,
        customer_name=f"Customer {buyer_id}",
        buyer_name=f"Customer {buyer_id}",
        buyer_id=buyer_id,
        order_date="2026-10-01",
        delivery_date="2026-10-02",
        lines=[OrderLine(item_sku="SKU-1", item_name="Widget", quantity=1, price=10.0)],
        total=10.0,
        preview_status=preview_status,
    )


def run(service: ExportService, db, *orders: UnifyOrder) -> Dict[str, Any]:
    return asyncio.run(service.run_export(db, "2026-10-01", "2026-10-02", list(orders)))


def details_by_id(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {detail["order_id"]: detail for detail in result["details"]}


def record(db, order_id: str) -> models.ExportedOrder | None:
    db.expire_all()
    return db.query(models.ExportedOrder).filter(models.ExportedOrder.unify_order_id == order_id).one_or_none()


def seed_record(db, order_id: str, status: str, message: str = "seeded") -> None:
    sync_run = models.SyncRun(date_from="2026-09-01", date_to="2026-09-01", status="failed")
    db.add(sync_run)
    db.commit()
    db.add(models.ExportedOrder(sync_run_id=sync_run.id, unify_order_id=order_id, customer_name="Seed", status=status, message=message))
    db.commit()


@pytest.fixture()
def zoho():
    return FakeZoho()


@pytest.fixture()
def service(zoho):
    return StubbedExportService(None, zoho)


# ---------------------------------------------------------------------------
# Export pipeline
# ---------------------------------------------------------------------------


def test_successful_invoice_creation(service, zoho, db):
    result = run(service, db, make_order("O-1"))

    detail = details_by_id(result)["O-1"]
    assert detail["status"] == "created"
    assert detail["zoho_invoice_id"] == "INV-1"
    assert result["created"] == 1 and result["status"] == "success" and result["total_invoices"] == 1
    assert len(zoho.invoices_for("O-1")) == 1
    assert zoho.lookup_calls == ["O-1"]  # every creation is preceded by a Zoho duplicate check
    assert record(db, "O-1").status == "exported"
    invoices = db.query(models.ExportedInvoice).all()
    assert [(inv.zoho_invoice_id, inv.unify_order_ids) for inv in invoices] == [("INV-1", ["O-1"])]


def test_explicit_zoho_failure_is_recorded_as_failed(service, zoho, db):
    zoho.create_behavior["O-1"] = "fail"
    result = run(service, db, make_order("O-1"))

    assert details_by_id(result)["O-1"]["status"] == "failed"
    assert zoho.invoices == []
    assert record(db, "O-1").status == "failed"
    assert db.query(models.ExportedInvoice).count() == 0


def test_timeout_after_zoho_accepted_needs_verification_and_is_never_retried(service, zoho, db):
    zoho.create_behavior["O-1"] = "timeout_after_accept"
    first = run(service, db, make_order("O-1"))

    assert details_by_id(first)["O-1"]["status"] == "needs_verification"
    assert first["needs_verification"] == 1
    assert record(db, "O-1").status == "needs_verification"
    assert len(zoho.invoices_for("O-1")) == 1  # Zoho did create it

    zoho.create_behavior.pop("O-1")
    second = run(service, db, make_order("O-1"))
    assert details_by_id(second)["O-1"]["status"] == "needs_verification"
    assert zoho.create_calls == ["O-1"]  # no second creation attempt
    assert len(zoho.invoices_for("O-1")) == 1


def test_database_failure_after_zoho_success_is_flagged_and_blocks_retry(service, zoho, db, monkeypatch):
    def failing_persist(*args, **kwargs):
        raise OperationalError("UPDATE exported_orders", {}, Exception("database is unavailable"))

    monkeypatch.setattr(service, "_persist_exported", failing_persist)
    result = run(service, db, make_order("O-1"))

    detail = details_by_id(result)["O-1"]
    assert detail["status"] == "needs_verification"
    assert detail["zoho_invoice_id"] == "INV-1"
    row = record(db, "O-1")
    assert row.status == "needs_verification"
    assert "INV-1" in row.message  # audit trail keeps the created invoice id

    monkeypatch.undo()
    retry = run(service, db, make_order("O-1"))
    assert details_by_id(retry)["O-1"]["status"] == "needs_verification"
    assert len(zoho.invoices_for("O-1")) == 1


def test_database_fully_down_after_zoho_success_leaves_order_blocked(service, zoho, db, monkeypatch):
    real_commit = db.commit

    def break_commits(_invoice):
        def failing_commit():
            raise OperationalError("COMMIT", {}, Exception("connection lost"))

        monkeypatch.setattr(db, "commit", failing_commit)

    zoho.on_create = break_commits
    with pytest.raises(ExportServiceError):
        run(service, db, make_order("O-1"))

    monkeypatch.setattr(db, "commit", real_commit)
    zoho.on_create = None
    db.rollback()
    assert record(db, "O-1").status == "in_progress"  # claim committed before calling Zoho

    retry = run(service, db, make_order("O-1"))
    assert details_by_id(retry)["O-1"]["status"] == "in_progress"
    assert len(zoho.invoices_for("O-1")) == 1


def test_two_concurrent_requests_create_only_one_invoice(session_factory, zoho):
    service = StubbedExportService(None, zoho)
    first_db, second_db = session_factory(), session_factory()

    async def both():
        return await asyncio.gather(
            service.run_export(first_db, "2026-10-01", "2026-10-02", [make_order("O-1")]),
            service.run_export(second_db, "2026-10-01", "2026-10-02", [make_order("O-1")]),
        )

    try:
        results = asyncio.run(both())
    finally:
        first_db.close()
        second_db.close()

    statuses = sorted(details_by_id(result)["O-1"]["status"] for result in results)
    assert statuses == ["created", "in_progress"]
    assert zoho.create_calls == ["O-1"]
    assert len(zoho.invoices_for("O-1")) == 1


def test_previously_exported_order_is_skipped_without_crashing_the_batch(service, zoho, db):
    seed_record(db, "O-1", "exported", "Invoice INV-OLD created")
    result = run(service, db, make_order("O-1"), make_order("O-2", buyer_id="B2"))

    details = details_by_id(result)
    assert details["O-1"]["status"] == "already_exported"
    assert details["O-2"]["status"] == "created"
    assert zoho.create_calls == ["O-2"]
    assert db.query(models.ExportedOrder).filter(models.ExportedOrder.unify_order_id == "O-1").count() == 1
    assert record(db, "O-1").status == "exported"


def test_previously_failed_order_is_retried_only_after_zoho_check(service, zoho, db):
    seed_record(db, "O-1", "failed", "Zoho API failed 400")
    result = run(service, db, make_order("O-1"))

    assert zoho.lookup_calls == ["O-1"]
    assert details_by_id(result)["O-1"]["status"] == "created"
    assert len(zoho.invoices_for("O-1")) == 1
    row = record(db, "O-1")
    assert row.status == "exported"
    assert "previous status failed" in row.message


def test_previously_failed_order_with_matching_zoho_invoice_is_linked_not_recreated(service, zoho, db):
    seed_record(db, "O-1", "failed", "timeout recorded as failed by the old code")
    existing = zoho.add_invoice("O-1", "C-B1")
    result = run(service, db, make_order("O-1"))

    detail = details_by_id(result)["O-1"]
    assert detail["status"] == "already_exported"
    assert detail["zoho_invoice_id"] == existing["invoice_id"]
    assert zoho.create_calls == []
    assert record(db, "O-1").status == "exported"
    assert db.query(models.ExportedInvoice).one().zoho_invoice_id == existing["invoice_id"]


@pytest.mark.parametrize(
    "case",
    ["two_matches", "customer_mismatch", "reference_superset_and_mismatch"],
)
def test_ambiguous_zoho_lookup_never_creates(service, zoho, db, case):
    seed_record(db, "O-1", "failed")
    if case == "two_matches":
        zoho.add_invoice("O-1", "C-B1")
        zoho.add_invoice("O-1", "C-B1")
    elif case == "customer_mismatch":
        zoho.add_invoice("O-1", "C-SOMEONE-ELSE")
    else:
        zoho.add_invoice("O-10", "C-B1")  # contains the reference but is not exact -> ignored
        zoho.add_invoice("O-1", "C-OTHER")
    before = len(zoho.invoices)
    result = run(service, db, make_order("O-1"))

    assert details_by_id(result)["O-1"]["status"] == "needs_verification"
    assert zoho.create_calls == []
    assert len(zoho.invoices) == before
    assert record(db, "O-1").status == "needs_verification"


def test_void_invoice_with_same_reference_does_not_block_retry(service, zoho, db):
    seed_record(db, "O-1", "failed")
    zoho.add_invoice("O-1", "C-B1", status="void")
    result = run(service, db, make_order("O-1"))

    assert details_by_id(result)["O-1"]["status"] == "created"
    assert zoho.create_calls == ["O-1"]


def test_failed_zoho_lookup_blocks_retry(service, zoho, db):
    seed_record(db, "O-1", "failed")
    zoho.lookup_behavior = "error"
    result = run(service, db, make_order("O-1"))

    detail = details_by_id(result)["O-1"]
    assert detail["status"] == "failed"
    assert "duplicate check could not be completed" in detail["message"]
    assert zoho.create_calls == []
    assert record(db, "O-1").status == "failed"  # still retryable later, after a successful check


@pytest.mark.parametrize("status", ["needs_verification", "in_progress", "pending", "something_unknown"])
def test_non_retryable_existing_status_is_never_sent_to_zoho(service, zoho, db, status):
    seed_record(db, "O-1", status)
    result = run(service, db, make_order("O-1"))

    assert details_by_id(result)["O-1"]["status"] in {"needs_verification", "in_progress"}
    assert zoho.create_calls == [] and zoho.lookup_calls == []
    assert record(db, "O-1").status == status


def test_partial_batch_failure_is_isolated_per_order(service, zoho, db):
    seed_record(db, "O-4", "exported")
    zoho.create_behavior.update({"O-2": "fail", "O-3": "timeout_after_accept"})
    result = run(
        service,
        db,
        make_order("O-1", "B1"),
        make_order("O-2", "B2"),
        make_order("O-3", "B3"),
        make_order("O-4", "B4"),
        make_order("O-5", "B5", preview_status="missing_items"),
        make_order("O-6", "B6"),
    )

    statuses = {order_id: detail["status"] for order_id, detail in details_by_id(result).items()}
    assert statuses == {
        "O-1": "created",
        "O-2": "failed",
        "O-3": "needs_verification",
        "O-4": "already_exported",
        "O-5": "skipped",
        "O-6": "created",
    }
    assert result["status"] == "partial"
    assert (result["created"], result["failed"], result["needs_verification"], result["skipped"]) == (2, 2, 1, 2)
    assert result["total_invoices"] == 2
    db.expire_all()
    assert db.get(models.SyncRun, result["sync_run_id"]).total_invoices == 2


def test_unexpected_error_after_zoho_call_started_is_needs_verification(service, zoho, db, monkeypatch):
    async def exploding_create(payload):
        zoho.create_calls.append(payload["reference_number"])
        raise RuntimeError("unexpected crash inside the Zoho client")

    monkeypatch.setattr(zoho, "create_draft_invoice_from_payload", exploding_create)
    result = run(service, db, make_order("O-1"), make_order("O-2", "B2"))

    assert details_by_id(result)["O-1"]["status"] == "needs_verification"
    assert details_by_id(result)["O-2"]["status"] == "needs_verification"
    assert record(db, "O-1").status == "needs_verification"


def test_unexpected_error_before_zoho_call_is_failed_and_batch_continues(service, zoho, db, monkeypatch):
    original = service._resolve_contact_for_order

    async def flaky_contact(db_, order, name):
        if order.order_id == "O-1":
            raise ValueError("unexpected contact data")
        return await original(db_, order, name)

    monkeypatch.setattr(service, "_resolve_contact_for_order", flaky_contact)
    result = run(service, db, make_order("O-1"), make_order("O-2", "B2"))

    assert details_by_id(result)["O-1"]["status"] == "failed"
    assert details_by_id(result)["O-2"]["status"] == "created"
    assert record(db, "O-1").status == "failed"
    assert zoho.create_calls == ["O-2"]


def test_unique_constraint_conflict_on_claim_is_handled(service, zoho, db, monkeypatch):
    # Simulate the race: another request inserts the row after our existence check.
    seed_record(db, "O-1", "exported")
    original = service._get_export_record
    calls = {"count": 0}

    def stale_first_read(db_, order_id):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return original(db_, order_id)

    monkeypatch.setattr(service, "_get_export_record", stale_first_read)
    result = run(service, db, make_order("O-1"), make_order("O-2", "B2"))

    assert details_by_id(result)["O-1"]["status"] == "already_exported"
    assert details_by_id(result)["O-2"]["status"] == "created"
    assert zoho.create_calls == ["O-2"]
    assert db.query(models.ExportedOrder).filter(models.ExportedOrder.unify_order_id == "O-1").count() == 1


def test_skipped_preview_order_does_not_overwrite_existing_record(service, zoho, db):
    seed_record(db, "O-1", "exported")
    result = run(service, db, make_order("O-1", preview_status="missing_items"))

    assert details_by_id(result)["O-1"]["status"] == "skipped"
    assert record(db, "O-1").status == "exported"


def test_previously_skipped_order_can_be_exported_once_ready(service, zoho, db):
    run(service, db, make_order("O-1", preview_status="missing_items"))
    assert record(db, "O-1").status == "skipped"

    result = run(service, db, make_order("O-1"))
    assert details_by_id(result)["O-1"]["status"] == "created"
    assert zoho.lookup_calls == ["O-1"]
    assert len(zoho.invoices_for("O-1")) == 1


# ---------------------------------------------------------------------------
# Unsync / History reset / reset-all (soft reset)
# ---------------------------------------------------------------------------


def test_unsync_of_failed_order_allows_a_single_retry(service, zoho, db):
    zoho.create_behavior["O-1"] = "fail"
    run(service, db, make_order("O-1"))
    asyncio.run(routes.reset_order("O-1", db))
    zoho.create_behavior.pop("O-1")

    result = run(service, db, make_order("O-1"))
    assert details_by_id(result)["O-1"]["status"] == "created"
    assert len(zoho.invoices_for("O-1")) == 1


def test_unsync_followed_by_retry_does_not_duplicate_invoice(service, zoho, db):
    run(service, db, make_order("O-1"))
    asyncio.run(routes.reset_order("O-1", db))
    run(service, db, make_order("O-1"))
    assert len(zoho.invoices_for("O-1")) == 1


def test_history_reset_followed_by_retry_does_not_duplicate_invoice(service, zoho, db):
    first = run(service, db, make_order("O-1"))
    asyncio.run(routes.reset_selected_runs(ResetSelectedRunsRequest(run_ids=[first["sync_run_id"]]), db))
    run(service, db, make_order("O-1"))
    assert len(zoho.invoices_for("O-1")) == 1


# ---------------------------------------------------------------------------
# Preview status
# ---------------------------------------------------------------------------


def test_preview_reports_export_status_and_keeps_blocking_existing_records(db):
    for order_id, status in [("O-1", "exported"), ("O-2", "failed"), ("O-3", "needs_verification"), ("O-4", "skipped")]:
        seed_record(db, order_id, status)
    previews = [
        UnifyOrderPreview(order_id=f"O-{index}", customer_name=f"Customer {index}", total=10, delivery_date="2026-10-02")
        for index in range(1, 6)
    ]
    payload = routes._build_preview_response_payload(previews, db, source="csv")

    by_id = {order.order_id: order for order in payload["orders"]}
    assert {k: (v.already_exported, v.export_status) for k, v in by_id.items()} == {
        "O-1": (True, "exported"),
        "O-2": (True, "failed"),
        "O-3": (True, "needs_verification"),
        "O-4": (True, "skipped"),
        "O-5": (False, None),
    }


def test_history_timestamps_are_serialized_without_timezone_offset(service, zoho, db):
    """Documents the API timestamp format (naive UTC, no offset) that the frontend parses.

    Verified here against the ORM/serializer only; the production column type still needs
    a one-off check before changing how the frontend interprets these values.
    """
    from fastapi.encoders import jsonable_encoder

    result = run(service, db, make_order("O-1"))
    history = asyncio.run(routes.export_history(db))
    started_at = jsonable_encoder(history)[0]["started_at"]
    assert history[0]["id"] == result["sync_run_id"]
    assert "T" in started_at
    assert not started_at.endswith("Z") and "+" not in started_at[10:]


# ---------------------------------------------------------------------------
# ZohoService outcome classification and lookup (no network: _request is faked)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, json_data: Any = None, text: str = "", invalid_json: bool = False):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self._invalid = invalid_json

    def json(self):
        if self._invalid:
            raise ValueError("invalid json")
        return self._json


def make_zoho_service(monkeypatch, request_impl) -> ZohoService:
    zoho_service = ZohoService("https://www.zohoapis.eu", "client", "secret", "refresh", "org")

    async def fake_token(force_refresh: bool = False):
        return "token"

    monkeypatch.setattr(zoho_service, "get_access_token", fake_token)
    monkeypatch.setattr(zoho_service, "_request", request_impl)
    return zoho_service


PAYLOAD = {"customer_id": "C-1", "reference_number": "O-1", "line_items": [{"name": "Widget", "quantity": 1, "rate": 10, "tax_id": "T"}]}


@pytest.mark.parametrize(
    "response, expected_error",
    [
        (httpx.ReadTimeout("timed out"), ZohoAmbiguousResponseError),
        (httpx.ConnectError("connection reset"), ZohoAmbiguousResponseError),
        (FakeResponse(500, text="server error"), ZohoAmbiguousResponseError),
        (FakeResponse(504, text="gateway timeout"), ZohoAmbiguousResponseError),
        (FakeResponse(200, {"code": 0, "message": "ok"}), ZohoAmbiguousResponseError),
        (FakeResponse(201, invalid_json=True, text="<html>"), ZohoAmbiguousResponseError),
        (FakeResponse(400, text="invalid customer"), ZohoServiceError),
        (FakeResponse(429, text="rate limited"), ZohoServiceError),
    ],
)
def test_zoho_create_outcome_classification(monkeypatch, response, expected_error):
    async def request_impl(method, path, **kwargs):
        if isinstance(response, Exception):
            raise response
        return response

    zoho_service = make_zoho_service(monkeypatch, request_impl)
    with pytest.raises(expected_error) as excinfo:
        asyncio.run(zoho_service.create_draft_invoice_from_payload(PAYLOAD))
    if expected_error is ZohoServiceError:
        assert not isinstance(excinfo.value, ZohoAmbiguousResponseError)


def test_zoho_create_success_returns_invoice_id(monkeypatch):
    async def request_impl(method, path, **kwargs):
        assert (method, path) == ("POST", "/invoices")
        return FakeResponse(201, {"invoice": {"invoice_id": "INV-9"}})

    zoho_service = make_zoho_service(monkeypatch, request_impl)
    assert asyncio.run(zoho_service.create_draft_invoice_from_payload(PAYLOAD)) == "INV-9"


def test_zoho_token_failure_is_a_definite_error(monkeypatch):
    zoho_service = ZohoService("https://www.zohoapis.eu", "client", "secret", "refresh", "org")

    async def failing_token(force_refresh: bool = False):
        raise httpx.ConnectError("token endpoint unreachable")

    sent = []

    async def request_impl(*args, **kwargs):
        sent.append(args)
        return FakeResponse(201, {"invoice": {"invoice_id": "X"}})

    monkeypatch.setattr(zoho_service, "get_access_token", failing_token)
    monkeypatch.setattr(zoho_service, "_request", request_impl)
    with pytest.raises(ZohoServiceError) as excinfo:
        asyncio.run(zoho_service.create_draft_invoice_from_payload(PAYLOAD))
    assert not isinstance(excinfo.value, ZohoAmbiguousResponseError)
    assert sent == []  # the invoice request was never sent


@pytest.mark.parametrize(
    "response, expect_error",
    [
        (FakeResponse(200, {"invoices": [{"invoice_id": "1", "reference_number": "O-1"}], "page_context": {"has_more_page": False}}), False),
        (FakeResponse(200, {"invoices": [], "page_context": {"has_more_page": False}}), False),
        (FakeResponse(200, {"invoices": []}), True),  # no page_context: completeness unknown -> fail closed
        (FakeResponse(200, {"invoices": [{"invoice_id": "1", "reference_number": "O-1"}], "page_context": {"has_more_page": True}}), True),
        (FakeResponse(200, {"invoices": [{"invoice_id": "1", "reference_number": "SOMETHING-ELSE"}]}), True),
        (FakeResponse(200, {"message": "no invoices key"}), True),
        (FakeResponse(200, invalid_json=True), True),
        (FakeResponse(401, text="unauthorized"), True),
        (httpx.ReadTimeout("timed out"), True),
    ],
)
def test_zoho_invoice_lookup_is_strict(monkeypatch, response, expect_error):
    seen = {}

    async def request_impl(method, path, **kwargs):
        seen["call"] = (method, path, kwargs.get("params"))
        if isinstance(response, Exception):
            raise response
        return response

    zoho_service = make_zoho_service(monkeypatch, request_impl)
    if expect_error:
        with pytest.raises(ZohoLookupError):
            asyncio.run(zoho_service.find_invoices_by_reference_number("O-1"))
    else:
        assert isinstance(asyncio.run(zoho_service.find_invoices_by_reference_number("O-1")), list)
    assert seen["call"][0:2] == ("GET", "/invoices")
    # The Zoho account ignores reference_number filtering; the service scans all pages
    # and filters exact references locally instead.
    assert "reference_number" not in seen["call"][2]
    assert seen["call"][2]["filter_by"] == "Status.All"
    # The mock can report more pages forever; the lookup must stop safely.
    if expect_error and seen["call"][2]["page"] > 1:
        assert seen["call"][2]["page"] == 1000
    else:
        assert seen["call"][2]["page"] == 1


# ---------------------------------------------------------------------------
# Soft reset, Zoho verification for new orders, crash recovery, audit integrity
# ---------------------------------------------------------------------------


def counts(db) -> Dict[str, int]:
    db.expire_all()
    return {
        "orders": db.query(models.ExportedOrder).count(),
        "invoices": db.query(models.ExportedInvoice).count(),
        "runs": db.query(models.SyncRun).count(),
    }


def run_snapshot(db, run_id: int):
    db.expire_all()
    run_row = db.get(models.SyncRun, run_id)
    return (run_row.status, run_row.total_invoices, run_row.finished_at, run_row.errors)


def seed_claim(db, order_id: str, status: str, minutes_ago: int, customer: str = "Customer B1") -> None:
    sync_run = models.SyncRun(date_from="2026-10-01", date_to="2026-10-01", status="running")
    db.add(sync_run)
    db.commit()
    stamp = (datetime.utcnow() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    message = f"Export attempt started (claim deadbeef) [run {sync_run.id}, {stamp}Z]"
    db.add(models.ExportedOrder(sync_run_id=sync_run.id, unify_order_id=order_id, customer_name=customer, status=status, message=message))
    db.commit()


def test_repeated_sync_creates_exactly_one_invoice(service, zoho, db):
    first = run(service, db, make_order("O-1"))
    second = run(service, db, make_order("O-1"))
    third = run(service, db, make_order("O-1"))

    assert details_by_id(first)["O-1"]["status"] == "created"
    assert details_by_id(second)["O-1"]["status"] == "already_exported"
    assert details_by_id(third)["O-1"]["status"] == "already_exported"
    assert len(zoho.invoices_for("O-1")) == 1
    assert zoho.create_calls == ["O-1"]


def test_unsync_is_a_soft_reset_and_next_sync_links_the_existing_invoice(service, zoho, db):
    first = run(service, db, make_order("O-1"))
    before = counts(db)
    response = asyncio.run(routes.reset_order("O-1", db))

    assert response.ok and response.data["previous_status"] == "exported" and response.data["status"] == "reset"
    assert counts(db) == before  # nothing deleted
    row = record(db, "O-1")
    assert row.status == "reset"
    assert "Reset by user (unsync)" in row.message and "previous status exported" in row.message
    assert db.query(models.ExportedInvoice).one().zoho_invoice_id == "INV-1"

    second = run(service, db, make_order("O-1"))
    detail = details_by_id(second)["O-1"]
    assert detail["status"] == "already_exported" and detail["zoho_invoice_id"] == "INV-1"
    assert zoho.create_calls == ["O-1"]  # no second invoice
    assert record(db, "O-1").status == "exported"
    assert db.query(models.ExportedInvoice).count() == 1  # the invoice event is not duplicated
    assert record(db, "O-1").sync_run_id == first["sync_run_id"]  # record stays in its original run


def test_unsync_after_invoice_was_voided_in_zoho_creates_a_new_invoice_deliberately(service, zoho, db):
    run(service, db, make_order("O-1"))
    zoho.invoices_for("O-1")[0]["status"] = "void"  # the user cancelled it in Zoho
    asyncio.run(routes.reset_order("O-1", db))

    result = run(service, db, make_order("O-1"))
    assert details_by_id(result)["O-1"]["status"] == "created"
    assert [inv["status"] for inv in zoho.invoices_for("O-1")] == ["void", "draft"]


def test_history_reset_is_soft_and_keeps_runs_and_invoices(service, zoho, db):
    first = run(service, db, make_order("O-1"), make_order("O-2", "B2"))
    snapshot = run_snapshot(db, first["sync_run_id"])
    before = counts(db)
    response = asyncio.run(routes.reset_selected_runs(ResetSelectedRunsRequest(run_ids=[first["sync_run_id"]]), db))

    assert response.ok
    assert response.data["reset_exported_orders"] == 2
    assert response.data["deleted_exported_orders"] == 0 and response.data["deleted_exported_invoices"] == 0
    assert counts(db) == before
    assert run_snapshot(db, first["sync_run_id"]) == snapshot  # run status/totals/timestamps untouched

    second = run(service, db, make_order("O-1"), make_order("O-2", "B2"))
    assert {d["status"] for d in second["details"]} == {"already_exported"}
    assert zoho.create_calls == ["O-1", "O-2"]
    assert db.query(models.ExportedInvoice).count() == 2


def test_reset_all_endpoint_is_soft_and_skips_safety_states(service, zoho, db):
    run(service, db, make_order("O-1"), make_order("O-2", "B2"))
    seed_record(db, "O-3", "needs_verification", "outcome unknown")
    seed_claim(db, "O-4", "in_progress", minutes_ago=1)
    before = counts(db)

    response = asyncio.run(routes.reset_sync_state(db))
    assert response.ok
    assert response.data["reset_exported_orders"] == 2
    assert sorted(response.data["blocked_order_ids"]) == ["O-3", "O-4"]
    assert counts(db) == before
    assert record(db, "O-3").status == "needs_verification"
    assert record(db, "O-4").status == "in_progress"

    again = run(service, db, make_order("O-1"), make_order("O-2", "B2"), make_order("O-3", "B3"), make_order("O-4", "B4"))
    statuses = {k: v["status"] for k, v in details_by_id(again).items()}
    assert statuses == {"O-1": "already_exported", "O-2": "already_exported", "O-3": "needs_verification", "O-4": "in_progress"}
    assert zoho.create_calls == ["O-1", "O-2"]


@pytest.mark.parametrize("status", ["in_progress", "needs_verification"])
def test_unsync_refuses_safety_states(db, status):
    seed_record(db, "O-1", status, "evidence")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(routes.reset_order("O-1", db))
    assert excinfo.value.status_code == 409
    row = record(db, "O-1")
    assert row.status == status and row.message == "evidence"


def test_new_order_with_existing_zoho_invoice_is_linked_not_duplicated(service, zoho, db):
    existing = zoho.add_invoice("O-1", "C-B1")  # e.g. created before the record was lost
    result = run(service, db, make_order("O-1"))

    detail = details_by_id(result)["O-1"]
    assert detail["status"] == "already_exported" and detail["zoho_invoice_id"] == existing["invoice_id"]
    assert zoho.create_calls == []
    assert record(db, "O-1").status == "exported"
    assert db.query(models.ExportedInvoice).one().zoho_invoice_id == existing["invoice_id"]


def test_new_order_with_conflicting_zoho_invoice_is_blocked(service, zoho, db):
    zoho.add_invoice("O-1", "C-ANOTHER-CUSTOMER")
    result = run(service, db, make_order("O-1"))

    assert details_by_id(result)["O-1"]["status"] == "needs_verification"
    assert zoho.create_calls == []


def test_new_order_lookup_failure_fails_closed_then_succeeds_once_lookup_works(service, zoho, db):
    zoho.lookup_behavior = "error"
    first = run(service, db, make_order("O-1"))
    assert details_by_id(first)["O-1"]["status"] == "failed"
    assert zoho.create_calls == []
    assert record(db, "O-1").status == "failed"

    zoho.lookup_behavior = None
    second = run(service, db, make_order("O-1"))
    assert details_by_id(second)["O-1"]["status"] == "created"
    assert len(zoho.invoices_for("O-1")) == 1


def test_reference_match_is_case_insensitive_and_exact(service, zoho, db):
    # Whether Zoho's reference_number search ignores case is undocumented; this checks our
    # matching of whatever Zoho returns: a case variant matches, a longer reference does not.
    case_variant = zoho.add_invoice("o-1", "C-B1")
    zoho.add_invoice("O-10", "C-B1")
    zoho.extra_lookup_results = [case_variant]  # as if Zoho's search were case-insensitive
    result = run(service, db, make_order("O-1"))

    assert details_by_id(result)["O-1"]["status"] == "already_exported"
    assert zoho.create_calls == []


class SimulatedCrash(BaseException):
    """Stands in for the process dying (not caught by per-order isolation)."""


def test_server_crash_during_sync_leaves_order_blocked_until_reconciled(service, zoho, db):
    async def crash(_invoice):
        raise SimulatedCrash()

    zoho.during_create = crash
    with pytest.raises(SimulatedCrash):
        run(service, db, make_order("O-1"))
    zoho.during_create = None
    db.rollback()

    assert record(db, "O-1").status == "in_progress"
    assert len(zoho.invoices_for("O-1")) == 1  # Zoho created it before the crash

    blocked = run(service, db, make_order("O-1"))
    assert details_by_id(blocked)["O-1"]["status"] == "in_progress"
    assert zoho.create_calls == ["O-1"]  # never retried automatically

    with pytest.raises(ReconcileError) as excinfo:
        asyncio.run(service.reconcile_order(db, "O-1"))  # too recent: may still be processing
    assert excinfo.value.status_code == 409

    later = datetime.utcnow() + timedelta(minutes=31)
    outcome = asyncio.run(service.reconcile_order(db, "O-1", now=later))
    assert outcome["result"] == "linked" and outcome["zoho_invoice_id"] == "INV-1"
    row = record(db, "O-1")
    assert row.status == "exported" and "Export attempt started" in row.message  # evidence kept
    assert db.query(models.ExportedInvoice).one().zoho_invoice_id == "INV-1"

    after = run(service, db, make_order("O-1"))
    assert details_by_id(after)["O-1"]["status"] == "already_exported"
    assert len(zoho.invoices_for("O-1")) == 1


def test_reconcile_abandoned_claim_without_zoho_invoice_resets_and_next_sync_verifies(service, zoho, db):
    seed_claim(db, "O-1", "in_progress", minutes_ago=45)
    outcome = asyncio.run(service.reconcile_order(db, "O-1"))
    assert outcome["result"] == "reset" and record(db, "O-1").status == "reset"
    assert zoho.create_calls == []  # reconciliation never creates

    result = run(service, db, make_order("O-1"))
    assert details_by_id(result)["O-1"]["status"] == "created"
    assert zoho.lookup_calls == ["O-1", "O-1"]  # checked again before creating
    assert len(zoho.invoices_for("O-1")) == 1


def test_reconcile_ambiguous_keeps_review_state(service, zoho, db):
    seed_claim(db, "O-1", "needs_verification", minutes_ago=10)
    zoho.add_invoice("O-1", "C-B1")
    zoho.add_invoice("O-1", "C-B1")
    outcome = asyncio.run(service.reconcile_order(db, "O-1"))

    assert outcome["result"] == "ambiguous"
    row = record(db, "O-1")
    assert row.status == "needs_verification" and "review required" in row.message
    assert zoho.create_calls == []


def test_reconcile_customer_name_mismatch_is_ambiguous(service, zoho, db):
    seed_claim(db, "O-1", "needs_verification", minutes_ago=10, customer="Customer B1")
    zoho.add_invoice("O-1", "C-X", customer_name="Someone Else Ltd")
    outcome = asyncio.run(service.reconcile_order(db, "O-1"))
    assert outcome["result"] == "ambiguous"


def test_reconcile_lookup_failure_changes_nothing(service, zoho, db):
    seed_claim(db, "O-1", "needs_verification", minutes_ago=10)
    before = record(db, "O-1").message
    zoho.lookup_behavior = "error"
    with pytest.raises(ReconcileError) as excinfo:
        asyncio.run(service.reconcile_order(db, "O-1"))
    assert excinfo.value.status_code == 502
    row = record(db, "O-1")
    assert row.status == "needs_verification" and row.message == before


def test_reconcile_waits_for_zoho_to_list_new_invoices(service, zoho, db):
    seed_claim(db, "O-1", "needs_verification", minutes_ago=1)
    with pytest.raises(ReconcileError) as excinfo:
        asyncio.run(service.reconcile_order(db, "O-1"))
    assert excinfo.value.status_code == 409
    assert zoho.lookup_calls == []


@pytest.mark.parametrize("status, code", [("exported", 409), ("failed", 409), ("reset", 409), (None, 404)])
def test_reconcile_only_applies_to_safety_states(service, zoho, db, status, code):
    if status:
        seed_record(db, "O-1", status)
    with pytest.raises(ReconcileError) as excinfo:
        asyncio.run(service.reconcile_order(db, "O-1"))
    assert excinfo.value.status_code == code


def test_concurrent_reconciliations_only_one_applies(session_factory, zoho):
    service = StubbedExportService(None, zoho)
    setup = session_factory()
    seed_claim(setup, "O-1", "in_progress", minutes_ago=45)
    zoho.add_invoice("O-1", "C-B1")
    setup.close()
    first_db, second_db = session_factory(), session_factory()
    results: Dict[str, Any] = {}

    async def interleave(_reference):
        if "inner" not in results:
            results["inner"] = "running"
            results["inner"] = await service.reconcile_order(second_db, "O-1")

    zoho.on_lookup = interleave
    try:
        with pytest.raises(ReconcileError) as excinfo:
            asyncio.run(service.reconcile_order(first_db, "O-1"))
    finally:
        first_db.close()
        second_db.close()
    assert excinfo.value.status_code == 409
    assert results["inner"]["result"] == "linked"
    check = session_factory()
    assert check.query(models.ExportedInvoice).count() == 1
    check.close()


def test_sync_cannot_claim_while_reconciliation_runs(session_factory, zoho):
    service = StubbedExportService(None, zoho)
    setup = session_factory()
    seed_claim(setup, "O-1", "in_progress", minutes_ago=45)
    setup.close()
    reconcile_db, sync_db = session_factory(), session_factory()
    during: Dict[str, Any] = {}

    async def sync_during_reconcile(_reference):
        if "sync" not in during:
            during["sync"] = "running"
            during["sync"] = await service.run_export(sync_db, "2026-10-01", "2026-10-02", [make_order("O-1")])

    zoho.on_lookup = sync_during_reconcile
    try:
        outcome = asyncio.run(service.reconcile_order(reconcile_db, "O-1"))
    finally:
        reconcile_db.close()
        sync_db.close()
    assert details_by_id(during["sync"])["O-1"]["status"] == "in_progress"
    assert outcome["result"] == "reset"
    assert zoho.create_calls == []


def test_sync_that_loses_its_claim_to_reconciliation_does_not_overwrite(session_factory, zoho):
    service = StubbedExportService(None, zoho)
    sync_db, reconcile_db = session_factory(), session_factory()
    reconciled: Dict[str, Any] = {}

    async def reconcile_mid_create(_invoice):
        reconciled["outcome"] = await service.reconcile_order(reconcile_db, "O-1", now=datetime.utcnow() + timedelta(hours=1))

    zoho.during_create = reconcile_mid_create
    try:
        result = asyncio.run(service.run_export(sync_db, "2026-10-01", "2026-10-02", [make_order("O-1")]))
    finally:
        sync_db.close()
        reconcile_db.close()

    assert reconciled["outcome"]["result"] == "linked"
    assert details_by_id(result)["O-1"]["status"] == "needs_verification"
    check = session_factory()
    assert check.query(models.ExportedOrder).one().status == "exported"
    assert check.query(models.ExportedInvoice).count() == 1
    check.close()
    assert len(zoho.invoices_for("O-1")) == 1


def test_production_style_legacy_records(service, zoho, db):
    """Rows as written by the previous code: no claim tokens, no audit timestamps."""
    legacy_run = models.SyncRun(date_from="2026-09-01", date_to="2026-09-01", status="partial", total_orders=3, total_invoices=1)
    db.add(legacy_run)
    db.commit()
    db.add_all(
        [
            models.ExportedOrder(sync_run_id=legacy_run.id, unify_order_id="L-1", customer_name="Customer B1", status="exported", message="Invoice ZINV-1 created"),
            models.ExportedOrder(sync_run_id=legacy_run.id, unify_order_id="L-2", customer_name="Customer B2", status="failed", message='{"error": "Zoho API failed"}'),
            models.ExportedOrder(sync_run_id=legacy_run.id, unify_order_id="L-3", customer_name="Customer B3", status="skipped", message="Some items are missing"),
            models.ExportedInvoice(sync_run_id=legacy_run.id, unify_customer_name="B1", unify_order_ids=["L-1"], zoho_invoice_id="ZINV-1", status="success", message="Draft invoice created"),
        ]
    )
    db.commit()
    zoho.invoices.append({"invoice_id": "ZINV-1", "invoice_number": "N-OLD", "reference_number": "L-1", "customer_id": "C-B1", "customer_name": "Customer B1", "status": "sent"})
    snapshot = run_snapshot(db, legacy_run.id)

    previews = [UnifyOrderPreview(order_id=f"L-{i}", customer_name=f"Customer B{i}", total=10) for i in (1, 2, 3)]
    preview = {o.order_id: (o.already_exported, o.export_status) for o in routes._build_preview_response_payload(previews, db, source="csv")["orders"]}
    assert preview == {"L-1": (True, "exported"), "L-2": (True, "failed"), "L-3": (True, "skipped")}

    resync = run(service, db, make_order("L-1", "B1"), make_order("L-2", "B2"), make_order("L-3", "B3"))
    statuses = {k: v["status"] for k, v in details_by_id(resync).items()}
    assert statuses == {"L-1": "already_exported", "L-2": "created", "L-3": "created"}
    assert zoho.create_calls == ["L-2", "L-3"]

    asyncio.run(routes.reset_order("L-1", db))
    relinked = run(service, db, make_order("L-1", "B1"))
    assert details_by_id(relinked)["L-1"]["zoho_invoice_id"] == "ZINV-1"
    assert zoho.create_calls == ["L-2", "L-3"]
    assert run_snapshot(db, legacy_run.id) == snapshot
    assert "Invoice ZINV-1 created" in record(db, "L-1").message  # legacy evidence kept in the trail


def test_no_reset_operation_ever_deletes_audit_information(service, zoho, db):
    first = run(service, db, make_order("O-1"), make_order("O-2", "B2"))
    invoice_ids = sorted(inv.zoho_invoice_id for inv in db.query(models.ExportedInvoice).all())
    history: List[Dict[str, int]] = [counts(db)]

    asyncio.run(routes.reset_order("O-1", db))
    history.append(counts(db))
    asyncio.run(routes.reset_selected_runs(ResetSelectedRunsRequest(run_ids=[first["sync_run_id"]]), db))
    history.append(counts(db))
    asyncio.run(routes.reset_sync_state(db))
    history.append(counts(db))
    run(service, db, make_order("O-1"), make_order("O-2", "B2"))
    history.append(counts(db))

    assert all(h["orders"] >= history[0]["orders"] and h["invoices"] >= history[0]["invoices"] for h in history)
    assert sorted(inv.zoho_invoice_id for inv in db.query(models.ExportedInvoice).all()) == invoice_ids
    trail = record(db, "O-1").message
    assert "Reset by user (unsync)" in trail and "Invoice INV-1 created" in trail


def test_preview_marks_soft_reset_orders_as_syncable(db):
    seed_record(db, "O-1", "reset")
    seed_record(db, "O-2", "exported")
    payload = routes._build_preview_response_payload(
        [UnifyOrderPreview(order_id=i, customer_name="C", total=1) for i in ("O-1", "O-2")], db, source="csv"
    )
    by_id = {o.order_id: (o.already_exported, o.export_status) for o in payload["orders"]}
    assert by_id == {"O-1": (False, "reset"), "O-2": (True, "exported")}


def test_reconcile_route_maps_errors_and_never_creates(monkeypatch, zoho, db):
    service = StubbedExportService(None, zoho)
    monkeypatch.setattr(routes, "_make_settings_export_service", lambda _db: service)
    seed_claim(db, "O-1", "in_progress", minutes_ago=45)
    zoho.add_invoice("O-1", "C-B1")

    response = asyncio.run(routes.reconcile_order("O-1", db))
    assert response.ok and response.data["result"] == "linked"
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(routes.reconcile_order("O-1", db))
    assert excinfo.value.status_code == 409
    assert zoho.create_calls == []


def test_latest_audit_time_reads_the_most_recent_entry():
    message = "Retry attempt [run 7, 2026-10-19T10:00:00Z] | previous status failed: x [run 3, 2026-10-01T09:00:00Z]"
    assert latest_audit_time(message) == datetime(2026, 10, 19, 10, 0, 0)
    assert latest_audit_time("Invoice ZINV-1 created") is None


def test_stale_attempt_cannot_overwrite_a_newer_claim(service, db):
    """A presumed-dead attempt (e.g. in another worker) must not write over a newer claim."""
    from app.services.export_service import _OrderAttemptState

    seed_record(db, "O-1", "in_progress", "Export attempt started (claim NEWCLAIM) [run 9, 2026-10-19T10:00:00Z]")
    stale = _OrderAttemptState(order_id="O-1", claimed=True, claim_token="OLDCLAIM")

    detail = service._finalize_claim(db, 1, stale, "failed", "stale attempt failed")
    assert "changed concurrently" in detail["message"]
    row = record(db, "O-1")
    assert row.status == "in_progress" and "NEWCLAIM" in row.message

    with pytest.raises(Exception):
        service._persist_exported(db, 1, stale, "INV-STALE", "B1", message="Invoice INV-STALE created")
    db.rollback()
    assert record(db, "O-1").status == "in_progress"
    assert db.query(models.ExportedInvoice).count() == 0
