"use client";

import { useEffect, useState } from "react";
import { AppShell } from "../../components/app-shell";
import { useAppSession } from "../../components/app-session";
import { Modal } from "../../components/ui/modal";
import { Notice } from "../../components/ui/notice";
import { PageHeader } from "../../components/ui/page-header";
import { BUTTON_DANGER, BUTTON_DANGER_SOLID, BUTTON_PRIMARY, BUTTON_SECONDARY, CARD, INPUT, LABEL } from "../../components/ui/styles";

type UserRow = {
  id: string;
  email: string;
  role: string;
  created_at: string | null;
  first_name?: string;
  last_name?: string;
};

type Message = { kind: "success" | "error"; text: string };

const dateFormatter = new Intl.DateTimeFormat("en-GB", { dateStyle: "medium" });

function fullName(row: UserRow) {
  return [row.first_name, row.last_name].map((part) => (part || "").trim()).filter(Boolean).join(" ");
}

function formatCreated(value: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : dateFormatter.format(date);
}

function RoleBadge({ role }: { role: string }) {
  const admin = role === "admin";
  return (
    <span
      className={[
        "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium",
        admin ? "border-brand-200 bg-brand-50 text-brand-800" : "border-slate-200 bg-slate-50 text-slate-600",
      ].join(" ")}
    >
      {admin ? "Admin" : "User"}
    </span>
  );
}

