"use client";

import { useEffect, useMemo, useRef, useState } from "react";

type SettingsState = {
  ZOHO_BASE_URL: string;
  ZOHO_STANDARD_TAX_ID: string;
  ZOHO_REDUCED_TAX_ID: string;
  ZOHO_ZERO_TAX_ID: string;
  UNIFY_CLIENT_ID: string;
  UNIFY_CLIENT_SECRET: string;
  ZOHO_CLIENT_ID: string;
  ZOHO_CLIENT_SECRET: string;
  ZOHO_REFRESH_TOKEN: string;
  ZOHO_ORG_ID: string;
};

type StoredSettingsFlags = {
  has_unify_client_id: boolean;
  has_unify_client_secret: boolean;
  has_zoho_client_id: boolean;
  has_zoho_client_secret: boolean;
  has_zoho_refresh_token: boolean;
  has_zoho_organization_id: boolean;
};

type SettingsResponse = Partial<SettingsState> & StoredSettingsFlags;
type SecretFlagKey = keyof StoredSettingsFlags;
type FieldKey = keyof SettingsState;

type FieldConfig = {
  key: FieldKey;
  label: string;
  helper: string;
  secret?: boolean;
  flagKey?: SecretFlagKey;
};

type ToastKind = "success" | "neutral" | "error";

const SECRET_FIELDS = new Set<FieldKey>([
  "UNIFY_CLIENT_ID",
  "UNIFY_CLIENT_SECRET",
  "ZOHO_CLIENT_ID",
  "ZOHO_CLIENT_SECRET",
  "ZOHO_REFRESH_TOKEN",
  "ZOHO_ORG_ID",
]);

const FIELD_SECTIONS: Array<{
  title: string;
  description: string;
  accent: string;
  fields: FieldConfig[];
}> = [
  {
    title: "Unify",
    description: "Credentials used to read orders and buyers from Unify.",
    accent: "from-emerald-500/20 via-emerald-500/5 to-transparent",
    fields: [
      {
        key: "UNIFY_CLIENT_ID",
        label: "Client ID",
        helper: "Stored securely. Leave blank to keep current value.",
        secret: true,
        flagKey: "has_unify_client_id",
      },
      {
        key: "UNIFY_CLIENT_SECRET",
        label: "Client Secret",
        helper: "Stored securely. Leave blank to keep current value.",
        secret: true,
        flagKey: "has_unify_client_secret",
      },
    ],
  },
  {
    title: "Zoho",
    description: "Credentials used to create and manage Zoho Invoice drafts.",
    accent: "from-sky-500/20 via-sky-500/5 to-transparent",
    fields: [
      {
        key: "ZOHO_BASE_URL",
        label: "Base URL",
        helper: "This endpoint can be edited directly if your Zoho region changes.",
      },
      {
        key: "ZOHO_CLIENT_ID",
        label: "Client ID",
        helper: "Stored securely. Leave blank to keep current value.",
        secret: true,
        flagKey: "has_zoho_client_id",
      },
      {
        key: "ZOHO_CLIENT_SECRET",
        label: "Client Secret",
        helper: "Stored securely. Leave blank to keep current value.",
        secret: true,
        flagKey: "has_zoho_client_secret",
      },
      {
        key: "ZOHO_REFRESH_TOKEN",
        label: "Refresh Token",
        helper: "Stored securely. Leave blank to keep current value.",
        secret: true,
        flagKey: "has_zoho_refresh_token",
      },
      {
        key: "ZOHO_ORG_ID",
        label: "Organization ID",
        helper: "Stored securely. Leave blank to keep current value.",
        secret: true,
        flagKey: "has_zoho_organization_id",
      },
      {
        key: "ZOHO_STANDARD_TAX_ID",
        label: "Standard Tax ID",
        helper: "Zoho tax ID used for standard VAT lines.",
      },
      {
        key: "ZOHO_REDUCED_TAX_ID",
        label: "Reduced Tax ID",
        helper: "Zoho tax ID used for reduced VAT lines.",
      },
      {
        key: "ZOHO_ZERO_TAX_ID",
        label: "Zero Tax ID",
        helper: "Zoho tax ID used for zero VAT lines.",
      },
    ],
  },
];

