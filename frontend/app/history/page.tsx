"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { AppShell } from "../../components/app-shell";
import { Modal } from "../../components/ui/modal";
import { PageHeader } from "../../components/ui/page-header";
import { StatusBadge } from "../../components/ui/status-badge";
import type { StatusTone } from "../../components/ui/status-badge";

type HistoryItem = {
  id: number;
  date_from: string;
  date_to: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  total_orders: number;
  total_customers: number;
  total_invoices: number;
  errors: string | null;
};

type HistoryDetail = {
  sync_run: HistoryItem;
  orders: Array<{
    unify_order_id: string;
    customer_name?: string | null;
    status: string;
    message?: string | null;
    created_at?: string | null;
  }>;
  invoices: Array<{
    unify_customer_name: string;
    unify_order_ids: string[];
    zoho_invoice_id?: string | null;
    status: string;
    message?: string | null;
    created_at?: string | null;
  }>;
};

type Notice = {
  kind: "success" | "error";
  text: string;
};

type ResetResponseData = {
  requested_run_ids?: number[];
  found_run_ids?: number[];
  missing_run_ids?: number[];
  // Soft reset: records are kept and marked "reset"; the deleted_* fields are always 0.
  reset_exported_orders?: number;
  already_reset_orders?: number;
  blocked_orders?: number;
  blocked_order_ids?: string[];
  deleted_exported_orders?: number;
  deleted_exported_invoices?: number;
  updated_sync_runs?: number;
};

type HistoryResponse = HistoryItem[] | { data?: unknown };

const SYNC_RESET_SIGNAL_KEY = "zconnect:last-selected-run-reset-at";

// Visual tone per stored status value (display only).
const STATUS_TONES: Record<string, StatusTone> = {
  success: "synced",
  exported: "synced",
  created: "synced",
  partial: "review",
  failed: "failed",
  running: "running",
  skipped: "blocked",
  pending: "neutral",
  not_synced: "neutral",
  needs_verification: "review",
  in_progress: "running",
  reset: "neutral",
};

// Filter chip order; statuses not listed here follow alphabetically.
const STATUS_FILTER_ORDER = ["success", "partial", "failed", "running", "pending", "not_synced"];

const BUTTON_SHAPE =
  "inline-flex items-center justify-center whitespace-nowrap rounded-lg px-3.5 py-2 text-sm font-medium transition-colors duration-150 motion-reduce:transition-none disabled:cursor-not-allowed disabled:opacity-50";
const BUTTON_SECONDARY = `${BUTTON_SHAPE} border border-slate-200 bg-white text-slate-700 shadow-card hover:border-slate-300 hover:bg-slate-50 hover:text-slate-950`;
const BUTTON_GHOST = `${BUTTON_SHAPE} text-slate-600 hover:bg-slate-100 hover:text-slate-950 disabled:hover:bg-transparent`;
const BUTTON_DANGER = `${BUTTON_SHAPE} border border-rose-200 bg-white text-rose-700 shadow-card hover:border-rose-300 hover:bg-rose-50 disabled:hover:bg-white`;
const BUTTON_DANGER_SOLID = `${BUTTON_SHAPE} bg-rose-600 text-white shadow-card hover:bg-rose-700 disabled:hover:bg-rose-600`;

const dateTimeFormatter = new Intl.DateTimeFormat("en-GB", {
  dateStyle: "medium",
  timeStyle: "short",
});

const plainDateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
});

function formatHistoryStatusLabel(value: string) {
  switch (value.toLowerCase()) {
    case "pending":
    case "not_synced":
      return "Not processed";
    case "success":
      return "Completed";
    case "failed":
      return "Failed";
    default:
      return value.replaceAll("_", " ").replace(/\b\w/g, (match) => match.toUpperCase());
  }
}

function formatDateTime(value: string | null | undefined) {
  if (!value) {
    return "Not finished yet";
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }

  return dateTimeFormatter.format(parsed);
}

