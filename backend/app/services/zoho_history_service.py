"""One-time, read-only import of Zoho Books invoice snapshots.

This module never creates or modifies a Zoho invoice. Import a complete validated
snapshot, then upsert by (company, organisation, Zoho invoice ID). All database
changes are committed together so partial imports cannot masquerade as complete.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .zoho_service import ZohoService, ZohoLookupError


class HistoricalImportError(ValueError):
    pass


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise HistoricalImportError(f"Invalid {field} in Zoho invoice") from exc
    if not result.is_finite():
        raise HistoricalImportError(f"Non-finite {field} in Zoho invoice")
    return result


def _required(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise HistoricalImportError(f"Zoho invoice is missing {field}")
    return result


def _normalise_invoice(invoice: Dict[str, Any]) -> Dict[str, Any]:
    invoice_id = _required(invoice.get("invoice_id"), "invoice_id")
    raw_date = _required(invoice.get("date"), "date")
    try:
        invoice_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise HistoricalImportError(f"Invalid invoice date for {invoice_id}") from exc
    if not isinstance(invoice.get("line_items"), list):
        raise HistoricalImportError(f"Missing full invoice lines for {invoice_id}")
    lines = []
    for index, line in enumerate(invoice["line_items"]):
        if not isinstance(line, dict):
            raise HistoricalImportError(f"Unexpected line for invoice {invoice_id}")
        if line.get("line_item_category", "line_item") not in (None, "", "line_item"):
            continue
        name = _required(line.get("name") or line.get("description"), f"line name for {invoice_id}")
        quantity = _decimal(line.get("quantity"), "quantity")
        net = _decimal(line.get("item_total"), "item_total")
        item_id = str(line.get("item_id") or "").strip()
        key = ("zoho:" + item_id) if item_id else ("name:" + " ".join(name.casefold().split()))
        if name.casefold() == "delivery charge":
            key = "delivery"
        lines.append({
            "zoho_line_item_id": str(line.get("line_item_id") or "").strip() or None,
            "product_key": key[:255],
            "product_name": name[:255],
            "quantity": quantity,
            "net_amount": net,
        })
    # Zoho subtotal is the pre-tax, pre-shipping value; do not substitute total,
    # which may include VAT and adjustments. Missing subtotal => fail closed.
    if invoice.get("sub_total") is None:
        raise HistoricalImportError(f"Invoice {invoice_id} missing pre-tax sub_total")
    subtotal = _decimal(invoice.get("sub_total"), "sub_total")
    product_total = sum((line["net_amount"] for line in lines), Decimal("0"))
    if abs(subtotal - product_total) > Decimal("0.01"):
        raise HistoricalImportError(
            f"Invoice {invoice_id} subtotal differs from line totals; verify discounts or adjustments"
        )
    return {
        "zoho_invoice_id": invoice_id,
        "invoice_number": str(invoice.get("invoice_number") or "")[:128] or None,
        "invoice_date": invoice_date,
        "customer_id": str(invoice.get("customer_id") or "").strip() or None,
        "customer_name": _required(invoice.get("customer_name"), "customer_name")[:255],
        "status": _required(invoice.get("status"), "status").lower()[:32],
        "currency": _required(invoice.get("currency_code"), "currency_code")[:3],
        "net_amount": subtotal,
        "lines": lines,
    }


async def fetch_snapshot(zoho: ZohoService) -> List[Dict[str, Any]]:
    """Read all pages and invoice detail records before modifying our database."""
    summaries = await zoho.list_all_invoices_for_duplicate_check()
    if len(summaries) > 10000:
        raise HistoricalImportError("More than 10,000 invoices; use a controlled batch import")
    ids = [_required(row.get("invoice_id"), "invoice_id") for row in summaries]
    if len(ids) != len(set(ids)):
        raise HistoricalImportError("Zoho returned duplicate invoice IDs")
    snapshot = []
    for invoice_id in ids:
        try:
            resp = await zoho._request("GET", f"/invoices/{invoice_id}")
        except Exception as exc:
            raise ZohoLookupError(f"Unable to retrieve invoice {invoice_id}") from exc
        if resp.status_code != 200:
            raise ZohoLookupError(f"Invoice detail {invoice_id} returned {resp.status_code}")
        try:
            detail = resp.json().get("invoice")
        except (ValueError, AttributeError) as exc:
            raise ZohoLookupError(f"Invalid detail response for {invoice_id}") from exc
        if not isinstance(detail, dict) or str(detail.get("invoice_id")) != invoice_id:
            raise ZohoLookupError(f"Invoice detail ID mismatch for {invoice_id}")
        snapshot.append(_normalise_invoice(detail))
    return snapshot



def preview_snapshot(snapshot: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Minimal monetary reconciliation metadata for an admin dry run.

    Never returns customer names, invoice IDs, or individual invoice details.
    Statuses stay separate so draft invoices are never described as paid sales.
    """
    counts: Dict[str, int] = {}
    net_by_currency: Dict[str, Decimal] = {}
    earliest = latest = None
    for invoice in snapshot:
        status = invoice["status"]
        currency = invoice["currency"]
        counts[status] = counts.get(status, 0) + 1
        net_by_currency[currency] = net_by_currency.get(currency, Decimal("0")) + invoice["net_amount"]
        day = invoice["invoice_date"]
        earliest = day if earliest is None else min(earliest, day)
        latest = day if latest is None else max(latest, day)
    return {
        "validated_invoices": len(snapshot),
        "status_counts": dict(sorted(counts.items())),
        "net_by_currency": {c: str(v.quantize(Decimal("0.01"))) for c, v in sorted(net_by_currency.items())},
        "first_invoice_date": earliest.isoformat() if earliest else None,
        "last_invoice_date": latest.isoformat() if latest else None,
    }