const EMPTY_SETTINGS: SettingsState = {
  ZOHO_BASE_URL: "https://www.zohoapis.eu",
  ZOHO_STANDARD_TAX_ID: "",
  ZOHO_REDUCED_TAX_ID: "",
  ZOHO_ZERO_TAX_ID: "",
  UNIFY_CLIENT_ID: "",
  UNIFY_CLIENT_SECRET: "",
  ZOHO_CLIENT_ID: "",
  ZOHO_CLIENT_SECRET: "",
  ZOHO_REFRESH_TOKEN: "",
  ZOHO_ORG_ID: "",
};

const EMPTY_FLAGS: StoredSettingsFlags = {
  has_unify_client_id: false,
  has_unify_client_secret: false,
  has_zoho_client_id: false,
  has_zoho_client_secret: false,
  has_zoho_refresh_token: false,
  has_zoho_organization_id: false,
};

const SETTINGS_LOAD_TIMEOUT_MS = 10000;

function isMaskedSecretValue(value: string) {
  const text = value.trim();
  if (!text) {
    return false;
  }

  return text === "********" || /^[*\u2022\u25CF\u25AA\u25AB]+$/.test(text);
}

function LockIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" fill="none" className={className}>
      <path
        d="M5.5 8.5V6.75A4.5 4.5 0 0 1 10 2.25a4.5 4.5 0 0 1 4.5 4.5V8.5"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <path
        d="M6.25 8.5h7.5c.69 0 1.25.56 1.25 1.25v4.5c0 .69-.56 1.25-1.25 1.25h-7.5A1.25 1.25 0 0 1 5 14.25v-4.5C5 9.06 5.56 8.5 6.25 8.5Z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CheckIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" fill="none" className={className}>
      <path
        d="m5.5 10.25 2.75 2.75L14.75 6.5"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ShieldCheckIcon({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" fill="none" className={className}>
      <path
        d="M10 2.25 15.25 4v4.2c0 3.7-2.05 6.82-5.25 8.55-3.2-1.73-5.25-4.85-5.25-8.55V4L10 2.25Z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
      <path
        d="m7.3 10.05 1.55 1.55 3.85-3.85"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function SparkIcon({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" fill="none" className={className}>
      <path
        d="M10 2.5l1.4 4.1 4.1 1.4-4.1 1.4-1.4 4.1-1.4-4.1-4.1-1.4 4.1-1.4L10 2.5Z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function ConnectionsPanel() {
  const [formValues, setFormValues] = useState<SettingsState>(EMPTY_SETTINGS);
  const [initialFormValues, setInitialFormValues] = useState<SettingsState>(EMPTY_SETTINGS);
  const [storedFlags, setStoredFlags] = useState<StoredSettingsFlags>(EMPTY_FLAGS);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [message, setMessage] = useState<string>("");
  const [messageKind, setMessageKind] = useState<ToastKind>("success");
  const [isSaving, setIsSaving] = useState(false);
  const messageTimer = useRef<number | null>(null);

  const storedCount = useMemo(
    () => Object.values(storedFlags).filter(Boolean).length,
    [storedFlags],
  );
  const hasStoredCredentials = settingsLoaded && storedCount > 0;
  const statusLabel = !settingsLoaded
    ? "Loading saved credentials..."
    : hasStoredCredentials
      ? "Credentials securely stored"
      : "Credentials not stored yet";

  useEffect(() => {
    const loadSettings = async () => {
      const controller = new AbortController();
      const timeoutId = window.setTimeout(() => controller.abort(), SETTINGS_LOAD_TIMEOUT_MS);

      try {
        const res = await fetch("/api/settings", { signal: controller.signal });
        if (!res.ok) {
          const errorBody = await res.text();
          throw new Error(errorBody || `HTTP ${res.status}`);
        }

        const data: SettingsResponse = await res.json();

        const nextSettings = {
          ...EMPTY_SETTINGS,
          ZOHO_BASE_URL: data.ZOHO_BASE_URL ?? EMPTY_SETTINGS.ZOHO_BASE_URL,
          ZOHO_STANDARD_TAX_ID: data.ZOHO_STANDARD_TAX_ID ?? EMPTY_SETTINGS.ZOHO_STANDARD_TAX_ID,
          ZOHO_REDUCED_TAX_ID: data.ZOHO_REDUCED_TAX_ID ?? EMPTY_SETTINGS.ZOHO_REDUCED_TAX_ID,
          ZOHO_ZERO_TAX_ID: data.ZOHO_ZERO_TAX_ID ?? EMPTY_SETTINGS.ZOHO_ZERO_TAX_ID,
        };

        setFormValues(nextSettings);
        setInitialFormValues(nextSettings);

        setStoredFlags({
          has_unify_client_id: Boolean(data.has_unify_client_id),
          has_unify_client_secret: Boolean(data.has_unify_client_secret),
          has_zoho_client_id: Boolean(data.has_zoho_client_id),
          has_zoho_client_secret: Boolean(data.has_zoho_client_secret),
          has_zoho_refresh_token: Boolean(data.has_zoho_refresh_token),
          has_zoho_organization_id: Boolean(data.has_zoho_organization_id),
        });
        setSettingsLoaded(true);
      } finally {
        window.clearTimeout(timeoutId);
      }
    };

    loadSettings().catch(() => {
      setMessageKind("error");
      setMessage("Unable to load settings. The backend may be busy or unreachable.");
      setSettingsLoaded(true);
    });
  }, []);

  useEffect(() => {
    if (!message) {
      return;
    }

    if (messageTimer.current) {
      window.clearTimeout(messageTimer.current);
    }

    messageTimer.current = window.setTimeout(() => {
      setMessage("");
    }, 3500);

    return () => {
      if (messageTimer.current) {
        window.clearTimeout(messageTimer.current);
      }
    };
  }, [message]);

  const showToast = (text: string, kind: ToastKind = "success") => {
    setMessageKind(kind);
    setMessage(text);
  };

  const hasPendingChanges = () => {
    if (!settingsLoaded) {
      return false;
    }

    return (Object.keys(formValues) as FieldKey[]).some((key) => {
      const nextValue = formValues[key].trim();
      const initialValue = initialFormValues[key].trim();
      if (SECRET_FIELDS.has(key)) {
        return nextValue !== "" && !isMaskedSecretValue(nextValue);
      }
      return nextValue !== initialValue;
    });
  };

  const handleSave = async () => {
    if (isSaving) {
      return;
    }

    const payload = Object.fromEntries(
      (Object.keys(formValues) as FieldKey[])
        .map((key) => [key, formValues[key].trim()] as const)
        .filter(([key, value]) => {
          if (SECRET_FIELDS.has(key)) {
            return value !== "" && !isMaskedSecretValue(value);
          }
          return value !== initialFormValues[key].trim();
        }),
    ) as Partial<SettingsState>;

    if (Object.keys(payload).length === 0) {
      showToast("No changes to save", "neutral");
      return;
    }

    setIsSaving(true);
    try {
      const response = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        throw new Error("Failed to save settings");
      }

      const clearedSecrets = {
        UNIFY_CLIENT_ID: "",
        UNIFY_CLIENT_SECRET: "",
        ZOHO_CLIENT_ID: "",
        ZOHO_CLIENT_SECRET: "",
        ZOHO_REFRESH_TOKEN: "",
        ZOHO_ORG_ID: "",
      };

      const nextSettings = {
        ...formValues,
        ...Object.fromEntries(
          (Object.keys(payload) as FieldKey[]).map((key) => [key, payload[key] ?? ""] as const),
        ),
        ...clearedSecrets,
      } as SettingsState;

      setFormValues(nextSettings);
      setInitialFormValues({
        ...initialFormValues,
        ...Object.fromEntries(
          (Object.keys(payload) as FieldKey[]).map((key) => [key, payload[key] ?? initialFormValues[key]] as const),
        ),
        ...clearedSecrets,
      });

      setStoredFlags((prev) => {
        const next = { ...prev };
        for (const key of Object.keys(payload) as FieldKey[]) {
          if (key === "UNIFY_CLIENT_ID") next.has_unify_client_id = true;
          if (key === "UNIFY_CLIENT_SECRET") next.has_unify_client_secret = true;
          if (key === "ZOHO_CLIENT_ID") next.has_zoho_client_id = true;
          if (key === "ZOHO_CLIENT_SECRET") next.has_zoho_client_secret = true;
          if (key === "ZOHO_REFRESH_TOKEN") next.has_zoho_refresh_token = true;
          if (key === "ZOHO_ORG_ID") next.has_zoho_organization_id = true;
        }
        return next;
      });

      showToast("Settings updated successfully", "success");
    } catch {
      showToast("Failed to save settings. Please try again.", "error");
    } finally {
      setIsSaving(false);
    }
  };

  const testUnify = async () => {
    const res = await fetch("/api/settings/unify/test", { method: "POST" });
    const data = await res.json();
    showToast(`Unify test: ${data.message}`, res.ok ? "success" : "error");
  };

  const testZoho = async () => {
    const res = await fetch("/api/settings/zoho/test", { method: "POST" });
    const data = await res.json();
    showToast(`Zoho test: ${data.message}`, res.ok ? "success" : "error");
  };

  return (
    <main className="relative overflow-hidden rounded-3xl border border-slate-200/80 bg-white/90 p-6 shadow-[0_20px_60px_-30px_rgba(15,23,42,0.28)] backdrop-blur">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(56,189,248,0.12),transparent_36%),radial-gradient(circle_at_bottom_right,rgba(16,185,129,0.12),transparent_28%)]" />
      <div className="relative space-y-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="max-w-2xl">
            <div
              className={[
                "inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs font-medium shadow-sm",
                hasStoredCredentials
                  ? "border border-emerald-200 bg-emerald-50 text-emerald-700"
                  : "border border-slate-200 bg-slate-50 text-slate-600",
              ].join(" ")}
            >
              <ShieldCheckIcon className={`h-4 w-4 ${hasStoredCredentials ? "text-emerald-600" : "text-slate-500"}`} />
              {statusLabel}
            </div>
            <h2 className="mt-4 text-3xl font-semibold tracking-tight text-slate-900">Connections</h2>
            <p className="mt-2 text-sm leading-6 text-slate-600">
              Secret values stay hidden here. Stored credentials are marked below, and blank secure fields keep the current value when you save.
            </p>
          </div>

          <div className="grid min-w-[220px] gap-3 rounded-2xl border border-slate-200 bg-slate-50/80 p-4 shadow-sm">
            <div className="flex items-center justify-between text-sm text-slate-600">
              <span>Stored fields</span>
              <span className="font-semibold text-slate-900">{settingsLoaded ? storedCount : "Loading..."}</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-slate-200">
              <div
                className="h-full rounded-full bg-gradient-to-r from-emerald-500 to-sky-500 transition-all"
                style={{ width: settingsLoaded ? `${(storedCount / 6) * 100}%` : "0%" }}
              />
            </div>
            <p className="text-xs text-slate-500">
              {!settingsLoaded
                ? "Checking securely stored credentials..."
                : hasStoredCredentials
                  ? "At least one credential is already saved."
                  : "No credentials have been stored yet."}
            </p>
          </div>
        </div>

        {message && (
          <div
            className={[
              "flex items-start gap-3 rounded-2xl border px-4 py-3 shadow-sm",
              messageKind === "success"
                ? "border-emerald-200 bg-emerald-50 text-emerald-900"
                : messageKind === "error"
                  ? "border-rose-200 bg-rose-50 text-rose-900"
                  : "border-sky-200 bg-sky-50 text-sky-900",
            ].join(" ")}
          >
            <div
              className={[
                "mt-0.5 rounded-full p-1",
                messageKind === "success"
                  ? "bg-emerald-100 text-emerald-700"
                  : messageKind === "error"
                    ? "bg-rose-100 text-rose-700"
                    : "bg-sky-100 text-sky-700",
              ].join(" ")}
            >
              {messageKind === "success" ? <CheckIcon /> : <SparkIcon />}
            </div>
            <p className="text-sm font-medium">{message}</p>
          </div>
        )}

        <div className="grid gap-5 xl:grid-cols-2">
          {FIELD_SECTIONS.map((section) => (
            <section
              key={section.title}
              className={`rounded-3xl border border-slate-200 bg-gradient-to-br ${section.accent} p-5 shadow-sm`}
            >
              <div className="mb-5 flex items-start justify-between gap-4">
                <div>
                  <h3 className="text-lg font-semibold text-slate-900">{section.title}</h3>
                  <p className="mt-1 text-sm text-slate-600">{section.description}</p>
                </div>
                <div className="rounded-full border border-white/70 bg-white/80 px-3 py-1 text-xs font-medium text-slate-600 shadow-sm">
                  Secure
                </div>
              </div>

              <div className="space-y-4">
                {section.fields.map((field) => {
                  const isStored = field.flagKey ? storedFlags[field.flagKey] : false;
                  const isSecret = Boolean(field.secret);
                  const inputValue = formValues[field.key];
                  const fieldState = !settingsLoaded ? "loading" : isStored ? "saved" : "empty";

                  return (
                    <label
                      key={field.key}
                      className={[
                        "block rounded-2xl border p-4 transition-shadow",
                        fieldState === "saved"
                          ? "border-emerald-200 bg-white/80 shadow-[0_0_0_1px_rgba(16,185,129,0.08),0_12px_30px_-20px_rgba(16,185,129,0.35)]"
                          : fieldState === "loading"
                            ? "border-sky-200 bg-white/75 shadow-sm"
                            : "border-slate-200 bg-white/75 shadow-sm",
                      ].join(" ")}
                    >
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-sm font-semibold text-slate-900">{field.label}</span>
                        <div
                          className={[
                            "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
                            fieldState === "saved"
                              ? "border border-emerald-200 bg-emerald-50 text-emerald-700"
                              : fieldState === "loading"
                                ? "border border-sky-200 bg-sky-50 text-sky-700"
                                : "border border-slate-200 bg-slate-50 text-slate-500",
                          ].join(" ")}
                        >
                          {fieldState === "saved" ? <CheckIcon className="h-3.5 w-3.5" /> : <LockIcon className="h-3.5 w-3.5" />}
                          {fieldState === "saved" ? "Saved" : fieldState === "loading" ? "Loading" : "Not stored"}
                        </div>
                      </div>

                      <div className="relative mt-3">
                        {fieldState === "saved" && isSecret && (
                          <div className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-emerald-600">
                            <LockIcon className="h-4 w-4" />
                          </div>
                        )}
                        <input
                          type={isSecret ? "password" : "text"}
                          className={[
                            "w-full rounded-xl border bg-white px-3 py-2.5 text-sm text-slate-900 outline-none transition",
                            fieldState === "saved" && isSecret ? "pl-10" : "",
                            "border-slate-200 placeholder:text-slate-400 focus:border-sky-400 focus:ring-4 focus:ring-sky-100",
                          ].join(" ")}
                          placeholder={
                            isSecret
                              ? fieldState === "saved"
                                ? "Stored securely"
                                : fieldState === "loading"
                                  ? "Checking saved value..."
                                : "Enter a new value"
                              : field.key === "ZOHO_BASE_URL"
                                ? "https://www.zohoapis.eu"
                                : "Enter value"
                          }
                          value={inputValue}
                          onChange={(e) => setFormValues({ ...formValues, [field.key]: e.target.value })}
                          autoComplete="off"
                          spellCheck={false}
                        />
                      </div>

                      <p className="mt-2 text-xs leading-5 text-slate-500">
                        {!settingsLoaded && isSecret ? "Checking securely stored value..." : field.helper}
                      </p>
                    </label>
                  );
                })}
              </div>
            </section>
          ))}
        </div>

        <div className="flex flex-col gap-3 rounded-3xl border border-slate-200 bg-slate-50/80 p-4 shadow-sm sm:flex-row sm:items-center">
          <button
            onClick={handleSave}
            disabled={isSaving || !hasPendingChanges()}
            className="inline-flex items-center justify-center rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white shadow-[0_10px_24px_-14px_rgba(15,23,42,0.8)] transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isSaving ? "Saving..." : "Save settings"}
          </button>
          <button
            onClick={testUnify}
            className="inline-flex items-center justify-center rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 transition hover:border-slate-400 hover:bg-slate-50"
          >
            Test Unify
          </button>
          <button
            onClick={testZoho}
            className="inline-flex items-center justify-center rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 transition hover:border-slate-400 hover:bg-slate-50"
          >
            Test Zoho Connection
          </button>
          <div className="text-sm text-slate-500">
            {!settingsLoaded
              ? "Loading settings..."
              : hasPendingChanges()
                ? "You have unsaved changes."
                : "No changes to save."}
          </div>
        </div>
      </div>
    </main>
  );
}

export default function SettingsPage() {
  return (
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
  );
}
