"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

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
    throw new Error("useAppSession must be used within AppSessionProvider");
  }
  return session;
}

export function AppSessionProvider({ children }: { children: ReactNode }) {
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

  return <SessionContext.Provider value={{ user, loading, refresh }}>{children}</SessionContext.Provider>;
}

