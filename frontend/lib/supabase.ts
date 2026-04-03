const SUPABASE_URL = process.env.SUPABASE_URL || "";
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY || "";
const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || "";

function requireUrl() {
  if (!SUPABASE_URL) {
    throw new Error("SUPABASE_URL is missing");
  }
  return SUPABASE_URL.replace(/\/+$/, "");
}

function authUrl(path: string) {
  return `${requireUrl()}${path}`;
}

function authHeaders(kind: "anon" | "service_role", extra?: HeadersInit) {
  const key = kind === "service_role" ? SUPABASE_SERVICE_ROLE_KEY : SUPABASE_ANON_KEY;
  if (!key) {
    throw new Error(`${kind === "service_role" ? "SUPABASE_SERVICE_ROLE_KEY" : "SUPABASE_ANON_KEY"} is missing`);
  }
  return {
    apikey: key,
    Authorization: `Bearer ${key}`,
    ...extra,
  };
}

export type SupabaseAuthUser = {
  id: string;
  email: string;
  role: string;
  created_at?: string;
  app_metadata?: Record<string, unknown>;
  user_metadata?: Record<string, unknown>;
};

type SupabaseMaybeUser = Partial<SupabaseAuthUser> & {
  app_metadata?: Record<string, unknown> | null;
  user_metadata?: Record<string, unknown> | null;
};

export type SupabaseSession = {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  expires_at?: number;
  token_type?: string;
  user: SupabaseAuthUser;
};

export async function loginWithPassword(email: string, password: string) {
  const response = await fetch(authUrl("/auth/v1/token?grant_type=password"), {
    method: "POST",
    headers: authHeaders("anon", {
      "Content-Type": "application/json",
    }),
    body: JSON.stringify({ email, password }),
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload?.error_description || payload?.msg || payload?.message || "Login failed");
  }
  return payload as SupabaseSession;
}

export async function refreshSession(refreshToken: string) {
  const response = await fetch(authUrl("/auth/v1/token?grant_type=refresh_token"), {
    method: "POST",
    headers: authHeaders("anon", {
      "Content-Type": "application/json",
    }),
    body: JSON.stringify({ refresh_token: refreshToken }),
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload?.error_description || payload?.msg || payload?.message || "Session refresh failed");
  }
  return payload as SupabaseSession;
}

export async function fetchCurrentUser(accessToken: string) {
  const response = await fetch(authUrl("/auth/v1/user"), {
    method: "GET",
    headers: authHeaders("anon", {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    }),
  });

  if (!response.ok) {
    return null;
  }

  const payload = await response.json().catch(() => null);
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const user = payload as SupabaseAuthUser;
  const role = String(user?.app_metadata?.role || "user");
  return {
    id: String(user.id || ""),
    email: String(user.email || ""),
    role,
    app_metadata: user.app_metadata || {},
    user_metadata: user.user_metadata || {},
  } satisfies SupabaseAuthUser;
}

export async function listAuthUsers(page = 1, perPage = 100) {
  const response = await fetch(authUrl(`/auth/v1/admin/users?page=${page}&per_page=${perPage}`), {
    method: "GET",
    headers: authHeaders("service_role", {
      "Content-Type": "application/json",
    }),
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload?.msg || payload?.message || "Failed to list users");
  }
  const users = Array.isArray((payload as { users?: unknown }).users)
    ? ((payload as { users?: unknown[] }).users ?? [])
        .filter((user): user is SupabaseMaybeUser => Boolean(user && typeof user === "object"))
        .map((user) => normalizeAuthUser(user))
        .filter((user): user is SupabaseAuthUser => Boolean(user.id))
    : [];
  return { users };
}

export async function createAuthUser(input: { email: string; password: string; role: string }) {
  const response = await fetch(authUrl("/auth/v1/admin/users"), {
    method: "POST",
    headers: authHeaders("service_role", {
      "Content-Type": "application/json",
    }),
    body: JSON.stringify({
      email: input.email,
      password: input.password,
      email_confirm: true,
      app_metadata: { role: input.role },
    }),
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload?.msg || payload?.message || payload?.error_description || "Failed to create user");
  }

  const candidate = extractCreatedUser(payload);
  if (!candidate.id) {
    throw new Error("Supabase returned an unexpected response while creating the user");
  }
  return { user: candidate };
}

export async function deleteAuthUser(userId: string) {
  const response = await fetch(authUrl(`/auth/v1/admin/users/${encodeURIComponent(userId)}`), {
    method: "DELETE",
    headers: authHeaders("service_role", {
      "Content-Type": "application/json",
    }),
  });

  if (!(response.ok || response.status === 204)) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload?.msg || payload?.message || "Failed to delete user");
  }
  return true;
}

function normalizeAuthUser(input: SupabaseMaybeUser): SupabaseAuthUser {
  const appMetadata = input.app_metadata && typeof input.app_metadata === "object" ? input.app_metadata : {};
  const userMetadata = input.user_metadata && typeof input.user_metadata === "object" ? input.user_metadata : {};
  return {
    id: String(input.id || ""),
    email: String(input.email || ""),
    role: String(appMetadata.role || input.role || "user"),
    created_at: input.created_at ? String(input.created_at) : undefined,
    app_metadata: appMetadata,
    user_metadata: userMetadata,
  };
}

function extractCreatedUser(payload: unknown): SupabaseAuthUser {
  if (!payload || typeof payload !== "object") {
    return normalizeAuthUser({});
  }

  const record = payload as {
    user?: unknown;
    data?: { user?: unknown };
    id?: unknown;
    email?: unknown;
    app_metadata?: Record<string, unknown> | null;
    user_metadata?: Record<string, unknown> | null;
  };

  const candidate =
    (record.user && typeof record.user === "object" ? (record.user as SupabaseMaybeUser) : null) ??
    (record.data && record.data.user && typeof record.data.user === "object" ? (record.data.user as SupabaseMaybeUser) : null) ??
    (typeof record.id === "string" || typeof record.email === "string"
      ? (record as SupabaseMaybeUser)
      : null);

  return normalizeAuthUser(candidate ?? {});
}
