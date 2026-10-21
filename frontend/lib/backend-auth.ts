// Server-side only: imported solely by route handlers (node:crypto cannot be bundled for browsers).
import { createHash, createHmac } from "node:crypto";
import { rejectCrossSiteRequest } from "./request-origin";
import { fetchCurrentUser, refreshSession } from "./supabase";

// Server-to-server authentication for every request this Next.js server sends to the
// FastAPI backend. The browser never sees ZCONNECT_BACKEND_SECRET: it is read only here,
// in server-side route handlers, and has no NEXT_PUBLIC_ prefix.
//
// For each request we validate the Supabase session server-side (refreshing an expired
// access token when a refresh token is present), then send the verified user id and role
// with an HMAC-SHA256 signature over method, path, query, timestamp, user and role.
// The backend (app/security.py) rejects anything unsigned, tampered, replayed or expired.

export const SESSION_COOKIE = "zconnect_session";
const SIGNATURE_VERSION = "v3";
const MIN_SECRET_LENGTH = 32;
const COMPANY_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,63}$/;

// Headers the client must never be able to set on a backend request.
const CLIENT_CONTROLLED_HEADERS = [
  "x-zconnect-user",
  "x-zconnect-role",
  "x-zconnect-company",
  "x-zconnect-timestamp",
  "x-zconnect-signature",
  "x-zconnect-content-sha256",
  "x-zconnect-actor",
  "authorization",
  "cookie",
];

export type AuthenticatedUser = { id: string; role: "admin" | "user" | "platform_admin"; companyId: string };

/**
 * Company and role come only from Supabase app_metadata (writable with the service role, never
 * by the user). Users without a company belong to this deployment's default company.
 * A platform admin is an admin with app_metadata.platform_admin === true.
 * Fails closed (null) when a company is set but malformed, or the default is misconfigured:
 * such a user must never be treated as a member of the default company.
 */
export function backendIdentity(user: { id: unknown; role?: unknown; app_metadata?: Record<string, unknown> | null }): AuthenticatedUser | null {
  const meta = user.app_metadata ?? {};
  const assigned = meta.company_id !== undefined && meta.company_id !== null && meta.company_id !== "";
  const raw = assigned ? meta.company_id : process.env.ZCONNECT_DEFAULT_COMPANY_ID || "default";
  const companyId = typeof raw === "string" ? raw.trim().toLowerCase() : "";
  if (!COMPANY_ID_PATTERN.test(companyId)) {
    return null;
  }
  const admin = user.role === "admin";
  const role = admin && meta.platform_admin === true ? "platform_admin" : admin ? "admin" : "user";
  return { id: String(user.id), role, companyId };
}

export type AuthResult =
  | { ok: true; user: AuthenticatedUser; refreshedSessionCookie: string | null }
  | { ok: false; response: Response };

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function backendSecret(): string | null {
  const secret = (process.env.ZCONNECT_BACKEND_SECRET || "").trim();
  return secret.length >= MIN_SECRET_LENGTH ? secret : null;
}

function readSession(request: Request): { access_token?: string; refresh_token?: string } | null {
  const cookieHeader = request.headers.get("cookie") || "";
  for (const part of cookieHeader.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name !== SESSION_COOKIE) {
      continue;
    }
    try {
      const parsed = JSON.parse(decodeURIComponent(rest.join("=")));
      return parsed && typeof parsed === "object" ? parsed : null;
    } catch {
      return null;
    }
  }
  return null;
}

async function verifiedUser(accessToken: string | undefined) {
  if (!accessToken) {
    return null;
  }
  try {
    const user = await fetchCurrentUser(accessToken);
    return user?.id ? user : null;
  } catch {
    return null;
  }
}

function sessionCookie(session: { access_token: string; refresh_token: string; expires_at?: number; expires_in?: number }) {
  // Same attributes as /api/auth/session when it refreshes a session.
  const value = encodeURIComponent(
    JSON.stringify({
      access_token: session.access_token,
      refresh_token: session.refresh_token,
      expires_at: session.expires_at || Date.now() / 1000 + (session.expires_in || 0),
    }),
  );
  const secure = process.env.NODE_ENV === "production" ? "; Secure" : "";
  return `${SESSION_COOKIE}=${value}; Path=/; HttpOnly; SameSite=Lax${secure}`;
}

export async function authenticateRequest(request: Request): Promise<AuthResult> {
  const crossSite = rejectCrossSiteRequest(request);
  if (crossSite) {
    return { ok: false, response: crossSite };
  }
  if (!backendSecret()) {
    console.error("[Security] ZCONNECT_BACKEND_SECRET is missing or too short; refusing backend requests");
    return { ok: false, response: jsonResponse(503, { detail: "Server security configuration is missing" }) };
  }

  const session = readSession(request);
  let user = await verifiedUser(session?.access_token);
  let refreshedSessionCookie: string | null = null;

  if (!user && session?.refresh_token) {
    try {
      const refreshed = await refreshSession(session.refresh_token);
      user = await verifiedUser(refreshed.access_token);
      if (user) {
        refreshedSessionCookie = sessionCookie(refreshed);
      }
    } catch {
      user = null;
    }
  }

  if (!user) {
    return { ok: false, response: jsonResponse(401, { detail: "Authentication required" }) };
  }
  const identity = backendIdentity(user);
  if (!identity) {
    console.error("[Security] User has an invalid company assignment; refusing backend requests", { userId: String(user.id) });
    return { ok: false, response: jsonResponse(403, { detail: "Your account is not assigned to a valid company. Contact your administrator." }) };
  }
  return { ok: true, user: identity, refreshedSessionCookie };
}

/**
 * Removes client-supplied identity/credential headers and adds the signed identity.
 * `body` must be the exact bytes that will be sent to the backend (empty for no body):
 * their SHA-256 is part of the signature, so any change in transit is rejected.
 */
export function applySignedIdentity(
  headers: Headers,
  method: string,
  backendPath: string,
  query: string,
  user: AuthenticatedUser,
  body: Uint8Array = new Uint8Array(),
) {
  for (const name of CLIENT_CONTROLLED_HEADERS) {
    headers.delete(name);
  }
  const secret = backendSecret();
  if (!secret) {
    throw new Error("ZCONNECT_BACKEND_SECRET is missing");
  }
  const timestamp = String(Math.floor(Date.now() / 1000));
  const contentSha256 = createHash("sha256").update(body).digest("hex");
  const message = [SIGNATURE_VERSION, method.toUpperCase(), backendPath, query, timestamp, user.id, user.role, user.companyId, contentSha256].join("\n");
  headers.set("x-zconnect-user", user.id);
  headers.set("x-zconnect-role", user.role);
  headers.set("x-zconnect-company", user.companyId);
  headers.set("x-zconnect-timestamp", timestamp);
  headers.set("x-zconnect-content-sha256", contentSha256);
  headers.set("x-zconnect-signature", createHmac("sha256", secret).update(message, "utf8").digest("hex"));
}

/** Passes a refreshed session cookie (if any) back to the browser. */
export function withRefreshedSession(response: Response, auth: { refreshedSessionCookie: string | null }) {
  if (auth.refreshedSessionCookie) {
    response.headers.append("Set-Cookie", auth.refreshedSessionCookie);
  }
  return response;
}
