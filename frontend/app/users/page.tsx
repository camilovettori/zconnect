"use client";

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { AppShell } from "../../components/app-shell";
import { useAppSession } from "../../components/app-session";

type UserRow = {
  id: string;
  email: string;
  role: string;
  created_at: string | null;
  first_name?: string;
  last_name?: string;
};

function Modal({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  if (!open) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 px-4 py-6 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-3xl border border-slate-200 bg-white p-6 shadow-[0_30px_80px_-24px_rgba(15,23,42,0.5)]">
        <div className="flex items-start justify-between gap-4">
          <h3 className="text-xl font-semibold tracking-tight text-slate-950">{title}</h3>
          <button
            type="button"
            className="rounded-full border border-slate-200 bg-white px-3 py-1 text-sm font-medium text-slate-500 transition hover:border-slate-300 hover:text-slate-700"
            onClick={onClose}
          >
            Close
          </button>
        </div>
        <div className="mt-4">{children}</div>
      </div>
    </div>
  );
}

export default function UsersPage() {
  const { user, loading } = useAppSession();
  const [users, setUsers] = useState<UserRow[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("user");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [saving, setSaving] = useState(false);

  const isAdmin = user?.role === "admin";

  const loadUsers = async () => {
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/users", { cache: "no-store" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload?.error || "Failed to load users");
      }
      const nextUsers = Array.isArray(payload?.users)
        ? payload.users
            .filter((user: Partial<UserRow> | null | undefined) => Boolean(user && user.id))
              .map((user: Partial<UserRow>) => ({
              id: String(user.id || ""),
              email: String(user.email || ""),
              role: String(user.role || "user"),
              created_at: user.created_at ? String(user.created_at) : null,
              first_name: String(user.first_name || ""),
              last_name: String(user.last_name || ""),
            }))
        : [];
      setUsers(nextUsers);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load users");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!loading && isAdmin) {
      loadUsers().catch(() => undefined);
    } else if (!loading && !isAdmin) {
      setBusy(false);
    }
  }, [loading, isAdmin]);

  const userCount = useMemo(() => users.length, [users]);

  const createUser = async () => {
    if (saving) {
      return;
    }
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const response = await fetch("/api/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, role, first_name: firstName, last_name: lastName }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload?.error || "Failed to create user");
      }
      const createdUser = payload?.user;
      const createdEmail = String(createdUser?.email || email || "");
      setCreateOpen(false);
      setEmail("");
      setPassword("");
      setRole("user");
      setFirstName("");
      setLastName("");
      setSuccess(createdEmail ? `User ${createdEmail} created successfully` : "User created successfully");
      await loadUsers();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create user");
    } finally {
      setSaving(false);
    }
  };

  const deleteUser = async (id: string, emailLabel: string) => {
    if (!window.confirm(`Delete ${emailLabel}?`)) {
      return;
    }
    setError("");
    setSuccess("");
    const response = await fetch(`/api/users/${encodeURIComponent(id)}`, { method: "DELETE" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      setError(payload?.error || "Failed to delete user");
      return;
    }
    setSuccess(`User ${emailLabel} deleted`);
    await loadUsers();
  };

  return (
    <AppShell>
      <main className="space-y-8">
        <section className="rounded-3xl border border-slate-200/80 bg-white/90 p-8 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)] backdrop-blur">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.28em] text-slate-500">Users</p>
              <h2 className="mt-3 text-3xl font-semibold tracking-tight text-slate-950">Admin user management</h2>
              <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600">
                Create login access for operators and keep the workspace simple.
              </p>
            </div>
            <button
              onClick={() => setCreateOpen(true)}
              className="rounded-xl bg-slate-950 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-slate-900"
            >
              Add User
            </button>
          </div>
        </section>

        {error ? <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
        {success ? <div className="rounded-2xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">{success}</div> : null}

        {!isAdmin ? (
          <section className="rounded-3xl border border-slate-200/80 bg-white p-8 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)]">
            <h3 className="text-lg font-semibold text-slate-950">Access restricted</h3>
            <p className="mt-2 text-sm text-slate-600">Only admin users can manage app logins.</p>
          </section>
        ) : (
          <section className="rounded-3xl border border-slate-200/80 bg-white p-6 shadow-[0_20px_60px_-35px_rgba(15,23,42,0.28)]">
            <div className="mb-4 flex items-center justify-between">
              <div>
                <h3 className="text-lg font-semibold tracking-tight text-slate-950">Existing users</h3>
                <p className="text-sm text-slate-600">{busy ? "Loading users..." : `${userCount} user${userCount === 1 ? "" : "s"}`}</p>
              </div>
            </div>

            <div className="overflow-hidden rounded-2xl border border-slate-200">
              <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
                <thead className="bg-slate-50 text-slate-500">
                  <tr>
                    <th className="px-4 py-3 font-medium">Email</th>
                    <th className="px-4 py-3 font-medium">Role</th>
                    <th className="px-4 py-3 font-medium">Created</th>
                    <th className="px-4 py-3 font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200 bg-white">
                  {busy ? (
                    <tr>
                      <td className="px-4 py-6 text-slate-500" colSpan={4}>Loading users...</td>
                    </tr>
                  ) : users.length ? (
                    users.map((row) => (
                      <tr key={row.id}>
                        <td className="px-4 py-4 font-medium text-slate-950">{row.email}</td>
                        <td className="px-4 py-4 text-slate-600">{row.role}</td>
                        <td className="px-4 py-4 text-slate-600">{row.created_at ? new Date(row.created_at).toLocaleString() : "—"}</td>
                        <td className="px-4 py-4">
                          <button
                            onClick={() => deleteUser(row.id, row.email)}
                            className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50"
                          >
                            Delete
                          </button>
                        </td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td className="px-4 py-6 text-slate-500" colSpan={4}>No users yet.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>
        )}

        <Modal open={createOpen} title="Add User" onClose={() => setCreateOpen(false)}>
          <div className="space-y-4">
            <label className="block">
              <span className="mb-2 block text-sm font-medium text-slate-700">Email</span>
              <input
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm outline-none transition focus:border-sky-400 focus:ring-4 focus:ring-sky-100"
              />
            </label>
            <label className="block">
              <span className="mb-2 block text-sm font-medium text-slate-700">First Name</span>
              <input
                type="text"
                value={firstName}
                onChange={(event) => setFirstName(event.target.value)}
                className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm outline-none transition focus:border-sky-400 focus:ring-4 focus:ring-sky-100"
              />
            </label>
            <label className="block">
              <span className="mb-2 block text-sm font-medium text-slate-700">Last Name</span>
              <input
                type="text"
                value={lastName}
                onChange={(event) => setLastName(event.target.value)}
                className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm outline-none transition focus:border-sky-400 focus:ring-4 focus:ring-sky-100"
              />
            </label>
            <label className="block">
              <span className="mb-2 block text-sm font-medium text-slate-700">Password</span>
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm outline-none transition focus:border-sky-400 focus:ring-4 focus:ring-sky-100"
              />
            </label>
            <label className="block">
              <span className="mb-2 block text-sm font-medium text-slate-700">Role</span>
              <select
                value={role}
                onChange={(event) => setRole(event.target.value)}
                className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm outline-none transition focus:border-sky-400 focus:ring-4 focus:ring-sky-100"
              >
                <option value="user">User</option>
                <option value="admin">Admin</option>
              </select>
            </label>
            <div className="flex justify-end gap-3 pt-2">
              <button
                type="button"
                onClick={() => setCreateOpen(false)}
                className="rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 transition hover:border-slate-400 hover:bg-slate-50"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={saving}
                onClick={createUser}
                className="rounded-xl bg-slate-950 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-70"
              >
                {saving ? "Creating..." : "Create User"}
              </button>
            </div>
          </div>
        </Modal>
      </main>
    </AppShell>
  );
}
