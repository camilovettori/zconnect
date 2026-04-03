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

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("content-length");

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
