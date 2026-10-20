// Server-side only: imported solely by route handlers (node:crypto cannot be bundled for browsers).
import { createHmac } from "node:crypto";
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
const SIGNATURE_VERSION = "v1";
const MIN_SECRET_LENGTH = 32;

// Headers the client must never be able to set on a backend request.
const CLIENT_CONTROLLED_HEADERS = [
  "x-zconnect-user",
  "x-zconnect-role",
  "x-zconnect-timestamp",
  "x-zconnect-signature",
  "x-zconnect-actor",
  "authorization",
  "cookie",
];

export type AuthenticatedUser = { id: string; role: "admin" | "user" };

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
  return {
    ok: true,
    user: { id: String(user.id), role: user.role === "admin" ? "admin" : "user" },
    refreshedSessionCookie,
  };
}

/** Removes client-supplied identity/credential headers and adds the signed identity. */
export function applySignedIdentity(headers: Headers, method: string, backendPath: string, query: string, user: AuthenticatedUser) {
  for (const name of CLIENT_CONTROLLED_HEADERS) {
    headers.delete(name);
  }
  const secret = backendSecret();
  if (!secret) {
    throw new Error("ZCONNECT_BACKEND_SECRET is missing");
  }
  const timestamp = String(Math.floor(Date.now() / 1000));
  const message = [SIGNATURE_VERSION, method.toUpperCase(), backendPath, query, timestamp, user.id, user.role].join("\n");
  headers.set("x-zconnect-user", user.id);
  headers.set("x-zconnect-role", user.role);
  headers.set("x-zconnect-timestamp", timestamp);
  headers.set("x-zconnect-signature", createHmac("sha256", secret).update(message, "utf8").digest("hex"));
}

/** Passes a refreshed session cookie (if any) back to the browser. */
export function withRefreshedSession(response: Response, auth: { refreshedSessionCookie: string | null }) {
  if (auth.refreshedSessionCookie) {
    response.headers.append("Set-Cookie", auth.refreshedSessionCookie);
  }
  return response;
}