// Formats plain YYYY-MM-DD values for display; anything else is shown as received.
function formatPlainDate(value: unknown) {
  if (typeof value !== "string") {
    return value == null ? "" : String(value);
  }
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (!match) {
    return value;
  }
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(date.getTime()) ? value : plainDateFormatter.format(date);
}

function formatPeriod(from: unknown, to: unknown) {
  const start = formatPlainDate(from);
  const end = formatPlainDate(to);
  if (!start && !end) {
    return "—";
  }
  if (!end || start === end) {
    return start || end;
  }
  return `${start} – ${end}`;
}

function pluralize(count: number, singular: string) {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}

function cleanMessage(value: unknown) {
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) {
      return "";
    }

    try {
      const parsed = JSON.parse(trimmed) as Record<string, unknown>;
      const extracted =
        parsed.detail || parsed.error || parsed.message || parsed.details || parsed.summary || parsed.title;
      if (typeof extracted === "string" && extracted.trim()) {
        return extracted.trim();
      }
      return "";
    } catch {
      if ((trimmed.startsWith("{") && trimmed.endsWith("}")) || (trimmed.startsWith("[") && trimmed.endsWith("]"))) {
        return "";
      }
      return trimmed;
    }
  }

  if (value && typeof value === "object") {
    const extracted =
      (value as Record<string, unknown>).detail ||
      (value as Record<string, unknown>).error ||
      (value as Record<string, unknown>).message ||
      (value as Record<string, unknown>).details ||
      (value as Record<string, unknown>).summary ||
      (value as Record<string, unknown>).title;
    if (typeof extracted === "string" && extracted.trim()) {
      return extracted.trim();
    }
  }

  return "";
}

function formatResetNotice(data: ResetResponseData, requestedCount: number) {
  // Soft reset: nothing is deleted. Orders are marked for re-sync; Zoho invoices, run totals
  // and history are kept, and every re-sync checks Zoho before creating an invoice.
  const foundCount = data.found_run_ids?.length ?? requestedCount;
  const missingCount = data.missing_run_ids?.length ?? 0;
  const resetOrders = data.reset_exported_orders ?? 0;
  const alreadyReset = data.already_reset_orders ?? 0;
  const blocked = data.blocked_orders ?? 0;

  const parts = [
    `${pluralize(foundCount, "run")} reset: ${pluralize(resetOrders, "order")} marked for re-sync. Zoho invoices and history were kept`,
  ];
  if (alreadyReset) {
    parts.push(`${pluralize(alreadyReset, "order")} ${alreadyReset === 1 ? "was" : "were"} already reset`);
  }
  if (blocked) {
    parts.push(`${pluralize(blocked, "order")} ${blocked === 1 ? "needs" : "need"} checking in Zoho and ${blocked === 1 ? "was" : "were"} left unchanged`);
  }
  if (missingCount) {
    parts.push(missingCount === 1 ? "1 selected run was not found" : `${missingCount} selected runs were not found`);
  }
  return `${parts.join(". ")}.`;
}

function normalizeHistoryResponse(data: HistoryResponse): HistoryItem[] {
  if (Array.isArray(data)) {
    return data;
  }

  return Array.isArray(data.data) ? data.data : [];
}

function HistoryStatusBadge({ status }: { status: string }) {
  const key = typeof status === "string" ? status.toLowerCase() : "";
  return <StatusBadge tone={STATUS_TONES[key] ?? "neutral"}>{key ? formatHistoryStatusLabel(status) : "Unknown"}</StatusBadge>;
}

function SummaryField({ label, value, wide = false }: { label: string; value: ReactNode; wide?: boolean }) {
  return (
    <div className={["min-w-0 px-4 py-3", wide ? "col-span-2" : ""].join(" ")}>
      <dt className="text-xs font-medium text-slate-500">{label}</dt>
      <dd className="mt-1 break-words text-sm font-medium tabular-nums text-slate-950">{value}</dd>
    </div>
  );
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" className="h-4 w-4">
      <path d="m14.5 14.5 3 3M16 9A7 7 0 1 1 2 9a7 7 0 0 1 14 0Z" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="1.6" />
    </svg>
  );
}

