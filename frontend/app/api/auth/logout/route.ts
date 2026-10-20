import { NextResponse } from "next/server";
import { rejectCrossSiteRequest } from "../../../../lib/request-origin";

const SESSION_COOKIE = "zconnect_session";

export async function POST(request: Request) {
  const crossSite = rejectCrossSiteRequest(request);
  if (crossSite) {
    return crossSite;
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE, "", { path: "/", maxAge: 0 });
  return response;
}

