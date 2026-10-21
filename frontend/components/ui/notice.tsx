import type { ReactNode } from "react";

export type NoticeKind = "success" | "error" | "warning" | "info";

const TONES: Record<NoticeKind, { box: string; icon: string }> = {
  success: { box: "border-emerald-200 bg-emerald-50 text-emerald-800", icon: "text-emerald-600" },
  error: { box: "border-rose-200 bg-rose-50 text-rose-700", icon: "text-rose-600" },
  warning: { box: "border-amber-200 bg-amber-50 text-amber-800", icon: "text-amber-600" },
  info: { box: "border-brand-200 bg-brand-50 text-brand-900", icon: "text-brand-600" },
};

function NoticeIcon({ kind, className }: { kind: NoticeKind; className: string }) {
  const path =
    kind === "success"
      ? "m5 12.5 4.2 4L19 7"
      : kind === "info"
        ? "M12 16v-4.5M12 8h.01"
        : "M12 8v4.5M12 16h.01";
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={`mt-px h-4 w-4 shrink-0 ${className}`}>
      {kind === "success" ? null : <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="1.8" />}
      <path d={path} fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
    </svg>
  );
}

// One consistent message box: errors are announced assertively (role="alert"), everything
// else politely (role="status").
export function Notice({
  kind,
  title,
  children,
  onDismiss,
  className = "",
}: {
  kind: NoticeKind;
  title?: string;
  children?: ReactNode;
  onDismiss?: () => void;
  className?: string;
}) {
  const tone = TONES[kind];
  return (
    <div role={kind === "error" ? "alert" : "status"} className={`flex items-start gap-3 rounded-xl border px-4 py-3 text-sm ${tone.box} ${className}`}>
      <NoticeIcon kind={kind} className={tone.icon} />
      <div className="min-w-0 flex-1 break-words">
        {title && <p className="font-medium">{title}</p>}
        {children && <div className={title ? "mt-0.5" : undefined}>{children}</div>}
      </div>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          className="-m-1 rounded-md p-1 opacity-70 transition-opacity hover:opacity-100"
          aria-label="Dismiss message"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true" className="h-4 w-4">
            <path d="M6 6l12 12M18 6 6 18" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="2" />
          </svg>
        </button>
      )}
    </div>
  );
}
