import { fetchCurrentUser } from "../../../lib/supabase";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

// The backend records this as the actor of export audit events. It is only ever set here,
// from a session validated server-side; any client-supplied value is removed.
const ACTOR_HEADER = "x-zconnect-actor";
const SESSION_COOKIE = "zconnect_session";

function readSessionAccessToken(request: Request): string | null {
  const cookieHeader = request.headers.get("cookie") || "";
  for (const part of cookieHeader.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name !== SESSION_COOKIE) {
      continue;
    }
    try {
      const parsed = JSON.parse(decodeURIComponent(rest.join("=")));
      return typeof parsed?.access_token === "string" && parsed.access_token ? parsed.access_token : null;
    } catch {
      return null;
    }
  }
  return null;
}

async function resolveActorId(request: Request): Promise<string | null> {
  const accessToken = readSessionAccessToken(request);
  if (!accessToken) {
    return null;
  }
  try {
    const user = await fetchCurrentUser(accessToken);
    return user?.id ? String(user.id) : null;
  } catch {
    return null;
  }
}

async function proxy(request: Request, pathSegments: string[]) {
  const requestStartedAt = performance.now();
  const path = pathSegments.join("/");
  const url = new URL(request.url);
  const targetUrl = `${BACKEND_URL}/api/${path}${url.search}`;
  const method = request.method.toUpperCase();

  console.info("[NextApi] proxy entered", {
    method,
    path: `/api/${path}${url.search}`,
    targetUrl,
  });

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("content-length");
  headers.delete(ACTOR_HEADER);
  if (method !== "GET" && method !== "HEAD" && path.startsWith("export/")) {
    const actorId = await resolveActorId(request);
    if (actorId) {
      headers.set(ACTOR_HEADER, actorId);
    }
  }

  const body = method === "GET" || method === "HEAD" ? undefined : await request.text();

  try {
    const backendStartedAt = performance.now();
    const res = await fetch(targetUrl, {
      method,
      headers,
      body,
    });
    const backendDurationMs = performance.now() - backendStartedAt;
    const text = await res.text();

    console.info("[NextApi] proxy backend response", {
      method,
      path: `/api/${path}${url.search}`,
      status: res.status,
      backendDurationMs,
      bodyLength: text.length,
    });

    return new Response(text, {
      status: res.status,
      headers: { "Content-Type": res.headers.get("content-type") || "application/json" },
    });
  } catch (err) {
    console.error("[NextApi] proxy failed", {
      method,
      path: `/api/${path}${url.search}`,
      error: String(err),
    });
    return new Response(
      JSON.stringify({
        error: "Proxy failed",
        details: String(err),
      }),
      { status: 500, headers: { "Content-Type": "application/json" } },
    );
  } finally {
    console.info("[NextApi] proxy completed", {
      method,
      path: `/api/${path}${url.search}`,
      totalDurationMs: performance.now() - requestStartedAt,
    });
  }
}

export async function GET(request: Request, context: { params: { path: string[] } }) {
  const { path } = context.params;
  return proxy(request, path);
}

export async function POST(request: Request, context: { params: { path: string[] } }) {
  const { path } = context.params;
  return proxy(request, path);
}

export async function PUT(request: Request, context: { params: { path: string[] } }) {
  const { path } = context.params;
  return proxy(request, path);
}

export async function PATCH(request: Request, context: { params: { path: string[] } }) {
  const { path } = context.params;
  return proxy(request, path);
}

export async function DELETE(request: Request, context: { params: { path: string[] } }) {
  const { path } = context.params;
  return proxy(request, path);
}
