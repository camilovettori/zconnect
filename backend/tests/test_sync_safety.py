"""Final synchronisation safety review: one test group per safety requirement.

Temporary SQLite databases and the in-memory FakeZoho only (see test_invoice_idempotency.py):
no Unify, Zoho or production database is ever contacted. FakeZoho records every invoice it
would have created, so duplicates are counted exactly.
"""
from __future__ import annotations

import asyncio

import pytest

from app import models
from app.api import routes
from app.services.export_service import ExportService, list_export_events
from tests.test_invoice_idempotency import (  # noqa: F401  (fixtures)
    FakeZoho,
    StubbedExportService,
    db,
    details_by_id,
    make_order,
    record,
    run,
    service,
    session_factory,
    zoho,
)


def history(db, run_id):
    db.expire_all()
    return asyncio.run(routes.export_history_detail(run_id, db))


def outcomes(db, order_id):
    db.expire_all()
    return [(e.event_type, e.previous_status, e.new_status) for e in list_export_events(db, "default", order_id)]


# ---------------------------------------------------------------------------
# 1. An order can never produce a duplicate Zoho invoice
# ---------------------------------------------------------------------------


def test_resyncing_an_exported_order_never_creates_a_second_invoice(service, zoho, db):
    run(service, db, make_order("O-1"))
    second = run(service, db, make_order("O-1"))
    third = run(service, db, make_order("O-1"), make_order("O-1"))  # also a duplicate inside one run

    assert len(zoho.invoices_for("O-1")) == 1 and zoho.create_calls == ["O-1"]
    assert details_by_id(second)["O-1"]["status"] == "already_exported"
    assert third["created"] == 0 and third["total_invoices"] == 0
    assert db.query(models.ExportedInvoice).count() == 1


def test_invoice_already_in_zoho_is_linked_not_recreated(service, zoho, db):
    zoho.add_invoice("O-1", "C-B1")  # e.g. created before a reset, or by an earlier attempt
    result = run(service, db, make_order("O-1"))

    assert zoho.create_calls == [] and len(zoho.invoices_for("O-1")) == 1
    assert details_by_id(result)["O-1"]["status"] == "already_exported"
    assert record(db, "O-1").status == "exported"
    assert [(i.zoho_invoice_id, i.unify_order_ids) for i in db.query(models.ExportedInvoice)] == [("INV-1", ["O-1"])]


@pytest.mark.parametrize("setup, expected", [("two_active", "needs_verification"), ("other_customer", "needs_verification"), ("lookup_error", "failed")])
def test_uncertain_duplicate_check_never_creates(service, zoho, db, setup, expected):
    if setup == "two_active":
        zoho.add_invoice("O-1", "C-B1")
        zoho.add_invoice("O-1", "C-B1")
    elif setup == "other_customer":
        zoho.add_invoice("O-1", "C-OTHER")
    else:
        zoho.lookup_behavior = "error"
    result = run(service, db, make_order("O-1"))

    assert zoho.create_calls == []
    assert details_by_id(result)["O-1"]["status"] == expected
    assert record(db, "O-1").status == expected
    assert result["status"] == "failed" and result["total_invoices"] == 0


# ---------------------------------------------------------------------------
# 2. A failed invoice creation is never reported as a successful synchronisation
# ---------------------------------------------------------------------------


def test_rejected_invoice_is_failed_everywhere(service, zoho, db):
    zoho.create_behavior["O-1"] = "fail"
    result = run(service, db, make_order("O-1"))

    assert details_by_id(result)["O-1"]["status"] == "failed"
    assert (result["status"], result["created"], result["total_invoices"]) == ("failed", 0, 0)
    assert "O-1" in (result["errors"] or "")
    assert record(db, "O-1").status == "failed"
    assert db.query(models.ExportedInvoice).count() == 0
    sync_run = db.get(models.SyncRun, result["sync_run_id"])
    assert (sync_run.status, sync_run.total_invoices) == ("failed", 0)


