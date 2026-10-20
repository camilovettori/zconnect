"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import { AppShell } from "../../components/app-shell";
import { useAppSession } from "../../components/app-session";
import { formatCurrencyEur } from "../utils/money";
import { Calendar } from "../../components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger, usePopoverContext } from "../../components/ui/popover";
import { PageHeader } from "../../components/ui/page-header";
import { StepIndicator } from "../../components/ui/step-indicator";
import type { Step, StepState } from "../../components/ui/step-indicator";
import { StatusBadge } from "../../components/ui/status-badge";
import type { StatusTone } from "../../components/ui/status-badge";

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
  // Stored export record status from the backend preview (exported, failed, skipped,
  // needs_verification, in_progress). Display only; eligibility still uses already_exported.
  export_status?: string | null;
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
  // Backend returns created / skipped / failed / already_exported; "unknown" is set by the
  // frontend when the response has no usable result for a submitted order.
  status: "created" | "skipped" | "failed" | "already_exported" | "needs_verification" | "in_progress" | "unknown";
  message: string;
  zoho_invoice_id?: string;
};

// Result statuses that mean the order now has an export record (it must not be sent again).
const EXPORTED_RESULT_STATUSES = new Set(["created", "skipped", "already_exported"]);

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

const WORKFLOW_STEPS: Step[] = [
  { label: "Upload file", description: "CSV or Excel report" },
  { label: "Review orders", description: "Customers and totals" },
  { label: "Select orders", description: "Choose what to invoice" },
  { label: "Sync to Zoho", description: "Create draft invoices" },
];

const BUTTON_SHAPE =
  "inline-flex items-center justify-center whitespace-nowrap rounded-lg px-3.5 py-2 text-sm font-medium transition-colors duration-150 motion-reduce:transition-none disabled:cursor-not-allowed";
const BUTTON_BASE = `${BUTTON_SHAPE} disabled:opacity-50`;
// Per-order Sync: neutral grey when disabled so ineligible orders never look actionable.
const BUTTON_ORDER_SYNC = `${BUTTON_SHAPE} bg-brand-600 text-white shadow-card hover:bg-brand-700 disabled:bg-slate-100 disabled:text-slate-400 disabled:shadow-none disabled:hover:bg-slate-100`;
const BUTTON_PRIMARY = `${BUTTON_BASE} bg-brand-600 text-white shadow-card hover:bg-brand-700 disabled:hover:bg-brand-600`;
const BUTTON_SECONDARY = `${BUTTON_BASE} border border-slate-200 bg-white text-slate-700 shadow-card hover:border-slate-300 hover:bg-slate-50 hover:text-slate-950`;
const BUTTON_GHOST = `${BUTTON_BASE} text-slate-600 hover:bg-slate-100 hover:text-slate-950 disabled:hover:bg-transparent`;
const BUTTON_DANGER = `${BUTTON_BASE} border border-rose-200 bg-white text-rose-700 shadow-card hover:border-rose-300 hover:bg-rose-50`;

// Visual tone for the label produced by getPreviewBadgeLabel (display only).
const BADGE_TONE_BY_LABEL: Record<string, StatusTone> = {
  Synced: "synced",
  "Ready to sync": "ready",
  "Needs review": "review",
  Error: "failed",
  Blocked: "blocked",
};

