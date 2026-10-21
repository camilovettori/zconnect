"""Weekly (or any configured period) business report for one company.

All figures come from analytics_service.compute_summary, the same company-scoped calculation
the Analytics dashboard uses. The report model is plain data, so it is tested independently of
the PDF; render_pdf only lays it out with the reusable components.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from .. import models
from ..services import analytics_service
from ..services.export_service import ANALYTICS_CURRENCY
from . import components as ui
from . import formatting as fmt
from .periods import ReportPeriod

REPORT_TITLE = "Sales & invoicing report"
DRAFT_NOTICE = (
    "<b>Invoiced value is not paid revenue.</b> Values are the net, pre-tax totals of the "
    "<b>draft invoices</b> Zconnect created in Zoho Books, dated by invoice date. They do not show "
    "whether an invoice was approved, sent or paid. Sync counts are activity, never money."
)


@dataclass
class ReportModel:
    company_id: str
    company_name: str
    period: ReportPeriod
    previous: ReportPeriod
    generated_at: datetime
    currency: Optional[str]
    current: Dict
    prior: Dict
    warnings: List[str] = field(default_factory=list)

    @property
    def kpis(self) -> Dict:
        return self.current["kpis"]

    @property
    def value_known(self) -> bool:
        return self.kpis["invoiced_value"] is not None

    @property
    def prior_complete(self) -> bool:
        """Values for the previous period were recorded for all of it, so a % change is fair."""
        return not self.prior["coverage"]["range_starts_before_capture"]


def company_name(db: Session, company_id: str) -> str:
    company = db.get(models.Company, company_id)
    return company.name if company and company.name else company_id


def _summary(db: Session, company_id: str, period: ReportPeriod, granularity: str) -> Dict:
    filters = analytics_service.AnalyticsFilters(company_id=company_id, date_from=period.start, date_to=period.end, granularity=granularity)
    return analytics_service.compute_summary(db, filters)


def build_report_model(db: Session, company_id: str, period: ReportPeriod, now: Optional[datetime] = None) -> ReportModel:
    granularity = "day" if period.days <= 31 else "week" if period.days <= 26 * 7 else "month"
    current = _summary(db, company_id, period, granularity)
    previous_period = period.previous()
    prior = _summary(db, company_id, previous_period, granularity)

    warnings: List[str] = []
    coverage = current["coverage"]
    if current["currency"] == "MIXED":
        warnings.append("Captured invoices use more than one currency, so values cannot be totalled and are shown as not recorded.")
    elif coverage["value_capture_started"] is None:
        warnings.append("No invoice values have been recorded for this company yet. Value capture starts with the first sync after Analytics was enabled; activity counts below are still accurate.")
    elif coverage["range_starts_before_capture"]:
        warnings.append(
            f"Invoice values are recorded from {fmt.day(date.fromisoformat(coverage['value_capture_started']))}. "
            "Days before that are shown as not recorded, not as zero."
        )
    missing = coverage.get("exported_orders_without_value")
    if missing:
        warnings.append(f"{missing} exported order(s) in this period have no recorded value (exported before value capture started).")

    return ReportModel(
        company_id=company_id,
        company_name=company_name(db, company_id),
        period=period,
        previous=previous_period,
        generated_at=now or datetime.now(timezone.utc),
        currency=current["currency"] if current["currency"] not in (None, "MIXED") else ANALYTICS_CURRENCY,
        current=current,
        prior=prior,
        warnings=warnings,
    )


def _change_caption(current: Optional[str], previous: Optional[str], fallback: str, comparable: bool = True) -> str:
    return (fmt.change(current, previous) if comparable else None) or fallback


def kpi_items(model: ReportModel) -> List[ui.Kpi]:
    k, p, cur = model.kpis, model.prior["kpis"], model.currency
    runs = k["sync_runs"]
    return [
        ui.Kpi("Invoiced value (net, draft)", fmt.money(k["invoiced_value"], cur), _change_caption(k["invoiced_value"], p["invoiced_value"], "Draft invoices · not paid revenue", model.prior_complete)),
        ui.Kpi("Orders invoiced", fmt.count(k["orders_with_value"]), _change_caption(k["orders_with_value"], p["orders_with_value"], "Orders with a recorded value", model.prior_complete)),
        ui.Kpi("Average order value", "–", "No invoiced orders in this period")
        if k["orders_with_value"] == 0
        else ui.Kpi("Average order value", fmt.money(k["average_order_value"], cur), "Net, per invoiced order"),
        ui.Kpi("Draft invoices created", fmt.count(k["invoices_created"]), "In Zoho Books, by sync date"),
        ui.Kpi("Successful syncs", fmt.count(runs["success"]), f"of {fmt.count(runs['total'])} sync runs"),
        ui.Kpi("Failed syncs", fmt.count(runs["failed"]), f"{fmt.count(runs['partial'])} partial · {fmt.count(runs['skipped'])} with nothing to export"),
    ]


def _bucket_label(bucket: Dict, granularity: str, period_start: date) -> str:
    start = max(date.fromisoformat(bucket["period_start"]), period_start)
    if granularity == "day":
        return f"{start.strftime('%a')} {start.day}"
    if granularity == "week":
        return f"{start.day} {start.strftime('%b')}"
    return start.strftime("%b %Y")


def trend_rows(model: ReportModel) -> List[List[str]]:
    grain = model.current["filters"]["granularity"]
    rows = []
    for bucket in model.current["series"]:
        # The first week or month bucket may begin before the period: label only the days covered.
        start = max(date.fromisoformat(bucket["period_start"]), model.period.start)
        end = date.fromisoformat(bucket["period_end"])
        label = fmt.day_range(start, end) if grain != "day" else fmt.day_range(start, start)
        if not bucket["captured"]:
            rows.append([label, fmt.NOT_RECORDED, fmt.NOT_RECORDED])
        else:
            value = fmt.money(bucket["value"], model.currency) + (" (partial)" if bucket["partial"] else "")
            rows.append([label, fmt.count(bucket["orders"]), value])
    return rows


def render_pdf(model: ReportModel, *, compress: bool = True) -> bytes:
    period_text = f"{model.period.label}: {fmt.day_range(model.period.start, model.period.end)}"
    header = ui.ReportHeader(
        title=REPORT_TITLE,
        company_name=model.company_name,
        period_text=period_text,
        generated_text=fmt.generated_at(model.generated_at),
        footer_note=f"Confidential · {model.company_name} · Amounts in {model.currency}, net of VAT · Draft invoices, not paid revenue",
    )
    cur, k = model.currency, model.kpis
    story: List = []
    story += ui.title_block(
        REPORT_TITLE,
        f"{model.company_name} · {period_text} · compared with {fmt.day_range(model.previous.start, model.previous.end)}",
    )
    story.append(ui.notice(DRAFT_NOTICE))
    for warning in model.warnings:
        story += [ui.Spacer(1, 5), ui.notice(ui.text(warning), tone="warning")]

    story += ui.section("Summary")
    story.append(ui.kpi_grid(kpi_items(model)))

    story += ui.section("Sales trend (invoiced value by invoice date)")
    grain = model.current["filters"]["granularity"]
    points = [
        (_bucket_label(b, grain, model.period.start), None if not b["captured"] else Decimal(b["value"])) for b in model.current["series"]
    ]
    if any(value is not None for _label, value in points):
        story.append(ui.bar_chart(points, lambda v: fmt.money(v, cur).split(".")[0]))
        if any(value is None for _label, value in points):
            story.append(ui.Paragraph("Dashed bars marked n/r: no values were recorded for that period (not zero).", ui.STYLES["small"]))
        story.append(ui.Spacer(1, 6))
    story.append(ui.data_table(["Period", "Orders invoiced", "Invoiced value (net)"], trend_rows(model), [0.5, 0.2, 0.3], numeric_columns=(1, 2)))

    story += ui.section("Invoice summary")
    partial = "" if model.prior_complete or model.prior["kpis"]["invoiced_value"] is None else " (partial)"
    invoice_rows = [
        ["Draft invoices created in Zoho Books", fmt.count(k["invoices_created"]), fmt.count(model.prior["kpis"]["invoices_created"])],
        ["Orders exported", fmt.count(k["exported_orders"]), fmt.count(model.prior["kpis"]["exported_orders"])],
        ["Orders with a recorded invoice value", fmt.count(k["orders_with_value"]), fmt.count(model.prior["kpis"]["orders_with_value"]) + partial],
        ["Invoiced value, net of VAT (draft)", fmt.money(k["invoiced_value"], cur), fmt.money(model.prior["kpis"]["invoiced_value"], cur) + partial],
    ]
    story.append(ui.data_table(["", "This period", "Previous period"], invoice_rows, [0.56, 0.22, 0.22], numeric_columns=(1, 2)))
    story.append(ui.Paragraph(ui.text(model.current["coverage"]["count_basis"]), ui.STYLES["small"]))

    story += ui.section("Top customers")
    total = Decimal(k["invoiced_value"]) if k["invoiced_value"] is not None else None
    customer_rows = [
        [c["name"], fmt.count(c["orders"]), fmt.money(c["value"], cur), fmt.percent(c["value"], total)] for c in model.current["top_customers"]
    ]
    story.append(
        ui.data_table(["Customer", "Orders", "Invoiced value (net)", "Share"], customer_rows, [0.46, 0.14, 0.24, 0.16], numeric_columns=(1, 2, 3),
                      empty_text="No invoice values recorded for this period.")
    )

    story += ui.section("Top products")
    product_rows = [
        [p["name"], fmt.quantity(p["quantity"]), fmt.count(p["orders"]), fmt.money(p["value"], cur)] for p in model.current["top_products"]
    ]
    story.append(
        ui.data_table(["Product", "Quantity", "Orders", "Invoiced value (net)"], product_rows, [0.46, 0.16, 0.14, 0.24], numeric_columns=(1, 2, 3),
                      empty_text="No invoice lines recorded for this period. Delivery charges are excluded from products.")
    )

    runs, prior_runs = k["sync_runs"], model.prior["kpis"]["sync_runs"]
    empty_caption = " - No synchronisation ran in this period." if runs["total"] == 0 else ""
    story += ui.section("Synchronisation summary" + empty_caption)
    sync_rows = [
        ["Successful", fmt.count(runs["success"]), fmt.percent(runs["success"], runs["total"]), fmt.count(prior_runs["success"])],
        ["Partial (some orders failed)", fmt.count(runs["partial"]), fmt.percent(runs["partial"], runs["total"]), fmt.count(prior_runs["partial"])],
        ["Failed", fmt.count(runs["failed"]), fmt.percent(runs["failed"], runs["total"]), fmt.count(prior_runs["failed"])],
        ["Nothing to export", fmt.count(runs["skipped"]), fmt.percent(runs["skipped"], runs["total"]), fmt.count(prior_runs["skipped"])],
        ["Total sync runs", fmt.count(runs["total"]), "", fmt.count(prior_runs["total"])],
    ]
    if runs["running"]:
        sync_rows.insert(4, ["Still running at report time", fmt.count(runs["running"]), fmt.percent(runs["running"], runs["total"]), fmt.count(prior_runs["running"])])
    story.append(ui.data_table(["Outcome", "Runs", "Share", "Previous period"], sync_rows, [0.46, 0.16, 0.16, 0.22], numeric_columns=(1, 2, 3)))
    return ui.BrandedReport(header, compress=compress).build(story)


def report_filename(model: ReportModel) -> str:
    safe = "".join(ch if ch.isalnum() else "-" for ch in model.company_name.lower()).strip("-") or model.company_id
    while "--" in safe:
        safe = safe.replace("--", "-")
    return f"zconnect-report-{safe}-{model.period.start.isoformat()}-to-{model.period.end.isoformat()}.pdf"
