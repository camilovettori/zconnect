"use client";

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { AppShell } from "../../components/app-shell";
import { Notice } from "../../components/ui/notice";
import { PageHeader } from "../../components/ui/page-header";
import { BUTTON_PRIMARY, CARD, INPUT, LABEL } from "../../components/ui/styles";

// Amounts arrive as decimal strings; null means "not recorded" and is never shown as zero.
type Bucket = { period_start: string; period_end: string; value: string | null; orders: number | null; captured: boolean; partial: boolean };
type Ranked = { key: string; name: string; value: string; orders: number; quantity?: string };
type Summary = {
  filters: { company_id: string; date_from: string; date_to: string; granularity: Granularity; customer: string | null; product: string | null };
  currency: string | null;
  value_basis: string;
  kpis: {
    invoiced_value: string | null;
    orders_with_value: number | null;
    average_order_value: string | null;
    invoices_created: number;
    exported_orders: number | null;
    sync_runs: { success: number; partial: number; failed: number; skipped: number; running: number; total: number };
  };
  coverage: {
    value_capture_started: string | null;
    range_starts_before_capture: boolean;
    exported_orders_without_value: number | null;
    count_basis: string;
    sync_runs_ignore_customer_and_product_filters: boolean;
  };
  series: Bucket[];
  top_customers: Ranked[];
  top_products: Ranked[];
};
type Option = { key: string; name: string };
type Companies = { companies: Array<{ id: string; name: string }>; can_switch: boolean; current: string };
type Granularity = "day" | "week" | "month";
type Preset = "last_7" | "last_30" | "last_90" | "this_month" | "last_month" | "this_year" | "custom";
type ReportPeriod = "last_week" | "this_week" | "last_month" | "this_month" | "custom";

const PRESETS: Array<{ value: Preset; label: string }> = [
  { value: "last_7", label: "Last 7 days" },
  { value: "last_30", label: "Last 30 days" },
  { value: "last_90", label: "Last 90 days" },
  { value: "this_month", label: "This month" },
  { value: "last_month", label: "Last month" },
  { value: "this_year", label: "This year" },
  { value: "custom", label: "Custom range" },
];

const REPORT_PERIODS: Array<{ value: ReportPeriod; label: string }> = [
  { value: "last_week", label: "Last week (Mon–Sun)" },
  { value: "this_week", label: "This week to date" },
  { value: "last_month", label: "Last month" },
  { value: "this_month", label: "This month to date" },
  { value: "custom", label: "Selected date range" },
];

const NOT_RECORDED = "Not recorded";

