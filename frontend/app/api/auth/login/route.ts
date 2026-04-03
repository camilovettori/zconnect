import { NextResponse } from "next/server";
import { loginWithPassword } from "../../../../lib/supabase";

const SESSION_COOKIE = "zconnect_session";
const COOKIE_BASE = {
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
};

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}));
  const email = String(body?.email || "").trim();
  const password = String(body?.password || "");
  const rememberMe = Boolean(body?.rememberMe);

  if (!email || !password) {
    return NextResponse.json({ error: "Email and password are required" }, { status: 400 });
  }

  try {
    const session = await loginWithPassword(email, password);
    const response = NextResponse.json({
      ok: true,
      user: {
        id: session.user.id,
        email: session.user.email,
        role: String(session.user.app_metadata?.role || "user"),
      },
    });

    response.cookies.set(
      SESSION_COOKIE,
      JSON.stringify({
        access_token: session.access_token,
        refresh_token: session.refresh_token,
        expires_at: session.expires_at || Date.now() / 1000 + session.expires_in,
      }),
      {
        ...COOKIE_BASE,
        maxAge: rememberMe ? 60 * 60 * 24 * 30 : undefined,
      },
    );
    return response;
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "Login failed" }, { status: 401 });
  }
}
