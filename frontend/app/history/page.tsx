"use client";

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { AppShell } from "../../components/app-shell";

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
  deleted_exported_orders?: number;
  deleted_exported_invoices?: number;
  updated_sync_runs?: number;
};

const SYNC_RESET_SIGNAL_KEY = "zconnect:last-selected-run-reset-at";

const statusStyles: Record<string, string> = {
  success: "border-emerald-200 bg-emerald-50 text-emerald-700",
  partial: "border-amber-200 bg-amber-50 text-amber-800",
  failed: "border-rose-200 bg-rose-50 text-rose-700",
  running: "border-sky-200 bg-sky-50 text-sky-700",
  pending: "border-slate-200 bg-slate-100 text-slate-600",
  not_synced: "border-slate-200 bg-slate-100 text-slate-600",
};

const dateTimeFormatter = new Intl.DateTimeFormat("en-GB", {
  dateStyle: "medium",
  timeStyle: "short",
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
  const foundCount = data.found_run_ids?.length ?? requestedCount;
  const missingCount = data.missing_run_ids?.length ?? 0;
  const deletedOrders = data.deleted_exported_orders ?? 0;
  const deletedInvoices = data.deleted_exported_invoices ?? 0;
  const updatedRuns = data.updated_sync_runs ?? foundCount;
  const missingMessage = missingCount === 1 ? "selected run was not found" : "selected runs were not found";

  const headline = `${pluralize(updatedRuns, "run")} reset successfully`;
  if (!missingCount) {
    return `${headline}. Removed ${pluralize(deletedOrders, "order record")} and ${pluralize(deletedInvoices, "invoice record")}.`;
  }

  return `${headline}. Removed ${pluralize(deletedOrders, "order record")} and ${pluralize(deletedInvoices, "invoice record")}. ${missingMessage}.`;
}

function StatusBadge({ status }: { status: string }) {
  const classes = statusStyles[status] ?? "border-slate-200 bg-slate-100 text-slate-600";
  return (
    <span className={`inline-flex rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] ${classes}`}>
      {formatHistoryStatusLabel(status)}
    </span>
  );
}

function SectionCard({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className="rounded-3xl border border-slate-200/80 bg-white p-6 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)]">
      <div className="mb-5 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-lg font-semibold tracking-tight text-slate-950">{title}</h3>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function SummaryField({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
      <div className="text-xs font-medium uppercase tracking-[0.22em] text-slate-500">{label}</div>
      <div className="mt-2 text-sm font-medium text-slate-950">{value}</div>
    </div>
  );
}

function Modal({
  open,
  title,
  children,
  actions,
  onClose,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  actions: ReactNode;
  onClose: () => void;
}) {
  if (!open) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 px-4 py-6 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-3xl border border-slate-200 bg-white p-6 shadow-[0_30px_80px_-24px_rgba(15,23,42,0.5)]">
        <div className="flex items-start justify-between gap-4">
          <h3 className="text-xl font-semibold tracking-tight text-slate-950">{title}</h3>
          <button
            type="button"
            className="rounded-full border border-slate-200 bg-white px-3 py-1 text-sm font-medium text-slate-500 transition hover:border-slate-300 hover:text-slate-700"
            onClick={onClose}
          >
            Close
          </button>
        </div>
        <div className="mt-4 text-sm leading-6 text-slate-600">{children}</div>
        <div className="mt-6 flex flex-wrap justify-end gap-3">{actions}</div>
      </div>
    </div>
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

  const fetchHistory = async () => {
    const res = await fetch("/api/export/history");
    const data = (await res.json()) as HistoryItem[];
    setHistory(data);
    return data;
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
    setSelectedDetailId(id);
    setDetailLoading(true);
    try {
      const res = await fetch(`/api/export/history/${id}`);
      const data = (await res.json()) as HistoryDetail;
      setDetail(data);
    } finally {
      setDetailLoading(false);
    }
  };

  const selectedRun = useMemo(
    () => detail?.sync_run ?? history.find((item) => item.id === selectedDetailId) ?? null,
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

  return (
    <AppShell>
      <main className="space-y-8">
      <section className="rounded-3xl border border-slate-200/80 bg-white/90 p-8 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)] backdrop-blur">
        <div className="flex flex-col gap-2">
          <p className="text-xs font-semibold uppercase tracking-[0.28em] text-slate-500">Export history</p>
          <h2 className="text-3xl font-semibold tracking-tight text-slate-950">Sync history and run summaries</h2>
          <p className="max-w-3xl text-sm leading-6 text-slate-600">
            Review previous export runs, inspect a clean summary, and reset selected runs without exposing technical payloads.
          </p>
        </div>
      </section>

      {notice && (
        <div
          className={[
            "rounded-2xl border px-4 py-3 text-sm",
            notice.kind === "success"
              ? "border-emerald-200 bg-emerald-50 text-emerald-800"
              : "border-rose-200 bg-rose-50 text-rose-700",
          ].join(" ")}
        >
          {notice.text}
        </div>
      )}

      <div className="grid gap-6 xl:grid-cols-[1.6fr_1fr]">
        <SectionCard
          title="Export runs"
          action={
            <div className="flex items-center gap-3">
              <div className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-medium uppercase tracking-[0.16em] text-slate-500">
                {selectedCount} selected
              </div>
              <button
                className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
                onClick={selectAllVisible}
                disabled={loading || history.length === 0 || allVisibleSelected}
              >
                Select all visible
              </button>
              <button
                className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
                onClick={clearSelection}
                disabled={loading || selectedCount === 0}
              >
                Clear selection
              </button>
              <button
                className="rounded-xl bg-slate-950 px-4 py-2 text-sm font-medium text-white shadow-[0_12px_24px_-16px_rgba(15,23,42,0.9)] transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-60"
                onClick={openResetConfirmation}
                disabled={loading || resetting || selectedCount === 0}
              >
                Reset selected
              </button>
            </div>
          }
        >
          {loading ? (
            <div className="space-y-3">
              {[1, 2, 3].map((item) => (
                <div key={item} className="h-16 animate-pulse rounded-2xl bg-slate-100" />
              ))}
            </div>
          ) : (
            <div className="overflow-hidden rounded-2xl border border-slate-200">
              <table className="w-full text-left">
                <thead className="bg-slate-50 text-xs uppercase tracking-[0.2em] text-slate-500">
                  <tr>
                    <th className="w-16 px-4 py-3 font-semibold">Select</th>
                    <th className="px-4 py-3 font-semibold">Run</th>
                    <th className="px-4 py-3 font-semibold">Period</th>
                    <th className="px-4 py-3 font-semibold">Status</th>
                    <th className="px-4 py-3 font-semibold">Invoices</th>
                    <th className="px-4 py-3 font-semibold">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {history.map((item) => {
                    const isSelected = selectedRunIds.has(item.id);
                    return (
                      <tr
                        key={item.id}
                        className={`transition hover:bg-slate-50 ${selectedDetailId === item.id ? "bg-slate-50/80" : "bg-white"}`}
                      >
                        <td className="px-4 py-4 align-top">
                          <input
                            type="checkbox"
                            className="mt-1 h-4 w-4 rounded border-slate-300 text-slate-900 focus:ring-slate-400 disabled:cursor-not-allowed"
                            checked={isSelected}
                            onChange={() => toggleSelected(item.id)}
                            aria-label={`Select run ${item.id}`}
                          />
                        </td>
                        <td className="px-4 py-4 align-top">
                          <div className="text-sm font-semibold text-slate-950">Run #{item.id}</div>
                          <div className="text-xs text-slate-500">
                            {pluralize(item.total_orders, "order")} • {pluralize(item.total_customers, "customer")}
                          </div>
                        </td>
                        <td className="px-4 py-4 align-top text-sm text-slate-700">
                          <div>{item.date_from}</div>
                          <div className="text-slate-400">to {item.date_to}</div>
                        </td>
                        <td className="px-4 py-4 align-top">
                          <StatusBadge status={item.status} />
                        </td>
                        <td className="px-4 py-4 align-top text-sm font-medium text-slate-900">{item.total_invoices}</td>
                        <td className="px-4 py-4 align-top">
                          <button
                            className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50"
                            onClick={() => loadDetail(item.id)}
                          >
                            View summary
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </SectionCard>

        <SectionCard title="Run summary" action={selectedRun ? <StatusBadge status={selectedRun.status} /> : null}>
          {detailLoading ? (
            <div className="space-y-3">
              <div className="h-5 w-2/3 animate-pulse rounded-full bg-slate-100" />
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="h-24 animate-pulse rounded-2xl bg-slate-100" />
                <div className="h-24 animate-pulse rounded-2xl bg-slate-100" />
                <div className="h-24 animate-pulse rounded-2xl bg-slate-100" />
                <div className="h-24 animate-pulse rounded-2xl bg-slate-100" />
              </div>
            </div>
          ) : selectedRun ? (
            <div className="space-y-4">
              <p className="text-sm leading-6 text-slate-600">{selectedSummary}</p>
              <div className="grid gap-3 sm:grid-cols-2">
                <SummaryField label="Period" value={`${selectedRun.date_from} to ${selectedRun.date_to}`} />
                <SummaryField label="Started at" value={formatDateTime(selectedRun.started_at)} />
                <SummaryField label="Finished at" value={formatDateTime(selectedRun.finished_at)} />
                <SummaryField label="Run status" value={<StatusBadge status={selectedRun.status} />} />
                <SummaryField label="Orders" value={selectedRun.total_orders} />
                <SummaryField label="Customers" value={selectedRun.total_customers} />
                <SummaryField label="Invoices" value={selectedRun.total_invoices} />
                {errorText && <SummaryField label="Error message" value={errorText} />}
              </div>
            </div>
          ) : (
            <div className="rounded-2xl border border-dashed border-slate-300 bg-slate-50 p-6 text-sm text-slate-600">
              Select a run to see the summary.
            </div>
          )}
        </SectionCard>
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
              className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50"
              onClick={() => {
                setResetConfirmOpen(false);
                setPendingResetRunIds([]);
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              className="rounded-xl bg-slate-950 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-60"
              onClick={() => resetSelectedRuns(pendingResetRunIds)}
              disabled={resetting}
            >
              Confirm reset
            </button>
          </>
        }
      >
        <p>Are you sure you want to reset the selected sync runs?</p>
        <p className="mt-2">This will remove their sync records and allow them to be processed again.</p>
      </Modal>
      </main>
    </AppShell>
  );
}
