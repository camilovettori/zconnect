"use client";

import Image from "next/image";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
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
            <div className="rounded-[2rem] border border-white/40 bg-white/65 p-4 shadow-[0_18px_50px_rgba(59,130,246,0.18),0_10px_30px_rgba(15,23,42,0.10)] backdrop-blur-xl">
              <Image
                src="/log.png"
                alt="Zconnect logo"
                width={120}
                height={120}
                priority
                className="h-auto w-[120px] object-contain"
                unoptimized
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
            <div className="relative">
              <input
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 pr-12 text-sm outline-none transition focus:border-sky-400 focus:ring-4 focus:ring-sky-100"
                placeholder="Enter your password"
                autoComplete="current-password"
                required
              />
              <button
                type="button"
                onClick={() => setShowPassword((current) => !current)}
                className="absolute inset-y-0 right-0 flex items-center justify-center px-3 text-slate-400 transition hover:text-slate-600"
                aria-label={showPassword ? "Hide password" : "Show password"}
              >
                {showPassword ? (
                  <svg viewBox="0 0 24 24" aria-hidden="true" className="h-5 w-5">
                    <path
                      d="M3.98 8.223A11.4 11.4 0 0 1 12 5c4.8 0 8.9 2.9 10.02 7-.48 1.77-1.47 3.36-2.84 4.63M6.1 6.1 17.9 17.9M9.88 9.88a3 3 0 1 0 4.24 4.24"
                      fill="none"
                      stroke="currentColor"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth="1.7"
                    />
                  </svg>
                ) : (
                  <svg viewBox="0 0 24 24" aria-hidden="true" className="h-5 w-5">
                    <path
                      d="M2.5 12s3.5-6.5 9.5-6.5S21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"
                      fill="none"
                      stroke="currentColor"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth="1.7"
                    />
                    <circle cx="12" cy="12" r="2.6" fill="none" stroke="currentColor" strokeWidth="1.7" />
                  </svg>
                )}
              </button>
            </div>
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
            className="flex w-full items-center justify-center gap-2 rounded-2xl bg-slate-950 px-4 py-3.5 text-sm font-semibold text-white transition duration-300 hover:bg-slate-900 hover:shadow-[0_12px_24px_-16px_rgba(15,23,42,0.75)] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-70"
          >
            {loading ? (
              <>
                <svg viewBox="0 0 24 24" aria-hidden="true" className="h-4 w-4 animate-spin">
                  <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeOpacity="0.2" strokeWidth="3" />
                  <path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="3" />
                </svg>
                <span>Signing in...</span>
              </>
            ) : (
              "Login"
            )}
          </button>

          <p className="text-center text-xs text-slate-400">🔒 Secure login • Encrypted connection</p>

          <div className="mt-6 space-y-1 text-center text-sm text-slate-400">
            <p>
              Developed by <span className="font-medium text-slate-500/80">Ziffera</span>
            </p>
            <p>
              <a
                href="https://www.ziffera.ie"
                target="_blank"
                rel="noreferrer"
                className="transition hover:text-slate-500/80 hover:underline"
              >
                www.ziffera.ie
              </a>
            </p>
            <p className="text-xs text-slate-400/80">Simple digital solutions for local businesses</p>
          </div>
        </form>
      </div>
    </main>
  );
}
