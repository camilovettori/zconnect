"""PDF reports: periods, Irish formatting, report calculations, PDF generation, missing data and
tenant isolation. Temporary in-memory databases only; nothing is sent anywhere."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.reports import formatting as fmt
from app.reports import weekly
from app.reports.periods import ReportPeriodError, irish_today, resolve_period
from tests.test_analytics import add_fact, add_run, client, db  # noqa: F401  (fixtures)

WEDNESDAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 13, 5, tzinfo=timezone.utc)
REPORT = "/api/reports/weekly.pdf"


# --- Periods ---------------------------------------------------------------------------------


def test_last_week_is_the_previous_monday_to_sunday():
    period = resolve_period("last_week", today=WEDNESDAY)
    assert (period.start, period.end, period.days) == (date(2026, 10, 12), date(2026, 10, 18), 7)
    previous = period.previous()
    assert (previous.start, previous.end) == (date(2026, 10, 5), date(2026, 10, 11))


def test_default_period_is_last_week_and_monday_reports_the_week_just_ended():
    period = resolve_period(None, today=date(2026, 10, 19))
    assert (period.key, period.start, period.end) == ("last_week", date(2026, 10, 12), date(2026, 10, 18))


def test_this_week_month_and_previous_month():
    assert resolve_period("this_week", today=WEDNESDAY).start == date(2026, 10, 19)
    assert resolve_period("this_month", today=WEDNESDAY).start == date(2026, 10, 1)
    last_month = resolve_period("last_month", today=date(2026, 3, 10))
    assert (last_month.start, last_month.end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert (last_month.previous().start, last_month.previous().end) == (date(2026, 1, 1), date(2026, 1, 31))


def test_custom_period_and_validation():
    period = resolve_period("custom", "2026-10-01", "2026-10-10", today=WEDNESDAY)
    assert period.days == 10 and period.previous().end == date(2026, 9, 30) and period.previous().days == 10
    for args in (("custom", "2026-10-10", "2026-10-01"), ("custom", "bad", "2026-10-01"), ("custom", "2025-01-01", "2026-10-01"), ("yearly", None, None)):
        with pytest.raises(ReportPeriodError):
            resolve_period(*args, today=WEDNESDAY)


def test_irish_today_uses_dublin_time():
    # 23:30 UTC on 21 Oct is 00:30 on 22 Oct in Ireland (IST, UTC+1).
    assert irish_today(datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc)) == date(2026, 10, 22)
    # After the clocks go back (GMT) the dates agree.
    assert irish_today(datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc)) == date(2026, 11, 2)


# --- Formatting ------------------------------------------------------------------------------


def test_irish_money_and_missing_values():
    assert fmt.money("1234.5") == "€1,234.50"
    assert fmt.money(Decimal("0.005")) == "€0.01"
    assert fmt.money("-12") == "-€12.00"
    assert fmt.money(None) == fmt.NOT_RECORDED
    assert fmt.money("0") == "€0.00"  # a recorded zero stays zero
    assert fmt.count(None) == fmt.NOT_RECORDED and fmt.count(1234) == "1,234"


def test_change_and_percent():
    assert fmt.change("110", "100") == "+10.0% vs previous period"
    assert fmt.change("90", "100") == "-10.0% vs previous period"
    assert fmt.change("100", "100") == "No change vs previous period"
    assert fmt.change("90", None) is None and fmt.change("90", "0") is None
    assert fmt.percent("25", "200") == "12.5%" and fmt.percent("1", None) == "–"


def test_dates_and_generation_time():
    assert fmt.day(date(2026, 10, 13)) == "13 Oct 2026"
    assert fmt.day_range(date(2026, 10, 12), date(2026, 10, 18)) == "Mon 12 Oct – Sun 18 Oct 2026"
    assert fmt.generated_at(NOW) == "21 Oct 2026, 14:05 Irish time"
    assert fmt.quantity("2.500") == "2.5" and fmt.quantity("3") == "3"


# --- Report calculations ---------------------------------------------------------------------


def seed(db):
    """Acme: two captured orders last week, one the week before, two runs. Other: a big order."""
    add_fact(db, "acme", "A1", date(2026, 10, 13), customer="B1", lines=(("zoho:1", "Sourdough", "2", "10.00"), ("zoho:2", "Rye", "1", "5.50")))
    add_fact(db, "acme", "A2", date(2026, 10, 15), customer="B2", lines=(("zoho:1", "Sourdough", "3", "10.00"),), captured_on=date(2026, 10, 5))
    add_fact(db, "acme", "A0", date(2026, 10, 7), customer="B1", lines=(("zoho:1", "Sourdough", "1", "20.00"),))
    add_fact(db, "other", "O1", date(2026, 10, 14), customer="SECRETCUST", lines=(("zoho:9", "Secret loaf", "100", "99.00"),))
    add_run(db, "acme", "success", datetime(2026, 10, 13, 9), invoices=(("A1", "B1"),))
    add_run(db, "acme", "failed", datetime(2026, 10, 16, 9))
    add_run(db, "other", "success", datetime(2026, 10, 14, 9))


def test_report_model_totals_comparison_and_rankings(db):
    seed(db)
    model = weekly.build_report_model(db, "acme", resolve_period("last_week", today=WEDNESDAY), now=NOW)
    k = model.kpis
    assert model.company_name == "Acme Bakery" and model.currency == "EUR"
    assert k["invoiced_value"] == "55.50" and k["orders_with_value"] == 2 and k["average_order_value"] == "27.75"
    assert k["invoices_created"] == 1
    assert k["sync_runs"]["success"] == 1 and k["sync_runs"]["failed"] == 1 and k["sync_runs"]["total"] == 2
    assert model.prior["kpis"]["invoiced_value"] == "20.00"
    assert [c["key"] for c in model.current["top_customers"]] == ["B2", "B1"]
    assert [p["name"] for p in model.current["top_products"]] == ["Sourdough", "Rye"]
    assert model.current["top_products"][0]["value"] == "50.00"
    items = {item.label: item for item in weekly.kpi_items(model)}
    assert items["Net invoiced value"].value == "€55.50"
    assert items["Net invoiced value"].caption == "+177.5% vs previous period"
    assert items["Active customers"].value == "2"
    assert model.warnings == []
    assert len(model.current["series"]) == 7 and model.current["filters"]["granularity"] == "day"


def test_report_never_includes_another_companys_data(db):
    seed(db)
    model = weekly.build_report_model(db, "acme", resolve_period("last_week", today=WEDNESDAY), now=NOW)
    names = [c["name"] for c in model.current["top_customers"]] + [p["name"] for p in model.current["top_products"]]
    assert "Customer SECRETCUST" not in names and "Secret loaf" not in names
    other = weekly.build_report_model(db, "other", resolve_period("last_week", today=WEDNESDAY), now=NOW)
    assert other.kpis["invoiced_value"] == "9900.00" and other.kpis["sync_runs"]["total"] == 1
    assert [c["key"] for c in other.current["top_customers"]] == ["SECRETCUST"]


def test_missing_history_is_not_recorded_rather_than_zero(db):
    add_run(db, "acme", "success", datetime(2026, 10, 13, 9))
    model = weekly.build_report_model(db, "acme", resolve_period("last_week", today=WEDNESDAY), now=NOW)
    assert model.kpis["invoiced_value"] is None and model.kpis["orders_with_value"] is None
    assert all(bucket["value"] is None for bucket in model.current["series"])
    assert any("No invoice values have been recorded" in warning for warning in model.warnings)
    items = {item.label: item for item in weekly.kpi_items(model)}
    assert items["Net invoiced value"].value == fmt.NOT_RECORDED
    assert items["Active customers"].value == "0"  # no invoiced customers in this period
    assert all(row[2] == fmt.NOT_RECORDED for row in weekly.trend_rows(model))


def test_capture_starting_mid_period_marks_earlier_days(db):
    add_fact(db, "acme", "A1", date(2026, 10, 16), lines=(("zoho:1", "Sourdough", "1", "8.00"),))
    model = weekly.build_report_model(db, "acme", resolve_period("last_week", today=WEDNESDAY), now=NOW)
    assert model.kpis["invoiced_value"] == "8.00"
    assert any("recorded from 16 Oct 2026" in warning for warning in model.warnings)
    values = [bucket["value"] for bucket in model.current["series"]]
    assert values[:4] == [None] * 4 and values[4] == "8.00" and values[5:] == ["0.00", "0.00"]


def test_no_percentage_change_against_a_partly_recorded_previous_period(db):
    # Capture started on Thu 8 Oct, during the previous week: comparing would overstate growth.
    add_fact(db, "acme", "A0", date(2026, 10, 8), lines=(("zoho:1", "Sourdough", "1", "10.00"),))
    add_fact(db, "acme", "A1", date(2026, 10, 13), lines=(("zoho:1", "Sourdough", "5", "10.00"),))
    model = weekly.build_report_model(db, "acme", resolve_period("last_week", today=WEDNESDAY), now=NOW)
    assert not model.prior_complete and model.prior["kpis"]["invoiced_value"] == "10.00"
    items = {item.label: item for item in weekly.kpi_items(model)}
    assert items["Net invoiced value"].caption == "Draft invoices · not paid revenue"
    assert rb"\(partial\)" in weekly.render_pdf(model, compress=False)  # PDF escapes parentheses


def test_recorded_quiet_week_is_zero_not_missing(db):
    add_fact(db, "acme", "A0", date(2026, 10, 1))
    model = weekly.build_report_model(db, "acme", resolve_period("last_week", today=WEDNESDAY), now=NOW)
    assert model.kpis["invoiced_value"] == "0.00" and model.kpis["orders_with_value"] == 0
    average = {item.label: item for item in weekly.kpi_items(model)}["Average order value"]
    assert (average.value, average.caption) == ("–", "No invoiced orders in this period")
    assert model.warnings == []


# --- PDF generation --------------------------------------------------------------------------


def test_pdf_is_valid_and_contains_the_required_sections(db):
    seed(db)
    model = weekly.build_report_model(db, "acme", resolve_period("last_week", today=WEDNESDAY), now=NOW)
    pdf = weekly.render_pdf(model, compress=False)
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    for expected in (
        b"Acme Bakery",
        b"Business performance report",
        b"Mon 12 Oct",
        b"Sun 18 Oct 2026",
        b"Generated 21 Oct 2026, 14:05 Irish time",
        b"Summary",
        b"Orders and invoices",
        b"Top customers",
        b"Top products",
        b"Sales trend",
        b"Best-selling products by quantity",
        b"Customer B2",
        b"Sourdough",
        b"invoices",
        b"not paid revenue",
    ):
        assert expected in pdf, expected
    assert b"SECRETCUST" not in pdf and b"Secret loaf" not in pdf
    compressed = weekly.render_pdf(model)
    assert compressed.startswith(b"%PDF-") and len(compressed) < len(pdf)


def test_pdf_renders_for_a_company_with_no_data_at_all(db):
    model = weekly.build_report_model(db, "acme", resolve_period("this_month", today=WEDNESDAY), now=NOW)
    pdf = weekly.render_pdf(model, compress=False)
    assert pdf.startswith(b"%PDF-")
    assert b"No invoice values recorded for this period." in pdf
    assert b"No synchronisation ran in this period." in pdf


def test_long_period_and_awkward_names_render(db):
    for index in range(15):
        add_fact(db, "acme", f"L{index}", date(2026, 1 + index % 9, 3), customer=f"C{index}", lines=((f"zoho:{index}", f"Bread <&> ‘special’ #{index}", "1", "12.34"),))
    model = weekly.build_report_model(db, "acme", resolve_period("custom", "2026-01-01", "2026-10-18", today=WEDNESDAY), now=NOW)
    assert model.current["filters"]["granularity"] == "week" or model.current["filters"]["granularity"] == "month"
    assert len(model.current["top_customers"]) == 10
    assert weekly.trend_rows(model)[0][0].startswith("Thu 1 Jan")  # first bucket clipped to the period
    assert weekly.render_pdf(model).startswith(b"%PDF-")


def test_filename_is_safe():
    class Model:
        company_name = 'Acme "Bakery" / Dublin'
        company_id = "acme"
        period = resolve_period("last_week", today=WEDNESDAY)

    assert weekly.report_filename(Model) == "zconnect-report-acme-bakery-dublin-2026-10-12-to-2026-10-18.pdf"


# --- Endpoint: authentication, authorization, tenant isolation -------------------------------


def download(client, sign_request, query="", company="acme", role="user"):
    return client.get(REPORT + query, headers=sign_request("GET", REPORT, query=query, role=role, company=company))


def test_download_returns_an_attachment_for_the_signed_company(client, sign_request, db):
    seed(db)
    query = "?period=custom&date_from=2026-10-12&date_to=2026-10-18"
    response = download(client, sign_request, query)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="zconnect-report-acme-bakery-2026-10-12-to-2026-10-18.pdf"'
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"%PDF-")


def test_other_company_report_is_forbidden_without_platform_role(client, sign_request, db):
    seed(db)
    for role in ("user", "admin"):
        assert download(client, sign_request, "?company_id=other", role=role).status_code == 403
    assert download(client, sign_request, "?company_id=acme", role="user").status_code == 200


def test_platform_admin_can_download_another_companys_report(client, sign_request, db):
    seed(db)
    response = download(client, sign_request, "?company_id=other", role="platform_admin")
    assert response.status_code == 200 and response.content.startswith(b"%PDF-")
    assert "other-ltd" in response.headers["content-disposition"]


def test_report_requires_a_signed_identity(client):
    assert client.get(REPORT).status_code == 401


def test_invalid_period_is_a_400(client, sign_request, db):
    assert download(client, sign_request, "?period=custom&date_from=2026-10-10&date_to=2026-10-01").status_code == 400
    assert download(client, sign_request, "?period=fortnight").status_code == 400
