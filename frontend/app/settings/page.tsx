import { AppShell } from "../../components/app-shell";

export default function SettingsPage() {
  return (
    <AppShell>
      <main className="space-y-6">
        <section className="rounded-3xl border border-slate-200/80 bg-white/90 p-8 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)] backdrop-blur">
          <p className="text-xs font-semibold uppercase tracking-[0.28em] text-slate-500">Settings</p>
          <h2 className="mt-3 text-3xl font-semibold tracking-tight text-slate-950">App preferences</h2>
          <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600">
            This space is reserved for app-level preferences. Connection credentials now live in Connections.
          </p>
          <div className="mt-6">
            <a className="rounded-xl bg-slate-950 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-slate-900" href="/connections">
              Open Connections
            </a>
          </div>
        </section>
      </main>
    </AppShell>
  );
}
