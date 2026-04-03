import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { createAuthUser, fetchCurrentUser, listAuthUsers } from "../../../lib/supabase";

const SESSION_COOKIE = "zconnect_session";

async function requireAdmin() {
  const raw = cookies().get(SESSION_COOKIE)?.value;
  if (!raw) {
    return null;
  }

  try {
    const parsed = JSON.parse(raw) as { access_token?: string };
    const user = parsed.access_token ? await fetchCurrentUser(parsed.access_token) : null;
    if (!user || user.role !== "admin") {
      return null;
    }
    return user;
  } catch {
    return null;
  }
}

export async function GET() {
  const admin = await requireAdmin();
  if (!admin) {
    return NextResponse.json({ error: "Admin access required" }, { status: 403 });
  }

  try {
    const { users } = await listAuthUsers(1, 100);
    return NextResponse.json({
      users: users
        .filter((user) => Boolean(user?.id))
        .map((user) => ({
          id: user.id,
          email: user.email || "",
          role: String(user.app_metadata?.role || "user"),
          created_at: user.created_at ?? null,
        })),
    });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "Failed to load users" }, { status: 500 });
  }
}

export async function POST(request: Request) {
  const admin = await requireAdmin();
  if (!admin) {
    return NextResponse.json({ error: "Admin access required" }, { status: 403 });
  }

  const body = await request.json().catch(() => ({}));
  const email = String(body?.email || "").trim();
  const password = String(body?.password || "");
  const role = String(body?.role || "user").trim() || "user";

  if (!email || !password) {
    return NextResponse.json({ error: "Email and password are required" }, { status: 400 });
  }

  try {
    const payload = await createAuthUser({ email, password, role });
    const createdUser = payload?.user;
    if (!createdUser?.id) {
      return NextResponse.json(
        { error: "Supabase returned an unexpected response while creating the user" },
        { status: 502 },
      );
    }
    return NextResponse.json({
      ok: true,
      user: {
        id: createdUser.id,
        email: createdUser.email || email,
        role: String(createdUser.app_metadata?.role || role),
      },
    });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "Failed to create user" }, { status: 500 });
  }
}
