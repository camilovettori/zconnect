"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useAppSession } from "../../components/app-session";
import { BrandLogo } from "../../components/brand-logo";
import { Notice } from "../../components/ui/notice";
import { BUTTON_PRIMARY, INPUT, LABEL } from "../../components/ui/styles";

export default function LoginPage() {
  const router = useRouter();
  const { refresh } = useAppSession();
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

      await refresh();
      router.replace("/sync");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 bg-[radial-gradient(ellipse_80%_45%_at_50%_-10%,rgba(63,207,240,0.14),transparent_70%)] px-4 py-10">
      <div className="w-full max-w-sm sm:max-w-md">
        <div className="overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-raised">
          <div aria-hidden="true" className="h-1 bg-gradient-to-r from-brand-700 via-brand-500 to-brand-cyan" />
          <div className="px-6 py-8 sm:px-8">
            <div className="flex flex-col items-center text-center">
              <BrandLogo width={72} height={72} className="rounded-2xl" />
              <h1 className="mt-5 text-2xl font-semibold tracking-tight text-slate-950">Sign in to Zconnect</h1>
              <p className="mt-1.5 text-sm text-slate-600">Invoice automation from Unify to Zoho</p>
            </div>

            <form onSubmit={submit} className="mt-8 space-y-4">
              <label className="block">
                <span className={LABEL}>Email</span>
                <input
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  className={`${INPUT} h-11`}
                  placeholder="you@company.com"
                  autoComplete="email"
                  required
                />
              </label>

              <label className="block">
                <span className={LABEL}>Password</span>
                <div className="relative">
                  <input
                    type={showPassword ? "text" : "password"}
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    className={`${INPUT} h-11 pr-11`}
                    placeholder="Enter your password"
                    autoComplete="current-password"
                    required
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((current) => !current)}
                    className="absolute inset-y-0 right-0 flex items-center justify-center rounded-r-lg px-3 text-slate-400 transition-colors hover:text-slate-600"
                    aria-label={showPassword ? "Hide password" : "Show password"}
                    aria-pressed={showPassword}
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

              <label className="flex w-fit items-center gap-2 text-sm text-slate-600">
                <input
                  type="checkbox"
                  checked={rememberMe}
                  onChange={(event) => setRememberMe(event.target.checked)}
                  className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-200"
                />
                Remember my email
              </label>

              {error ? <Notice kind="error">{error}</Notice> : null}

              <button type="submit" disabled={loading} className={`${BUTTON_PRIMARY} h-11 w-full`}>
                {loading ? (
                  <>
                    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-4 w-4 animate-spin motion-reduce:animate-none">
                      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
                      <path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="3" />
                    </svg>
                    <span>Signing in…</span>
                  </>
                ) : (
                  "Sign in"
                )}
              </button>

              <p className="flex items-center justify-center gap-1.5 text-xs text-slate-400">
                <svg viewBox="0 0 24 24" aria-hidden="true" className="h-3.5 w-3.5">
                  <rect x="5" y="10.5" width="14" height="10" rx="2" fill="none" stroke="currentColor" strokeWidth="1.8" />
                  <path d="M8.5 10.5V7.8a3.5 3.5 0 0 1 7 0v2.7" fill="none" stroke="currentColor" strokeWidth="1.8" />
                </svg>
                Secure sign-in over an encrypted connection
              </p>
            </form>
          </div>
        </div>

        <footer className="mt-6 space-y-1 text-center text-xs text-slate-400">
          <p>
            Developed by <span className="font-medium text-slate-500">Ziffera</span>
            <span className="mx-1.5" aria-hidden="true">·</span>
            <a href="https://www.ziffera.ie" target="_blank" rel="noopener noreferrer" className="transition-colors hover:text-brand-700">
              www.ziffera.ie
            </a>
          </p>
          <p>Simple digital solutions for local businesses</p>
        </footer>
      </div>
    </main>
  );
}