function isoDate(value: Date) {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`;
}

function presetRange(preset: Preset, today = new Date()): { from: string; to: string } | null {
  const day = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const minus = (days: number) => new Date(day.getFullYear(), day.getMonth(), day.getDate() - days);
  switch (preset) {
    case "last_7":
      return { from: isoDate(minus(6)), to: isoDate(day) };
    case "last_30":
      return { from: isoDate(minus(29)), to: isoDate(day) };
    case "last_90":
      return { from: isoDate(minus(89)), to: isoDate(day) };
    case "this_month":
      return { from: isoDate(new Date(day.getFullYear(), day.getMonth(), 1)), to: isoDate(day) };
    case "last_month":
      return { from: isoDate(new Date(day.getFullYear(), day.getMonth() - 1, 1)), to: isoDate(new Date(day.getFullYear(), day.getMonth(), 0)) };
    case "this_year":
      return { from: isoDate(new Date(day.getFullYear(), 0, 1)), to: isoDate(day) };
    default:
      return null;
  }
}

function parseDay(value: string) {
  const [y, m, d] = value.split("-").map(Number);
  return new Date(y, m - 1, d);
}

const DAY_FORMAT = new Intl.DateTimeFormat("en-IE", { day: "numeric", month: "short" });
const DAY_YEAR_FORMAT = new Intl.DateTimeFormat("en-IE", { day: "numeric", month: "short", year: "numeric" });
const MONTH_FORMAT = new Intl.DateTimeFormat("en-IE", { month: "short", year: "numeric" });

function formatDay(value: string, withYear = true) {
  return (withYear ? DAY_YEAR_FORMAT : DAY_FORMAT).format(parseDay(value));
}

function bucketLabel(bucket: Bucket, granularity: Granularity) {
  if (granularity === "month") return MONTH_FORMAT.format(parseDay(bucket.period_start));
  if (granularity === "week") return `w/c ${formatDay(bucket.period_start, false)}`;
  return formatDay(bucket.period_start, false);
}

function money(value: string | number | null | undefined, currency: string | null, compact = false) {
  if (value === null || value === undefined) return NOT_RECORDED;
  const code = currency && currency !== "MIXED" ? currency : "EUR";
  return new Intl.NumberFormat("en-IE", {
    style: "currency",
    currency: code,
    ...(compact ? { notation: "compact", maximumFractionDigits: 1 } : { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  }).format(Number(value));
}

function count(value: number | null | undefined) {
  return value === null || value === undefined ? NOT_RECORDED : new Intl.NumberFormat("en-IE").format(value);
}

async function readError(response: Response) {
  const payload = await response.json().catch(() => null);
  const detail = payload && typeof payload === "object" ? (payload as { detail?: unknown; error?: unknown }).detail ?? (payload as { error?: unknown }).error : null;
  return typeof detail === "string" ? detail : `Request failed (${response.status})`;
}

export default function AnalyticsPage() {
  const [preset, setPreset] = useState<Preset>("last_30");
  const initial = presetRange("last_30")!;
  const [dateFrom, setDateFrom] = useState(initial.from);
  const [dateTo, setDateTo] = useState(initial.to);
  const [granularity, setGranularity] = useState<"auto" | Granularity>("auto");
  const [customer, setCustomer] = useState("");
  const [product, setProduct] = useState("");
  const [companyId, setCompanyId] = useState("");
  const [companies, setCompanies] = useState<Companies | null>(null);
  const [options, setOptions] = useState<{ customers: Option[]; products: Option[] }>({ customers: [], products: [] });
  const [summary, setSummary] = useState<Summary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reportPeriod, setReportPeriod] = useState<ReportPeriod>("last_week");
  const [downloading, setDownloading] = useState(false);
  const [reportNotice, setReportNotice] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const companyQuery = companies?.can_switch && companyId ? `company_id=${encodeURIComponent(companyId)}` : "";

  useEffect(() => {
    let active = true;
    fetch("/api/analytics/companies", { cache: "no-store" })
      .then(async (response) => (response.ok ? ((await response.json()) as Companies) : null))
      .then((payload) => {
        if (active && payload) {
          setCompanies(payload);
          setCompanyId(payload.current);
        }
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    fetch(`/api/analytics/filters${companyQuery ? `?${companyQuery}` : ""}`, { cache: "no-store" })
      .then(async (response) => (response.ok ? ((await response.json()) as { customers: Option[]; products: Option[] }) : null))
      .then((payload) => {
        if (active && payload) setOptions(payload);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [companyQuery]);

  const rangeError = dateFrom && dateTo && dateFrom > dateTo ? "The start date must be on or before the end date." : null;

  useEffect(() => {
    if (!dateFrom || !dateTo || rangeError) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ date_from: dateFrom, date_to: dateTo });
    if (granularity !== "auto") params.set("granularity", granularity);
    if (customer) params.set("customer", customer);
    if (product) params.set("product", product);
    if (companyQuery) params.set("company_id", companyId);
    setLoading(true);
    setError(null);
    fetch(`/api/analytics/summary?${params.toString()}`, { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(await readError(response));
        setSummary((await response.json()) as Summary);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setSummary(null);
        setError(err instanceof Error ? err.message : "Analytics could not be loaded.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [dateFrom, dateTo, granularity, customer, product, companyId, companyQuery, rangeError]);

  const choosePreset = (value: Preset) => {
    setPreset(value);
    const range = presetRange(value);
    if (range) {
      setDateFrom(range.from);
      setDateTo(range.to);
    }
  };

  const chooseCompany = (value: string) => {
    setCompanyId(value);
    setCustomer("");
    setProduct("");
  };

  const downloadReport = async () => {
    setDownloading(true);
    setReportNotice(null);
    const params = new URLSearchParams({ period: reportPeriod });
    if (reportPeriod === "custom") {
      params.set("date_from", dateFrom);
      params.set("date_to", dateTo);
    }
    if (companyQuery) params.set("company_id", companyId);
    try {
      const response = await fetch(`/api/reports/weekly.pdf?${params.toString()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(await readError(response));
      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") || "";
      const filename = /filename="([^"]+)"/.exec(disposition)?.[1] || "zconnect-report.pdf";
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      setReportNotice({ kind: "success", text: `Report downloaded: ${filename}` });
    } catch (err) {
      setReportNotice({ kind: "error", text: err instanceof Error ? err.message : "The report could not be generated." });
    } finally {
      setDownloading(false);
    }
  };

  const currency = summary?.currency ?? null;
  const k = summary?.kpis;
  const coverage = summary?.coverage;
  const companyName = companies?.companies.find((c) => c.id === companyId)?.name;

  return (
    <AppShell>
      <main className="space-y-6">
        <PageHeader
          eyebrow="Analytics"
          title="Sales & invoicing analytics"
          description={
            <>
              Invoiced values are the net, pre-tax totals of the <strong className="font-semibold text-slate-800">draft invoices</strong> Zconnect created in
              Zoho Books — not paid revenue. Sync counts show activity and are never money.
              {companyName && <span className="text-slate-500"> Showing {companyName}.</span>}
            </>
          }
        />

        <section aria-label="Filters" className={`${CARD} p-4 sm:p-5`}>
          <div role="group" aria-label="Date range" className="flex flex-wrap gap-1.5">
            {PRESETS.map((item) => {
              const active = preset === item.value;
              return (
                <button
                  key={item.value}
                  type="button"
                  aria-pressed={active}
                  onClick={() => choosePreset(item.value)}
                  className={[
                    "rounded-full border px-3 py-1 text-xs font-medium transition-colors duration-150 motion-reduce:transition-none",
                    active ? "border-slate-900 bg-slate-900 text-white" : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:text-slate-900",
                  ].join(" ")}
                >
                  {item.label}
                </button>
              );
            })}
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
            <label className="block">
              <span className={LABEL}>From</span>
              <input
                type="date"
                className={INPUT}
                value={dateFrom}
                max={dateTo || undefined}
                onChange={(event) => {
                  setPreset("custom");
                  setDateFrom(event.target.value);
                }}
              />
            </label>
            <label className="block">
              <span className={LABEL}>To</span>
              <input
                type="date"
                className={INPUT}
                value={dateTo}
                min={dateFrom || undefined}
                onChange={(event) => {
                  setPreset("custom");
                  setDateTo(event.target.value);
                }}
              />
            </label>
            <label className="block">
              <span className={LABEL}>Group by</span>
              <select className={INPUT} value={granularity} onChange={(event) => setGranularity(event.target.value as "auto" | Granularity)}>
                <option value="auto">Automatic</option>
                <option value="day">Day</option>
                <option value="week">Week (Mon–Sun)</option>
                <option value="month">Month</option>
              </select>
            </label>
            <label className="block">
              <span className={LABEL}>Customer</span>
              <select className={INPUT} value={customer} onChange={(event) => setCustomer(event.target.value)}>
                <option value="">All customers</option>
                {options.customers.map((option) => (
                  <option key={option.key} value={option.key}>
                    {option.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className={LABEL}>Product</span>
              <select className={INPUT} value={product} onChange={(event) => setProduct(event.target.value)}>
                <option value="">All products</option>
                {options.products.map((option) => (
                  <option key={option.key} value={option.key}>
                    {option.name}
                  </option>
                ))}
              </select>
            </label>
            {companies?.can_switch ? (
              <label className="block">
                <span className={LABEL}>Company</span>
                <select className={INPUT} value={companyId} onChange={(event) => chooseCompany(event.target.value)}>
                  {companies.companies.map((company) => (
                    <option key={company.id} value={company.id}>
                      {company.name}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
          </div>
        </section>

        {rangeError && <Notice kind="error">{rangeError}</Notice>}
        {error && <Notice kind="error" title="Analytics could not be loaded">{error}</Notice>}

        {coverage && <CoverageNotice summary={summary!} />}

        <section aria-label="Key figures" aria-busy={loading} className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          <KpiCard label="Invoiced value (net, draft)" loading={loading && !summary} value={k ? money(k.invoiced_value, currency) : "–"} caption="Draft invoices · not paid revenue" />
          <KpiCard label="Orders invoiced" loading={loading && !summary} value={k ? count(k.orders_with_value) : "–"} caption="Orders with a recorded value" />
          <KpiCard
            label="Average order value"
            loading={loading && !summary}
            value={k ? (k.orders_with_value === 0 ? "–" : money(k.average_order_value, currency)) : "–"}
            caption={k?.orders_with_value === 0 ? "No invoiced orders in this range" : "Net, per invoiced order"}
          />
          <KpiCard
            label="Draft invoices created"
            loading={loading && !summary}
            value={k ? count(k.invoices_created) : "–"}
            caption={product ? "Captured orders with this product" : "In Zoho Books, by sync date"}
          />
          <KpiCard
            label="Successful syncs"
            loading={loading && !summary}
            value={k ? count(k.sync_runs.success) : "–"}
            caption={k ? `of ${count(k.sync_runs.total)} sync runs` : ""}
            tone="success"
          />
          <KpiCard
            label="Failed syncs"
            loading={loading && !summary}
            value={k ? count(k.sync_runs.failed) : "–"}
            caption={k ? `${count(k.sync_runs.partial)} partial · ${count(k.sync_runs.skipped)} with nothing to export` : ""}
            tone={k && k.sync_runs.failed > 0 ? "danger" : undefined}
          />
        </section>

        {summary && (
          <>
            <div className="grid gap-6 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
              <Panel title="Invoiced value" subtitle={`Net value by invoice date, per ${summary.filters.granularity}.`}>
                <ValueChart summary={summary} />
              </Panel>
              <Panel title="Cumulative trend" subtitle="Running total of invoiced value over the selected range.">
                <TrendChart summary={summary} />
              </Panel>
            </div>
            <div className="grid gap-6 lg:grid-cols-2">
              <Panel title="Top customers" subtitle="By invoiced value (net).">
                <RankedBars items={summary.top_customers} currency={currency} detail={(item) => `${count(item.orders)} order${item.orders === 1 ? "" : "s"}`} empty="No invoice values recorded for this range." />
              </Panel>
              <Panel title="Top products" subtitle="By invoiced value (net). Delivery charges excluded.">
                <RankedBars
                  items={summary.top_products}
                  currency={currency}
                  detail={(item) => `Qty ${Number(item.quantity ?? 0).toLocaleString("en-IE")} · ${count(item.orders)} order${item.orders === 1 ? "" : "s"}`}
                  empty="No invoice lines recorded for this range."
                />
              </Panel>
            </div>
            <Panel
              title="Sync outcomes"
              subtitle={
                summary.coverage.sync_runs_ignore_customer_and_product_filters
                  ? "Sync runs cover all customers and products, so customer and product filters do not apply here."
                  : "Sync runs started in the selected range. Activity counts, not money."
              }
            >
              <SyncOutcomes runs={summary.kpis.sync_runs} />
            </Panel>
          </>
        )}

        <section aria-labelledby="report-heading" className={`${CARD} p-4 sm:p-5`}>
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
            <div className="max-w-xl">
              <h2 id="report-heading" className="text-base font-semibold tracking-tight text-slate-950">
                PDF report
              </h2>
              <p className="mt-1 text-sm leading-6 text-slate-600">
                A branded report with the sales summary, invoices, top customers and products, trend and sync outcomes for {companyName || "your company"},
                compared with the previous period. Weeks run Monday to Sunday, Irish time.
              </p>
            </div>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
              <label className="block sm:w-60">
                <span className={LABEL}>Report period</span>
                <select className={INPUT} value={reportPeriod} onChange={(event) => setReportPeriod(event.target.value as ReportPeriod)}>
                  {REPORT_PERIODS.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>
              <button type="button" className={BUTTON_PRIMARY} onClick={downloadReport} disabled={downloading || (reportPeriod === "custom" && Boolean(rangeError))}>
                <DownloadIcon />
                {downloading ? "Preparing report…" : "Download PDF"}
              </button>
            </div>
          </div>
          {reportPeriod === "custom" && (
            <p className="mt-3 text-xs text-slate-500">
              Uses the dashboard range: {formatDay(dateFrom)} – {formatDay(dateTo)} (at most one year).
            </p>
          )}
          {reportNotice && (
            <Notice kind={reportNotice.kind} onDismiss={() => setReportNotice(null)} className="mt-4">
              {reportNotice.text}
            </Notice>
          )}
        </section>
      </main>
    </AppShell>
  );
}

function CoverageNotice({ summary }: { summary: Summary }) {
  const { coverage, currency } = summary;
  if (currency === "MIXED") {
    return <Notice kind="warning">Captured invoices use more than one currency, so values cannot be totalled for this range.</Notice>;
  }
  if (!coverage.value_capture_started) {
    return (
      <Notice kind="info" title="No invoice values recorded yet">
        Zconnect records invoice values from the first sync after Analytics was enabled. Earlier syncs have no stored amounts, so values show as “Not recorded”,
        never as zero. Sync activity below is complete.
      </Notice>
    );
  }
  if (coverage.range_starts_before_capture || coverage.exported_orders_without_value) {
    return (
      <Notice kind="info">
        Invoice values are recorded from {formatDay(coverage.value_capture_started)}. Earlier periods show as “Not recorded”, not zero.
        {coverage.exported_orders_without_value ? ` ${count(coverage.exported_orders_without_value)} exported order(s) in this range have no recorded value.` : ""}
      </Notice>
    );
  }
  return null;
}

function KpiCard({ label, value, caption, loading, tone }: { label: string; value: string; caption: string; loading: boolean; tone?: "success" | "danger" }) {
  const missing = value === NOT_RECORDED;
  return (
    <div className={`${CARD} p-4 sm:p-5`}>
      <p className="text-xs font-semibold uppercase tracking-[0.08em] text-slate-500">{label}</p>
      {loading ? (
        <div className="mt-3 h-8 w-32 animate-pulse rounded-md bg-slate-100 motion-reduce:animate-none" aria-hidden="true" />
      ) : (
        <p
          className={[
            "mt-2 tabular-nums tracking-tight",
            missing ? "text-lg font-medium text-slate-400" : "text-[1.75rem] font-semibold leading-9",
            !missing && tone === "success" ? "text-emerald-700" : !missing && tone === "danger" ? "text-rose-700" : !missing ? "text-slate-950" : "",
          ].join(" ")}
        >
          {value}
        </p>
      )}
      <p className="mt-1 text-xs text-slate-500">{caption}</p>
    </div>
  );
}

function Panel({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  return (
    <section className={`${CARD} min-w-0 p-4 sm:p-5`}>
      <h2 className="text-base font-semibold tracking-tight text-slate-950">{title}</h2>
      {subtitle && <p className="mt-0.5 text-xs leading-5 text-slate-500">{subtitle}</p>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

function TableFallback({ caption, headers, rows }: { caption: string; headers: string[]; rows: string[][] }) {
  return (
    <details className="mt-3 text-sm">
      <summary className="cursor-pointer text-xs font-medium text-brand-700 hover:text-brand-800">Show as table</summary>
      <div className="mt-2 max-h-72 overflow-auto rounded-lg border border-slate-200">
        <table className="w-full text-left text-xs">
          <caption className="sr-only">{caption}</caption>
          <thead className="sticky top-0 bg-slate-50 text-slate-500">
            <tr>
              {headers.map((header, index) => (
                <th key={header} scope="col" className={`px-3 py-2 font-semibold ${index > 0 ? "text-right" : ""}`}>
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 text-slate-700">
            {rows.map((row) => (
              <tr key={row[0]}>
                {row.map((cell, index) => (
                  <td key={index} className={`px-3 py-1.5 ${index > 0 ? "text-right tabular-nums" : ""} ${cell === NOT_RECORDED ? "text-slate-400" : ""}`}>
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function bucketRange(bucket: Bucket, summary: Summary) {
  const start = bucket.period_start < summary.filters.date_from ? summary.filters.date_from : bucket.period_start;
  return start === bucket.period_end ? formatDay(start) : `${formatDay(start, false)} – ${formatDay(bucket.period_end)}`;
}

const CHART_HEIGHT = 200;

function ValueChart({ summary }: { summary: Summary }) {
  const { series, currency } = summary;
  const known = series.filter((b) => b.value !== null).map((b) => Number(b.value));
  const peak = Math.max(...known, 0) || 1;
  const width = Math.max(series.length * 28, 320);
  const slot = width / Math.max(series.length, 1);
  const bar = Math.min(slot * 0.66, 34);
  const labelEvery = Math.max(1, Math.ceil(series.length / 10));
  const [hover, setHover] = useState<number | null>(null);

  if (known.length === 0) {
    return <EmptyChart text="No invoice values recorded for this range." />;
  }

  return (
    <div>
      <div className="relative overflow-x-auto">
        <svg viewBox={`0 0 ${width} ${CHART_HEIGHT}`} className="h-[200px] w-full min-w-[320px]" role="img" aria-label={`Invoiced value per ${summary.filters.granularity}. Peak ${money(peak, currency)}.`} preserveAspectRatio="none">
          {[0.25, 0.5, 0.75, 1].map((ratio) => (
            <line key={ratio} x1={0} x2={width} y1={CHART_HEIGHT - CHART_HEIGHT * ratio * 0.9} y2={CHART_HEIGHT - CHART_HEIGHT * ratio * 0.9} stroke="#f1f5f9" strokeWidth={1} />
          ))}
          <line x1={0} x2={width} y1={CHART_HEIGHT} y2={CHART_HEIGHT} stroke="#e2e8f0" strokeWidth={1} />
          {series.map((bucket, index) => {
            const x = slot * index + (slot - bar) / 2;
            if (bucket.value === null) {
              return (
                <g key={bucket.period_start}>
                  <rect x={x} y={CHART_HEIGHT - 36} width={bar} height={36} fill="none" stroke="#cbd5e1" strokeDasharray="3 3" rx={3} />
                  <title>{`${bucketRange(bucket, summary)}: not recorded`}</title>
                </g>
              );
            }
            const height = Math.max((Number(bucket.value) / peak) * CHART_HEIGHT * 0.9, Number(bucket.value) > 0 ? 2 : 1);
            return (
              <g key={bucket.period_start} onMouseEnter={() => setHover(index)} onMouseLeave={() => setHover(null)}>
                <rect x={slot * index} y={0} width={slot} height={CHART_HEIGHT} fill="transparent" />
                <rect x={x} y={CHART_HEIGHT - height} width={bar} height={height} rx={3} fill={hover === index ? "#1854d8" : bucket.partial ? "#93c5fd" : "#1f6aeb"} />
                <title>{`${bucketRange(bucket, summary)}: ${money(bucket.value, currency)} from ${count(bucket.orders)} orders${bucket.partial ? " (partly recorded)" : ""}`}</title>
              </g>
            );
          })}
        </svg>
        {/* Axis labels in HTML so the stretched SVG never distorts text */}
        <div className="grid min-w-[320px] text-center text-[10px] text-slate-500" style={{ gridTemplateColumns: `repeat(${series.length}, minmax(0, 1fr))` }} aria-hidden="true">
          {series.map((bucket, index) => (
            <span key={bucket.period_start} className="overflow-visible whitespace-nowrap pt-1.5">
              {index % labelEvery === 0 ? bucketLabel(bucket, summary.filters.granularity) : ""}
            </span>
          ))}
        </div>
        {hover !== null && series[hover] && (
          <div className="pointer-events-none absolute left-2 top-2 rounded-lg border border-slate-200 bg-white/95 px-3 py-2 text-xs shadow-card">
            <p className="font-medium text-slate-900">{bucketRange(series[hover], summary)}</p>
            <p className="tabular-nums text-slate-600">
              {money(series[hover].value, currency)} · {count(series[hover].orders)} orders
            </p>
          </div>
        )}
      </div>
      <ChartLegend
        items={[
          { swatch: "bg-brand-600", label: "Invoiced value" },
          ...(series.some((b) => b.partial) ? [{ swatch: "bg-blue-300", label: "Partly recorded" }] : []),
          ...(series.some((b) => b.value === null) ? [{ swatch: "border border-dashed border-slate-300 bg-white", label: "Not recorded" }] : []),
        ]}
      />
      <TableFallback
        caption="Invoiced value per period"
        headers={["Period", "Orders", "Invoiced value (net)"]}
        rows={series.map((b) => [bucketRange(b, summary), count(b.orders), money(b.value, currency) + (b.partial ? " (partial)" : "")])}
      />
    </div>
  );
}

function TrendChart({ summary }: { summary: Summary }) {
  const { series, currency } = summary;
  const points = useMemo(() => {
    let running = 0;
    return series.map((bucket) => {
      if (bucket.value === null) return null;
      running += Number(bucket.value);
      return running;
    });
  }, [series]);
  const known = points.filter((p): p is number => p !== null);
  if (known.length === 0) {
    return <EmptyChart text="No invoice values recorded for this range." />;
  }
  const width = 320;
  const peak = Math.max(...known) || 1;
  const step = series.length > 1 ? width / (series.length - 1) : width;
  const coords = points.map((p, i) => (p === null ? null : [i * step, CHART_HEIGHT - (p / peak) * CHART_HEIGHT * 0.88] as const));
  const firstKnown = coords.findIndex((c) => c !== null);
  const path = coords
    .filter((c): c is readonly [number, number] => c !== null)
    .map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`)
    .join(" ");
  const knownCoords = coords.filter((c): c is readonly [number, number] => c !== null);
  const last = knownCoords[knownCoords.length - 1];
  const area = `${path} L${last[0].toFixed(1)},${CHART_HEIGHT} L${(coords[firstKnown]![0]).toFixed(1)},${CHART_HEIGHT} Z`;
  const first = series[0];
  const end = series[series.length - 1];
  return (
    <div>
      <p className="text-2xl font-semibold tabular-nums tracking-tight text-slate-950">{money(known[known.length - 1], currency)}</p>
      <p className="text-xs text-slate-500">
        Total {firstKnown > 0 ? `since ${formatDay(series[firstKnown].period_start)}` : "for the range"}
      </p>
      <svg viewBox={`0 0 ${width} ${CHART_HEIGHT + 4}`} className="mt-3 h-40 w-full" role="img" aria-label={`Cumulative invoiced value reaching ${money(known[known.length - 1], currency)}.`} preserveAspectRatio="none">
        <defs>
          <linearGradient id="trend-fill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#1f6aeb" stopOpacity={0.18} />
            <stop offset="100%" stopColor="#1f6aeb" stopOpacity={0} />
          </linearGradient>
        </defs>
        {firstKnown > 0 && <rect x={0} y={0} width={coords[firstKnown]![0]} height={CHART_HEIGHT} fill="#f8fafc" />}
        <line x1={0} x2={width} y1={CHART_HEIGHT} y2={CHART_HEIGHT} stroke="#e2e8f0" />
        <path d={area} fill="url(#trend-fill)" />
        <path d={path} fill="none" stroke="#1f6aeb" strokeWidth={2} vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
      </svg>
      <div className="mt-1 flex justify-between text-[11px] text-slate-500">
        <span>{formatDay(first.period_start < summary.filters.date_from ? summary.filters.date_from : first.period_start, false)}</span>
        <span>{formatDay(end.period_end, false)}</span>
      </div>
      {firstKnown > 0 && <p className="mt-2 text-xs text-slate-500">Shaded area: before values were recorded.</p>}
    </div>
  );
}

function RankedBars({ items, currency, detail, empty }: { items: Ranked[]; currency: string | null; detail: (item: Ranked) => string; empty: string }) {
  if (items.length === 0) return <EmptyChart text={empty} compact />;
  const peak = Math.max(...items.map((item) => Number(item.value))) || 1;
  return (
    <ol className="space-y-3">
      {items.map((item, index) => (
        <li key={item.key}>
          <div className="flex items-baseline justify-between gap-3 text-sm">
            <span className="min-w-0 truncate text-slate-800" title={item.name}>
              <span className="mr-2 tabular-nums text-slate-400">{index + 1}</span>
              {item.name}
            </span>
            <span className="shrink-0 font-medium tabular-nums text-slate-950">{money(item.value, currency)}</span>
          </div>
          <div className="mt-1 flex items-center gap-3">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
              <div className="h-full rounded-full bg-brand-600" style={{ width: `${Math.max((Number(item.value) / peak) * 100, 1.5)}%` }} />
            </div>
            <span className="w-36 shrink-0 text-right text-[11px] tabular-nums text-slate-500">{detail(item)}</span>
          </div>
        </li>
      ))}
    </ol>
  );
}

function SyncOutcomes({ runs }: { runs: Summary["kpis"]["sync_runs"] }) {
  const parts = [
    { key: "success", label: "Successful", value: runs.success, color: "bg-emerald-500" },
    { key: "partial", label: "Partial", value: runs.partial, color: "bg-amber-400" },
    { key: "failed", label: "Failed", value: runs.failed, color: "bg-rose-500" },
    { key: "skipped", label: "Nothing to export", value: runs.skipped, color: "bg-slate-300" },
    { key: "running", label: "Running", value: runs.running, color: "bg-brand-400" },
  ].filter((part) => part.value > 0 || part.key !== "running");
  if (runs.total === 0) return <EmptyChart text="No sync runs in this range." compact />;
  return (
    <div>
      <div className="flex h-3 overflow-hidden rounded-full bg-slate-100" role="img" aria-label={parts.map((p) => `${p.label}: ${p.value}`).join(", ")}>
        {parts.map((part) =>
          part.value > 0 ? <div key={part.key} className={part.color} style={{ width: `${(part.value / runs.total) * 100}%` }} title={`${part.label}: ${part.value}`} /> : null,
        )}
      </div>
      <dl className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-5">
        {parts.map((part) => (
          <div key={part.key}>
            <dt className="flex items-center gap-1.5 text-xs text-slate-500">
              <span className={`h-2 w-2 rounded-full ${part.color}`} aria-hidden="true" />
              {part.label}
            </dt>
            <dd className="mt-0.5 text-lg font-semibold tabular-nums text-slate-950">
              {count(part.value)}
              <span className="ml-1.5 text-xs font-normal text-slate-500">{Math.round((part.value / runs.total) * 100)}%</span>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function ChartLegend({ items }: { items: Array<{ swatch: string; label: string }> }) {
  return (
    <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
      {items.map((item) => (
        <li key={item.label} className="flex items-center gap-1.5">
          <span className={`h-2.5 w-2.5 rounded-sm ${item.swatch}`} aria-hidden="true" />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

function EmptyChart({ text, compact = false }: { text: string; compact?: boolean }) {
  return (
    <div className={`flex items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-4 text-center text-sm text-slate-500 ${compact ? "py-8" : "h-48"}`}>
      {text}
    </div>
  );
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-4 w-4">
      <path d="M12 4v11m0 0-4.5-4.5M12 15l4.5-4.5M5 19h14" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" />
    </svg>
  );
}
