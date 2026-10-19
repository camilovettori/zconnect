import type { ReactNode } from "react";

// Shared visual status system for Sync and History. Purely presentational:
// callers decide which tone applies; this component never derives status.
export type StatusTone = "ready" | "review" | "synced" | "blocked" | "failed" | "running" | "neutral";

const TONE_CLASSES: Record<StatusTone, { pill: string; dot: string }> = {
  ready: { pill: "border-brand-200 bg-brand-50 text-brand-700", dot: "bg-brand-500" },
  review: { pill: "border-amber-200 bg-amber-50 text-amber-800", dot: "bg-amber-500" },
  synced: { pill: "border-emerald-200 bg-emerald-50 text-emerald-700", dot: "bg-emerald-500" },
  blocked: { pill: "border-slate-200 bg-slate-100 text-slate-600", dot: "bg-slate-400" },
  failed: { pill: "border-rose-200 bg-rose-50 text-rose-700", dot: "bg-rose-500" },
  running: { pill: "border-sky-200 bg-sky-50 text-sky-700", dot: "bg-sky-500" },
  neutral: { pill: "border-slate-200 bg-white text-slate-600", dot: "bg-slate-300" },
};

export function StatusBadge({
  tone,
  children,
  className = "",
}: {
  tone: StatusTone;
  children: ReactNode;
  className?: string;
}) {
  const classes = TONE_CLASSES[tone];
  return (
    <span
      className={[
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium",
        classes.pill,
        className,
      ].join(" ")}
    >
      <span aria-hidden="true" className={`h-1.5 w-1.5 shrink-0 rounded-full ${classes.dot}`} />
      {children}
    </span>
  );
}