def test_unknown_zoho_outcome_is_never_success_and_never_auto_retried(service, zoho, db):
    zoho.create_behavior["O-1"] = "timeout_after_accept"  # Zoho stored it, the response was lost
    first = run(service, db, make_order("O-1"))
    second = run(service, db, make_order("O-1"))

    assert details_by_id(first)["O-1"]["status"] == "needs_verification"
    assert first["status"] == "failed" and first["created"] == 0
    assert details_by_id(second)["O-1"]["status"] == "needs_verification"
    assert zoho.create_calls == ["O-1"]  # blocked until reconciled against Zoho
    assert record(db, "O-1").status == "needs_verification"


def test_invoice_created_but_not_recorded_is_needs_verification_not_success(service, zoho, db, monkeypatch):
    def broken_persist(*args, **kwargs):
        raise RuntimeError("database write failed after Zoho accepted the invoice")

    monkeypatch.setattr(service, "_persist_exported", broken_persist)
    result = run(service, db, make_order("O-1"))

    detail = details_by_id(result)["O-1"]
    assert detail["status"] == "needs_verification" and detail["zoho_invoice_id"] == "INV-1"
    assert result["status"] == "failed" and result["created"] == 0
    assert record(db, "O-1").status == "needs_verification"
    assert db.query(models.ExportedInvoice).count() == 0


# ---------------------------------------------------------------------------
# 3. Retries, idempotency claims and the audit trail
# ---------------------------------------------------------------------------


def test_failed_order_is_retried_once_with_a_fresh_duplicate_check_and_full_audit(service, zoho, db):
    zoho.create_behavior["O-1"] = "fail"
    first = run(service, db, make_order("O-1"))
    zoho.create_behavior["O-1"] = "ok"
    second = run(service, db, make_order("O-1"))

    assert zoho.create_calls == ["O-1", "O-1"] and len(zoho.invoices_for("O-1")) == 1
    assert zoho.lookup_calls == ["O-1", "O-1"]  # a Zoho check before every creation attempt
    assert outcomes(db, "O-1") == [
        ("claimed", None, "in_progress"),
        ("failed", "in_progress", "failed"),
        ("claimed", "failed", "in_progress"),
        ("invoice_created", "in_progress", "exported"),
    ]
    runs = [e.sync_run_id for e in list_export_events(db, "default", "O-1")]
    assert runs == [first["sync_run_id"], first["sync_run_id"], second["sync_run_id"], second["sync_run_id"]]


def test_needs_verification_and_in_progress_records_block_new_attempts_with_audit(service, zoho, db):
    from tests.test_invoice_idempotency import seed_record

    seed_record(db, "O-1", "needs_verification")
    seed_record(db, "O-2", "in_progress")
    result = run(service, db, make_order("O-1"), make_order("O-2", "B2"))

    assert zoho.create_calls == [] and zoho.lookup_calls == []
    assert {k: v["status"] for k, v in details_by_id(result).items()} == {"O-1": "needs_verification", "O-2": "in_progress"}
    assert [e[0] for e in outcomes(db, "O-1")] == ["sync_blocked"]
    assert [e[0] for e in outcomes(db, "O-2")] == ["sync_blocked"]


# ---------------------------------------------------------------------------
# 4. Partial failures keep every order's and invoice's status correct
# ---------------------------------------------------------------------------


def test_partial_failure_keeps_each_order_and_invoice_status(service, zoho, db, monkeypatch):
    original = StubbedExportService._build_line_items

    async def flaky_lines(self, db_, order):
        if order.order_id == "O-3":
            raise RuntimeError("unexpected bug while building lines")
        return await original(self, db_, order)

    monkeypatch.setattr(StubbedExportService, "_build_line_items", flaky_lines)
    zoho.create_behavior["O-2"] = "fail"
    result = run(service, db, make_order("O-1"), make_order("O-2", "B2"), make_order("O-3", "B3"), make_order("O-4", "B4"))

    assert {k: v["status"] for k, v in details_by_id(result).items()} == {"O-1": "created", "O-2": "failed", "O-3": "failed", "O-4": "created"}
    assert {o: record(db, o).status for o in ("O-1", "O-2", "O-3", "O-4")} == {"O-1": "exported", "O-2": "failed", "O-3": "failed", "O-4": "exported"}
    assert sorted(i.unify_order_ids[0] for i in db.query(models.ExportedInvoice)) == ["O-1", "O-4"]
    assert (result["status"], result["created"], result["failed"], result["total_invoices"]) == ("partial", 2, 2, 2)
    assert sorted(zoho.create_calls) == ["O-1", "O-2", "O-4"]  # the broken order never reached Zoho


