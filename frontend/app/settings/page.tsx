import Link from "next/link";
import { AppShell } from "../../components/app-shell";

export default function SettingsPage() {
  return (
    <AppShell>
      <main className="space-y-8">
        <section className="rounded-3xl border border-slate-200/80 bg-white/90 p-8 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)] backdrop-blur">
          <p className="text-xs font-semibold uppercase tracking-[0.28em] text-slate-500">Settings</p>
          <h2 className="mt-3 text-3xl font-semibold tracking-tight text-slate-950">App configuration</h2>
          <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600">
            Manage the workspace from one clean place.
          </p>
        </section>

        <section className="grid gap-4 md:grid-cols-2">
          <Link
            href="/connections"
            className="group rounded-3xl border border-slate-200/80 bg-white/90 p-6 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.22)] transition hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-[0_24px_70px_-36px_rgba(15,23,42,0.28)]"
          >
            <p className="text-xs font-semibold uppercase tracking-[0.28em] text-slate-500">Connections</p>
            <h3 className="mt-3 text-2xl font-semibold tracking-tight text-slate-950">Unify and Zoho</h3>
            <p className="mt-3 max-w-md text-sm leading-6 text-slate-600">
              Update integration credentials, tax IDs, and connection settings.
            </p>
            <div className="mt-6 inline-flex rounded-xl bg-slate-950 px-4 py-2.5 text-sm font-semibold text-white transition group-hover:bg-slate-900">
              Open Connections
            </div>
          </Link>

          <Link
            href="/users"
            className="group rounded-3xl border border-slate-200/80 bg-white/90 p-6 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.22)] transition hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-[0_24px_70px_-36px_rgba(15,23,42,0.28)]"
          >
            <p className="text-xs font-semibold uppercase tracking-[0.28em] text-slate-500">Users</p>
            <h3 className="mt-3 text-2xl font-semibold tracking-tight text-slate-950">Manage app access</h3>
            <p className="mt-3 max-w-md text-sm leading-6 text-slate-600">
              Create operator logins and manage admin access for the workspace.
            </p>
            <div className="mt-6 inline-flex rounded-xl bg-slate-950 px-4 py-2.5 text-sm font-semibold text-white transition group-hover:bg-slate-900">
              Open Users
            </div>
          </Link>
        </section>
      </main>
    </AppShell>
  );
}
