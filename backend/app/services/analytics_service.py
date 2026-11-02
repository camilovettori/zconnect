"""Analytics V1: company-scoped metrics from data Zconnect actually stores.

Two sources, never mixed:
- Captured order values (zconnect_order_facts / zconnect_order_lines): the net, pre-tax value
  of the lines Zconnect sent to Zoho as DRAFT invoices. An invoiced amount, not paid revenue.
  Dated by invoice date. Orders invoiced before value capture started have no value; the
  response marks those periods as not captured instead of showing zero.
- Counts from the original tables (sync_runs, exported_orders, exported_invoices), scoped to
  a company through zconnect_sync_run_companies; runs without a row belong to the default
  company. Dated by sync time. Never presented as revenue.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List, Optional
from types import SimpleNamespace

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import models
from .export_service import DELIVERY_PRODUCT_KEY

GRANULARITIES = ("day", "week", "month")
MAX_RANGE_DAYS = 366 * 3
TOP_N = 10
_CENT = Decimal("0.01")


class AnalyticsError(ValueError):
    pass


@dataclass(frozen=True)
class AnalyticsFilters:
    company_id: str
    date_from: date
    date_to: date
    granularity: str
    customer: Optional[str] = None  # customer_key
    product: Optional[str] = None  # product_key


def default_granularity(date_from: date, date_to: date) -> str:
    days = (date_to - date_from).days + 1
    return "day" if days <= 45 else "week" if days <= 26 * 7 else "month"


def build_filters(company_id: str, date_from: Optional[str], date_to: Optional[str], granularity: Optional[str], customer: Optional[str], product: Optional[str], today: Optional[date] = None) -> AnalyticsFilters:
    today = today or datetime.utcnow().date()
    try:
        end = date.fromisoformat(date_to) if date_to else today
        start = date.fromisoformat(date_from) if date_from else end - timedelta(days=89)
    except ValueError:
        raise AnalyticsError("Dates must use the YYYY-MM-DD format")
    if start > end:
        raise AnalyticsError("The start date must be on or before the end date")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise AnalyticsError("The date range can be at most three years")
    grain = (granularity or default_granularity(start, end)).strip().lower()
    if grain not in GRANULARITIES:
        raise AnalyticsError("Granularity must be day, week or month")
    return AnalyticsFilters(
        company_id=company_id,
        date_from=start,
        date_to=end,
        granularity=grain,
        customer=(customer or "").strip() or None,
        product=(product or "").strip() or None,
    )


def money(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP))


def _bucket_start(day: date, grain: str) -> date:
    if grain == "week":
        return day - timedelta(days=day.weekday())  # ISO week, Monday
    if grain == "month":
        return day.replace(day=1)
    return day


def _next_bucket(start: date, grain: str) -> date:
    if grain == "week":
        return start + timedelta(days=7)
    if grain == "month":
        return (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start + timedelta(days=1)


def _buckets(date_from: date, date_to: date, grain: str) -> List[date]:
    starts, current = [], _bucket_start(date_from, grain)
    while current <= date_to:
        starts.append(current)
        current = _next_bucket(current, grain)
    return starts


def _company_runs(company_id: str):
    """Condition selecting sync runs that belong to the company (legacy runs -> default)."""
    owned = exists().where(models.SyncRunCompany.sync_run_id == models.SyncRun.id)
    mine = exists().where(and_(models.SyncRunCompany.sync_run_id == models.SyncRun.id, models.SyncRunCompany.company_id == company_id))
    if company_id == models.DEFAULT_COMPANY_ID:
        return or_(mine, ~owned)
    return mine


def _range_bounds(f: AnalyticsFilters):
    return datetime.combine(f.date_from, datetime.min.time()), datetime.combine(f.date_to + timedelta(days=1), datetime.min.time())


def _value_rows(db: Session, f: AnalyticsFilters):
    """Captured Unify orders plus historical invoices, counted only once per Zoho ID.

    The Zoho invoice snapshot takes precedence if the same invoice appears in
    both stores. Historical invoice counts represent invoices, not individual
    Unify order IDs, since multiple orders may share a single Zoho invoice.
    """
    historical = db.execute(
        select(models.HistoricalInvoice)
        .options(selectinload(models.HistoricalInvoice.lines))
        .where(
            models.HistoricalInvoice.company_id == f.company_id,
            models.HistoricalInvoice.invoice_date >= f.date_from,
            models.HistoricalInvoice.invoice_date <= f.date_to,
        )
    ).scalars().all()
    # Deduplicate across dates, including when a Zoho invoice date changes.
    history_ids = set(db.execute(
        select(models.HistoricalInvoice.zoho_invoice_id)
        .where(models.HistoricalInvoice.company_id == f.company_id)
    ).scalars().all())
    rows = []
    original_query = (
        select(models.OrderFact)
        .options(selectinload(models.OrderFact.lines))
        .where(
            models.OrderFact.company_id == f.company_id,
            models.OrderFact.invoice_date >= f.date_from,
            models.OrderFact.invoice_date <= f.date_to,
        )
    )
    for fact in db.execute(original_query).scalars().all():
        if fact.zoho_invoice_id and fact.zoho_invoice_id in history_ids:
            continue
        if f.customer and f.customer != fact.customer_key:
            continue
        lines = [line for line in fact.lines if line.company_id == f.company_id]
        if f.product:
            lines = [line for line in lines if line.product_key == f.product]
            if not lines:
                continue
            value = sum((Decimal(line.net_amount) for line in lines), Decimal("0"))
        else:
            value = Decimal(fact.net_amount)
        rows.append((fact, lines, value))

    for invoice in historical:
        if invoice.status.lower() in ("void", "cancelled", "canceled"):
            continue
        customer_key = invoice.customer_id or "name:" + " ".join(invoice.customer_name.casefold().split())
        if f.customer and f.customer != customer_key:
            continue
        lines = [line for line in invoice.lines if line.company_id == f.company_id]
        if f.product:
            lines = [line for line in lines if line.product_key == f.product]
            if not lines:
                continue
            value = sum((Decimal(line.net_amount) for line in lines), Decimal("0"))
        else:
            value = Decimal(invoice.net_amount)
        fact = SimpleNamespace(
            id="history:" + str(invoice.id),
            invoice_date=invoice.invoice_date,
            currency=invoice.currency,
            zoho_invoice_id=invoice.zoho_invoice_id,
            customer_key=customer_key,
            customer_name=invoice.customer_name,
        )
        rows.append((fact, lines, value))
    return rows



def compute_summary(db: Session, f: AnalyticsFilters) -> Dict:
    rows = _value_rows(db, f)
    currencies = sorted({fact.currency for fact, _lines, _value in rows})
    total_value = sum((value for _fact, _lines, value in rows), Decimal("0"))
    orders_with_value = len(rows)
    active_customers = len({fact.customer_key for fact, _lines, _value in rows})
    units_sold = sum((Decimal(line.quantity) for _fact, lines, _value in rows for line in lines if line.product_key != DELIVERY_PRODUCT_KEY), Decimal('0'))

    capture_start_at = db.execute(select(func.min(models.OrderFact.captured_at)).where(models.OrderFact.company_id == f.company_id)).scalar()
    capture_start = capture_start_at.date() if isinstance(capture_start_at, datetime) else capture_start_at
    history_start = db.execute(select(func.min(models.HistoricalInvoice.invoice_date)).where(models.HistoricalInvoice.company_id == f.company_id)).scalar()
    if history_start is not None:
        capture_start = min(capture_start, history_start) if capture_start is not None else history_start
    # A value is known when capture had started by the end of the range (or values exist in it),
    # and all captured values share one currency.
    value_known = len(currencies) <= 1 and (bool(rows) or (capture_start is not None and capture_start <= f.date_to))

    # Counts from the original tables (sync time), scoped by the run's company.
    start_at, end_at = _range_bounds(f)
    company_runs = _company_runs(f.company_id)
    run_status_rows = db.execute(
        select(models.SyncRun.status, func.count())
        .where(company_runs, models.SyncRun.started_at >= start_at, models.SyncRun.started_at < end_at)
        .group_by(models.SyncRun.status)
    ).all()
    runs_by_status = {status: count for status, count in run_status_rows}

    invoice_query = (
        select(func.count())
        .select_from(models.ExportedInvoice)
        .join(models.SyncRun, models.SyncRun.id == models.ExportedInvoice.sync_run_id)
        .where(company_runs, models.ExportedInvoice.created_at >= start_at, models.ExportedInvoice.created_at < end_at)
    )
    exported_query = (
        select(func.count())
        .select_from(models.ExportedOrder)
        .join(models.SyncRun, models.SyncRun.id == models.ExportedOrder.sync_run_id)
        .where(company_runs, models.ExportedOrder.status == "exported", models.ExportedOrder.created_at >= start_at, models.ExportedOrder.created_at < end_at)
    )
    if f.customer:
        invoice_query = invoice_query.where(models.ExportedInvoice.unify_customer_name == f.customer)
    if f.product:
        # Invoices and exports are not stored per product: count captured orders instead.
        invoices_created = sum(1 for fact, _l, _v in rows if fact.zoho_invoice_id)
        exported_orders = None
    else:
        invoices_created = db.execute(invoice_query).scalar() or 0
        exported_orders = None if f.customer else (db.execute(exported_query).scalar() or 0)

    # Time series by invoice date. Periods before value capture started are not zero: unknown.
    series_value: Dict[date, Decimal] = defaultdict(Decimal)
    series_orders: Dict[date, int] = defaultdict(int)
    for fact, _lines, value in rows:
        bucket = _bucket_start(fact.invoice_date, f.granularity)
        series_value[bucket] += value
        series_orders[bucket] += 1
    series = []
    for start in _buckets(f.date_from, f.date_to, f.granularity):
        end = min(_next_bucket(start, f.granularity) - timedelta(days=1), f.date_to)
        has_data = start in series_orders
        if capture_start is None or (end < capture_start and not has_data):
            series.append({"period_start": start.isoformat(), "period_end": end.isoformat(), "value": None, "orders": None, "captured": False, "partial": False})
            continue
        series.append(
            {
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "value": money(series_value.get(start, Decimal("0"))),
                "orders": series_orders.get(start, 0),
                "captured": True,
                "partial": max(start, f.date_from) < capture_start,
            }
        )

    # Top customers and products by captured and imported invoice value.
    customers: Dict[str, Dict] = {}
    for fact, _lines, value in rows:
        entry = customers.setdefault(fact.customer_key, {"key": fact.customer_key, "name": fact.customer_name or fact.customer_key, "value": Decimal("0"), "orders": 0})
        entry["value"] += value
        entry["orders"] += 1
    products: Dict[str, Dict] = {}
    for fact, lines, _value in rows:
        for line in lines:
            if line.product_key == DELIVERY_PRODUCT_KEY:
                continue
            entry = products.setdefault(line.product_key, {"key": line.product_key, "name": line.product_name, "value": Decimal("0"), "quantity": Decimal("0"), "order_ids": set()})
            entry["value"] += Decimal(line.net_amount)
            entry["quantity"] += Decimal(line.quantity)
            entry["order_ids"].add(fact.id)

    def ranked(items: Iterable[Dict]) -> List[Dict]:
        return sorted(items, key=lambda item: (-item["value"], item["name"]))[:TOP_N]

    def ranked_by_quantity(items: Iterable[Dict]) -> List[Dict]:
        return sorted(items, key=lambda item: (-item["quantity"], -item["value"], item["name"]))[:TOP_N]

    return {
        "filters": {
            "company_id": f.company_id,
            "date_from": f.date_from.isoformat(),
            "date_to": f.date_to.isoformat(),
            "granularity": f.granularity,
            "customer": f.customer,
            "product": f.product,
        },
        "currency": currencies[0] if len(currencies) == 1 else (None if not currencies else "MIXED"),
        "value_basis": "Net pre-tax invoice value from Zconnect captures and historical Zoho Books imports. Not cash received.",
        "kpis": {
            # None = unknown (nothing captured for this range yet, or mixed currencies); never 0.
            "invoiced_value": None if not value_known else money(total_value),
            "orders_with_value": orders_with_value if value_known else None,
            "active_customers": active_customers,
            "units_sold": str(units_sold.normalize()),
            "average_order_value": money(total_value / orders_with_value) if value_known and orders_with_value else None,
            "invoices_created": invoices_created,
            "exported_orders": exported_orders,
            "sync_runs": {
                "success": runs_by_status.get("success", 0),
                "partial": runs_by_status.get("partial", 0),
                "failed": runs_by_status.get("failed", 0),
                "skipped": runs_by_status.get("skipped", 0),
                "running": runs_by_status.get("running", 0),
                "total": sum(runs_by_status.values()),
            },
        },
        "coverage": {
            "value_capture_started": capture_start.isoformat() if capture_start else None,
            "range_starts_before_capture": capture_start is None or f.date_from < capture_start,
            "exported_orders_without_value": None if exported_orders is None else max(exported_orders - orders_with_value, 0),
            "count_basis": "Invoices, exported orders and sync runs are counted by sync date and are not monetary values.",
            "sync_runs_ignore_customer_and_product_filters": bool(f.customer or f.product),
        },
        "series": series,
        "top_customers": [
            {"key": c["key"], "name": c["name"], "value": money(c["value"]), "orders": c["orders"]} for c in ranked(customers.values())
        ],
        "top_products": [
            {"key": p["key"], "name": p["name"], "value": money(p["value"]), "quantity": str(p["quantity"].normalize()), "orders": len(p["order_ids"])}
            for p in ranked(products.values())
        ],
        "top_products_by_quantity": [
            {"key": p["key"], "name": p["name"], "value": money(p["value"]), "quantity": str(p["quantity"].normalize()), "orders": len(p["order_ids"])}
            for p in ranked_by_quantity(products.values())
        ],
    }


def filter_options(db: Session, company_id: str) -> Dict:
    customers = {
        key: name or key
        for key, name in db.execute(
            select(models.OrderFact.customer_key, func.max(models.OrderFact.customer_name))
            .where(models.OrderFact.company_id == company_id)
            .group_by(models.OrderFact.customer_key)
        ).all()
    }
    for contact_id, name in db.execute(
        select(models.HistoricalInvoice.customer_id, models.HistoricalInvoice.customer_name)
        .where(models.HistoricalInvoice.company_id == company_id)
    ).all():
        key = contact_id or "name:" + " ".join(name.casefold().split())
        customers[key] = name or key

    products = {
        key: name or key
        for key, name in db.execute(
            select(models.OrderLineFact.product_key, func.max(models.OrderLineFact.product_name))
            .where(models.OrderLineFact.company_id == company_id, models.OrderLineFact.product_key != DELIVERY_PRODUCT_KEY)
            .group_by(models.OrderLineFact.product_key)
        ).all()
    }
    for key, name in db.execute(
        select(models.HistoricalInvoiceLine.product_key, models.HistoricalInvoiceLine.product_name)
        .where(models.HistoricalInvoiceLine.company_id == company_id, models.HistoricalInvoiceLine.product_key != DELIVERY_PRODUCT_KEY)
    ).all():
        products.setdefault(key, name or key)
    return {
        "customers": sorted(({"key": key, "name": name} for key, name in customers.items()), key=lambda c: c["name"].casefold()),
        "products": sorted(({"key": key, "name": name} for key, name in products.items()), key=lambda p: p["name"].casefold()),
    }



def list_companies(db: Session) -> List[Dict]:
    return [{"id": c.id, "name": c.name} for c in db.execute(select(models.Company).order_by(models.Company.name)).scalars().all()]
