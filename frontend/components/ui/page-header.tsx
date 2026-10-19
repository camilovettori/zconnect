import type { ReactNode } from "react";

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0 max-w-3xl">
        {eyebrow && <p className="text-xs font-semibold uppercase tracking-[0.14em] text-brand-700">{eyebrow}</p>}
        <h1 className="mt-1.5 text-2xl font-semibold tracking-tight text-slate-950 sm:text-[1.75rem] sm:leading-9">{title}</h1>
        {description && <p className="mt-2 text-sm leading-6 text-slate-600">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}
