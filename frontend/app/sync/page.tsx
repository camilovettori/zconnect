"use client";

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import * as XLSX from "xlsx";
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

type UnifiedOrder = UnifyOrderPreview & {
  order_date?: string;
  lines?: OrderLine[];
  total_net_amount?: number;
  total_vat_amount?: number;
  total_delivery_fee?: number;
};

type UnifyOrderDetail = UnifyOrderPreview & {
  order_date: string;
  lines: OrderLine[];
  total_net_amount?: number;
  total_vat_amount?: number;
  total_delivery_fee?: number;
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
  source?: string;
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
type SyncSource = "api" | "csv";
type DateRange = { from: Date; to: Date };

const FETCH_TIMEOUT_MS = 95000;
const HISTORY_RESET_SIGNAL_KEY = "zconnect:last-selected-run-reset-at";
const SUPPORTED_UPLOAD_EXTENSIONS = [".csv", ".tsv", ".txt", ".xlsx", ".xls"];

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

function UploadCloudIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-6 w-6">
      <path
        d="M7.5 18.5h9A4.5 4.5 0 0 0 18 9.65a5.5 5.5 0 0 0-10.63-1.49A3.75 3.75 0 0 0 7.5 18.5Zm4.5-9v6m0-6-2.2 2.2M12 9.5l2.2 2.2"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.6"
      />
    </svg>
  );
}

function DocumentIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-4 w-4">
      <path
        d="M7 3.5h6.4L18.5 8v12A2.5 2.5 0 0 1 16 22.5H7A2.5 2.5 0 0 1 4.5 20V6A2.5 2.5 0 0 1 7 3.5Z"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.6"
      />
      <path d="M13.5 3.5V8H18" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" />
    </svg>
  );
}

function CheckBadgeIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-4 w-4">
      <path
        d="M12 2.9 14.7 5l3.4.5 1.4 3.1-.3 3.5 2.1 2.8-2.1 2.8.3 3.5-3.4.5-2.7 2.1-2.7-2.1-3.4-.5-.3-3.5-2.1-2.8 2.1-2.8-.3-3.5L8.6 5 12 2.9Z"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.4"
      />
      <path d="m8.8 12.4 2.1 2.1 4.4-4.8" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" />
    </svg>
  );
}

function AlertIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-4 w-4">
      <path
        d="M12 3.5 21 19.5H3L12 3.5Z"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.6"
      />
      <path d="M12 9v4.5" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" />
      <path d="M12 16.9h.01" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.2" />
    </svg>
  );
}

