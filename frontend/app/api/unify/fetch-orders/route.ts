const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";
const BACKEND_TIMEOUT_MS = 95000;

export const runtime = "nodejs";
export const maxDuration = 90;

export async function POST(request: Request) {
  const requestStartedAt = performance.now();
  try {
    const body = await request.json();
    console.info("[NextApi] fetch-orders route entered", { backendUrl: BACKEND_URL, body });

    const backendStartedAt = performance.now();
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), BACKEND_TIMEOUT_MS);
    let res: Response | undefined;
    try {
      res = await fetch(`${BACKEND_URL}/api/unify/fetch-orders`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }
    if (!res) {
      throw new Error("Backend request did not return a response");
    }
    const backendDurationMs = performance.now() - backendStartedAt;

    const text = await res.text();

    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }

    if (!res.ok) {
      console.error("[NextApi] fetch-orders backend error", { status: res.status, data, backendDurationMs });
      return new Response(JSON.stringify(data), {
        status: res.status,
        headers: { "Content-Type": "application/json" },
      });
    }

    console.info("[NextApi] fetch-orders success", {
      status: res.status,
      backendDurationMs,
      responseLength: text.length,
      totalDurationMs: performance.now() - requestStartedAt,
    });
    return new Response(JSON.stringify(data), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      console.error("[NextApi] fetch-orders proxy timed out", {
        timeoutMs: BACKEND_TIMEOUT_MS,
        totalDurationMs: performance.now() - requestStartedAt,
      });
      return new Response(
        JSON.stringify({
          error: "Proxy timeout",
          details: `Fetch orders timed out after ${Math.round(BACKEND_TIMEOUT_MS / 1000)} seconds`,
        }),
        { status: 504, headers: { "Content-Type": "application/json" } }
      );
    }
    console.error("[NextApi] fetch-orders proxy failed", {
      error: String(err),
      totalDurationMs: performance.now() - requestStartedAt,
    });
    return new Response(
      JSON.stringify({
        error: "Proxy failed",
        details: String(err),
      }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }
}