export default function UsersPage() {
  const { user, loading } = useAppSession();
  const [users, setUsers] = useState<UserRow[]>([]);
  const [busy, setBusy] = useState(true);
  const [message, setMessage] = useState<Message | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("user");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");
  const [pendingDelete, setPendingDelete] = useState<UserRow | null>(null);
  const [deleting, setDeleting] = useState(false);

  const isAdmin = user?.role === "admin";

  const loadUsers = async () => {
    setBusy(true);
    try {
      const response = await fetch("/api/users", { cache: "no-store" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload?.error || "Unable to load users.");
      }
      const nextUsers = Array.isArray(payload?.users)
        ? payload.users
            .filter((row: Partial<UserRow> | null | undefined) => Boolean(row && row.id))
            .map((row: Partial<UserRow>) => ({
              id: String(row.id || ""),
              email: String(row.email || ""),
              role: String(row.role || "user"),
              created_at: row.created_at ? String(row.created_at) : null,
              first_name: String(row.first_name || ""),
              last_name: String(row.last_name || ""),
            }))
        : [];
      setUsers(nextUsers);
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : "Unable to load users." });
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!loading && isAdmin) {
      loadUsers().catch(() => undefined);
    } else if (!loading) {
      setBusy(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, isAdmin]);

  const resetForm = () => {
    setEmail("");
    setPassword("");
    setRole("user");
    setFirstName("");
    setLastName("");
    setFormError("");
  };

  const openCreate = () => {
    resetForm();
    setCreateOpen(true);
  };

  const createUser = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) {
      return;
    }
    setSaving(true);
    setFormError("");
    try {
      const response = await fetch("/api/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, role, first_name: firstName, last_name: lastName }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload?.error || "Unable to create the user.");
      }
      const createdEmail = String(payload?.user?.email || email || "");
      setCreateOpen(false);
      resetForm();
      setMessage({ kind: "success", text: createdEmail ? `${createdEmail} can now sign in.` : "User created." });
      await loadUsers();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Unable to create the user.");
    } finally {
      setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (!pendingDelete || deleting) {
      return;
    }
    const target = pendingDelete;
    setDeleting(true);
    try {
      const response = await fetch(`/api/users/${encodeURIComponent(target.id)}`, { method: "DELETE" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload?.error || "Unable to delete the user.");
      }
      setMessage({ kind: "success", text: `${target.email} was removed and can no longer sign in.` });
      await loadUsers();
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : "Unable to delete the user." });
    } finally {
      setDeleting(false);
      setPendingDelete(null);
    }
  };

  const deleteButton = (row: UserRow) => {
    const self = row.id === user?.id;
    return (
      <button
        type="button"
        onClick={() => setPendingDelete(row)}
        disabled={self}
        title={self ? "You can't remove your own account" : undefined}
        className={BUTTON_DANGER}
      >
        Remove
      </button>
    );
  };

  return (
    <AppShell>
      <main className="space-y-6">
        <PageHeader
          eyebrow="Settings"
          title="Users"
          description="People who can sign in to Zconnect. Admins can change connections, reset sync runs and manage users."
          actions={
            isAdmin ? (
              <button type="button" onClick={openCreate} className={BUTTON_PRIMARY}>
                Add user
              </button>
            ) : null
          }
        />

        {message && (
          <Notice kind={message.kind} onDismiss={() => setMessage(null)}>
            {message.text}
          </Notice>
        )}

        {loading ? (
          <div className={`${CARD} space-y-3 p-5`} aria-label="Loading">
            {[1, 2, 3].map((item) => (
              <div key={item} className="h-12 animate-pulse rounded-xl bg-slate-100 motion-reduce:animate-none" />
            ))}
          </div>
        ) : !isAdmin ? (
          <section className={`${CARD} p-6 sm:p-8`}>
            <h2 className="text-base font-semibold text-slate-950">Admin access required</h2>
            <p className="mt-1 text-sm text-slate-600">Only admins can view and manage who can sign in. Ask an admin if you need access for someone.</p>
          </section>
        ) : (
          <section aria-labelledby="users-heading" className={CARD}>
            <div className="flex items-center justify-between gap-3 border-b border-slate-200/80 px-4 py-3.5 sm:px-5">
              <h2 id="users-heading" className="text-base font-semibold tracking-tight text-slate-950">
                People with access
                {!busy && <span className="ml-2 text-sm font-normal tabular-nums text-slate-500">{users.length}</span>}
              </h2>
            </div>

            {busy ? (
              <div className="space-y-3 p-4 sm:p-5" aria-label="Loading users">
                {[1, 2, 3].map((item) => (
                  <div key={item} className="h-12 animate-pulse rounded-xl bg-slate-100 motion-reduce:animate-none" />
                ))}
              </div>
            ) : users.length === 0 ? (
              <div className="p-8 text-center">
                <p className="text-sm font-medium text-slate-900">No users yet</p>
                <p className="mt-1 text-sm text-slate-500">Add the first operator to give them access.</p>
              </div>
            ) : (
              <>
                <div className="hidden overflow-x-auto md:block">
                  <table className="w-full text-left text-sm">
                    <caption className="sr-only">Users</caption>
                    <thead className="border-b border-slate-200 bg-slate-50/70 text-xs text-slate-500">
                      <tr>
                        <th scope="col" className="px-5 py-2.5 font-medium">Name</th>
                        <th scope="col" className="px-5 py-2.5 font-medium">Email</th>
                        <th scope="col" className="px-5 py-2.5 font-medium">Role</th>
                        <th scope="col" className="px-5 py-2.5 font-medium">Added</th>
                        <th scope="col" className="px-5 py-2.5 text-right font-medium">
                          <span className="sr-only">Actions</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {users.map((row) => (
                        <tr key={row.id} className="hover:bg-slate-50/60">
                          <td className="px-5 py-3 font-medium text-slate-900">
                            {fullName(row) || <span className="font-normal text-slate-400">No name</span>}
                            {row.id === user?.id && <span className="ml-2 text-xs font-normal text-slate-500">(you)</span>}
                          </td>
                          <td className="break-all px-5 py-3 text-slate-600">{row.email}</td>
                          <td className="px-5 py-3"><RoleBadge role={row.role} /></td>
                          <td className="whitespace-nowrap px-5 py-3 tabular-nums text-slate-600">{formatCreated(row.created_at)}</td>
                          <td className="px-5 py-3 text-right">{deleteButton(row)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <ul className="divide-y divide-slate-100 md:hidden">
                  {users.map((row) => (
                    <li key={row.id} className="flex items-start justify-between gap-3 px-4 py-3.5">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-slate-900">
                          {fullName(row) || row.email}
                          {row.id === user?.id && <span className="ml-1.5 text-xs font-normal text-slate-500">(you)</span>}
                        </p>
                        {fullName(row) && <p className="truncate text-sm text-slate-600">{row.email}</p>}
                        <div className="mt-1.5 flex items-center gap-2 text-xs text-slate-500">
                          <RoleBadge role={row.role} />
                          <span>Added {formatCreated(row.created_at)}</span>
                        </div>
                      </div>
                      {deleteButton(row)}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>
        )}

        <Modal open={createOpen} title="Add user" onClose={saving ? undefined : () => setCreateOpen(false)}>
          <form onSubmit={createUser} className="space-y-4 pt-1">
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="block">
                <span className={LABEL}>First name</span>
                <input type="text" value={firstName} onChange={(event) => setFirstName(event.target.value)} className={INPUT} autoComplete="off" />
              </label>
              <label className="block">
                <span className={LABEL}>Last name</span>
                <input type="text" value={lastName} onChange={(event) => setLastName(event.target.value)} className={INPUT} autoComplete="off" />
              </label>
            </div>
            <label className="block">
              <span className={LABEL}>Email</span>
              <input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} className={INPUT} autoComplete="off" />
            </label>
            <label className="block">
              <span className={LABEL}>Temporary password</span>
              <input
                type="password"
                required
                minLength={6}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className={INPUT}
                autoComplete="new-password"
              />
              <span className="mt-1.5 block text-xs text-slate-500">At least 6 characters. Share it with the person securely.</span>
            </label>
            <label className="block">
              <span className={LABEL}>Role</span>
              <select value={role} onChange={(event) => setRole(event.target.value)} className={INPUT}>
                <option value="user">User — sync orders and view history</option>
                <option value="admin">Admin — also manage connections, resets and users</option>
              </select>
            </label>
            {formError && <Notice kind="error">{formError}</Notice>}
            <div className="flex flex-col-reverse gap-2 pt-1 sm:flex-row sm:justify-end">
              <button type="button" onClick={() => setCreateOpen(false)} disabled={saving} className={BUTTON_SECONDARY}>
                Cancel
              </button>
              <button type="submit" disabled={saving} className={BUTTON_PRIMARY}>
                {saving ? "Adding…" : "Add user"}
              </button>
            </div>
          </form>
        </Modal>

        <Modal
          open={pendingDelete !== null}
          title="Remove this user?"
          onClose={deleting ? undefined : () => setPendingDelete(null)}
          actions={
            <>
              <button type="button" onClick={() => setPendingDelete(null)} disabled={deleting} className={BUTTON_SECONDARY}>
                Cancel
              </button>
              <button type="button" onClick={confirmDelete} disabled={deleting} className={BUTTON_DANGER_SOLID}>
                {deleting ? "Removing…" : "Remove user"}
              </button>
            </>
          }
        >
          <p>
            <span className="font-medium text-slate-900">{pendingDelete?.email}</span> will no longer be able to sign in. Their past sync
            runs and audit history are kept.
          </p>
        </Modal>
      </main>
    </AppShell>
  );
}
