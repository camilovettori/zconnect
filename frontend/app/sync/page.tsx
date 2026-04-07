"use client";

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { AppShell } from "../../components/app-shell";
import { formatCurrencyEur } from "../utils/money";
import { Calendar } from "../../components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger, usePopoverContext } from "../../components/ui/popover";

type OrderLine = { item_sku: string; item_name: string; quantity: number; price: number };
type UnifyOrderPreview = {
  order_id: string;
  customer_name: string;
  buyer_name?: string | null;
  buyer_id?: string | null;
  delivery_address?: string | null;
  delivery_date: string;
  order_date?: string;
  total: number;
  already_exported?: boolean;
  preview_status?: string;
  preview_reason?: string;
  status?: string;
};

type UnifyOrderDetail = UnifyOrderPreview & {
  order_date: string;
  lines: OrderLine[];
};

type CustomerSummary = { order_count: number; total: number };

type SyncDetail = {
  order_id: string;
  status: "created" | "skipped" | "failed";
  message: string;
  zoho_invoice_id?: string;
};

type SyncResult = {
  sync_run_id?: number;
  status?: string;
  created?: number;
  skipped?: number;
  failed?: number;
  details?: SyncDetail[];
  errors?: string | null;
};

type FetchSummary = {
  preview_count?: number;
  ready_count?: number;
  blocked_in_range_count?: number;
  dropped_out_of_range_count?: number;
  total_scanned_orders?: number;
  total_in_range_orders?: number;
  missing_items_count?: number;
  item_fetch_failed_count?: number;
  mapping_issue_count?: number;
  already_synced_count?: number;
  rawOrdersCount?: number;
  filteredOrdersCount?: number;
  previewTruncated?: boolean;
  previewTruncationReason?: string | null;
  rawPageCount?: number;
  droppedOrders?: Array<{ id: string; reason: string; rawDeliveryDate?: string | null }>;
};

type OrderStatus = "ready" | "already_synced" | "synced" | "failed";
type DateRange = { from: Date; to: Date };

const FETCH_TIMEOUT_MS = 95000;
const HISTORY_RESET_SIGNAL_KEY = "zconnect:last-selected-run-reset-at";

const tryParseJson = (text: string) => {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
};

const resolveErrorMessage = (candidate: unknown, fallback: string) => {
  if (typeof candidate === "string") {
    const trimmed = candidate.trim();
    if (!trimmed) {
      return fallback;
    }
    const nested = tryParseJson(trimmed);
    if (nested && typeof nested === "object") {
      return (
        (nested as Record<string, unknown>).detail ||
        (nested as Record<string, unknown>).error ||
        (nested as Record<string, unknown>).message ||
        (nested as Record<string, unknown>).details ||
        trimmed
      ) as string;
    }
    return trimmed;
  }

  if (candidate && typeof candidate === "object") {
    const obj = candidate as Record<string, unknown>;
    const message = obj.detail || obj.error || obj.message || obj.details;
    if (typeof message === "string" && message.trim()) {
      const nested = tryParseJson(message.trim());
      if (nested && typeof nested === "object") {
        return (
          (nested as Record<string, unknown>).detail ||
          (nested as Record<string, unknown>).error ||
          (nested as Record<string, unknown>).message ||
          (nested as Record<string, unknown>).details ||
          message
        ) as string;
      }
      return message.trim();
    }
  }

  return fallback;
};

const dateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
});

function cloneDate(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function toISODate(date: Date | null) {
  if (!date) {
    return "";
  }
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatDateDisplay(date: Date | null) {
  return date ? dateFormatter.format(date) : "";
}

function normalizeRange(from: Date | null, to: Date | null) {
  if (from && to && to < from) {
    return { from, to: cloneDate(from) };
  }
  return { from, to };
}

function CalendarIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-4 w-4 text-slate-500">
      <path
        d="M8 2v3M16 2v3M3.5 9h17M6 5.5h12A2.5 2.5 0 0 1 20.5 8v11A2.5 2.5 0 0 1 18 21.5H6A2.5 2.5 0 0 1 3.5 19V8A2.5 2.5 0 0 1 6 5.5Z"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.6"
      />
    </svg>
  );
}

function DatePickerContent({
  value,
  initialMonth,
  onSelect,
}: {
  value: Date | null;
  initialMonth?: Date | null;
  onSelect: (date: Date) => void;
}) {
  const { setOpen } = usePopoverContext();
  const [month, setMonth] = useState<Date>(initialMonth ?? value ?? new Date());

  return (
    <div className="space-y-3">
      <Calendar
        selected={value}
        month={month}
        onMonthChange={setMonth}
        onSelect={(date) => {
          onSelect(date);
          setMonth(new Date(date.getFullYear(), date.getMonth(), 1));
          setOpen(false);
        }}
      />
      <div className="text-xs text-slate-400">Pick a date to filter the delivery range.</div>
    </div>
  );
}