export default function SyncPage() {
  const [dateFrom, setDateFrom] = useState<Date | null>(null);
  const [dateTo, setDateTo] = useState<Date | null>(null);
  const [orders, setOrders] = useState<UnifiedOrder[]>([]);
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
  const [activeSource, setActiveSource] = useState<SyncSource | null>(null);
  const [csvFileName, setCsvFileName] = useState<string>("");
  const [csvLoading, setCsvLoading] = useState(false);
  const [csvDragActive, setCsvDragActive] = useState(false);
  const csvInputRef = useRef<HTMLInputElement | null>(null);

  const dedupeOrders = (items: UnifiedOrder[]) => {
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
    setActiveSource(null);
    setCsvFileName("");
    setCsvDragActive(false);
  };

  const openCsvPicker = () => {
    csvInputRef.current?.click();
  };

  const handleCsvSelection = (file?: File | null) => {
    if (!file) {
      return;
    }
    void previewCsvFile(file);
  };

  const csvFileExtension = csvFileName ? csvFileName.split(".").pop()?.toUpperCase() || "FILE" : "FILE";
  const csvUploadSucceeded = activeSource === "csv" && orders.length > 0 && !csvLoading;
  const csvUploadMessage = message.startsWith("Error:") && (csvFileName || csvLoading || activeSource === "csv") ? message : "";
  const csvUploadHelperState = csvLoading
    ? "Parsing and previewing your file."
    : csvUploadSucceeded
      ? `Preview ready with ${orders.length} order${orders.length === 1 ? "" : "s"}.`
      : csvFileName
        ? "File selected. You can replace it or clear the preview."
        : "Upload a Unify report to preview and sync orders without using the date-based fetch.";

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

  const getOrderStatus = (order: UnifiedOrder): OrderStatus => {
    if (order.already_exported) {
      return "already_synced";
    }
    if (order.preview_status && !["ready", "mapping_issue"].includes(order.preview_status)) return "failed";
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

  const resolveCustomerDisplayName = (order: UnifiedOrder) =>
    [
      order.buyer_name,
      order.customer_name,
      order.buyer_id ? `Customer ${order.buyer_id}` : null,
      `Customer ${order.order_id}`,
    ].find((candidate) => isMeaningfulCustomerLabel(candidate)) || `Customer ${order.order_id}`;

  const getBuyerDisplayName = (order: UnifiedOrder) => resolveCustomerDisplayName(order);
  const getCustomerDisplayName = (order: UnifiedOrder) => resolveCustomerDisplayName(order);
  const getDeliveryLabel = (order: UnifiedOrder | UnifyOrderDetail) => order.delivery_date || order.order_date || "";
  const getOrderDetail = (order: UnifiedOrder) => {
    if (order.lines && order.lines.length > 0) {
      return {
        ...order,
        order_date: order.order_date || order.delivery_date || "",
        lines: order.lines,
      } as UnifyOrderDetail;
    }
    return orderDetails[order.order_id];
  };
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
  const getPreviewBadgeLabel = (order: UnifiedOrder) => {
    if (order.already_exported) {
      return "Synced";
    }
    if (order.preview_status === "ready") {
      return "Ready to sync";
    }
    if (order.preview_status === "mapping_issue") {
      return "Needs review";
    }
    if (order.status === "new") {
      return "Error";
    }
    return "Blocked";
  };

  const getBadgeClasses = (order: UnifiedOrder) => {
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

    const existingOrder = orders.find((order) => order.order_id === orderId);
    if (existingOrder?.lines && existingOrder.lines.length > 0) {
      setOrderDetails((current) => ({
        ...current,
        [orderId]: {
          ...existingOrder,
          order_date: existingOrder.order_date || existingOrder.delivery_date || "",
          lines: existingOrder.lines,
        } as UnifyOrderDetail,
      }));
      setExpandedOrderIds((current) => {
        const next = new Set(current);
        next.add(orderId);
        return next;
      });
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

    const existingOrder = orders.find((order) => order.order_id === orderId);
    if (existingOrder?.lines && existingOrder.lines.length > 0) {
      setOrderDetails((current) => ({
        ...current,
        [orderId]: {
          ...existingOrder,
          order_date: existingOrder.order_date || existingOrder.delivery_date || "",
          lines: existingOrder.lines,
        } as UnifyOrderDetail,
      }));
      setExpandedOrderIds((current) => {
        const next = new Set(current);
        next.add(orderId);
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
      setActiveSource("api");
      setCsvFileName("");
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

  const readFileAsText = (file: File) =>
    new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result ?? ""));
      reader.onerror = () => reject(new Error("Could not read the selected CSV file"));
      reader.readAsText(file, "utf-8");
    });

  const readExcelAsCsvText = async (file: File) => {
    const buffer = await file.arrayBuffer();
    const workbook = XLSX.read(buffer, { type: "array" });
    const firstSheetName = workbook.SheetNames[0];
    if (!firstSheetName) {
      throw new Error("The Excel file does not contain any sheets");
    }

    const worksheet = workbook.Sheets[firstSheetName];
    if (!worksheet) {
      throw new Error("The first sheet in the Excel file is empty");
    }

    return XLSX.utils.sheet_to_csv(worksheet, { FS: ",", blankrows: false });
  };

  const readUploadAsCsvText = async (file: File) => {
    const extension = `.${file.name.split(".").pop()?.toLowerCase() || ""}`;
    if (extension === ".xlsx" || extension === ".xls") {
      return readExcelAsCsvText(file);
    }
    return readFileAsText(file);
  };

  const previewCsvFile = async (file: File) => {
    const extension = `.${file.name.split(".").pop()?.toLowerCase() || ""}`;
    if (!SUPPORTED_UPLOAD_EXTENSIONS.includes(extension)) {
      setMessage("Please select a CSV or Excel file");
      return;
    }

    const endpoint = "/api/unify/preview-csv";
    setLoading(true);
    setCsvLoading(true);
    setMessage("");
    clearLoadedState();
    setCsvFileName(file.name);

    try {
      const fd = new FormData();
      fd.append("file", file, file.name);
      const requestStartedAt = performance.now();
      console.info("[Sync] CSV preview (upload) started", { fileName: file.name, size: file.size });
      const res = await fetch(endpoint, {
        method: "POST",
        body: fd,
      });
      const data = await parseApiResponse<any>(res, "preview-csv", endpoint);
      const responseOrders = dedupeOrders((data.orders ?? []) as UnifiedOrder[]);
      const uniqueCustomers = responseOrders.reduce((acc: Record<string, CustomerSummary>, order: UnifiedOrder) => {
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
      setFetchSummary(data.debug_summary ?? null);
      setActiveSource("csv");
      console.info("[Sync] CSV preview completed", {
        fileName: file.name,
        orders: responseOrders.length,
        customers: Object.keys(uniqueCustomers).length,
        durationMs: performance.now() - requestStartedAt,
        debugSummary: data.debug_summary,
      });
      setMessage(
        responseOrders.length === 0
          ? `No orders were parsed from ${file.name}`
          : `Loaded ${responseOrders.length} unique orders from ${file.name}`,
      );
    } catch (error) {
      const text = describeError(error);
      const errorMessage = resolveErrorMessage(error, text);
      console.error("[Sync] CSV preview failed", {
        action: "preview-csv",
        endpoint,
        fileName: file.name,
        error,
        message: errorMessage,
      });
      setMessage(`Error: ${errorMessage}`);
      setCsvFileName("");
    } finally {
      setLoading(false);
      setCsvLoading(false);
    }
  };

  const executeExport = async (orderIds: string[], actionLabel = "sync-selected-orders") => {
    if (!orderIds.length) {
      setMessage("Select at least one order to sync");
      return;
    }

    setLoading(true);
    setSyncRunning(true);
    setMessage("");
    let endpoint = "/api/export/run";
    try {
      const requestStartedAt = performance.now();
      let body: string;
      if (activeSource === "csv") {
        const exportOrders = orders
          .filter((order) => orderIds.includes(order.order_id))
          .map((order) => ({
            ...order,
            order_date: order.order_date || order.delivery_date || "",
            lines: (order.lines ?? orderDetails[order.order_id]?.lines ?? []).map((line) => ({ ...line })),
          }));
        endpoint = "/api/export/run-csv";
        body = JSON.stringify({
          orders: exportOrders,
          date_from: dateFrom ? toISODate(dateFrom) : undefined,
          date_to: dateTo ? toISODate(dateTo) : undefined,
        });
        console.info("[Sync] CSV export payload", {
          order_ids: orderIds,
          order_count: exportOrders.length,
        });
      } else {
        if (!dateFrom || !dateTo) {
          setMessage("Provide both from and to dates");
          return;
        }
        const isoDateFrom = toISODate(dateFrom);
        const isoDateTo = toISODate(dateTo);
        console.info("[Sync] Export payload", { date_from: isoDateFrom, date_to: isoDateTo, order_ids: orderIds });
        body = JSON.stringify({ date_from: isoDateFrom, date_to: isoDateTo, order_ids: orderIds });
      }

      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
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
  const syncablePreviewStatuses = new Set(["ready", "mapping_issue"]);
  const readyPreviewCount = orders.filter((order) => syncablePreviewStatuses.has(order.preview_status ?? "")).length;
  const blockedInRangePreviewCount = orders.filter((order) => !syncablePreviewStatuses.has(order.preview_status ?? "")).length;

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

          <div className="mt-6 grid grid-cols-1 gap-4 xl:grid-cols-[1.25fr_0.75fr]">
            <section
              className={[
                "relative overflow-hidden rounded-3xl border bg-white/95 shadow-[0_18px_50px_-32px_rgba(15,23,42,0.32)] transition",
                csvDragActive ? "border-emerald-300 ring-4 ring-emerald-100/70" : "border-slate-200",
              ].join(" ")}
              onDragEnter={(event) => {
                event.preventDefault();
                setCsvDragActive(true);
              }}
              onDragOver={(event) => {
                event.preventDefault();
                setCsvDragActive(true);
              }}
              onDragLeave={(event) => {
                event.preventDefault();
                setCsvDragActive(false);
              }}
              onDrop={(event) => {
                event.preventDefault();
                setCsvDragActive(false);
                const file = event.dataTransfer.files?.[0];
                handleCsvSelection(file);
              }}
              onClick={openCsvPicker}
              role="button"
              tabIndex={0}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  openCsvPicker();
                }
              }}
            >
              <div className="absolute inset-0 bg-[radial-gradient(circle_at_top_right,rgba(16,185,129,0.12),transparent_28%),linear-gradient(180deg,rgba(255,255,255,0.9)_0%,rgba(248,250,252,0.96)_100%)]" />
              <input
                ref={csvInputRef}
                type="file"
                accept=".csv,.tsv,.txt,.xlsx,.xls,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                className="hidden"
                disabled={loading || csvLoading}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  event.target.value = "";
                  handleCsvSelection(file);
                }}
              />

              <div className="relative p-6 sm:p-7">
                <div className="flex flex-col gap-5">
                  <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                    <div className="max-w-2xl">
                      <span
                        className={[
                          "inline-flex items-center rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.22em]",
                          activeSource === "csv"
                            ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                            : "border-slate-200 bg-white text-slate-500",
                        ].join(" ")}
                      >
                        {activeSource === "csv" ? "CSV active" : "Optional"}
                      </span>
                      <h3 className="mt-3 text-xl font-semibold tracking-tight text-slate-950">Upload CSV or Excel</h3>
                      <p className="mt-2 max-w-xl text-sm leading-6 text-slate-600">
                        Upload a Unify report to preview and sync orders without using the date-based fetch.
                      </p>
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <span className="inline-flex items-center rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500 shadow-sm">
                        Supports .csv, .xlsx, .xls
                      </span>
                      <span
                        className={[
                          "inline-flex items-center rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em]",
                          csvUploadSucceeded
                            ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                            : csvLoading
                              ? "border-amber-200 bg-amber-50 text-amber-700"
                              : "border-slate-200 bg-white text-slate-500",
                        ].join(" ")}
                      >
                        {csvLoading ? "Processing" : csvUploadSucceeded ? "Parsed" : "Ready"}
                      </span>
                    </div>
                  </div>

                  <div
                    className={[
                      "rounded-3xl border border-dashed p-6 shadow-sm transition sm:p-7",
                      csvDragActive
                        ? "border-emerald-300 bg-emerald-50/80 shadow-[0_16px_35px_-26px_rgba(16,185,129,0.35)]"
                        : csvLoading
                          ? "border-emerald-200 bg-white/90"
                          : csvUploadSucceeded
                            ? "border-emerald-200 bg-emerald-50/60"
                            : "border-slate-200 bg-white/95",
                    ].join(" ")}
                  >
                    <div className="flex flex-col gap-5 lg:flex-row lg:items-center lg:justify-between">
                      <div className="flex items-start gap-4">
                        <div
                          className={[
                            "flex h-14 w-14 shrink-0 items-center justify-center rounded-2xl border shadow-sm transition",
                            csvDragActive
                              ? "border-emerald-200 bg-emerald-600 text-white"
                              : csvUploadSucceeded
                                ? "border-emerald-200 bg-emerald-600 text-white"
                                : "border-slate-200 bg-slate-950 text-white",
                          ].join(" ")}
                        >
                          <UploadCloudIcon />
                        </div>
                        <div className="min-w-0 space-y-2">
                          <p className="text-base font-semibold tracking-tight text-slate-950">
                            {csvLoading
                              ? "Parsing file..."
                              : csvFileName
                                ? "Drop a new file to replace the current preview"
                                : "Drag and drop your file here"}
                          </p>
                          <p className="max-w-2xl text-sm leading-6 text-slate-600">
                            {csvUploadHelperState}
                          </p>
                          <div className="flex flex-wrap gap-2 pt-1 text-xs text-slate-500">
                            <span className="inline-flex items-center rounded-full border border-white/70 bg-white/90 px-3 py-1 font-medium shadow-sm">
                              Fast preview
                            </span>
                            <span className="inline-flex items-center rounded-full border border-white/70 bg-white/90 px-3 py-1 font-medium shadow-sm">
                              Same export pipeline
                            </span>
                            <span className="inline-flex items-center rounded-full border border-white/70 bg-white/90 px-3 py-1 font-medium shadow-sm">
                              No date fetch required
                            </span>
                          </div>
                        </div>
                      </div>

                      <div className="flex flex-col gap-3 sm:items-end">
                        <button
                          type="button"
                          className="inline-flex items-center justify-center rounded-2xl bg-slate-950 px-5 py-3 text-sm font-semibold text-white shadow-[0_12px_28px_-18px_rgba(15,23,42,0.95)] transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-60"
                          onClick={(event) => {
                            event.stopPropagation();
                            openCsvPicker();
                          }}
                          disabled={loading || csvLoading}
                        >
                          {csvLoading ? (
                            <>
                              <Spinner />
                              <span className="ml-2">Parsing...</span>
                            </>
                          ) : csvFileName ? (
                            "Replace file"
                          ) : (
                            "Upload CSV or Excel"
                          )}
                        </button>
                        <p className="text-xs leading-5 text-slate-400">
                          Click to browse or drop a file into this zone.
                        </p>
                      </div>
                    </div>

                    {csvLoading && (
                      <div className="mt-5 flex items-center gap-3 rounded-2xl border border-emerald-100 bg-white/90 px-4 py-3 text-sm text-emerald-700 shadow-sm">
                        <Spinner />
                        <div>
                          <div className="font-medium text-emerald-800">Generating preview</div>
                          <div className="text-xs text-emerald-600">Reading your Unify file and mapping the order lines.</div>
                        </div>
                      </div>
                    )}

                    {csvFileName && !csvLoading && (
                      <div className="mt-5 grid gap-3 lg:grid-cols-[1fr_auto]">
                        <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
                          <div className="flex items-center gap-2">
                            <DocumentIcon />
                            <span className="text-xs font-semibold uppercase tracking-[0.22em] text-slate-500">Selected file</span>
                          </div>
                          <div className="mt-2 truncate text-sm font-semibold text-slate-950">{csvFileName}</div>
                          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                            <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 font-semibold tracking-[0.14em] text-slate-700">
                              {csvFileExtension}
                            </span>
                            <span>{csvUploadSucceeded ? "Parsed successfully and ready to sync." : "Queued for preview."}</span>
                          </div>
                        </div>

                        <div className="flex flex-wrap gap-2 lg:justify-end">
                          <button
                            type="button"
                            className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
                            onClick={(event) => {
                              event.stopPropagation();
                              openCsvPicker();
                            }}
                            disabled={loading || csvLoading}
                          >
                            Replace file
                          </button>
                          <button
                            type="button"
                            className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
                            onClick={(event) => {
                              event.stopPropagation();
                              clearLoadedState();
                              setMessage("CSV preview cleared");
                            }}
                            disabled={loading || csvLoading || orders.length === 0}
                          >
                            Clear preview
                          </button>
                        </div>
                      </div>
                    )}

                    {csvUploadSucceeded && (
                      <div className="mt-5 flex items-start gap-3 rounded-2xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
                        <CheckBadgeIcon />
                        <div>
                          <div className="font-medium">Preview generated successfully</div>
                          <div className="mt-1 text-emerald-700">
                            You can review the orders below, sync now, or replace the file at any time.
                          </div>
                        </div>
                      </div>
                    )}

                    {csvUploadMessage && (
                      <div className="mt-5 flex items-start gap-3 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                        <AlertIcon />
                        <div>
                          <div className="font-medium">Upload failed</div>
                          <div className="mt-1 leading-6">{csvUploadMessage}</div>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </section>

            <aside className="rounded-3xl border border-slate-200/80 bg-white p-6 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)]">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <div className="text-xs font-semibold uppercase tracking-[0.22em] text-slate-500">Mode</div>
                  <div className="mt-2 text-lg font-semibold tracking-tight text-slate-950">Choose your source</div>
                </div>
                {activeSource === "csv" ? (
                  <span className="inline-flex items-center rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-emerald-700">
                    CSV active
                  </span>
                ) : (
                  <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">
                    Date sync active
                  </span>
                )}
              </div>

              <div className="mt-4 space-y-3 text-sm leading-6 text-slate-600">
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
                  <div className="font-medium text-slate-950">Date-based fetch</div>
                  <div className="mt-1">Use the Unify API flow to fetch orders by delivery date as usual.</div>
                </div>
                <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4">
                  <div className="font-medium text-emerald-950">CSV / Excel upload</div>
                  <div className="mt-1 text-emerald-800">
                    Upload a report to preview and sync the same invoices without running the date fetch.
                  </div>
                </div>
              </div>

            </aside>
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
                    const detail = getOrderDetail(order);
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
