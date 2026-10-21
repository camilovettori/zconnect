// Display helpers for the signed-in user. Names come from Supabase user_metadata:
// first_name / last_name (users created in Zconnect) or full_name / name (users created
// elsewhere, e.g. the Supabase dashboard). The email is the last resort.

type UserLike = {
  email?: string | null;
  user_metadata?: Record<string, unknown> | null;
} | null | undefined;

function text(value: unknown): string {
  return typeof value === "string" ? value.trim().replace(/\s+/g, " ") : "";
}

export function userFullName(user: UserLike): string {
  const meta = user?.user_metadata ?? {};
  const joined = [text(meta.first_name), text(meta.last_name)].filter(Boolean).join(" ");
  return joined || text(meta.full_name) || text(meta.name);
}

export function userDisplayName(user: UserLike): string {
  return userFullName(user) || text(user?.email) || "Signed in";
}

export function userInitials(user: UserLike): string {
  const name = userFullName(user);
  if (name) {
    const parts = name.split(" ").filter(Boolean);
    return ((parts[0]?.[0] ?? "") + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
  }
  return (text(user?.email)[0] ?? "").toUpperCase();
}

export function roleLabel(role: string | null | undefined): string {
  return role === "admin" ? "Admin" : "User";
}