function DatePickerField({
  label,
  value,
  initialMonth,
  onSelect,
}: {
  label: string;
  value: Date | null;
  initialMonth?: Date | null;
  onSelect: (date: Date) => void;
}) {
  return (
    <div className="block">
      <span className="mb-1 block text-sm font-medium text-slate-700">{label}</span>
      <Popover>
        <div className="relative">
          <PopoverTrigger asChild>
            <button
              type="button"
              className="flex h-12 w-full items-center justify-between rounded-xl border border-slate-300 bg-white px-3 text-left text-sm text-slate-900 shadow-sm transition hover:border-slate-400 hover:bg-slate-50 focus:outline-none focus:ring-4 focus:ring-sky-100"
            >
              <span className={value ? "text-slate-900" : "text-slate-400"}>
                {value ? formatDateDisplay(value) : "Select date"}
              </span>
              <span className="ml-3 shrink-0">
                <CalendarIcon />
              </span>
            </button>
          </PopoverTrigger>
          <PopoverContent className="mt-3 w-[340px] p-4" align="start">
            <DatePickerContent value={value} initialMonth={initialMonth} onSelect={onSelect} />
          </PopoverContent>
        </div>
      </Popover>
    </div>
  );
}

async function parseApiResponse<T = any>(response: Response, action: string, endpoint: string): Promise<T> {
  const text = await response.text();
  const parsed = text ? tryParseJson(text) : null;
  const fallback = `HTTP ${response.status}`;

  if (!response.ok) {
    const message = resolveErrorMessage(parsed ?? text, fallback);
    console.error("[Sync] API request failed", {
      action,
      endpoint,
      status: response.status,
      message,
      bodyText: text,
    });
    throw new Error(message);
  }

  return (parsed ?? (text ? (text as unknown as T) : null)) as T;
}

function pluralize(count: number, singular: string) {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}

function formatSyncResultMessage(result: SyncResult) {
  const created = result.created ?? 0;
  const skipped = result.skipped ?? 0;
  const failed = result.failed ?? 0;
  const parts = [];

  if (created > 0) {
    parts.push(`${pluralize(created, "invoice")} created`);
  }
  if (skipped > 0) {
    parts.push(`${pluralize(skipped, "invoice")} skipped`);
  }
  if (failed > 0) {
    parts.push(`${pluralize(failed, "invoice")} failed`);
  }

  if (!parts.length) {
    return "Sync completed successfully";
  }

  return `${parts.join(", ")}${failed === 0 ? " successfully" : ""}`;
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
  actions?: ReactNode;
  onClose?: () => void;
}) {
  if (!open) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 px-4 py-6 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-3xl border border-slate-200 bg-white p-6 shadow-[0_30px_80px_-24px_rgba(15,23,42,0.5)]">
        <div className="flex items-start justify-between gap-4">
          <h3 className="text-xl font-semibold tracking-tight text-slate-950">{title}</h3>
          {onClose ? (
            <button
              type="button"
              className="rounded-full border border-slate-200 bg-white px-3 py-1 text-sm font-medium text-slate-500 transition hover:border-slate-300 hover:text-slate-700"
              onClick={onClose}
            >
              Close
            </button>
          ) : null}
        </div>
        <div className="mt-4 text-sm leading-6 text-slate-600">{children}</div>
        {actions ? <div className="mt-6 flex flex-wrap justify-end gap-3">{actions}</div> : null}
      </div>
    </div>
  );
}

function Spinner() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-5 w-5 animate-spin text-slate-500">
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeOpacity="0.2" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="3" />
    </svg>
  );
}

