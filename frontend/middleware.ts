import { NextRequest, NextResponse } from "next/server";
import { fetchCurrentUser, refreshSession } from "./lib/supabase";

const SESSION_COOKIE = "zconnect_session";

// Same attributes as /api/auth/session uses when it refreshes a session.
const SESSION_COOKIE_OPTIONS = {
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
};

type SessionCheck =
  | { valid: true; refreshed: { access_token: string; refresh_token: string; expires_at: number } | null }
  | { valid: false };

function parseSession(raw: string | undefined): { access_token?: string; refresh_token?: string } | null {
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

// Validates the session with Supabase on every page request (not just cookie presence).
// An expired access token is refreshed with the refresh token; anything else is invalid.
// If Supabase cannot be reached the session is treated as invalid (fail closed).
async function checkSession(raw: string | undefined): Promise<SessionCheck> {
  const session = parseSession(raw);
  if (!session) {
    return { valid: false };
  }
  if (session.access_token) {
    const user = await fetchCurrentUser(session.access_token).catch(() => null);
    if (user?.id) {
      return { valid: true, refreshed: null };
    }
  }
  if (session.refresh_token) {
    try {
      const refreshed = await refreshSession(session.refresh_token);
      const user = await fetchCurrentUser(refreshed.access_token).catch(() => null);
      if (user?.id) {
        return {
          valid: true,
          refreshed: {
            access_token: refreshed.access_token,
            refresh_token: refreshed.refresh_token,
            expires_at: refreshed.expires_at || Date.now() / 1000 + refreshed.expires_in,
          },
        };
      }
    } catch {
      return { valid: false };
    }
  }
  return { valid: false };
}

function withSessionUpdate(response: NextResponse, check: SessionCheck, hadCookie: boolean) {
  if (check.valid && check.refreshed) {
    response.cookies.set(SESSION_COOKIE, JSON.stringify(check.refreshed), SESSION_COOKIE_OPTIONS);
  } else if (!check.valid && hadCookie) {
    response.cookies.set(SESSION_COOKIE, "", { path: "/", maxAge: 0 });
  }
  return response;
}

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const raw = request.cookies.get(SESSION_COOKIE)?.value;
  const check = await checkSession(raw);

  if (pathname === "/login") {
    if (check.valid) {
      return withSessionUpdate(NextResponse.redirect(new URL("/sync", request.url)), check, Boolean(raw));
    }
    return withSessionUpdate(NextResponse.next(), check, Boolean(raw));
  }

  if (!check.valid) {
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("next", pathname);
    return withSessionUpdate(NextResponse.redirect(loginUrl), check, Boolean(raw));
  }

  return withSessionUpdate(NextResponse.next(), check, Boolean(raw));
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|api|.*\\..*).*)"],
};
