"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

type SessionUser = {
  id: string;
  email: string;
  role: string;
};

type SessionState = {
  user: SessionUser | null;
  loading: boolean;
  refresh: () => Promise<void>;
};

const SessionContext = createContext<SessionState | null>(null);

const NAV_ITEMS = [
  { href: "/sync", label: "Sync" },
  { href: "/history", label: "History" },
  { href: "/connections", label: "Connections" },
  { href: "/users", label: "Users", adminOnly: true },
  { href: "/settings", label: "Settings" },
];

function BrandMark() {
  return (
    <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-950 text-white shadow-[0_18px_40px_-20px_rgba(15,23,42,0.9)]">
      <span className="text-xl font-semibold tracking-tight">Z</span>
    </div>
  );
}

async function loadSession() {
  const response = await fetch("/api/auth/session", { cache: "no-store" });
  if (!response.ok) {
    return null;
  }
  const payload = await response.json().catch(() => null);
  return payload?.user ?? null;
}

export function useAppSession() {
  const session = useContext(SessionContext);
  if (!session) {
    throw new Error("useAppSession must be used within AppShell");
  }
  return session;
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<SessionUser | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    setLoading(true);
    try {
      const nextUser = await loadSession();
      setUser(nextUser);
      if (!nextUser && pathname !== "/login") {
        router.replace("/login");
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh().catch(() => {
      router.replace("/login");
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const visibleNav = useMemo(
    () => NAV_ITEMS.filter((item) => !item.adminOnly || user?.role === "admin"),
    [user?.role],
  );

  const handleLogout = async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    router.replace("/login");
  };

  return (
    <SessionContext.Provider value={{ user, loading, refresh }}>
      <div className="min-h-screen bg-[radial-gradient(circle_at_top_left,rgba(56,189,248,0.09),transparent_28%),radial-gradient(circle_at_bottom_right,rgba(15,23,42,0.08),transparent_25%),linear-gradient(180deg,#f8fafc_0%,#ffffff_42%,#f8fafc_100%)] text-slate-900">
        <header className="sticky top-0 z-40 border-b border-white/70 bg-white/72 backdrop-blur-xl">
          <div className="mx-auto flex max-w-7xl flex-col gap-4 px-4 py-4 sm:px-6 lg:px-8">
            <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
              <Link href="/sync" className="flex items-center gap-3">
                <BrandMark />
                <div>
                  <div className="text-lg font-semibold tracking-tight text-slate-950">Zconnect</div>
                  <div className="text-xs text-slate-500">Invoice automation for Unify → Zoho</div>
                </div>
              </Link>

              <div className="flex flex-wrap items-center gap-2">
                {visibleNav.map((item) => {
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
                  <div className="text-sm font-medium text-slate-700">{user?.email ?? "Loading session..."}</div>
                  <div className="text-xs text-slate-500">{user?.role ? user.role.toUpperCase() : "..."}</div>
                </div>
                <button
                  onClick={handleLogout}
                  className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 transition hover:border-slate-400 hover:bg-slate-50"
                >
                  Logout
                </button>
              </div>
            </div>
            <div className="text-[11px] text-slate-400">Developed by Ziffera • www.ziffera.ie • Simple digital solutions for local businesses</div>
          </div>
        </header>

        <div className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 lg:px-8">{children}</div>
      </div>
    </SessionContext.Provider>
  );
}
