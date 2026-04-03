import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { fetchCurrentUser, refreshSession } from "../../../../lib/supabase";

const SESSION_COOKIE = "zconnect_session";

export async function GET() {
  const cookieStore = cookies();
  const raw = cookieStore.get(SESSION_COOKIE)?.value;
  if (!raw) {
    return NextResponse.json({ user: null }, { status: 401 });
  }

  let parsed: { access_token?: string; refresh_token?: string } = {};
  try {
    parsed = JSON.parse(raw);
  } catch {
    return NextResponse.json({ user: null }, { status: 401 });
  }

  const accessToken = String(parsed.access_token || "");
  const refreshToken = String(parsed.refresh_token || "");
  if (!accessToken) {
    return NextResponse.json({ user: null }, { status: 401 });
  }

  let currentUser = null;
  try {
    currentUser = await fetchCurrentUser(accessToken);
  } catch {
    currentUser = null;
  }
  if (currentUser) {
    return NextResponse.json({ user: currentUser });
  }

  if (!refreshToken) {
    return NextResponse.json({ user: null }, { status: 401 });
  }

  try {
    const refreshed = await refreshSession(refreshToken);
    const nextUser = await fetchCurrentUser(refreshed.access_token).catch(() => null);
    if (!nextUser) {
      return NextResponse.json({ user: null }, { status: 401 });
    }

    const response = NextResponse.json({ user: nextUser });
    response.cookies.set(
      SESSION_COOKIE,
      JSON.stringify({
        access_token: refreshed.access_token,
        refresh_token: refreshed.refresh_token,
        expires_at: refreshed.expires_at || Date.now() / 1000 + refreshed.expires_in,
      }),
      {
        httpOnly: true,
        secure: process.env.NODE_ENV === "production",
        sameSite: "lax",
        path: "/",
      },
    );
    return response;
  } catch {
    return NextResponse.json({ user: null }, { status: 401 });
  }
}
