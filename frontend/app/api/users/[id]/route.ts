import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { deleteAuthUser, fetchCurrentUser } from "../../../../lib/supabase";

const SESSION_COOKIE = "zconnect_session";

export async function DELETE(_request: Request, context: { params: { id: string } }) {
  const raw = cookies().get(SESSION_COOKIE)?.value;
  if (!raw) {
    return NextResponse.json({ error: "Admin access required" }, { status: 403 });
  }

  try {
    const parsed = JSON.parse(raw) as { access_token?: string };
    const user = parsed.access_token ? await fetchCurrentUser(parsed.access_token) : null;
    if (!user || user.role !== "admin") {
      return NextResponse.json({ error: "Admin access required" }, { status: 403 });
    }
  } catch {
    return NextResponse.json({ error: "Admin access required" }, { status: 403 });
  }

  try {
    await deleteAuthUser(context.params.id);
    return NextResponse.json({ ok: true });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "Failed to delete user" }, { status: 500 });
  }
}
