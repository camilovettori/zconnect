"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useAppSession } from "./app-session";
import { BrandLogo } from "./brand-logo";

const NAV_ITEMS = [
  { href: "/sync", label: "Sync" },
  { href: "/history", label: "History" },
  { href: "/settings", label: "Settings" },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, loading } = useAppSession();

  const firstName = user?.user_metadata?.first_name?.trim() || "";
  const lastName = user?.user_metadata?.last_name?.trim() || "";
  const displayName = firstName ? `Hi, ${[firstName, lastName].filter(Boolean).join(" ")}` : user?.email || "Signed in";

  const handleLogout = async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    router.replace("/login");
  };

  return (
    <div className="flex min-h-screen flex-col bg-[radial-gradient(circle_at_top_left,rgba(56,189,248,0.09),transparent_28%),radial-gradient(circle_at_bottom_right,rgba(15,23,42,0.08),transparent_25%),linear-gradient(180deg,#f8fafc_0%,#ffffff_42%,#f8fafc_100%)] text-slate-900">
      <header className="sticky top-0 z-40 border-b border-white/70 bg-white/72 backdrop-blur-xl">
        <div className="mx-auto flex max-w-7xl flex-col gap-4 px-4 py-4 sm:px-6 lg:px-8">
          <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
            <Link href="/sync" className="flex items-center gap-3">
              <BrandLogo width={44} height={44} />
              <div>
                <div className="text-lg font-semibold tracking-tight text-slate-950">Zconnect</div>
                <div className="text-xs text-slate-500">Invoice automation for Unify {"->"} Zoho</div>
              </div>
            </Link>

            <div className="flex flex-wrap items-center gap-2">
              {NAV_ITEMS.map((item) => {
                const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={[
                      "rounded-full px-3 py-1.5 text-sm font-medium transition",
                      active ? "bg-slate-950 text-white shadow-sm" : "text-slate-600 hover:bg-slate-100 hover:text-slate-950",
                    ].join(" ")}
                  >
                    {item.label}
                  </Link>
                );
              })}
            </div>

            <div className="flex items-center gap-3">
              <div className="hidden text-right sm:block">
                <div className="text-sm font-medium text-slate-700">
                  {loading ? "Loading session..." : displayName}
                </div>
                <div className="text-xs text-slate-500">
                  {loading ? "..." : user?.role ? user.role.toUpperCase() : "SIGNED IN"}
                </div>
              </div>
              <button
                onClick={handleLogout}
                className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 transition hover:border-slate-400 hover:bg-slate-50"
              >
                Logout
              </button>
            </div>
          </div>
        </div>
      </header>

      <div className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6 lg:px-8">{children}</div>

      <footer className="border-t border-white/70 bg-white/55 px-4 py-8 backdrop-blur-xl sm:px-6 lg:px-8">
        <div className="mx-auto flex max-w-7xl flex-col items-center gap-1 text-center text-[11px] leading-5 text-slate-400">
          <div>Developed by Ziffera</div>
          <div>www.ziffera.ie</div>
          <div>Simple digital solutions for local businesses</div>
        </div>
      </footer>
    </div>
  );
}