# ---------------------------------------------------------------------------
# 5. Sync history reflects the real outcome
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "created, failed, not_exported, expected",
    [
        (3, 0, 0, "success"),
        (2, 1, 0, "partial"),
        (2, 0, 1, "partial"),
        (0, 1, 0, "failed"),
        (0, 1, 2, "failed"),
        (0, 0, 2, "skipped"),
        (0, 0, 0, "skipped"),
    ],
)
def test_final_run_status_matrix(created, failed, not_exported, expected):
    assert ExportService._final_run_status(created=created, failed=failed, not_exported=not_exported) == expected


def test_run_of_already_exported_orders_is_not_reported_as_failed(service, zoho, db):
    run(service, db, make_order("O-1"))
    again = run(service, db, make_order("O-1"))
    mixed = run(service, db, make_order("O-1"), make_order("O-2", "B2"))

    assert (again["status"], again["errors"], again["total_invoices"]) == ("skipped", None, 0)
    assert (mixed["status"], mixed["created"]) == ("success", 1)  # nothing failed: already exported is correct


def test_retry_run_history_shows_its_own_outcomes(service, zoho, db):
    zoho.create_behavior["O-1"] = "fail"
    failed_run = run(service, db, make_order("O-1"))
    zoho.create_behavior["O-1"] = "ok"
    retry_run = run(service, db, make_order("O-1"))

    retry = history(db, retry_run["sync_run_id"])
    assert [(o["unify_order_id"], o["status"]) for o in retry["orders"]] == [("O-1", "exported")]
    assert "Draft invoice created in this run" in retry["orders"][0]["message"]
    assert [i["unify_order_ids"] for i in retry["invoices"]] == [["O-1"]]

    original = history(db, failed_run["sync_run_id"])
    assert [(o["unify_order_id"], o["status"]) for o in original["orders"]] == [("O-1", "failed")]
    assert "Current status: exported." in original["orders"][0]["message"]
    assert original["invoices"] == []


def test_blocked_orders_appear_in_the_run_that_skipped_them(service, zoho, db):
    run(service, db, make_order("O-1"))
    again = run(service, db, make_order("O-1"))

    detail = history(db, again["sync_run_id"])
    assert [(o["unify_order_id"], o["status"]) for o in detail["orders"]] == [("O-1", "skipped")]
    assert "Current status: exported." in detail["orders"][0]["message"]


# ---------------------------------------------------------------------------
# 7. Historical runs (before the audit trail) are preserved exactly
# ---------------------------------------------------------------------------


def test_legacy_run_history_is_unchanged(db):
    legacy = models.SyncRun(date_from="2026-09-01", date_to="2026-09-01", status="success", total_orders=1, total_invoices=1)
    db.add(legacy)
    db.commit()
    db.add(models.ExportedOrder(sync_run_id=legacy.id, unify_order_id="L-1", customer_name="Legacy", status="exported", message="Invoice ZINV-1 created"))
    db.add(models.ExportedInvoice(sync_run_id=legacy.id, unify_customer_name="Legacy", unify_order_ids=["L-1"], zoho_invoice_id="ZINV-1", status="success", message="Draft invoice created"))
    db.commit()

    detail = history(db, legacy.id)
    assert detail["sync_run"]["status"] == "success"
    assert [(o["unify_order_id"], o["status"], o["message"]) for o in detail["orders"]] == [("L-1", "exported", "Invoice ZINV-1 created")]
    assert [(i["zoho_invoice_id"], i["unify_order_ids"]) for i in detail["invoices"]] == [("ZINV-1", ["L-1"])]
    listed = asyncio.run(routes.export_history(db))
    assert [(r["id"], r["status"], r["total_invoices"]) for r in listed] == [(legacy.id, "success", 1)]
