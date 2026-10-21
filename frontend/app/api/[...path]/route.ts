import { applySignedIdentity, authenticateRequest, withRefreshedSession } from "../../../lib/backend-auth";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

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

  // Every backend request needs a validated session; the backend also verifies a signature.
  const auth = await authenticateRequest(request);
  if (!auth.ok) {
    console.warn("[NextApi] proxy rejected request", { method, path: `/api/${path}`, status: auth.response.status });
    return auth.response;
  }

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("content-length");
  // Forward the exact bytes received and sign their SHA-256 (empty for GET/HEAD).
  const body = method === "GET" || method === "HEAD" ? undefined : new Uint8Array(await request.arrayBuffer());
  applySignedIdentity(headers, method, `/api/${path}`, url.search, auth.user, body ?? new Uint8Array());

  try {
    const backendStartedAt = performance.now();
    const res = await fetch(targetUrl, {
      method,
      headers,
      body,
    });
    const backendDurationMs = performance.now() - backendStartedAt;
    // Raw bytes, so binary downloads (PDF reports) pass through unchanged.
    const payload = new Uint8Array(await res.arrayBuffer());

    console.info("[NextApi] proxy backend response", {
      method,
      path: `/api/${path}${url.search}`,
      status: res.status,
      backendDurationMs,
      bodyLength: payload.byteLength,
    });

    const responseHeaders = new Headers({ "Content-Type": res.headers.get("content-type") || "application/json" });
    for (const name of ["content-disposition", "cache-control"]) {
      const value = res.headers.get(name);
      if (value) {
        responseHeaders.set(name, value);
      }
    }
    return withRefreshedSession(new Response(payload, { status: res.status, headers: responseHeaders }), auth);
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