def persist_snapshot(db: Session, *, company_id: str, zoho_org_id: str, snapshot: List[Dict[str, Any]]) -> Dict[str, int]:
    """Idempotent import. Caller must ensure this company owns the Zoho organisation."""
    if not company_id or not zoho_org_id:
        raise HistoricalImportError("Company and Zoho organisation are required")
    if db.get(models.Company, company_id) is None:
        raise HistoricalImportError("Company does not exist")
    existing = {
        row.zoho_invoice_id: row
        for row in db.execute(
            select(models.HistoricalInvoice).where(
                models.HistoricalInvoice.company_id == company_id,
                models.HistoricalInvoice.zoho_org_id == zoho_org_id,
            )
        ).scalars().all()
    }
    if len({i["zoho_invoice_id"] for i in snapshot}) != len(snapshot):
        raise HistoricalImportError("Duplicate invoice IDs within import snapshot")

    created = updated = 0
    try:
        for data in snapshot:
            invoice_id = data["zoho_invoice_id"]
            invoice = existing.get(invoice_id)
            if invoice is None:
                invoice = models.HistoricalInvoice(
                    company_id=company_id, zoho_org_id=zoho_org_id, zoho_invoice_id=invoice_id
                )
                db.add(invoice)
                created += 1
            else:
                updated += 1
            for field in ("invoice_number", "invoice_date", "customer_id", "customer_name", "status", "currency", "net_amount"):
                setattr(invoice, field, data[field])
            db.flush()
            invoice.lines[:] = [
                models.HistoricalInvoiceLine(company_id=company_id, **line)
                for line in data["lines"]
            ]
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"created": created, "updated": updated, "total": len(snapshot)}

async def import_next_batch(zoho: ZohoService, db: Session, *, company_id: str, max_details: int = 100) -> Dict[str, Any]:
    """Import one complete invoice-date group at a time, newest first.

    Existing invoice IDs are skipped without detail calls. A date group is
    committed only after every detail for that date has been validated.
    A group larger than the remaining budget is deferred in full.
    """
    if not 1 <= max_details <= 100:
        raise HistoricalImportError("Batch limit must be between 1 and 100")
    summaries = await zoho.list_all_invoices_for_duplicate_check()
    ids = [_required(row.get("invoice_id"), "invoice_id") for row in summaries]
    if len(ids) != len(set(ids)):
        raise HistoricalImportError("Zoho returned duplicate invoice IDs")
    if len(ids) > 10000:
        raise HistoricalImportError("More than 10,000 invoices; manual review required")
    existing = set(db.execute(
        select(models.HistoricalInvoice.zoho_invoice_id).where(
            models.HistoricalInvoice.company_id == company_id,
            models.HistoricalInvoice.zoho_org_id == zoho.org_id,
        )
    ).scalars().all())
    pending = {}
    for row in summaries:
        invoice_id = _required(row.get("invoice_id"), "invoice_id")
        if invoice_id in existing:
            continue
        try:
            day = date.fromisoformat(_required(row.get("date"), "date"))
        except ValueError as exc:
            raise HistoricalImportError("Invalid invoice date in Zoho listing") from exc
        pending.setdefault(day, []).append(invoice_id)
    total_pending = sum(map(len, pending.values()))
    imported = 0
    dates_completed = 0
    for day in sorted(pending, reverse=True):
        group = pending[day]
        if imported + len(group) > max_details:
            break
        snapshot = []
        for invoice_id in group:
            try:
                response = await zoho._request("GET", f"/invoices/{invoice_id}")
            except Exception as exc:
                raise ZohoLookupError(f"Unable to fetch Zoho invoice detail for {invoice_id}") from exc
            if response.status_code != 200:
                raise ZohoLookupError(f"Zoho invoice detail failed with HTTP {response.status_code}")
            try:
                detail = response.json().get("invoice")
            except (ValueError, AttributeError) as exc:
                raise ZohoLookupError("Invalid Zoho invoice detail JSON") from exc
            if not isinstance(detail, dict) or str(detail.get("invoice_id")) != invoice_id:
                raise ZohoLookupError("Zoho invoice ID mismatch")
            normalised = _normalise_invoice(detail)
            if normalised["invoice_date"] != day:
                raise HistoricalImportError("Invoice date changed during import; retry after reconciliation")
            snapshot.append(normalised)
        persist_snapshot(db, company_id=company_id, zoho_org_id=zoho.org_id, snapshot=snapshot)
        imported += len(group)
        dates_completed += 1
    return {
        "imported": imported, "remaining_estimate": total_pending - imported,
        "completed_dates": dates_completed, "listing_count": len(summaries),
        "blocked_large_day": bool(pending) and imported == 0,
    }


async def preview_next_batch(zoho: ZohoService, db: Session, *, company_id: str) -> Dict[str, Any]:
    """Cheap preview: listing pages only, no individual invoice detail requests."""
    summaries = await zoho.list_all_invoices_for_duplicate_check()
    existing = set(db.execute(
        select(models.HistoricalInvoice.zoho_invoice_id).where(
            models.HistoricalInvoice.company_id == company_id,
            models.HistoricalInvoice.zoho_org_id == zoho.org_id,
        )
    ).scalars().all())
    ids = [_required(row.get("invoice_id"), "invoice_id") for row in summaries]
    if len(ids) != len(set(ids)):
        raise HistoricalImportError("Duplicate invoice IDs in listing")
    pending = len([invoice_id for invoice_id in ids if invoice_id not in existing])
    return {"dry_run": True, "saved": False, "listed_invoices": len(ids),
            "already_imported": len(ids) - pending, "remaining_estimate": pending,
            "estimated_minimum_days_at_100": (pending + 99) // 100,
            "note": "Invoice totals require detail requests; not available in cheap preview"}
