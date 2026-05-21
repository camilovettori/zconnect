const BACKEND_URL = process.env.BACKEND_URL || "http://127.0.0.1:8000";
const BACKEND_TIMEOUT_MS = 95000;

export const runtime = "nodejs";
export const maxDuration = 90;

function asJsonResponse(payload: unknown, status: number) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export async function POST(request: Request) {
  const requestStartedAt = performance.now();
  try {
    const formData = await request.formData();
    const file = formData.get("file");

    if (!(file instanceof File)) {
      console.error("[NextApi] preview-csv missing file field", {
        backendUrl: `${BACKEND_URL}/api/unify/preview-csv`,
      });
      return asJsonResponse({ detail: "Missing file field named \"file\"" }, 400);
    }

    const forward = new FormData();
    forward.append("file", file, file.name || "upload.csv");

    const backendUrl = `${BACKEND_URL}/api/unify/preview-csv`;
    console.info("[NextApi] preview-csv proxy forwarding to backend", {
      backendUrl,
      fileName: file.name,
      fileSize: file.size,
      contentType: file.type,
    });

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), BACKEND_TIMEOUT_MS);
    let res: Response | undefined;
    try {
      res = await fetch(backendUrl, {
        method: "POST",
        body: forward,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }

    if (!res) {
      throw new Error("No response from backend");
    }

    const backendText = await res.text();
    let backendData: any = null;
    try {
      backendData = backendText ? JSON.parse(backendText) : null;
    } catch (e) {
      backendData = null;
    }

    if (!res.ok) {
      console.error("[NextApi] preview-csv backend error", {
        backendUrl,
        status: res.status,
        responseText: backendText,
      });
      const payload =
        backendData && typeof backendData === "object"
          ? backendData
          : { detail: backendText?.trim() || `Backend returned HTTP ${res.status}` };
      return asJsonResponse(payload, res.status);
    }

    console.info("[NextApi] preview-csv proxy success", {
      backendUrl,
      status: res.status,
      backendResponseText: backendText,
      backendDurationMs: performance.now() - requestStartedAt,
    });

    return asJsonResponse(backendData ?? {}, res.status);
  } catch (err) {
    const errorMessage = err instanceof Error ? err.message : String(err);
    if (err instanceof DOMException && err.name === "AbortError") {
      console.error("[NextApi] preview-csv proxy timed out", {
        backendUrl: `${BACKEND_URL}/api/unify/preview-csv`,
        timeoutMs: BACKEND_TIMEOUT_MS,
        errorMessage,
      });
      return asJsonResponse(
        {
          detail: `Proxy error: Preview proxy timed out after ${Math.round(BACKEND_TIMEOUT_MS / 1000)}s`,
        },
        504
      );
    }
    console.error("[NextApi] preview-csv proxy failed", {
      backendUrl: `${BACKEND_URL}/api/unify/preview-csv`,
      errorMessage,
    });
    return asJsonResponse({ detail: `Proxy error: ${errorMessage}` }, 500);
  }
}
