import Link from "next/link";
import type { ReactNode } from "react";
import { AppShell } from "../../components/app-shell";
import { PageHeader } from "../../components/ui/page-header";

function SettingsCard({
  href,
  title,
  description,
  note,
  icon,
}: {
  href: string;
  title: string;
  description: string;
  note: string;
  icon: ReactNode;
}) {
  return (
    <Link
      href={href}
      className="group flex flex-col rounded-2xl border border-slate-200/80 bg-white p-5 shadow-card transition-[border-color,box-shadow] duration-150 hover:border-slate-300 hover:shadow-raised motion-reduce:transition-none sm:p-6"
    >
      <div className="flex items-start gap-4">
        <span aria-hidden="true" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-brand-50 text-brand-700">
          {icon}
        </span>
        <div className="min-w-0">
          <h2 className="text-base font-semibold tracking-tight text-slate-950">{title}</h2>
          <p className="mt-1 text-sm leading-6 text-slate-600">{description}</p>
        </div>
      </div>
      <div className="mt-5 flex items-center justify-between gap-3 border-t border-slate-100 pt-4">
        <span className="text-xs font-medium text-slate-500">{note}</span>
        <span className="inline-flex items-center gap-1 text-sm font-medium text-brand-700 group-hover:text-brand-800">
          Open
          <svg viewBox="0 0 24 24" aria-hidden="true" className="h-4 w-4 transition-transform duration-150 group-hover:translate-x-0.5 motion-reduce:transition-none">
            <path d="m9 6 6 6-6 6" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
          </svg>
        </span>
      </div>
    </Link>
  );
}

export default function SettingsPage() {
  return (
    <AppShell>
      <main className="space-y-6">
        <PageHeader
          eyebrow="Settings"
          title="Workspace settings"
          description="Manage the integrations Zconnect uses and who can sign in. Changes here are restricted to admins."
        />

        <section aria-label="Settings areas" className="grid gap-4 md:grid-cols-2">
          <SettingsCard
            href="/connections"
            title="Connections"
            description="Unify and Zoho credentials, the Zoho API region and the tax IDs used on invoice lines."
            note="Admins can edit · everyone can view"
            icon={
              <svg viewBox="0 0 24 24" className="h-5 w-5">
                <path
                  d="M9.5 14.5 14.5 9.5M8 12l-1.8 1.8a3.2 3.2 0 0 0 4.5 4.5L12.5 16.5M16 12l1.8-1.8a3.2 3.2 0 0 0-4.5-4.5L11.5 7.5"
                  fill="none"
                  stroke="currentColor"
                  strokeLinecap="round"
                  strokeWidth="1.8"
                />
              </svg>
            }
          />
          <SettingsCard
            href="/users"
            title="Users"
            description="Create operator logins, grant admin access and remove people who no longer need it."
            note="Admins only"
            icon={
              <svg viewBox="0 0 24 24" className="h-5 w-5">
                <circle cx="9" cy="8.5" r="3.2" fill="none" stroke="currentColor" strokeWidth="1.8" />
                <path d="M3.5 19a5.5 5.5 0 0 1 11 0M15.5 5.6a3.2 3.2 0 0 1 0 5.8M17.5 14.3A5.5 5.5 0 0 1 20.5 19" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="1.8" />
              </svg>
            }
          />
        </section>
      </main>
    </AppShell>
  );
}
