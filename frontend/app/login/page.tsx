"use client";

import Image from "next/image";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const rememberedEmail = window.localStorage.getItem("zconnect:remembered-email");
    if (rememberedEmail) {
      setEmail(rememberedEmail);
    }
  }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (loading) {
      return;
    }

    setLoading(true);
    setError("");
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, rememberMe }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload?.error || payload?.message || "Login failed");
      }

      if (rememberMe) {
        window.localStorage.setItem("zconnect:remembered-email", email);
      } else {
        window.localStorage.removeItem("zconnect:remembered-email");
      }

      router.replace("/sync");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="w-full max-w-md rounded-[2rem] border border-slate-200/80 bg-white/85 p-8 shadow-[0_24px_70px_-35px_rgba(15,23,42,0.35)] backdrop-blur-xl">
        <div className="flex flex-col items-center text-center">
          <div className="mb-6 flex justify-center">
            <div className="rounded-3xl bg-white p-4 shadow-[0_10px_30px_rgba(15,23,42,0.12)]">
              <Image
                src="/log.png"
                alt="Ziffera logo"
                width={100}
                height={100}
                priority
                className="object-contain"
              />
            </div>
          </div>
          <h1 className="text-3xl font-semibold tracking-tight text-slate-950">Zconnect</h1>
          <p className="mt-2 text-sm text-slate-600">Invoice automation for Unify {"->"} Zoho</p>
        </div>

        <form onSubmit={submit} className="mt-8 space-y-4">
          <label className="block">
            <span className="mb-2 block text-sm font-medium text-slate-700">Email</span>
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm outline-none transition focus:border-sky-400 focus:ring-4 focus:ring-sky-100"
              placeholder="you@company.com"
              autoComplete="email"
              required
            />
          </label>

          <label className="block">
            <span className="mb-2 block text-sm font-medium text-slate-700">Password</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm outline-none transition focus:border-sky-400 focus:ring-4 focus:ring-sky-100"
              placeholder="Enter your password"
              autoComplete="current-password"
              required
            />
          </label>

          <label className="flex items-center gap-2 text-sm text-slate-600">
            <input
              type="checkbox"
              checked={rememberMe}
              onChange={(event) => setRememberMe(event.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-slate-950 focus:ring-slate-400"
            />
            Remember me
          </label>

          {error ? (
            <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div>
          ) : null}

          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-2xl bg-slate-950 px-4 py-3.5 text-sm font-semibold text-white transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-70"
          >
            {loading ? "Signing in..." : "Login"}
          </button>

          <div className="mt-6 space-y-1 text-center text-sm text-slate-400">
            <p>
              Developed by <span className="font-medium text-slate-500">Ziffera</span>
            </p>
            <p>
              <a
                href="https://www.ziffera.ie"
                target="_blank"
                rel="noreferrer"
                className="transition hover:text-slate-500"
              >
                www.ziffera.ie
              </a>
            </p>
            <p className="text-xs">Simple digital solutions for local businesses</p>
          </div>
        </form>
      </div>
    </main>
  );
}
