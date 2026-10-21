"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { roleLabel, userDisplayName, userInitials } from "../lib/user-display";
import { useAppSession } from "./app-session";
import { BrandLogo } from "./brand-logo";

const NAV_ITEMS = [
  { href: "/sync", label: "Sync", match: ["/sync"] },
  { href: "/history", label: "History", match: ["/history"] },
  { href: "/analytics", label: "Analytics", match: ["/analytics"] },
  { href: "/settings", label: "Settings", match: ["/settings", "/connections", "/users"] },
];

function isActivePath(pathname: string, prefixes: string[]) {
  return prefixes.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, loading } = useAppSession();
  const [signingOut, setSigningOut] = useState(false);

  const displayName = userDisplayName(user);
  const initials = userInitials(user);

  const handleLogout = async () => {
    if (signingOut) {
      return;
    }
    setSigningOut(true);
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } catch {
      // The session cookie is HttpOnly; whatever happened, leave the app. The page
      // middleware validates the session again on the next request.
    }
    router.replace("/login");
  };

  const nav = (
    <nav aria-label="Main" className="flex items-center gap-1">
      {NAV_ITEMS.map((item) => {
        const active = isActivePath(pathname, item.match);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={[
              "whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium transition-colors duration-150 motion-reduce:transition-none",
              active ? "bg-brand-50 text-brand-800" : "text-slate-500 hover:bg-slate-900/[0.04] hover:text-slate-900",
            ].join(" ")}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );

  return (
    <div className="flex min-h-screen flex-col bg-slate-50 bg-[radial-gradient(ellipse_80%_40%_at_50%_-10%,rgba(63,207,240,0.10),transparent_70%)] text-slate-900">
      <a
        href="#content"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-3 focus:z-50 focus:rounded-lg focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:shadow-raised"
      >
        Skip to content
      </a>

      <header className="sticky top-0 z-40 border-b border-slate-200/80 bg-white/80 backdrop-blur-md">
        <div aria-hidden="true" className="h-0.5 bg-gradient-to-r from-brand-700 via-brand-500 to-brand-cyan" />
        <div className="mx-auto flex h-14 max-w-7xl items-center justify-between gap-3 px-4 sm:h-16 sm:gap-4 sm:px-6 lg:px-8">
          <div className="flex min-w-0 items-center gap-4 lg:gap-6">
            <Link href="/sync" className="flex shrink-0 items-center gap-2.5 rounded-lg" aria-label="Zconnect home">
              <BrandLogo width={32} height={32} />
              <span className="flex flex-col leading-tight">
                <span className="text-[15px] font-semibold tracking-tight text-slate-950">Zconnect</span>
                <span className="hidden text-[11px] text-slate-500 lg:block">Invoice automation to Zoho</span>
              </span>
            </Link>
            <span aria-hidden="true" className="hidden h-5 w-px bg-slate-200 md:block" />
            <div className="hidden md:block">{nav}</div>
          </div>

          <div className="flex min-w-0 shrink-0 items-center gap-2.5 sm:gap-3">
            {loading ? (
              <div className="flex items-center gap-3" aria-label="Loading your account">
                <div className="hidden space-y-1.5 md:block">
                  <div className="ml-auto h-3 w-28 animate-pulse rounded-full bg-slate-200 motion-reduce:animate-none" />
                  <div className="ml-auto h-2.5 w-12 animate-pulse rounded-full bg-slate-100 motion-reduce:animate-none" />
                </div>
                <div className="h-8 w-8 animate-pulse rounded-full bg-slate-200 motion-reduce:animate-none" />
              </div>
            ) : user ? (
              <div className="flex min-w-0 items-center gap-3">
                <div className="hidden min-w-0 text-right md:block">
                  <div className="max-w-[14rem] truncate text-sm font-medium text-slate-800" title={user.email || undefined}>
                    {displayName}
                  </div>
                  <div className="text-[11px] font-medium uppercase tracking-[0.08em] text-slate-400">{roleLabel(user.role)}</div>
                </div>
                <span
                  role="img"
                  aria-label={`Signed in as ${displayName}`}
                  title={displayName}
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-brand-600 to-brand-cyan text-xs font-semibold text-white"
                >
                  {initials || "?"}
                </span>
              </div>
            ) : null}
            <button
              type="button"
              onClick={handleLogout}
              disabled={signingOut}
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 shadow-card transition-colors hover:border-slate-300 hover:bg-slate-50 hover:text-slate-950 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {signingOut ? "Signing out…" : "Logout"}
            </button>
          </div>
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-slate-200/70 px-2 py-1.5 md:hidden">
          <div className="min-w-0 overflow-x-auto">{nav}</div>
          {!loading && user && (
            <span className="min-w-0 truncate pr-2 text-xs font-medium text-slate-600" title={user.email || undefined}>
              {displayName}
            </span>
          )}
        </div>
      </header>

      <div id="content" className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6 sm:py-8 lg:px-8">
        {children}
      </div>

      <footer className="border-t border-slate-200/70 bg-white/60">
        <div className="mx-auto flex max-w-7xl flex-col items-center gap-1 px-4 py-6 text-center text-xs text-slate-400 sm:flex-row sm:justify-between sm:px-6 sm:text-left lg:px-8">
          <div>
            <span className="font-medium text-slate-500">Developed by Ziffera</span>
            <span className="mx-2 hidden sm:inline" aria-hidden="true">·</span>
            <span className="block sm:inline">Simple digital solutions for local businesses</span>
          </div>
          <a
            href="https://www.ziffera.ie"
            target="_blank"
            rel="noopener noreferrer"
            className="rounded transition-colors hover:text-brand-700"
          >
            www.ziffera.ie
          </a>
        </div>
      </footer>
    </div>
  );
}
