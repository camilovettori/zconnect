// CSRF protection for state-changing requests, in addition to the SameSite=Lax session
// cookie. A request that changes state (anything but GET/HEAD/OPTIONS) must come from this
// application's own origin, proven by the browser-controlled Origin header (or Referer when
// Origin is absent), and must not be marked cross-site by Sec-Fetch-Site. A cross-site page
// cannot forge these headers in a victim's browser.
//
// Allowed origins: this request's own origin (host as seen by the server, including a
// reverse proxy's X-Forwarded-Host/Proto) plus optional APP_ORIGINS (comma-separated),
// e.g. "https://app.example.com" when serving from a custom domain.

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

function allowedOrigins(request: Request): Set<string> {
  const allowed = new Set<string>();
  const url = new URL(request.url);
  allowed.add(url.origin);
  const host = request.headers.get("x-forwarded-host") || request.headers.get("host");
  const proto = (request.headers.get("x-forwarded-proto") || url.protocol.replace(":", "")).split(",")[0].trim();
  if (host) {
    allowed.add(`${proto}://${host.split(",")[0].trim()}`);
  }
  for (const origin of (process.env.APP_ORIGINS || "").split(",")) {
    const trimmed = origin.trim().replace(/\/+$/, "");
    if (trimmed) {
      allowed.add(trimmed);
    }
  }
  return allowed;
}

function requestOrigin(request: Request): string | null {
  const origin = request.headers.get("origin");
  if (origin && origin !== "null") {
    return origin;
  }
  const referer = request.headers.get("referer");
  if (referer) {
    try {
      return new URL(referer).origin;
    } catch {
      return null;
    }
  }
  return null;
}

/** Returns a 403 response for cross-site state-changing requests, or null when allowed. */
export function rejectCrossSiteRequest(request: Request): Response | null {
  if (SAFE_METHODS.has(request.method.toUpperCase())) {
    return null;
  }
  const fetchSite = (request.headers.get("sec-fetch-site") || "").toLowerCase();
  const origin = requestOrigin(request);
  if (fetchSite === "cross-site" || !origin || !allowedOrigins(request).has(origin)) {
    console.warn("[Security] Blocked cross-site state-changing request", { method: request.method, path: new URL(request.url).pathname });
    return new Response(JSON.stringify({ detail: "Cross-site request blocked" }), {
      status: 403,
      headers: { "Content-Type": "application/json" },
    });
  }
  return null;
}