export default function SyncPage() {
  const [dateFrom, setDateFrom] = useState<Date | null>(null);
  const [dateTo, setDateTo] = useState<Date | null>(null);
  const [orders, setOrders] = useState<UnifyOrderPreview[]>([]);
  const [customers, setCustomers] = useState<Record<string, CustomerSummary>>({});
  const [message, setMessage] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [exportResult, setExportResult] = useState<SyncResult | null>(null);
  const [selectedOrderIds, setSelectedOrderIds] = useState<Set<string>>(new Set());
  const [lastFetchedRange, setLastFetchedRange] = useState<DateRange | null>(null);
  const [fetchSummary, setFetchSummary] = useState<FetchSummary | null>(null);
  const [expandedOrderIds, setExpandedOrderIds] = useState<Set<string>>(new Set());
  const [orderDetails, setOrderDetails] = useState<Record<string, UnifyOrderDetail | undefined>>({});
  const [orderDetailsLoading, setOrderDetailsLoading] = useState<Record<string, boolean>>({});
  const [syncConfirmOpen, setSyncConfirmOpen] = useState(false);
  const [syncRunning, setSyncRunning] = useState(false);
  const [pendingSyncOrderIds, setPendingSyncOrderIds] = useState<string[]>([]);

  const dedupeOrders = (items: UnifyOrderPreview[]) => {
    const seen = new Set<string>();
    return items.filter((order) => {
      if (seen.has(order.order_id)) {
        return false;
      }
      seen.add(order.order_id);
      return true;
    });
  };

  const clearLoadedState = () => {
    setExportResult(null);
    setOrders([]);
    setCustomers({});
    setSelectedOrderIds(new Set());
    setFetchSummary(null);
    setExpandedOrderIds(new Set());
    setOrderDetails({});
    setOrderDetailsLoading({});
  };

  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      if (event.key === HISTORY_RESET_SIGNAL_KEY) {
        clearLoadedState();
        setMessage("Sync state refreshed after a history reset");
      }
    };

    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, []);

  const getResultByOrderId = (orderId: string) => exportResult?.details?.find((item) => item.order_id === orderId);

  const getOrderStatus = (order: UnifyOrderPreview): OrderStatus => {
    if (order.already_exported) {
      return "already_synced";
    }
    if (order.preview_status && order.preview_status !== "ready") return "failed";
    const result = getResultByOrderId(order.order_id);
    if (result?.status === "created") return "synced";
    if (result?.status === "failed") return "failed";
    if (result?.status === "skipped") return "already_synced";
    return "ready";
  };

  const readyOrderIds = orders
    .filter((order) => !order.already_exported && getOrderStatus(order) === "ready")
    .map((order) => order.order_id);
  const exportedCount = orders.filter((order) => order.already_exported).length;
  const previewOrderCount = orders.length;
  const readyPreviewCount = orders.filter((order) => order.preview_status === "ready").length;
  const blockedInRangePreviewCount = orders.filter((order) => order.preview_status !== "ready").length;
  const droppedOutOfRangeCount = fetchSummary?.dropped_out_of_range_count ?? 0;

  const describeError = (error: unknown) => {
    if (error instanceof DOMException && error.name === "AbortError") {
      return "Fetch orders timed out after 95 seconds";
    }
    if (error instanceof Error) {
      return error.message;
    }
    return "Unknown error while fetching orders";
  };

  const setSelectedFrom = (ids: string[]) => {
    setSelectedOrderIds(new Set(ids));
  };

  const isMeaningfulCustomerLabel = (value?: string | null) => {
    if (!value) {
      return false;
    }
    const trimmed = value.trim();
    return trimmed.length > 0 && /[A-Za-z]/.test(trimmed);
  };

  const resolveCustomerDisplayName = (order: UnifyOrderPreview) =>
    [
      order.buyer_name,
      order.customer_name,
      order.buyer_id ? `Customer ${order.buyer_id}` : null,
      `Customer ${order.order_id}`,
    ].find((candidate) => isMeaningfulCustomerLabel(candidate)) || `Customer ${order.order_id}`;

  const getBuyerDisplayName = (order: UnifyOrderPreview) => resolveCustomerDisplayName(order);
  const getCustomerDisplayName = (order: UnifyOrderPreview) => resolveCustomerDisplayName(order);
  const getDeliveryLabel = (order: UnifyOrderPreview | UnifyOrderDetail) => order.delivery_date || order.order_date || "";
  const getSyncSummary = (orderIds: string[]) => {
    const selectedOrders = orders.filter((order) => orderIds.includes(order.order_id));
    const selectedCustomers = new Set(selectedOrders.map((order) => getBuyerDisplayName(order)));
    const total = selectedOrders.reduce((sum, order) => sum + Number(order.total || 0), 0);

    return {
      orderCount: selectedOrders.length,
      customerCount: selectedCustomers.size,
      total,
    };
  };
  const updateFromDate = (nextFrom: Date) => {
    const normalized = normalizeRange(nextFrom, dateTo);
    setDateFrom(normalized.from);
    setDateTo(normalized.to);
  };
  const updateToDate = (nextTo: Date) => {
    const normalized = normalizeRange(dateFrom, nextTo);
    setDateFrom(normalized.from);
    setDateTo(normalized.to);
  };
  const getPreviewBadgeLabel = (order: UnifyOrderPreview) => {
    if (order.already_exported) {
      return "Synced";
    }
    if (order.preview_status === "ready") {
      return "Ready to sync";
    }
    if (order.status === "new") {
      return "Error";
    }
    return "Blocked";
  };

  const getBadgeClasses = (order: UnifyOrderPreview) => {
    if (order.already_exported) {
      return "border-emerald-200 bg-emerald-50 text-emerald-700";
    }
    if (order.preview_status === "ready") {
      return "border-sky-200 bg-sky-50 text-sky-700";
    }
    if (order.preview_status === "mapping_issue") {
      return "border-rose-200 bg-rose-50 text-rose-700";
    }
    if (order.preview_status && order.preview_status !== "ready") {
      return "border-rose-200 bg-rose-50 text-rose-700";
    }
    return "border-slate-200 bg-slate-100 text-slate-600";
  };

  const toggleSelected = (orderId: string) => {
    setSelectedOrderIds((current) => {
      const next = new Set(current);
      if (next.has(orderId)) {
        next.delete(orderId);
      } else {
        next.add(orderId);
      }
      return next;
    });
  };

  const loadOrderDetails = async (orderId: string) => {
    if (orderDetails[orderId] || orderDetailsLoading[orderId]) {
      return;
    }

    const requestStartedAt = performance.now();
    const endpoint = `/api/unify/order-preview/${orderId}`;
    setOrderDetailsLoading((current) => ({ ...current, [orderId]: true }));
    try {
      console.info("[Sync] Detail load requested", { orderId });
      const res = await fetch(endpoint);
      const data = await parseApiResponse<UnifyOrderDetail>(res, "load-order-details", endpoint);
      console.info("[Sync] Detail load received", {
        orderId,
        lineCount: data.lines?.length ?? 0,
        durationMs: performance.now() - requestStartedAt,
      });
      setOrderDetails((current) => ({ ...current, [orderId]: data }));
      setExpandedOrderIds((current) => {
        const next = new Set(current);
        next.add(orderId);
        return next;
      });
    } catch (error) {
      console.error("[Sync] Order detail fetch failed", {
        action: "load-order-details",
        endpoint,
        orderId,
        error,
      });
      setMessage(`Error loading order ${orderId}: ${describeError(error)}`);
    } finally {
      setOrderDetailsLoading((current) => ({ ...current, [orderId]: false }));
    }
  };

  const toggleOrderExpansion = async (orderId: string) => {
    const isExpanded = expandedOrderIds.has(orderId);
    if (isExpanded) {
      setExpandedOrderIds((current) => {
        const next = new Set(current);
        next.delete(orderId);
        return next;
      });
      return;
    }

    if (orderDetails[orderId]) {
      setExpandedOrderIds((current) => {
        const next = new Set(current);
        next.add(orderId);
        return next;
      });
      return;
    }

    await loadOrderDetails(orderId);
  };

  const fetchOrders = async () => {
    if (!dateFrom || !dateTo) {
      setMessage("Provide both from and to dates");
      return;
    }

    const clickStartedAt = performance.now();
    console.info("[Sync] Fetch orders clicked", { dateFrom, dateTo, stage: "button_clicked" });
    setLoading(true);
    setMessage("");
    clearLoadedState();
    setLastFetchedRange({ from: dateFrom, to: dateTo });

    const isoDateFrom = toISODate(dateFrom);
    const isoDateTo = toISODate(dateTo);
    console.info("[Sync] Fetch orders payload", { date_from: isoDateFrom, date_to: isoDateTo, stage: "frontend_payload_sent" });

    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    const endpoint = "/api/unify/fetch-orders";
    try {
      const requestStartedAt = performance.now();
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date_from: isoDateFrom, date_to: isoDateTo }),
        signal: controller.signal,
      });
      console.info("[Sync] Fetch orders proxy returned", {
        status: res.status,
        durationMs: performance.now() - requestStartedAt,
      });

      const data = await parseApiResponse<any>(res, "fetch-orders", endpoint);
      const responseOrders = dedupeOrders(data.orders ?? []);
      const responseReadyCount = responseOrders.filter((order: UnifyOrderPreview) => order.preview_status === "ready").length;
      const responseBlockedInRangeCount = responseOrders.filter((order: UnifyOrderPreview) => order.preview_status !== "ready").length;
      const responseDroppedOutOfRangeCount = data?.debug_summary?.dropped_out_of_range_count ?? 0;
      console.info("[Sync] Response received", {
        totalOrders: data.total_orders,
        responseOrdersCount: responseOrders.length,
        readyCount: responseReadyCount,
        blockedInRangeCount: responseBlockedInRangeCount,
        droppedOutOfRangeCount: responseDroppedOutOfRangeCount,
        debugSummary: data.debug_summary,
        durationMs: performance.now() - clickStartedAt,
      });
      setFetchSummary(data.debug_summary ?? null);
      const uniqueCustomers = responseOrders.reduce((acc: Record<string, CustomerSummary>, order: UnifyOrderPreview) => {
        const key = getBuyerDisplayName(order);
        if (!acc[key]) {
          acc[key] = { order_count: 0, total: 0 };
        }
        acc[key].order_count += 1;
        acc[key].total += Number(order.total || 0);
        return acc;
      }, {});

      setOrders(responseOrders);
      setCustomers(uniqueCustomers);
      setSelectedOrderIds(new Set());
      console.info("[Sync] State updated", {
        orders: responseOrders.length,
        customers: Object.keys(uniqueCustomers).length,
        durationMs: performance.now() - clickStartedAt,
      });
      setMessage(
        responseOrders.length === 0
          ? "0 orders found for the selected delivery date range"
          : `Fetched ${responseOrders.length} unique orders for ${Object.keys(uniqueCustomers).length} customers on delivery date`,
      );
    } catch (error) {
      const text = describeError(error);
      console.error("[Sync] Fetch orders request failed", {
        action: "fetch-orders",
        endpoint,
        error,
        message: text,
      });
      setMessage(`Error: ${text}`);
    } finally {
      window.clearTimeout(timeoutId);
      setLoading(false);
    }
  };

  const executeExport = async (orderIds: string[], actionLabel = "sync-selected-orders") => {
    if (!dateFrom || !dateTo) {
      setMessage("Provide both from and to dates");
      return;
    }

    if (!orderIds.length) {
      setMessage("Select at least one order to sync");
      return;
    }

    setLoading(true);
    setSyncRunning(true);
    setMessage("");
    const endpoint = "/api/export/run";
    try {
      const requestStartedAt = performance.now();
      const isoDateFrom = toISODate(dateFrom);
      const isoDateTo = toISODate(dateTo);
      console.info("[Sync] Export payload", { date_from: isoDateFrom, date_to: isoDateTo, order_ids: orderIds });
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date_from: isoDateFrom, date_to: isoDateTo, order_ids: orderIds }),
      });
      const data = await parseApiResponse<SyncResult>(res, actionLabel, endpoint);
      console.info("[Sync] Export response received", {
        orderIds,
        status: data.status,
        durationMs: performance.now() - requestStartedAt,
      });
      setExportResult(data);
      const syncedOrderIds = new Set(
        (data.details ?? [])
          .filter((detail) => detail.status === "created" || detail.status === "skipped")
          .map((detail) => detail.order_id),
      );
      if (syncedOrderIds.size === 0 && orderIds.length === 1) {
        syncedOrderIds.add(orderIds[0]);
      }
      if (syncedOrderIds.size > 0) {
        setOrders((currentOrders) =>
          currentOrders.map((order) => (syncedOrderIds.has(order.order_id) ? { ...order, already_exported: true } : order)),
        );
        setSelectedOrderIds((currentSelected) => {
          const next = new Set(currentSelected);
          syncedOrderIds.forEach((orderId) => next.delete(orderId));
          return next;
        });
      }
      setMessage(formatSyncResultMessage(data));
    } catch (error) {
      console.error("[Sync] Export request failed", {
        action: actionLabel,
        endpoint,
        orderIds,
        error,
      });
      setMessage(`Error: ${describeError(error)}`);
    } finally {
      setLoading(false);
      setSyncRunning(false);
    }
  };

  const requestExport = (orderIds?: string[]) => {
    const syncIds = orderIds ?? Array.from(selectedOrderIds);
    if (!syncIds.length) {
      setMessage("Select at least one order to sync");
      return;
    }
    setPendingSyncOrderIds(syncIds);
    setSyncConfirmOpen(true);
  };

  const totalRevenue = orders.reduce((sum, order) => sum + Number(order.total || 0), 0);
  const selectedReadyCount = orders.filter((order) => selectedOrderIds.has(order.order_id) && getOrderStatus(order) === "ready").length;
  const pendingSyncSummary = getSyncSummary(pendingSyncOrderIds);

  return (
    <AppShell>
      <main className="space-y-8">
      <section className="space-y-8">
        <div className="rounded-3xl border border-slate-200/80 bg-white/90 p-8 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)] backdrop-blur">
          <div className="flex flex-col gap-2">
            <p className="text-xs font-semibold uppercase tracking-[0.28em] text-slate-500">Sync workspace</p>
            <h2 className="text-3xl font-semibold tracking-tight text-slate-950">Invoice automation for Unify {"->"} Zoho</h2>
            <p className="max-w-3xl text-sm leading-6 text-slate-600">
              Fetch orders, review the invoice preview, and sync each Unify order into its own Zoho draft invoice.
            </p>
          </div>

          <div className="mt-8 grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
            <DatePickerField label="Date From" value={dateFrom} onSelect={updateFromDate} />
            <DatePickerField label="Date To" value={dateTo} initialMonth={dateFrom ?? undefined} onSelect={updateToDate} />
            <div className="flex items-end">
              <button
                className="w-full rounded-xl bg-slate-950 px-4 py-2.5 font-medium text-white shadow-[0_12px_24px_-16px_rgba(15,23,42,0.9)] transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-60"
                onClick={fetchOrders}
                disabled={loading}
              >
                {loading ? "Working..." : "Fetch orders"}
              </button>
            </div>
            <div className="flex items-end">
              <button
                className="w-full rounded-xl bg-emerald-600 px-4 py-2.5 font-medium text-white shadow-[0_12px_24px_-16px_rgba(16,185,129,0.9)] transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-60"
                onClick={() => requestExport()}
                disabled={loading || selectedReadyCount === 0}
              >
                {loading ? "Working..." : "Sync selected orders"}
              </button>
            </div>
          </div>

          {message && (
            <div
              className={[
                "mt-5 rounded-xl px-4 py-3 text-sm",
                message.startsWith("Error:")
                  ? "border border-rose-200 bg-rose-50 text-rose-700"
                  : "border border-slate-200 bg-slate-50 text-slate-700",
              ].join(" ")}
            >
              {message}
            </div>
          )}
        </div>

        {fetchSummary && (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            <div className="rounded-2xl border border-slate-200/80 bg-white p-5 shadow-[0_16px_40px_-28px_rgba(15,23,42,0.35)]">
              <div className="text-xs font-medium uppercase tracking-[0.22em] text-slate-500">Ready to sync</div>
              <div className="mt-2 text-4xl font-semibold tracking-tight text-slate-950">{readyPreviewCount}</div>
            </div>
            <div className="rounded-2xl border border-slate-200/80 bg-white p-5 shadow-[0_16px_40px_-28px_rgba(15,23,42,0.35)]">
              <div className="text-xs font-medium uppercase tracking-[0.22em] text-slate-500">Blocked</div>
              <div className="mt-2 text-4xl font-semibold tracking-tight text-slate-950">{blockedInRangePreviewCount}</div>
            </div>
            <div className="rounded-2xl border border-slate-200/80 bg-white p-5 shadow-[0_16px_40px_-28px_rgba(15,23,42,0.35)]">
              <div className="text-xs font-medium uppercase tracking-[0.22em] text-slate-500">Synced</div>
              <div className="mt-2 text-4xl font-semibold tracking-tight text-slate-950">{exportedCount}</div>
            </div>
          </div>
        )}

        {(orders.length > 0 || exportResult) && (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
            <div className="rounded-2xl border border-slate-200/80 bg-white p-5 shadow-[0_16px_40px_-28px_rgba(15,23,42,0.35)]">
              <div className="text-xs font-medium uppercase tracking-[0.22em] text-slate-500">Total Orders</div>
              <div className="mt-2 text-4xl font-semibold tracking-tight text-slate-950">{previewOrderCount}</div>
            </div>
            <div className="rounded-2xl border border-slate-200/80 bg-white p-5 shadow-[0_16px_40px_-28px_rgba(15,23,42,0.35)]">
              <div className="text-xs font-medium uppercase tracking-[0.22em] text-slate-500">Customers</div>
              <div className="mt-2 text-4xl font-semibold tracking-tight text-slate-950">{Object.keys(customers).length}</div>
            </div>
            <div className="rounded-2xl border border-slate-200/80 bg-white p-5 shadow-[0_16px_40px_-28px_rgba(15,23,42,0.35)]">
              <div className="text-xs font-medium uppercase tracking-[0.22em] text-slate-500">Revenue</div>
              <div className="mt-2 text-4xl font-semibold tracking-tight text-slate-950">{formatCurrencyEur(totalRevenue)}</div>
            </div>
            <div className="rounded-2xl border border-slate-200/80 bg-white p-5 shadow-[0_16px_40px_-28px_rgba(15,23,42,0.35)]">
              <div className="text-xs font-medium uppercase tracking-[0.22em] text-slate-500">Invoices Created</div>
              <div className="mt-2 text-4xl font-semibold tracking-tight text-slate-950">{exportResult?.created ?? 0}</div>
            </div>
          </div>
        )}

        {orders.length > 0 && (
          <div className="rounded-3xl border border-slate-200/80 bg-white p-6 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)]">
            <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
              <div>
                <h3 className="text-lg font-semibold tracking-tight text-slate-950">Selection</h3>
                <p className="text-sm text-slate-500">
                  {previewOrderCount} fetched orders, {selectedOrderIds.size} selected, {exportedCount} Synced
                </p>
                {lastFetchedRange && (
                  <p className="mt-1 text-xs text-slate-400">
                    Delivery date range: {formatDateDisplay(lastFetchedRange.from)} to {formatDateDisplay(lastFetchedRange.to)}
                  </p>
                )}
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
                  onClick={() => setSelectedFrom(readyOrderIds)}
                  disabled={readyOrderIds.length === 0 || loading}
                >
                  Select all
                </button>
                <button
                  className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
                  onClick={() => setSelectedFrom([])}
                  disabled={loading || selectedOrderIds.size === 0}
                >
                  Unselect all
                </button>
                <div className="rounded-xl bg-slate-100 px-4 py-2 text-sm text-slate-700">
                  Selected orders: <span className="font-semibold text-slate-900">{selectedOrderIds.size}</span>
                </div>
              </div>
            </div>
          </div>
        )}

        {orders.length > 0 && (
          <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
            <div className="space-y-4 xl:col-span-2">
              <div className="rounded-3xl border border-slate-200/80 bg-white p-6 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)]">
                <div className="flex items-end justify-between gap-4">
                  <div>
                    <h3 className="text-lg font-semibold tracking-tight text-slate-950">Order preview</h3>
                    <p className="text-sm text-slate-500">Each card below represents one unique Unify order.</p>
                  </div>
                  <div className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-sm text-slate-500">{previewOrderCount} unique orders</div>
                </div>

                <div className="mt-5 space-y-4">
                  {orders.map((order) => {
                    const detail = orderDetails[order.order_id];
                    const isExpanded = expandedOrderIds.has(order.order_id);
                    const isLoadingDetail = orderDetailsLoading[order.order_id];
                    return (
                      <article key={order.order_id} className="rounded-2xl border border-slate-200 bg-gradient-to-br from-white to-slate-50 p-5 shadow-[0_10px_30px_-24px_rgba(15,23,42,0.35)] transition hover:shadow-[0_20px_45px_-28px_rgba(15,23,42,0.3)]">
                        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-3">
                              <label className="inline-flex items-center gap-2 text-sm text-slate-700">
                                <input
                                  type="checkbox"
                                  className="h-4 w-4 rounded border-slate-300 text-slate-900 focus:ring-slate-400 disabled:cursor-not-allowed"
                                  checked={selectedOrderIds.has(order.order_id)}
                                  disabled={order.already_exported || loading || getOrderStatus(order) !== "ready"}
                                  onChange={() => toggleSelected(order.order_id)}
                                />
                                Select
                              </label>
                              <span
                                className={[
                                  "rounded-full border px-3 py-1 text-xs font-semibold tracking-[0.16em]",
                                  getBadgeClasses(order),
                                ].join(" ")}
                              >
                                {getPreviewBadgeLabel(order)}
                              </span>
                            </div>
                            <p className="mt-3 text-xs font-medium uppercase tracking-[0.24em] text-slate-500">Order</p>
                            <h4 className="mt-1 text-2xl font-semibold tracking-tight text-slate-950">{order.order_id}</h4>
                            <p className="mt-1 text-sm font-medium text-slate-700">Customer: {getBuyerDisplayName(order)}</p>
                            {order.customer_name && getCustomerDisplayName(order) !== getBuyerDisplayName(order) && (
                              <p className="mt-1 text-xs font-light text-slate-400">Recipient: {getCustomerDisplayName(order)}</p>
                            )}
                            {order.delivery_address && <p className="mt-1 text-xs font-light text-slate-400">Delivery address: {order.delivery_address}</p>}
                            {order.buyer_id && <p className="mt-1 text-xs font-light text-slate-400">Buyer ID: {order.buyer_id}</p>}
                            <p className="mt-1 text-xs font-light text-slate-400">Delivery: {getDeliveryLabel(order)}</p>
                            {order.preview_reason && <p className="mt-1 text-xs text-slate-500">Reason: {order.preview_reason}</p>}
                          </div>
                          <div className="self-start rounded-2xl border border-slate-200 bg-white px-4 py-3 text-right shadow-sm">
                            <div className="text-xs font-medium uppercase tracking-[0.22em] text-slate-500">Total</div>
                            <div className="text-2xl font-semibold tracking-tight text-slate-950">{formatCurrencyEur(order.total)}</div>
                            <div className="text-sm text-slate-500">Excl. VAT</div>
                          </div>
                        </div>

                        <div className="mt-5 flex flex-wrap gap-2">
                          <button
                            className="rounded-xl bg-slate-950 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-60"
                            onClick={() => requestExport([order.order_id])}
                            disabled={loading || order.already_exported || getOrderStatus(order) !== "ready"}
                          >
                            Sync this order
                          </button>
                          <button
                            className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
                            onClick={() => toggleOrderExpansion(order.order_id)}
                            disabled={loading || isLoadingDetail}
                          >
                            {isExpanded ? "Hide items" : isLoadingDetail ? "Loading items..." : "View items"}
                          </button>
                        </div>

                        {isExpanded && (
                          <div className="mt-5 overflow-visible rounded-2xl border border-slate-200 bg-white">
                            {!detail ? (
                              <div className="px-4 py-4 text-sm text-slate-500">Loading order items...</div>
                            ) : detail.lines.length === 0 ? (
                              <div className="px-4 py-4 text-sm text-slate-500">No line items returned for this order.</div>
                            ) : (
                              <table className="w-full text-left text-sm">
                                <thead className="bg-slate-50 text-slate-600">
                                  <tr>
                                    <th className="px-4 py-3 font-medium">Product</th>
                                    <th className="px-4 py-3 font-medium">Quantity</th>
                                    <th className="px-4 py-3 font-medium">Unit price</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {detail.lines.map((line, idx) => (
                                    <tr key={`${order.order_id}-${idx}`} className="border-t border-slate-100 transition hover:bg-slate-50">
                                      <td className="px-4 py-3">
                                        <div className="font-medium text-slate-900">{line.item_name}</div>
                                        <div className="text-xs text-slate-500">{line.item_sku}</div>
                                      </td>
                                      <td className="px-4 py-3 text-slate-700">{line.quantity}</td>
                                      <td className="px-4 py-3 text-slate-700">{formatCurrencyEur(line.price)}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            )}
                          </div>
                        )}
                      </article>
                    );
                  })}
                </div>
              </div>
            </div>

            <aside className="space-y-4">
              <div className="rounded-3xl border border-slate-200/80 bg-white p-6 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)]">
                <h3 className="text-lg font-semibold tracking-tight text-slate-950">Customer summary</h3>
                <p className="text-sm text-slate-500">Orders grouped only for display.</p>
                <div className="mt-4 space-y-3">
                  {Object.entries(customers).map(([customer, summary]) => (
                    <div key={customer} className="rounded-2xl border border-slate-200 bg-slate-50 p-4 shadow-sm">
                      <div className="text-sm font-medium text-slate-950">{customer}</div>
                      <div className="mt-1 text-sm text-slate-600">{summary.order_count} order{summary.order_count === 1 ? "" : "s"}</div>
                      <div className="mt-1 text-sm font-semibold text-slate-950">{formatCurrencyEur(summary.total)}</div>
                    </div>
                  ))}
                </div>
              </div>

              {exportResult && (
                <div className="rounded-3xl border border-slate-200/80 bg-white p-6 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)]">
                  <h3 className="text-lg font-semibold tracking-tight text-slate-950">Sync result</h3>
                  {exportResult.errors && <div className="mt-3 whitespace-pre-wrap rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{exportResult.errors}</div>}
                  <div className="mt-4 space-y-3 text-sm">
                    <div className="flex items-center justify-between rounded-2xl bg-slate-50 px-4 py-3">
                      <span className="text-slate-600">Created</span>
                      <span className="font-semibold text-slate-900">{exportResult.created ?? 0}</span>
                    </div>
                    <div className="flex items-center justify-between rounded-2xl bg-slate-50 px-4 py-3">
                      <span className="text-slate-600">Skipped</span>
                      <span className="font-semibold text-slate-900">{exportResult.skipped ?? 0}</span>
                    </div>
                    <div className="flex items-center justify-between rounded-2xl bg-slate-50 px-4 py-3">
                      <span className="text-slate-600">Failed</span>
                      <span className="font-semibold text-slate-900">{exportResult.failed ?? 0}</span>
                    </div>
                  </div>
                </div>
              )}
            </aside>
          </div>
        )}

        {previewOrderCount === 0 && lastFetchedRange && !loading && !message.startsWith("Error:") && (
          <div className="rounded-3xl border border-dashed border-slate-300 bg-white/80 p-6 text-sm text-slate-600">
            No orders matched the selected delivery date range ({formatDateDisplay(lastFetchedRange.from)} to {formatDateDisplay(lastFetchedRange.to)}).
            The Sync page filters by delivery date, not order creation date.
            {fetchSummary && (
              <div className="mt-2 text-xs text-slate-400">
                Preview count: {previewOrderCount}, ready: {readyPreviewCount}, blocked in range: {blockedInRangePreviewCount}
              </div>
            )}
          </div>
        )}

        {exportResult?.details && exportResult.details.length > 0 && (
          <div className="rounded-3xl border border-slate-200/80 bg-white p-6 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)]">
            <h3 className="text-lg font-semibold tracking-tight text-slate-950">Per-order result log</h3>
            <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {exportResult.details.map((item) => (
                <div key={item.order_id} className="rounded-2xl border border-slate-200 bg-slate-50 p-4 shadow-sm">
                  <div className="flex items-center justify-between gap-3">
                    <div className="font-medium text-slate-950">{item.order_id}</div>
                    <div className="rounded-full bg-white px-3 py-1 text-xs font-semibold uppercase tracking-[0.15em] text-slate-600">
                      {item.status}
                    </div>
                  </div>
                  <div className="mt-2 text-sm text-slate-600">{item.message}</div>
                  {item.zoho_invoice_id && <div className="mt-2 text-xs text-slate-500">Zoho invoice: {item.zoho_invoice_id}</div>}
                </div>
              ))}
            </div>
          </div>
        )}

        <Modal
          open={syncConfirmOpen}
          title="Confirm sync"
          onClose={() => {
            setSyncConfirmOpen(false);
            setPendingSyncOrderIds([]);
          }}
          actions={
            <>
              <button
                type="button"
                className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50"
                onClick={() => {
                  setSyncConfirmOpen(false);
                  setPendingSyncOrderIds([]);
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                className="rounded-xl bg-slate-950 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-60"
                onClick={() => {
                  setSyncConfirmOpen(false);
                  void executeExport(pendingSyncOrderIds);
                }}
                disabled={syncRunning}
              >
                Confirm sync
              </button>
            </>
          }
        >
          <p>Are you sure you want to sync the selected invoices?</p>
          <p className="mt-2">This will create draft invoices in Zoho.</p>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
              <div className="text-xs font-medium uppercase tracking-[0.22em] text-slate-500">Orders</div>
              <div className="mt-2 text-sm font-medium text-slate-950">{pendingSyncSummary.orderCount}</div>
            </div>
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
              <div className="text-xs font-medium uppercase tracking-[0.22em] text-slate-500">Customers</div>
              <div className="mt-2 text-sm font-medium text-slate-950">{pendingSyncSummary.customerCount}</div>
            </div>
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 sm:col-span-2">
              <div className="text-xs font-medium uppercase tracking-[0.22em] text-slate-500">Total excl. VAT</div>
              <div className="mt-2 text-sm font-medium text-slate-950">{formatCurrencyEur(pendingSyncSummary.total)}</div>
            </div>
          </div>
        </Modal>

        <Modal open={syncRunning} title="Syncing invoices" onClose={undefined}>
          <div className="flex items-start gap-3">
            <Spinner />
            <div>
              <p>Please wait while Zconnect creates draft invoices in Zoho.</p>
            </div>
          </div>
        </Modal>
      </section>
      </main>
    </AppShell>
  );
}