export default function HistoryPage() {
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [selectedDetailId, setSelectedDetailId] = useState<number | null>(null);
  const [detail, setDetail] = useState<HistoryDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [selectedRunIds, setSelectedRunIds] = useState<Set<number>>(new Set());
  const [notice, setNotice] = useState<Notice | null>(null);
  const [resetting, setResetting] = useState(false);
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false);
  const [pendingResetRunIds, setPendingResetRunIds] = useState<number[]>([]);
  // Presentation-only state: client-side search and status filter over already-loaded runs.
  const [runQuery, setRunQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const detailPanelRef = useRef<HTMLElement | null>(null);
  const latestDetailRequestRef = useRef(0);

  const fetchHistory = async () => {
    const res = await fetch("/api/export/history");
    const data = (await res.json()) as HistoryResponse;
    if (!res.ok) {
      throw new Error(cleanMessage(data) || "Unable to load export history.");
    }
    const historyItems = normalizeHistoryResponse(data);
    setHistory(historyItems);
    return historyItems;
  };

  useEffect(() => {
    let mounted = true;
    fetchHistory()
      .catch(() => {
        if (mounted) {
          setNotice({ kind: "error", text: "Unable to load export history." });
        }
      })
      .finally(() => {
        if (mounted) {
          setLoading(false);
        }
      });

    return () => {
      mounted = false;
    };
  }, []);

  const loadDetail = async (id: number) => {
    latestDetailRequestRef.current += 1;
    const requestId = latestDetailRequestRef.current;
    const isLatest = () => latestDetailRequestRef.current === requestId;
    setSelectedDetailId(id);
    setDetailLoading(true);
    try {
      const res = await fetch(`/api/export/history/${id}`);
      const data = (await res.json()) as HistoryDetail;
      if (isLatest()) {
        // Error payloads (e.g. 404 { detail }) are not run details; keep no stale detail.
        setDetail(res.ok ? data : null);
      }
    } catch (error) {
      // Network or parse failure: show the run from the list without stale details.
      console.error("[History] Run detail request failed", { id, error });
      if (isLatest()) {
        setDetail(null);
      }
    } finally {
      if (isLatest()) {
        setDetailLoading(false);
      }
    }
  };

  const selectedRun = useMemo(
    () => detail?.sync_run ?? (Array.isArray(history) ? history.find((item) => item.id === selectedDetailId) ?? null : null),
    [detail, history, selectedDetailId],
  );

  const selectedCount = selectedRunIds.size;
  const allVisibleSelected = history.length > 0 && selectedCount === history.length;
  const errorText = cleanMessage(selectedRun?.errors);
  const selectedSummary = selectedRun
    ? `${pluralize(selectedRun.total_orders, "order")} processed, ${pluralize(selectedRun.total_customers, "customer")}, ${pluralize(selectedRun.total_invoices, "invoice")} created`
    : "";

  const updateSelection = (updater: (current: Set<number>) => Set<number>) => {
    setSelectedRunIds((current) => updater(new Set(current)));
  };

  const toggleSelected = (id: number) => {
    updateSelection((current) => {
      if (current.has(id)) {
        current.delete(id);
      } else {
        current.add(id);
      }
      return current;
    });
  };

  const selectAllVisible = () => {
    setSelectedRunIds(new Set(history.map((item) => item.id)));
  };

  const clearSelection = () => {
    setSelectedRunIds(new Set());
  };

  const openResetConfirmation = () => {
    const runIds = Array.from(selectedRunIds);
    if (!runIds.length) {
      return;
    }
    setPendingResetRunIds(runIds);
    setResetConfirmOpen(true);
  };

  const resetSelectedRuns = async (runIds: number[]) => {
    if (!runIds.length) {
      return;
    }

    setResetting(true);
    setNotice(null);

    try {
      const res = await fetch("/api/export/reset-selected-runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_ids: runIds }),
      });

      const data = (await res.json()) as { ok?: boolean; message?: string; data?: ResetResponseData; error?: string };
      if (!res.ok || data.ok === false) {
        throw new Error(cleanMessage(data) || "Unable to reset the selected runs.");
      }

      try {
        window.localStorage.setItem(SYNC_RESET_SIGNAL_KEY, String(Date.now()));
      } catch {
        // Ignore storage failures; the reset already succeeded.
      }

      const refreshedHistory = await fetchHistory();
      const impactedSelectedDetail = selectedDetailId !== null && runIds.includes(selectedDetailId);
      setSelectedRunIds(new Set());
      setResetConfirmOpen(false);
      setPendingResetRunIds([]);
      setNotice({
        kind: "success",
        text: formatResetNotice(data.data ?? {}, runIds.length),
      });

      if (impactedSelectedDetail && selectedDetailId !== null) {
        await loadDetail(selectedDetailId);
      } else if (selectedDetailId !== null && !refreshedHistory.some((item) => item.id === selectedDetailId)) {
        setSelectedDetailId(null);
        setDetail(null);
      }
    } catch (error) {
      setResetConfirmOpen(false);
      setPendingResetRunIds([]);
      setNotice({
        kind: "error",
        text: error instanceof Error ? error.message : "Unable to reset the selected runs.",
      });
    } finally {
      setResetting(false);
    }
  };

  // Presentation-only values derived from existing state; no API or reset logic.
  const normalizedQuery = runQuery.trim().replace(/^#/, "");
  const statusCounts = history.reduce<Record<string, number>>((acc, item) => {
    const key = String(item.status || "").toLowerCase();
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});
  const statusOptions = Object.keys(statusCounts).sort(
    (a, b) => (STATUS_FILTER_ORDER.indexOf(a) + 1 || 99) - (STATUS_FILTER_ORDER.indexOf(b) + 1 || 99) || a.localeCompare(b),
  );
  const filteredHistory = history.filter(
    (item) =>
      (statusFilter === "all" || String(item.status || "").toLowerCase() === statusFilter) &&
      (!normalizedQuery || String(item.id).includes(normalizedQuery)),
  );
  const filtersActive = statusFilter !== "all" || normalizedQuery.length > 0;
  const allFilteredSelected = filteredHistory.length > 0 && filteredHistory.every((item) => selectedRunIds.has(item.id));
  const hiddenSelectedCount = Array.from(selectedRunIds).filter((id) => !filteredHistory.some((item) => item.id === id)).length;
  // Only use a loaded detail payload when it belongs to the run being shown.
  const detailForSelected = detail && detail.sync_run?.id === selectedDetailId ? detail : null;
  const displayedRun = detailForSelected?.sync_run ?? history.find((item) => item.id === selectedDetailId) ?? null;
  const displayedErrorText = cleanMessage(displayedRun?.errors);
  const detailOrders = Array.isArray(detailForSelected?.orders) ? detailForSelected.orders : [];
  const detailInvoices = Array.isArray(detailForSelected?.invoices) ? detailForSelected.invoices : [];

  const selectFiltered = () => {
    setSelectedRunIds(new Set([...Array.from(selectedRunIds), ...filteredHistory.map((item) => item.id)]));
  };

  const viewRun = (id: number) => {
    void loadDetail(id);
    if (window.matchMedia("(max-width: 1279px)").matches) {
      detailPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  };

  const clearFilters = () => {
    setRunQuery("");
    setStatusFilter("all");
  };

  return (
    <AppShell>
      <main className="space-y-6">
        <PageHeader
          eyebrow="History"
          title="Sync history"
          description="Review previous sync runs, inspect the orders and invoices they produced, and reset runs so their orders can be re-synced safely. Resets never delete invoices or history."
        />

        {notice && (
          <div
            role={notice.kind === "error" ? "alert" : "status"}
            className={[
              "rounded-xl border px-4 py-3 text-sm",
              notice.kind === "success" ? "border-emerald-200 bg-emerald-50 text-emerald-800" : "border-rose-200 bg-rose-50 text-rose-700",
            ].join(" ")}
          >
            {notice.text}
          </div>
        )}

        <div className="grid gap-6 xl:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)] xl:items-start">
          <section aria-labelledby="runs-heading" className="min-w-0 rounded-2xl border border-slate-200/80 bg-white shadow-card">
            <div className="space-y-4 border-b border-slate-200/80 p-4 sm:p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h2 id="runs-heading" className="text-base font-semibold tracking-tight text-slate-950">
                  Sync runs
                  {!loading && (
                    <span className="ml-2 text-sm font-normal tabular-nums text-slate-500">
                      {filtersActive ? `${filteredHistory.length} of ${history.length}` : history.length}
                    </span>
                  )}
                </h2>
                <label className="relative block w-full sm:w-56">
                  <span className="sr-only">Search by run ID</span>
                  <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-slate-400">
                    <SearchIcon />
                  </span>
                  <input
                    type="search"
                    inputMode="numeric"
                    value={runQuery}
                    onChange={(event) => setRunQuery(event.target.value)}
                    placeholder="Search run ID"
                    className="h-9 w-full rounded-lg border border-slate-200 bg-white pl-9 pr-3 text-sm text-slate-900 shadow-card placeholder:text-slate-400 focus:border-brand-400 focus:outline-none focus:ring-4 focus:ring-brand-100"
                  />
                </label>
              </div>

              {statusOptions.length > 0 && (
                <div role="group" aria-label="Filter by status" className="flex flex-wrap gap-1.5">
                  {["all", ...statusOptions].map((value) => {
                    const active = statusFilter === value;
                    return (
                      <button
                        key={value}
                        type="button"
                        aria-pressed={active}
                        onClick={() => setStatusFilter(value)}
                        className={[
                          "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors duration-150 motion-reduce:transition-none",
                          active ? "border-slate-900 bg-slate-900 text-white" : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:text-slate-900",
                        ].join(" ")}
                      >
                        {value === "all" ? "All" : formatHistoryStatusLabel(value)}
                        <span className={["tabular-nums", active ? "text-white/70" : "text-slate-400"].join(" ")}>
                          {value === "all" ? history.length : statusCounts[value]}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}

              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <p className="text-sm text-slate-600" aria-live="polite">
                  <span className="font-medium tabular-nums text-slate-900">{selectedCount}</span> selected
                  {hiddenSelectedCount > 0 && <span className="text-slate-500"> ({hiddenSelectedCount} hidden by filters)</span>}
                </p>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    className={BUTTON_GHOST}
                    onClick={filtersActive ? selectFiltered : selectAllVisible}
                    disabled={loading || filteredHistory.length === 0 || (filtersActive ? allFilteredSelected : allVisibleSelected)}
                  >
                    Select all visible
                  </button>
                  <button type="button" className={BUTTON_GHOST} onClick={clearSelection} disabled={loading || selectedCount === 0}>
                    Clear selection
                  </button>
                  <button
                    type="button"
                    className={BUTTON_DANGER}
                    onClick={openResetConfirmation}
                    disabled={loading || resetting || selectedCount === 0}
                  >
                    Reset selected
                  </button>
                </div>
              </div>
            </div>

            {loading ? (
              <div className="space-y-3 p-4 sm:p-5" aria-label="Loading sync runs">
                {[1, 2, 3].map((item) => (
                  <div key={item} className="h-14 animate-pulse rounded-xl bg-slate-100 motion-reduce:animate-none" />
                ))}
              </div>
            ) : history.length === 0 ? (
              <div className="p-8 text-center">
                <p className="text-sm font-medium text-slate-900">No sync runs yet</p>
                <p className="mt-1 text-sm text-slate-500">Runs appear here after you sync orders from the Sync page.</p>
              </div>
            ) : filteredHistory.length === 0 ? (
              <div className="p-8 text-center">
                <p className="text-sm font-medium text-slate-900">No runs match these filters</p>
                <p className="mt-1 text-sm text-slate-500">Try a different run ID or status.</p>
                <button type="button" className={`${BUTTON_SECONDARY} mt-4`} onClick={clearFilters}>
                  Clear filters
                </button>
              </div>
            ) : (
              <>
                <div className="hidden overflow-x-auto md:block">
                  <table className="w-full text-left text-sm">
                    <caption className="sr-only">Sync runs</caption>
                    <thead className="border-b border-slate-200 bg-slate-50/70 text-xs text-slate-500">
                      <tr>
                        <th scope="col" className="w-12 px-4 py-2.5 font-medium">
                          <span className="sr-only">Select</span>
                        </th>
                        <th scope="col" className="px-3 py-2.5 font-medium">Run</th>
                        <th scope="col" className="px-3 py-2.5 font-medium">Period</th>
                        <th scope="col" className="px-3 py-2.5 font-medium">Status</th>
                        <th scope="col" className="px-3 py-2.5 text-right font-medium">Invoices</th>
                        <th scope="col" className="px-4 py-2.5 text-right font-medium">
                          <span className="sr-only">Actions</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {filteredHistory.map((item) => {
                        const isSelected = selectedRunIds.has(item.id);
                        const isViewing = selectedDetailId === item.id;
                        return (
                          <tr key={item.id} className={isViewing ? "bg-brand-50/50" : "transition-colors hover:bg-slate-50/70"}>
                            <td className="px-4 py-3 align-top">
                              <input
                                type="checkbox"
                                className="mt-0.5 h-4 w-4 cursor-pointer accent-brand-600"
                                checked={isSelected}
                                onChange={() => toggleSelected(item.id)}
                                aria-label={`Select run ${item.id}`}
                              />
                            </td>
                            <td className="px-3 py-3 align-top">
                              <div className="font-semibold text-slate-950">Run #{item.id}</div>
                              <div className="mt-0.5 whitespace-nowrap text-xs text-slate-500">{formatDateTime(item.started_at)}</div>
                            </td>
                            <td className="px-3 py-3 align-top">
                              <div className="text-slate-700">{formatPeriod(item.date_from, item.date_to)}</div>
                              <div className="mt-0.5 text-xs text-slate-500">
                                {pluralize(item.total_orders, "order")} · {pluralize(item.total_customers, "customer")}
                              </div>
                            </td>
                            <td className="px-3 py-3 align-top">
                              <HistoryStatusBadge status={item.status} />
                            </td>
                            <td className="px-3 py-3 text-right align-top font-semibold tabular-nums text-slate-950">{item.total_invoices}</td>
                            <td className="px-4 py-3 text-right align-top">
                              <button
                                type="button"
                                className={[
                                  "whitespace-nowrap rounded-md px-2 py-1 text-sm font-medium transition-colors duration-150 motion-reduce:transition-none",
                                  isViewing ? "bg-brand-100 text-brand-800" : "text-brand-700 hover:bg-brand-50 hover:text-brand-800",
                                ].join(" ")}
                                onClick={() => viewRun(item.id)}
                                aria-pressed={isViewing}
                              >
                                View details
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>

                <ul className="divide-y divide-slate-100 md:hidden">
                  {filteredHistory.map((item) => {
                    const isSelected = selectedRunIds.has(item.id);
                    const isViewing = selectedDetailId === item.id;
                    return (
                      <li key={item.id} className={["flex gap-3 p-4", isViewing ? "bg-brand-50/50" : ""].join(" ")}>
                        <input
                          type="checkbox"
                          className="mt-1 h-[18px] w-[18px] shrink-0 cursor-pointer accent-brand-600"
                          checked={isSelected}
                          onChange={() => toggleSelected(item.id)}
                          aria-label={`Select run ${item.id}`}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <span className="font-semibold text-slate-950">Run #{item.id}</span>
                            <HistoryStatusBadge status={item.status} />
                          </div>
                          <div className="mt-1 text-xs text-slate-500">{formatDateTime(item.started_at)}</div>
                          <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                            <div className="col-span-2 flex gap-1.5">
                              <dt className="text-slate-400">Period</dt>
                              <dd className="min-w-0 text-slate-700">{formatPeriod(item.date_from, item.date_to)}</dd>
                            </div>
                            <div className="flex gap-1.5">
                              <dt className="text-slate-400">Orders</dt>
                              <dd className="tabular-nums text-slate-700">{item.total_orders}</dd>
                            </div>
                            <div className="flex gap-1.5">
                              <dt className="text-slate-400">Invoices</dt>
                              <dd className="font-semibold tabular-nums text-slate-950">{item.total_invoices}</dd>
                            </div>
                          </dl>
                          <button
                            type="button"
                            className={`${isViewing ? BUTTON_SECONDARY : BUTTON_GHOST} -ml-3 mt-2`}
                            onClick={() => viewRun(item.id)}
                            aria-pressed={isViewing}
                          >
                            View details
                          </button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
          </section>

          <section
            ref={detailPanelRef}
            aria-labelledby="run-detail-heading"
            className="min-w-0 scroll-mt-28 rounded-2xl border border-slate-200/80 bg-white shadow-card xl:sticky xl:top-24"
          >
            <div className="flex items-center justify-between gap-3 border-b border-slate-200/80 px-4 py-4 sm:px-5">
              <h2 id="run-detail-heading" className="text-base font-semibold tracking-tight text-slate-950">
                {displayedRun ? `Run #${displayedRun.id}` : "Run details"}
              </h2>
              {displayedRun && !detailLoading ? <HistoryStatusBadge status={displayedRun.status} /> : null}
            </div>

            <div className="p-4 sm:p-5" aria-busy={detailLoading}>
              {detailLoading ? (
                <div className="space-y-3">
                  <div className="h-4 w-2/3 animate-pulse rounded-full bg-slate-100 motion-reduce:animate-none" />
                  <div className="h-28 animate-pulse rounded-xl bg-slate-100 motion-reduce:animate-none" />
                  <div className="h-20 animate-pulse rounded-xl bg-slate-100 motion-reduce:animate-none" />
                </div>
              ) : displayedRun ? (
                <div className="space-y-5">
                  <p className="text-sm text-slate-600">
                    {`${pluralize(displayedRun.total_orders, "order")} processed, ${pluralize(displayedRun.total_customers, "customer")}, ${pluralize(displayedRun.total_invoices, "invoice")} created`}
                  </p>

                  <dl className="grid grid-cols-2 divide-y divide-slate-200/70 overflow-hidden rounded-xl border border-slate-200 bg-slate-50/60 [&>*:nth-child(odd)]:border-r [&>*]:border-slate-200/70">
                    <SummaryField label="Period" value={formatPeriod(displayedRun.date_from, displayedRun.date_to)} wide />
                    <SummaryField label="Started" value={formatDateTime(displayedRun.started_at)} />
                    <SummaryField label="Finished" value={formatDateTime(displayedRun.finished_at)} />
                    <SummaryField label="Orders" value={displayedRun.total_orders} />
                    <SummaryField label="Customers" value={displayedRun.total_customers} />
                    <SummaryField label="Invoices" value={displayedRun.total_invoices} wide />
                  </dl>

                  {displayedErrorText && (
                    <div role="note" className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                      <div className="font-medium">Run error</div>
                      <p className="mt-1 whitespace-pre-wrap break-words leading-6">{displayedErrorText}</p>
                    </div>
                  )}

                  {!detailForSelected ? (
                    <p className="rounded-xl border border-dashed border-slate-300 bg-slate-50 px-4 py-3 text-sm text-slate-600">
                      Order and invoice details are not available for this run.
                    </p>
                  ) : (
                    <>
                      <div>
                        <h3 className="text-sm font-semibold text-slate-950">
                          Orders <span className="font-normal tabular-nums text-slate-500">{detailOrders.length}</span>
                        </h3>
                        {detailOrders.length === 0 ? (
                          <p className="mt-2 text-sm text-slate-500">No order records for this run.</p>
                        ) : (
                          <ul className="mt-2 divide-y divide-slate-100 rounded-xl border border-slate-200">
                            {detailOrders.map((order, index) => (
                              <li key={`${order.unify_order_id}-${index}`} className="px-3.5 py-3">
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                  <span className="min-w-0 break-all font-mono text-xs font-medium text-slate-700">{order.unify_order_id}</span>
                                  <HistoryStatusBadge status={order.status} />
                                </div>
                                {order.customer_name && <div className="mt-1 break-words text-sm font-medium text-slate-950">{order.customer_name}</div>}
                                {cleanMessage(order.message) && (
                                  <p
                                    className={[
                                      "mt-1 break-words text-xs leading-5",
                                      String(order.status).toLowerCase() === "failed" ? "text-rose-700" : "text-slate-500",
                                    ].join(" ")}
                                  >
                                    {cleanMessage(order.message)}
                                  </p>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>

                      <div>
                        <h3 className="text-sm font-semibold text-slate-950">
                          Invoices <span className="font-normal tabular-nums text-slate-500">{detailInvoices.length}</span>
                        </h3>
                        {detailInvoices.length === 0 ? (
                          <p className="mt-2 text-sm text-slate-500">No invoice records for this run.</p>
                        ) : (
                          <ul className="mt-2 divide-y divide-slate-100 rounded-xl border border-slate-200">
                            {detailInvoices.map((invoice, index) => (
                              <li key={`${invoice.zoho_invoice_id ?? "invoice"}-${index}`} className="px-3.5 py-3">
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                  <span className="min-w-0 break-words text-sm font-medium text-slate-950">{invoice.unify_customer_name}</span>
                                  <HistoryStatusBadge status={invoice.status} />
                                </div>
                                <dl className="mt-1.5 space-y-0.5 text-xs">
                                  {invoice.zoho_invoice_id && (
                                    <div className="flex gap-1.5">
                                      <dt className="shrink-0 text-slate-400">Zoho invoice ID</dt>
                                      <dd className="min-w-0 break-all font-mono text-slate-700">{invoice.zoho_invoice_id}</dd>
                                    </div>
                                  )}
                                  {Array.isArray(invoice.unify_order_ids) && invoice.unify_order_ids.length > 0 && (
                                    <div className="flex gap-1.5">
                                      <dt className="shrink-0 text-slate-400">{invoice.unify_order_ids.length === 1 ? "Order" : "Orders"}</dt>
                                      <dd className="min-w-0 break-all font-mono text-slate-700">{invoice.unify_order_ids.join(", ")}</dd>
                                    </div>
                                  )}
                                </dl>
                                {cleanMessage(invoice.message) && (
                                  <p className="mt-1 break-words text-xs leading-5 text-slate-500">{cleanMessage(invoice.message)}</p>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    </>
                  )}
                </div>
              ) : (
                <p className="rounded-xl border border-dashed border-slate-300 bg-slate-50 px-4 py-6 text-center text-sm text-slate-600">
                  Select a run to see its summary, orders and invoices.
                </p>
              )}
            </div>
          </section>
        </div>

        <Modal
          open={resetConfirmOpen}
          title="Reset selected runs"
          onClose={() => {
            setResetConfirmOpen(false);
            setPendingResetRunIds([]);
          }}
          actions={
            <>
              <button
                type="button"
                className={BUTTON_SECONDARY}
                onClick={() => {
                  setResetConfirmOpen(false);
                  setPendingResetRunIds([]);
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                className={BUTTON_DANGER_SOLID}
                onClick={() => resetSelectedRuns(pendingResetRunIds)}
                disabled={resetting}
              >
                Confirm reset
              </button>
            </>
          }
        >
          <p>The orders in the selected runs will be marked for re-sync.</p>
          <p className="mt-2">
            Nothing is deleted: Zoho invoices, run totals and history are kept. When an order is synced again, Zconnect
            checks Zoho first and links an existing invoice instead of creating a duplicate. Orders that need checking in
            Zoho are left unchanged.
          </p>
          {pendingResetRunIds.length > 0 && (
            <p className="mt-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
              <span className="font-medium text-slate-900">{pluralize(pendingResetRunIds.length, "run")}:</span>{" "}
              <span className="break-words tabular-nums">{pendingResetRunIds.map((id) => `#${id}`).join(", ")}</span>
            </p>
          )}
        </Modal>
      </main>
    </AppShell>
  );
}
