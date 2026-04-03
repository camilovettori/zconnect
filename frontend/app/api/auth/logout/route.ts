import { NextResponse } from "next/server";

const SESSION_COOKIE = "zconnect_session";

export async function POST() {
  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE, "", { path: "/", maxAge: 0 });
  return response;
}

