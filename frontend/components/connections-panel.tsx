"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useAppSession } from "./app-session";
import { Notice } from "./ui/notice";
import { PageHeader } from "./ui/page-header";
import { BUTTON_PRIMARY, BUTTON_SECONDARY, CARD, INPUT } from "./ui/styles";

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
  fields: FieldConfig[];
}> = [
  {
    title: "Unify",
    description: "Credentials used to read orders and buyers from Unify.",
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

export function ConnectionsPanel() {
  // Display only: the backend enforces admin permission for saving and testing connections.
  const { user } = useAppSession();
  const isAdmin = user?.role === "admin";
  const [formValues, setFormValues] = useState<SettingsState>(EMPTY_SETTINGS);
  const [initialFormValues, setInitialFormValues] = useState<SettingsState>(EMPTY_SETTINGS);
  const [storedFlags, setStoredFlags] = useState<StoredSettingsFlags>(EMPTY_FLAGS);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [message, setMessage] = useState<string>("");
  const [messageKind, setMessageKind] = useState<ToastKind>("success");
  const [isSaving, setIsSaving] = useState(false);
  const [testing, setTesting] = useState<"unify" | "zoho" | null>(null);
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
    // Confirmations disappear on their own; errors stay until dismissed or replaced.
    if (!message || messageKind === "error") {
      return;
    }

    if (messageTimer.current) {
      window.clearTimeout(messageTimer.current);
    }

    messageTimer.current = window.setTimeout(() => {
      setMessage("");
    }, 5000);

    return () => {
      if (messageTimer.current) {
        window.clearTimeout(messageTimer.current);
      }
    };
  }, [message, messageKind]);

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

  // The backend answers a failed test with HTTP 200 and ok=false, so both must be checked.
  const testConnection = async (target: "unify" | "zoho") => {
    if (testing) {
      return;
    }
    const label = target === "unify" ? "Unify" : "Zoho";
    setTesting(target);
    try {
      const res = await fetch(`/api/settings/${target}/test`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      const passed = res.ok && data?.ok === true;
      const detail = String(data?.message || data?.detail || data?.error || (res.ok ? "" : `HTTP ${res.status}`)).trim();
      showToast(
        passed ? `${label} connection works.` : `${label} connection failed${detail ? `: ${detail}` : "."}`,
        passed ? "success" : "error",
      );
    } catch {
      showToast(`The ${label} connection test could not be completed. Check your network and try again.`, "error");
    } finally {
      setTesting(null);
    }
  };

  const noticeKind = messageKind === "neutral" ? "info" : messageKind;
  const secretTotal = Object.keys(EMPTY_FLAGS).length;
  const statusLine = !isAdmin
    ? "Only admins can change or test connections."
    : !settingsLoaded
      ? "Loading settings…"
      : isSaving
        ? "Saving changes…"
        : testing
          ? `Testing the ${testing === "unify" ? "Unify" : "Zoho"} connection…`
          : hasPendingChanges()
            ? "You have unsaved changes."
            : "All changes saved.";

  return (
    <main className="space-y-6">
      <PageHeader
        eyebrow="Settings"
        title="Connections"
        description="Credentials Zconnect uses to read orders from Unify and create draft invoices in Zoho. Saved secrets are never shown again: leave a secure field blank to keep its stored value."
        actions={
          <span
            className={[
              "inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium",
              hasStoredCredentials ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-slate-200 bg-white text-slate-600",
            ].join(" ")}
          >
            <ShieldCheckIcon className={`h-4 w-4 ${hasStoredCredentials ? "text-emerald-600" : "text-slate-400"}`} />
            {settingsLoaded ? `${storedCount} of ${secretTotal} secure values stored` : statusLabel}
          </span>
        }
      />

      {!isAdmin && settingsLoaded && (
        <Notice kind="info">You can see which credentials are stored. Only admins can change or test connections.</Notice>
      )}

      {message && (
        <Notice kind={noticeKind} onDismiss={() => setMessage("")}>
          {message}
        </Notice>
      )}

      <div className="grid gap-5 xl:grid-cols-2 xl:items-start">
        {FIELD_SECTIONS.map((section) => {
          const sectionSecrets = section.fields.filter((field) => field.flagKey);
          const sectionStored = sectionSecrets.filter((field) => field.flagKey && storedFlags[field.flagKey]).length;
          return (
            <section key={section.title} aria-labelledby={`connection-${section.title}`} className={`${CARD} p-5 sm:p-6`}>
              <div className="mb-5 flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <h2 id={`connection-${section.title}`} className="text-base font-semibold tracking-tight text-slate-950">
                    {section.title}
                  </h2>
                  <p className="mt-1 text-sm text-slate-600">{section.description}</p>
                </div>
                {settingsLoaded && sectionSecrets.length > 0 && (
                  <span className="shrink-0 whitespace-nowrap rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-xs font-medium tabular-nums text-slate-600">
                    {sectionStored}/{sectionSecrets.length} stored
                  </span>
                )}
              </div>

              <div className="space-y-4">
                {section.fields.map((field) => {
                  const isStored = field.flagKey ? storedFlags[field.flagKey] : false;
                  const isSecret = Boolean(field.secret);
                  const inputValue = formValues[field.key];
                  const fieldState = !settingsLoaded ? "loading" : isStored ? "saved" : "empty";

                  return (
                    <label key={field.key} className="block">
                      <span className="mb-1.5 flex items-center justify-between gap-3">
                        <span className="text-sm font-medium text-slate-700">{field.label}</span>
                        {isSecret && (
                          <span
                            className={[
                              "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium",
                              fieldState === "saved"
                                ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                                : fieldState === "loading"
                                  ? "border-slate-200 bg-slate-50 text-slate-500"
                                  : "border-amber-200 bg-amber-50 text-amber-700",
                            ].join(" ")}
                          >
                            {fieldState === "saved" ? <CheckIcon className="h-3 w-3" /> : <LockIcon className="h-3 w-3" />}
                            {fieldState === "saved" ? "Stored" : fieldState === "loading" ? "Checking…" : "Not stored"}
                          </span>
                        )}
                      </span>
                      <span className="relative block">
                        {fieldState === "saved" && isSecret && (
                          <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-emerald-600">
                            <LockIcon className="h-4 w-4" />
                          </span>
                        )}
                        <input
                          type={isSecret ? "password" : "text"}
                          className={`${INPUT} ${fieldState === "saved" && isSecret ? "pl-9" : ""}`}
                          placeholder={
                            isSecret
                              ? fieldState === "saved"
                                ? "Stored securely — leave blank to keep"
                                : fieldState === "loading"
                                  ? "Checking saved value…"
                                  : "Enter a value"
                              : field.key === "ZOHO_BASE_URL"
                                ? "https://www.zohoapis.eu"
                                : "Enter value"
                          }
                          value={inputValue}
                          onChange={(e) => setFormValues({ ...formValues, [field.key]: e.target.value })}
                          disabled={!isAdmin}
                          autoComplete="off"
                          spellCheck={false}
                        />
                      </span>
                      <span className="mt-1.5 block text-xs leading-5 text-slate-500">
                        {isSecret && fieldState === "empty"
                          ? "Not stored yet. Enter a value and save to store it securely."
                          : isSecret && fieldState === "loading"
                            ? "Checking whether a value is stored…"
                            : field.helper}
                      </span>
                    </label>
                  );
                })}
              </div>
            </section>
          );
        })}
      </div>

      <div className={`${CARD} flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between sm:p-5`}>
        <p className="text-sm text-slate-600" aria-live="polite">
          {statusLine}
        </p>
        <div className="flex flex-col-reverse gap-2 sm:flex-row">
          <button type="button" onClick={() => testConnection("unify")} disabled={!isAdmin || testing !== null} className={BUTTON_SECONDARY}>
            {testing === "unify" ? "Testing Unify…" : "Test Unify"}
          </button>
          <button type="button" onClick={() => testConnection("zoho")} disabled={!isAdmin || testing !== null} className={BUTTON_SECONDARY}>
            {testing === "zoho" ? "Testing Zoho…" : "Test Zoho"}
          </button>
          <button type="button" onClick={handleSave} disabled={!isAdmin || isSaving || !hasPendingChanges()} className={BUTTON_PRIMARY}>
            {isSaving ? "Saving…" : "Save changes"}
          </button>
        </div>
      </div>
    </main>
  );
}
