"""Integration tests for the real batch snapshot path, using SQLite and a fake HTTP layer.

No Zoho credentials, production database or external network calls.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.services.zoho_service import ZohoService, ZohoLookupError
from test_invoice_idempotency import StubbedExportService, make_order, details_by_id


def make_batch_zoho(monkeypatch, existing=(), fail_lookup=False):
    zoho = object.__new__(ZohoService)
    calls = {"pages": [], "created": []}
    invoices = [dict(inv) for inv in existing]

    async def request(method, path, **kwargs):
        assert method == "GET" and path == "/invoices"
        params = kwargs["params"]
        calls["pages"].append(params["page"])
        if fail_lookup:
            raise TimeoutError("simulated lookup timeout")
        assert params["filter_by"] == "Status.All"
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"invoices": list(invoices), "page_context": {"has_more_page": False}},
        )

    async def create(payload):
        reference = payload["reference_number"]
        calls["created"].append(reference)
        invoice_id = f"NEW-{len(calls['created'])}"
        invoices.append({
            "invoice_id": invoice_id,
            "reference_number": reference,
            "customer_id": payload["customer_id"],
            "status": "draft",
        })
        return invoice_id

    monkeypatch.setattr(zoho, "_request", request)
    monkeypatch.setattr(zoho, "create_draft_invoice_from_payload", create)
    return zoho, calls


def test_batch_uses_one_snapshot_for_10_orders(db, monkeypatch):
    zoho, calls = make_batch_zoho(monkeypatch)
    service = StubbedExportService(None, zoho)
    orders = [make_order(f"O-{i}") for i in range(10)]
    result = asyncio.run(service.run_export(db, "2026-10-01", "2026-10-02", orders))
    assert result["created"] == 10
    assert calls["pages"] == [1]
    assert calls["created"] == [f"O-{i}" for i in range(10)]


def test_batch_links_existing_and_creates_only_missing(db, monkeypatch):
    existing = [{
        "invoice_id": "OLD-1", "reference_number": "O-1",
        "customer_id": "C-B1", "status": "draft",
    }]
    zoho, calls = make_batch_zoho(monkeypatch, existing)
    service = StubbedExportService(None, zoho)
    result = asyncio.run(service.run_export(
        db, "2026-10-01", "2026-10-02", [make_order("O-1"), make_order("O-2")]
    ))
    details = details_by_id(result)
    assert details["O-1"]["status"] == "already_exported"
    assert details["O-2"]["status"] == "created"
    assert calls["pages"] == [1]
    assert calls["created"] == ["O-2"]


def test_batch_lookup_failure_never_creates(db, monkeypatch):
    zoho, calls = make_batch_zoho(monkeypatch, fail_lookup=True)
    service = StubbedExportService(None, zoho)
    result = asyncio.run(service.run_export(
        db, "2026-10-01", "2026-10-02", [make_order("O-1"), make_order("O-2")]
    ))
    assert result["created"] == 0
    assert calls["pages"] == [1]
    assert calls["created"] == []
    assert all(detail["status"] == "failed" for detail in result["details"])