// Badges for orders whose stored export record is not a completed export (display only).
const PREVIOUS_EXPORT_BADGES: Record<string, { tone: StatusTone; label: string }> = {
  failed: { tone: "failed", label: "Previously failed" },
  skipped: { tone: "blocked", label: "Previously skipped" },
  needs_verification: { tone: "review", label: "Needs verification" },
  in_progress: { tone: "running", label: "Sync in progress" },
};

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
  return Number.isNaN(date.getTime()) ? value : dateFormatter.format(date);
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden="true"
      className={["ml-1.5 h-3.5 w-3.5 transition-transform duration-150 motion-reduce:transition-none", open ? "rotate-180" : ""].join(" ")}
    >
      <path d="m4 6 4 4 4-4" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" />
    </svg>
  );
}

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
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Keyboard support: focus moves into the dialog, Tab stays inside it, Escape
  // acts like the Close button (only when one exists), and focus returns afterwards.
  useEffect(() => {
    if (!open) {
      return;
    }
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panelRef.current?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      const panel = panelRef.current;
      if (!panel) {
        return;
      }
      if (event.key === "Escape") {
        if (onCloseRef.current) {
          event.preventDefault();
          onCloseRef.current();
        }
        return;
      }
      if (event.key !== "Tab") {
        return;
      }
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])'),
      );
      if (focusable.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || active === panel || !panel.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !panel.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      if (previouslyFocused && document.contains(previouslyFocused)) {
        previouslyFocused.focus();
      }
    };
  }, [open]);

  if (!open) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 px-4 py-6 backdrop-blur-sm">
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="w-full max-w-lg rounded-2xl border border-slate-200 bg-white p-6 shadow-overlay focus:outline-none"
      >
        <div className="flex items-start justify-between gap-4">
          <h2 id={titleId} className="text-lg font-semibold tracking-tight text-slate-950">
            {title}
          </h2>
          {onClose ? (
            <button
              type="button"
              className="rounded-lg px-2.5 py-1 text-sm font-medium text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-800"
              onClick={onClose}
            >
              Close
            </button>
          ) : null}
        </div>
        <div className="mt-3 text-sm leading-6 text-slate-600">{children}</div>
        {actions ? <div className="mt-6 flex flex-wrap justify-end gap-2">{actions}</div> : null}
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
  // Display only: the backend enforces admin permission for reset and reconciliation.
  const { user: sessionUser } = useAppSession();
  const isAdmin = sessionUser?.role === "admin";
  const [dateFrom, setDateFrom] = useState<Date | null>(null);
  const [dateTo, setDateTo] = useState<Date | null>(null);
  const [orders, setOrders] = useState<UnifiedOrder[]>([]);
  const [customers, setCustomers] = useState<Record<string, CustomerSummary>>({});
  const [message, setMessage] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [exportResult, setExportResult] = useState<SyncResult | null>(null);
  // Per-order results accumulated across sync runs in this session, so a failed or
  // unknown outcome is not forgotten when a later run replaces exportResult.
  const [orderSyncResults, setOrderSyncResults] = useState<Record<string, SyncDetail>>({});
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
    setOrderSyncResults({});
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
        : "Upload a Unify report to preview the orders before syncing them to Zoho.";

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

  const getResultByOrderId = (orderId: string): SyncDetail | undefined => orderSyncResults[orderId];

  const getOrderStatus = (order: UnifiedOrder): OrderStatus => {
    if (order.already_exported) {
      return "already_synced";
    }
    if (order.preview_status && !["ready", "mapping_issue"].includes(order.preview_status)) return "failed";
    const result = getResultByOrderId(order.order_id);
    if (result?.status === "created") return "synced";
    if (result?.status === "failed") return "failed";
    if (result?.status === "skipped") return "already_synced";
    if (result?.status === "already_exported") return "already_synced";
    // Any other (unknown or missing) result is ambiguous: never ready, never successful.
    if (result) return "failed";
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

  // Only orders whose effective status (getOrderStatus) is "ready" may be submitted.
  const getEligibleSyncOrderIds = (orderIds: string[]) =>
    orderIds.filter((orderId) => {
      const order = orders.find((candidate) => candidate.order_id === orderId);
      return order !== undefined && getOrderStatus(order) === "ready";
    });

  const executeExport = async (orderIds: string[], actionLabel = "sync-selected-orders") => {
    if (!orderIds.length) {
      setMessage("Select at least one order to sync");
      return;
    }
    // Request-level guard: never submit an order that is no longer eligible, and never
    // send a different set than the one the user confirmed.
    if (getEligibleSyncOrderIds(orderIds).length !== orderIds.length) {
      setMessage("Error: Some of the confirmed orders are no longer ready to sync. Review the selection and try again.");
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
      // Only explicit per-order results are trusted. A submitted order with no usable
      // result is recorded as "unknown" (blocked, never treated as successful).
      const result: SyncResult = data && typeof data === "object" ? data : {};
      const responseDetails = (Array.isArray(result.details) ? result.details : []).filter(
        (detail): detail is SyncDetail => Boolean(detail) && typeof detail.order_id === "string" && orderIds.includes(detail.order_id),
      );
      const missingOrderIds = orderIds.filter((orderId) => !responseDetails.some((detail) => detail.order_id === orderId));
      const unknownDetails: SyncDetail[] = missingOrderIds.map((orderId) => ({
        order_id: orderId,
        status: "unknown",
        message: "No result was returned for this order. Check History or Zoho before retrying.",
      }));
      const runDetails = [...responseDetails, ...unknownDetails];
      setExportResult({ ...result, details: runDetails });
      setOrderSyncResults((current) => {
        const next = { ...current };
        runDetails.forEach((detail) => {
          next[detail.order_id] = detail;
        });
        return next;
      });
      const syncedOrderIds = new Set(
        runDetails.filter((detail) => EXPORTED_RESULT_STATUSES.has(detail.status)).map((detail) => detail.order_id),
      );
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
      setMessage(
        missingOrderIds.length
          ? `Error: No result was returned for ${pluralize(missingOrderIds.length, "order")}. Check History or Zoho before retrying.${
              responseDetails.length ? ` ${formatSyncResultMessage(result)}.` : ""
            }`
          : formatSyncResultMessage(result),
      );
    } catch (error) {
      console.error("[Sync] Export request failed", {
        action: actionLabel,
        endpoint,
        orderIds,
        error,
      });
      // The backend may have processed some orders before the error or timeout, so their
      // outcome is unknown: block them from being re-sent until the preview is reloaded.
      setOrderSyncResults((current) => {
        const next = { ...current };
        orderIds.forEach((orderId) => {
          next[orderId] = {
            order_id: orderId,
            status: "unknown",
            message: "The sync request did not complete. Check History or Zoho, then re-upload the file before retrying.",
          };
        });
        return next;
      });
      setMessage(`Error: ${describeError(error)}. Check History or Zoho, then re-upload the file before retrying.`);
    } finally {
      setLoading(false);
      setSyncRunning(false);
    }
  };

  const requestExport = (orderIds?: string[]) => {
    const requestedIds = orderIds ?? Array.from(selectedOrderIds);
    const syncIds = getEligibleSyncOrderIds(requestedIds);
    if (!syncIds.length) {
      setMessage(requestedIds.length ? "None of the selected orders are ready to sync" : "Select at least one order to sync");
      return;
    }
    setPendingSyncOrderIds(syncIds);
    setSyncConfirmOpen(true);
  };

  const unsyncOrder = async (orderId: string) => {
    const endpoint = `/api/export/reset-order/${encodeURIComponent(orderId)}`;
    setLoading(true);
    setMessage("");
    try {
      const res = await fetch(endpoint, { method: "POST" });
      await parseApiResponse(res, "unsync-order", endpoint);
      // Soft reset: the record is kept; the next sync checks Zoho before creating an invoice.
      setOrders((currentOrders) =>
        currentOrders.map((order) => (order.order_id === orderId ? { ...order, already_exported: false, export_status: "reset" } : order)),
      );
      setOrderSyncResults((current) => {
        const next = { ...current };
        delete next[orderId];
        return next;
      });
      setMessage(`Order ${orderId} reset. The next sync checks Zoho first and links an existing invoice instead of creating a new one.`);
    } catch (error) {
      console.error("[Sync] Unsync request failed", { orderId, endpoint, error });
      setMessage(`Error: ${describeError(error)}`);
    } finally {
      setLoading(false);
    }
  };

  // Resolve a needs-verification / in-progress order by checking Zoho (never creates an invoice).
  const reconcileOrder = async (orderId: string) => {
    const endpoint = `/api/export/reconcile-order/${encodeURIComponent(orderId)}`;
    setLoading(true);
    setMessage("");
    try {
      const res = await fetch(endpoint, { method: "POST" });
      const data = await parseApiResponse<{ data?: { result?: string; status?: string; message?: string } }>(res, "reconcile-order", endpoint);
      const outcome = data?.data;
      const nextStatus = outcome?.status;
      if (nextStatus) {
        setOrders((currentOrders) =>
          currentOrders.map((order) =>
            order.order_id === orderId ? { ...order, already_exported: nextStatus !== "reset", export_status: nextStatus } : order,
          ),
        );
        setOrderSyncResults((current) => {
          const next = { ...current };
          delete next[orderId];
          return next;
        });
      }
      const text = outcome?.message || "Zoho check completed";
      setMessage(outcome?.result === "ambiguous" ? `Error: ${text}` : text);
    } catch (error) {
      console.error("[Sync] Reconcile request failed", { orderId, endpoint, error });
      setMessage(`Error: ${describeError(error)}`);
    } finally {
      setLoading(false);
    }
  };

  const needsZohoCheck = (order: UnifiedOrder) => {
    const sessionStatus = orderSyncResults[order.order_id]?.status;
    if (sessionStatus === "needs_verification" || sessionStatus === "in_progress") {
      return true;
    }
    return Boolean(order.already_exported) && (order.export_status === "needs_verification" || order.export_status === "in_progress");
  };

  const totalRevenue = orders.reduce((sum, order) => sum + Number(order.total || 0), 0);
  const selectedReadyCount = orders.filter((order) => selectedOrderIds.has(order.order_id) && getOrderStatus(order) === "ready").length;
  const pendingSyncSummary = getSyncSummary(pendingSyncOrderIds);
  const syncablePreviewStatuses = new Set(["ready", "mapping_issue"]);
  const readyPreviewCount = orders.filter((order) => syncablePreviewStatuses.has(order.preview_status ?? "")).length;
  const blockedInRangePreviewCount = orders.filter((order) => !syncablePreviewStatuses.has(order.preview_status ?? "")).length;

  // Presentation-only values derived from existing state; no workflow logic.
  // The bar shows exactly what a bulk sync would submit (eligible selected orders only).
  const selectionSummary = getSyncSummary(getEligibleSyncOrderIds(Array.from(selectedOrderIds)));
  const ineligibleSelectedCount = selectedOrderIds.size - selectedReadyCount;
  const hasOrders = orders.length > 0;
  const workflowStepStates: StepState[] = [
    hasOrders ? "complete" : "current",
    !hasOrders ? "upcoming" : selectedOrderIds.size > 0 || exportResult ? "complete" : "current",
    !hasOrders ? "upcoming" : selectedOrderIds.size > 0 || exportResult ? "complete" : "upcoming",
    selectedOrderIds.size > 0 ? "current" : exportResult ? "complete" : "upcoming",
  ];
  const kpis = [
    { label: "Order value", hint: "excl. VAT", value: formatCurrencyEur(totalRevenue), wide: true },
    { label: "Orders", value: previewOrderCount },
    { label: "Customers", value: Object.keys(customers).length },
    { label: "Ready to sync", value: readyOrderIds.length },
    { label: "Blocked", value: blockedInRangePreviewCount },
    // Only real exports: a stored failed/skipped/unverified record is not a sync.
    { label: "Synced", value: orders.filter((order) => order.already_exported && (!order.export_status || order.export_status === "exported")).length },
    { label: "Invoices created", value: exportResult?.created ?? 0 },
  ];
  const showGeneralMessage = Boolean(message) && message !== csvUploadMessage;
  const getOrderBadge = (order: UnifiedOrder): { tone: StatusTone; label: string } => {
    const syncResult = order.already_exported ? undefined : getResultByOrderId(order.order_id);
    if (syncResult?.status === "failed") {
      return { tone: "failed", label: "Sync failed" };
    }
    if (syncResult?.status === "needs_verification" || syncResult?.status === "in_progress") {
      return { tone: "review", label: "Needs verification" };
    }
    if (syncResult && !EXPORTED_RESULT_STATUSES.has(syncResult.status)) {
      return { tone: "review", label: "Result unknown" };
    }
    // Existing record that is not a completed export: say what it is instead of "Synced".
    if (order.already_exported && !getResultByOrderId(order.order_id) && order.export_status && order.export_status !== "exported") {
      return PREVIOUS_EXPORT_BADGES[order.export_status] ?? { tone: "review", label: "Check status" };
    }
    const label = getPreviewBadgeLabel(order);
    return { tone: BADGE_TONE_BY_LABEL[label] ?? "neutral", label };
  };

  return (
    <AppShell>
      <main className="space-y-6">
      <section className="space-y-6">
        <PageHeader
          eyebrow="Sync"
          title="Import orders and create Zoho invoices"
          description="Upload a Unify CSV or Excel report, review the orders, then sync them to Zoho as draft invoices."
        />

        <div className="rounded-2xl border border-slate-200/80 bg-white p-5 shadow-card sm:p-6">
          <StepIndicator steps={WORKFLOW_STEPS} states={workflowStepStates} />

          <section
            aria-label="Upload a Unify report"
            className={[
              "mt-6 rounded-xl border transition-colors duration-150 motion-reduce:transition-none",
              csvDragActive
                ? "border-brand-400 bg-brand-50/70 ring-4 ring-brand-100"
                : csvFileName && !csvLoading
                  ? "border-slate-200 bg-slate-50/60"
                  : "border-dashed border-slate-300 bg-slate-50/40 hover:border-brand-300 hover:bg-brand-50/30",
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

            {csvFileName && !csvLoading ? (
              <div className="flex flex-col gap-4 p-4 sm:flex-row sm:items-center sm:justify-between sm:p-5">
                <div className="flex min-w-0 items-center gap-3.5">
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-emerald-200 bg-emerald-50 text-emerald-700">
                    <DocumentIcon />
                  </div>
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold text-slate-950" title={csvFileName}>
                      {csvFileName}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-500">
                      <span className="rounded border border-slate-200 bg-white px-1.5 py-0.5 font-semibold tracking-wide text-slate-600">
                        {csvFileExtension}
                      </span>
                      <span className="tabular-nums">
                        {previewOrderCount} order{previewOrderCount === 1 ? "" : "s"}
                      </span>
                      <span aria-hidden="true">·</span>
                      <span className="tabular-nums">
                        <span className="font-medium text-slate-700">{formatCurrencyEur(totalRevenue)}</span> excl. VAT
                      </span>
                      {csvUploadSucceeded && (
                        <>
                          <span aria-hidden="true">·</span>
                          <span className="font-medium text-emerald-700">Preview ready</span>
                        </>
                      )}
                    </div>
                  </div>
                </div>

                <div className="flex shrink-0 gap-2">
                  <button
                    type="button"
                    className={BUTTON_SECONDARY}
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
                    className={BUTTON_GHOST}
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
            ) : (
              <div className="flex flex-col items-center gap-4 p-5 text-center sm:flex-row sm:p-6 sm:text-left">
                <div
                  className={[
                    "flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border transition-colors duration-150 motion-reduce:transition-none",
                    csvDragActive ? "border-brand-500 bg-brand-600 text-white" : "border-brand-100 bg-brand-50 text-brand-600",
                  ].join(" ")}
                >
                  {csvLoading ? <Spinner /> : <UploadCloudIcon />}
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-slate-950">
                    {csvLoading ? "Parsing file..." : csvDragActive ? "Release to upload" : "Drop your Unify report here"}
                  </p>
                  <p className="mt-0.5 text-sm text-slate-500">
                    {csvLoading ? csvUploadHelperState : "CSV, XLSX or XLS. Click to browse or drag a file into this area."}
                  </p>
                </div>
                <button
                  type="button"
                  className={`${BUTTON_PRIMARY} w-full sm:w-auto`}
                  onClick={(event) => {
                    event.stopPropagation();
                    openCsvPicker();
                  }}
                  disabled={loading || csvLoading}
                >
                  {csvLoading ? "Parsing..." : "Upload file"}
                </button>
              </div>
            )}
          </section>

          {csvUploadMessage && (
            <div role="alert" className="mt-4 flex items-start gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
              <span className="mt-0.5 shrink-0">
                <AlertIcon />
              </span>
              <div>
                <div className="font-medium">{orders.length > 0 ? "Sync error" : "Upload failed"}</div>
                <div className="mt-0.5 leading-6">{csvUploadMessage}</div>
              </div>
            </div>
          )}

          {showGeneralMessage && (
            <div
              role={message.startsWith("Error:") ? "alert" : "status"}
              className={[
                "mt-4 rounded-xl border px-4 py-3 text-sm",
                message.startsWith("Error:")
                  ? "border-rose-200 bg-rose-50 text-rose-700"
                  : "border-slate-200 bg-slate-50 text-slate-700",
              ].join(" ")}
            >
              {message}
            </div>
          )}
        </div>

        {(orders.length > 0 || exportResult) && (
          <dl className="grid grid-cols-2 overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-card sm:grid-cols-4 xl:grid-cols-[minmax(0,1.5fr)_repeat(6,minmax(0,1fr))]">
            {kpis.map((kpi) => (
              <div
                key={kpi.label}
                className={[
                  "border-b border-r border-slate-100 px-4 py-3.5 sm:px-5",
                  kpi.wide ? "col-span-2 xl:col-span-1" : "",
                ].join(" ")}
              >
                <dt className="text-xs font-medium text-slate-500">
                  {kpi.label}
                  {kpi.hint && <span className="ml-1 font-normal text-slate-400">{kpi.hint}</span>}
                </dt>
                <dd className="mt-1 truncate text-lg font-semibold tabular-nums tracking-tight text-slate-950">{kpi.value}</dd>
              </div>
            ))}
          </dl>
        )}

        {orders.length > 0 && (
          <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
            <div className="min-w-0 space-y-4 xl:col-span-2">
              <section aria-labelledby="order-preview-heading" className="rounded-2xl border border-slate-200/80 bg-white p-4 shadow-card sm:p-6">
                <div className="flex items-center justify-between gap-4">
                  <div className="min-w-0">
                    <h2 id="order-preview-heading" className="text-base font-semibold tracking-tight text-slate-950">
                      Order preview
                    </h2>
                    <p className="mt-0.5 text-sm text-slate-500">One card per unique Unify order. Totals exclude VAT.</p>
                  </div>
                  <span className="shrink-0 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-xs font-medium tabular-nums text-slate-600">
                    {previewOrderCount} unique orders
                  </span>
                </div>

                <div className="mt-5 space-y-3">
                  {orders.map((order) => {
                    const detail = getOrderDetail(order);
                    const isExpanded = expandedOrderIds.has(order.order_id);
                    const isLoadingDetail = orderDetailsLoading[order.order_id];
                    const isSelected = selectedOrderIds.has(order.order_id);
                    const isIneligible = order.already_exported || getOrderStatus(order) !== "ready";
                    const badge = getOrderBadge(order);
                    const customerName = getBuyerDisplayName(order);
                    const itemsPanelId = `order-items-${String(order.order_id).replace(/[^A-Za-z0-9_-]/g, "_")}`;
                    return (
                      <article
                        key={order.order_id}
                        aria-label={`Order ${order.order_id}, ${customerName}`}
                        className={[
                          "rounded-xl border bg-white transition-colors duration-150 motion-reduce:transition-none",
                          isSelected ? "border-brand-300 ring-1 ring-brand-200" : "border-slate-200 hover:border-slate-300",
                        ].join(" ")}
                      >
                        <div className="flex gap-3 p-4 sm:gap-4 sm:p-5">
                          <label
                            className={[
                              "-m-1.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md",
                              isIneligible || loading ? "cursor-not-allowed" : "cursor-pointer hover:bg-slate-100",
                            ].join(" ")}
                            title={order.already_exported ? "Already synced" : isIneligible ? "Not eligible for sync" : undefined}
                          >
                            <input
                              type="checkbox"
                              className="h-[18px] w-[18px] cursor-[inherit] accent-brand-600 disabled:opacity-40"
                              checked={selectedOrderIds.has(order.order_id)}
                              disabled={order.already_exported || loading || getOrderStatus(order) !== "ready"}
                              onChange={() => toggleSelected(order.order_id)}
                              aria-label={`Select order ${order.order_id} for ${customerName}`}
                            />
                          </label>

                          <div className="min-w-0 flex-1">
                            <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
                              <div className="min-w-0">
                                <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
                                  <h3 className="min-w-0 break-words text-[15px] font-semibold leading-6 text-slate-950">{customerName}</h3>
                                  <StatusBadge tone={badge.tone}>{badge.label}</StatusBadge>
                                </div>
                                <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-slate-500">
                                  <span>
                                    Order <span className="font-mono font-medium text-slate-700">{order.order_id}</span>
                                  </span>
                                  {order.order_date && (
                                    <>
                                      <span aria-hidden="true">·</span>
                                      <span>Ordered {formatPlainDate(order.order_date)}</span>
                                    </>
                                  )}
                                  <span aria-hidden="true">·</span>
                                  <span>Delivery {formatPlainDate(getDeliveryLabel(order))}</span>
                                </div>
                              </div>
                              <div className="flex shrink-0 items-baseline gap-1.5 sm:block sm:text-right">
                                <div className="text-lg font-semibold tabular-nums tracking-tight text-slate-950">{formatCurrencyEur(order.total)}</div>
                                <div className="text-xs text-slate-500">excl. VAT</div>
                              </div>
                            </div>

                            {((order.customer_name && getCustomerDisplayName(order) !== getBuyerDisplayName(order)) ||
                              order.delivery_address ||
                              order.buyer_id) && (
                              <dl className="mt-3 space-y-0.5 text-xs">
                                {order.customer_name && getCustomerDisplayName(order) !== getBuyerDisplayName(order) && (
                                  <div className="flex gap-1.5">
                                    <dt className="shrink-0 text-slate-400">Recipient</dt>
                                    <dd className="min-w-0 break-words text-slate-600">{getCustomerDisplayName(order)}</dd>
                                  </div>
                                )}
                                {order.delivery_address && (
                                  <div className="flex gap-1.5">
                                    <dt className="shrink-0 text-slate-400">Delivery address</dt>
                                    <dd className="min-w-0 break-words text-slate-600">{order.delivery_address}</dd>
                                  </div>
                                )}
                                {order.buyer_id && (
                                  <div className="flex gap-1.5">
                                    <dt className="shrink-0 text-slate-400">Buyer ID</dt>
                                    <dd className="min-w-0 break-words font-mono text-slate-600">{order.buyer_id}</dd>
                                  </div>
                                )}
                              </dl>
                            )}

                            {order.preview_reason && (
                              <p
                                className={[
                                  "mt-3 rounded-lg border px-3 py-2 text-xs leading-5",
                                  badge.tone === "review"
                                    ? "border-amber-200 bg-amber-50 text-amber-800"
                                    : badge.tone === "failed"
                                      ? "border-rose-200 bg-rose-50 text-rose-700"
                                      : "border-slate-200 bg-slate-50 text-slate-600",
                                ].join(" ")}
                              >
                                <span className="font-medium">Reason:</span> {order.preview_reason}
                              </p>
                            )}

                            <div className="mt-4 flex flex-wrap items-center gap-2">
                              <button
                                type="button"
                                className={BUTTON_ORDER_SYNC}
                                onClick={() => requestExport([order.order_id])}
                                disabled={loading || order.already_exported || getOrderStatus(order) !== "ready"}
                              >
                                Sync this order
                              </button>
                              {!isAdmin ? null : needsZohoCheck(order) ? (
                                <button
                                  type="button"
                                  className={BUTTON_SECONDARY}
                                  onClick={() => reconcileOrder(order.order_id)}
                                  disabled={loading}
                                  title="Check Zoho for an existing invoice for this order"
                                >
                                  Check Zoho
                                </button>
                              ) : (
                                order.already_exported && (
                                  <button
                                    type="button"
                                    className={BUTTON_DANGER}
                                    onClick={() => unsyncOrder(order.order_id)}
                                    disabled={loading}
                                    title="Allow this order to be synced again. Existing Zoho invoices are kept and checked first."
                                  >
                                    Reset sync
                                  </button>
                                )
                              )}
                              <button
                                type="button"
                                className={BUTTON_GHOST}
                                onClick={() => toggleOrderExpansion(order.order_id)}
                                disabled={loading || isLoadingDetail}
                                aria-expanded={isExpanded}
                                aria-controls={itemsPanelId}
                              >
                                {isExpanded ? "Hide items" : isLoadingDetail ? "Loading items..." : "View items"}
                                <ChevronIcon open={isExpanded} />
                              </button>
                            </div>
                          </div>
                        </div>

                        {isExpanded && (
                          <div id={itemsPanelId} className="rounded-b-xl border-t border-slate-200 bg-slate-50/70 px-4 py-4 sm:px-5">
                            {!detail ? (
                              <p className="text-sm text-slate-500">Loading order items...</p>
                            ) : detail.lines.length === 0 ? (
                              <p className="text-sm text-slate-500">No line items returned for this order.</p>
                            ) : (
                              <div
                                className="overflow-x-auto rounded-lg border border-slate-200 bg-white"
                                role="region"
                                aria-label={`Items in order ${order.order_id}`}
                                tabIndex={0}
                              >
                                <table className="w-full min-w-[26rem] text-left text-sm">
                                  <thead className="border-b border-slate-200 bg-slate-50 text-xs text-slate-500">
                                    <tr>
                                      <th scope="col" className="px-4 py-2.5 font-medium">Product</th>
                                      <th scope="col" className="px-4 py-2.5 text-right font-medium">Quantity</th>
                                      <th scope="col" className="px-4 py-2.5 text-right font-medium">Unit price</th>
                                    </tr>
                                  </thead>
                                  <tbody className="divide-y divide-slate-100">
                                    {detail.lines.map((line, idx) => (
                                      <tr key={`${order.order_id}-${idx}`}>
                                        <td className="px-4 py-2.5">
                                          <div className="font-medium text-slate-900">{line.item_name}</div>
                                          <div className="font-mono text-xs text-slate-500">{line.item_sku}</div>
                                        </td>
                                        <td className="whitespace-nowrap px-4 py-2.5 text-right tabular-nums text-slate-700">{line.quantity}</td>
                                        <td className="whitespace-nowrap px-4 py-2.5 text-right tabular-nums text-slate-700">{formatCurrencyEur(line.price)}</td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )}
                          </div>
                        )}
                      </article>
                    );
                  })}
                </div>
              </section>
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

        {orders.length > 0 && (
          <div
            role="region"
            aria-label="Selection and sync"
            className="sticky bottom-0 z-30 -mx-4 border-t border-slate-200 bg-white/95 px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-3 backdrop-blur-md sm:bottom-4 sm:mx-0 sm:rounded-2xl sm:border sm:px-5 sm:pb-3 sm:shadow-overlay"
          >
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0" aria-live="polite">
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
                  <span className="text-sm font-semibold tabular-nums text-slate-950">
                    {selectedReadyCount} of {previewOrderCount} selected
                  </span>
                  <span className="text-sm tabular-nums text-slate-600">
                    <span className="font-medium text-slate-900">{formatCurrencyEur(selectionSummary.total)}</span> excl. VAT
                  </span>
                  {ineligibleSelectedCount > 0 && (
                    <span className="text-xs font-medium text-amber-700">
                      {ineligibleSelectedCount} selected order{ineligibleSelectedCount === 1 ? " is" : "s are"} not ready and won&apos;t be synced
                    </span>
                  )}
                </div>
                {lastFetchedRange && (
                  <p className="mt-0.5 text-xs text-slate-400">
                    Delivery date range: {formatDateDisplay(lastFetchedRange.from)} to {formatDateDisplay(lastFetchedRange.to)}
                  </p>
                )}
              </div>
              <div className="grid grid-cols-2 items-center gap-2 min-[360px]:grid-cols-[auto_auto_1fr] sm:flex">
                <button
                  type="button"
                  className={BUTTON_GHOST}
                  onClick={() => setSelectedFrom(readyOrderIds)}
                  disabled={readyOrderIds.length === 0 || loading}
                >
                  Select all
                </button>
                <button
                  type="button"
                  className={BUTTON_GHOST}
                  onClick={() => setSelectedFrom([])}
                  disabled={loading || selectedOrderIds.size === 0}
                >
                  Unselect all
                </button>
                <button
                  type="button"
                  className={`${BUTTON_PRIMARY} col-span-2 min-[360px]:col-span-1`}
                  onClick={() => requestExport()}
                  disabled={loading || selectedReadyCount === 0}
                >
                  {loading ? (
                    "Working..."
                  ) : (
                    <>
                      Sync selected<span className="hidden sm:inline">&nbsp;orders</span>
                    </>
                  )}
                </button>
              </div>
            </div>
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
                className={BUTTON_SECONDARY}
                onClick={() => {
                  setSyncConfirmOpen(false);
                  setPendingSyncOrderIds([]);
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                className={BUTTON_PRIMARY}
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
          <dl className="mt-4 grid grid-cols-2 overflow-hidden rounded-xl border border-slate-200 bg-slate-50">
            <div className="border-b border-r border-slate-200 px-4 py-3">
              <dt className="text-xs font-medium text-slate-500">Orders</dt>
              <dd className="mt-1 text-base font-semibold tabular-nums text-slate-950">{pendingSyncSummary.orderCount}</dd>
            </div>
            <div className="border-b border-slate-200 px-4 py-3">
              <dt className="text-xs font-medium text-slate-500">Customers</dt>
              <dd className="mt-1 text-base font-semibold tabular-nums text-slate-950">{pendingSyncSummary.customerCount}</dd>
            </div>
            <div className="col-span-2 px-4 py-3">
              <dt className="text-xs font-medium text-slate-500">Total excl. VAT</dt>
              <dd className="mt-1 text-lg font-semibold tabular-nums text-slate-950">{formatCurrencyEur(pendingSyncSummary.total)}</dd>
            </div>
          </dl>
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
