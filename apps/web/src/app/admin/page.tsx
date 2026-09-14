"use client";

import { useState } from "react";
import Shell from "@/components/Shell";
import {
  Button,
  Card,
  EmptyState,
  PageHeader,
  Pill,
  Skeleton,
  Td,
  Th,
  inputClass,
  labelClass,
} from "@/components/ui";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { AdminUser, Invite, User } from "@/lib/types";

export default function AdminPage() {
  const { data: me } = useApi<User>("/auth/me");
  const { data: users, loading, reload: reloadUsers } = useApi<AdminUser[]>("/admin/users");
  const { data: invites, reload: reloadInvites } = useApi<Invite[]>("/admin/invites");
  const { push } = useToast();

  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [asAdmin, setAsAdmin] = useState(false);
  const [busy, setBusy] = useState(false);

  async function invite(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const created = await api<Invite>("/admin/invites", {
        method: "POST",
        body: JSON.stringify({ email, full_name: fullName || null, as_admin: asAdmin }),
      });
      if (created.invite_url) {
        // Delivery failed — the invite is valid, so hand the operator the
        // link rather than leaving them guessing.
        await navigator.clipboard?.writeText(created.invite_url).catch(() => {});
        push("error", "Invite created, but the email failed to send. Link copied to clipboard.");
      } else {
        push("success", `Invite sent to ${created.email}`);
      }
      setEmail("");
      setFullName("");
      setAsAdmin(false);
      reloadInvites();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  async function act(path: string, method: string, body?: unknown, label?: string) {
    try {
      await api(path, { method, ...(body ? { body: JSON.stringify(body) } : {}) });
      push("success", label ?? "Done");
      reloadUsers();
      reloadInvites();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Failed");
    }
  }

  const pending = (invites ?? []).filter((i) => !i.consumed_at && !i.revoked_at);

  return (
    <Shell>
      <PageHeader
        title="Users"
        sub="Invite people, manage access, and suspend accounts"
      />

      <Card title="Invite someone">
        <form onSubmit={invite} className="flex flex-wrap items-end gap-3">
          <div className="min-w-[16rem] flex-1">
            <label className={labelClass}>Email</label>
            <input
              className={inputClass}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              type="email"
              required
              placeholder="them@example.com"
            />
          </div>
          <div className="min-w-[12rem] flex-1">
            <label className={labelClass}>Name</label>
            <input
              className={inputClass}
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Optional"
            />
          </div>
          <label className="mb-2 flex items-center gap-2 text-sm text-ink-dim">
            <input
              type="checkbox"
              checked={asAdmin}
              onChange={(e) => setAsAdmin(e.target.checked)}
            />
            Operator
          </label>
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? "…" : "Send invite"}
          </Button>
        </form>
        <p className="mt-3 text-sm text-ink-faint">
          They set their own password from the emailed link. Invites expire in 7
          days and work once.
        </p>
      </Card>

      {pending.length > 0 && (
        <div className="mt-4">
          <Card title={`Pending invites (${pending.length})`} pad={false}>
            <table className="w-full">
              <thead>
                <tr>
                  <Th>Email</Th>
                  <Th>Role</Th>
                  <Th>Expires</Th>
                  <Th right>Actions</Th>
                </tr>
              </thead>
              <tbody>
                {pending.map((i) => (
                  <tr key={i.id}>
                    <Td>{i.email}</Td>
                    <Td>{i.as_admin ? "Operator" : "User"}</Td>
                    <Td>{new Date(i.expires_at).toLocaleDateString()}</Td>
                    <Td right>
                      <Button
                        onClick={() =>
                          act(`/admin/invites/${i.id}`, "DELETE", undefined, "Invite revoked")
                        }
                      >
                        Revoke
                      </Button>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </div>
      )}

      <div className="mt-4">
        {loading && !users ? (
          <Skeleton className="h-40" />
        ) : !users?.length ? (
          <EmptyState title="No users yet" />
        ) : (
          <Card title={`Users (${users.length})`} pad={false}>
            <table className="w-full">
              <thead>
                <tr>
                  <Th>Email</Th>
                  <Th>Name</Th>
                  <Th>Role</Th>
                  <Th>2FA</Th>
                  <Th>Status</Th>
                  <Th>Devices</Th>
                  <Th right>Actions</Th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  const self = u.id === me?.id;
                  return (
                    <tr key={u.id}>
                      <Td>
                        {u.email}
                        {self && <span className="ml-2 text-xs text-ink-faint">(you)</span>}
                      </Td>
                      <Td>{u.full_name ?? "—"}</Td>
                      <Td>{u.is_admin ? "Operator" : "User"}</Td>
                      <Td>
                        <Pill
                          value={u.totp_enabled ? "connected" : "warn"}
                          label={u.totp_enabled ? "2FA ON" : "NOT SET UP"}
                        />
                      </Td>
                      <Td>
                        <Pill
                          value={u.is_active ? "connected" : "error"}
                          label={u.is_active ? "ACTIVE" : "SUSPENDED"}
                        />
                      </Td>
                      <Td>{u.active_sessions}</Td>
                      <Td right>
                        <div className="flex justify-end gap-2">
                          {!self && (
                            <Button
                              onClick={() =>
                                act(
                                  `/admin/users/${u.id}/admin`,
                                  "PATCH",
                                  { is_admin: !u.is_admin },
                                  u.is_admin ? "Operator access removed" : "Promoted to operator",
                                )
                              }
                            >
                              {u.is_admin ? "Demote" : "Make operator"}
                            </Button>
                          )}
                          {!self && u.totp_enabled && (
                            <Button
                              onClick={() =>
                                act(
                                  `/admin/users/${u.id}/reset-2fa`,
                                  "POST",
                                  undefined,
                                  "2FA reset — they will set it up again at next sign-in",
                                )
                              }
                            >
                              Reset 2FA
                            </Button>
                          )}
                          {!self &&
                            (u.is_active ? (
                              <Button
                                variant="danger"
                                onClick={() =>
                                  act(
                                    `/admin/users/${u.id}/suspend`,
                                    "POST",
                                    undefined,
                                    "Account suspended",
                                  )
                                }
                              >
                                Suspend
                              </Button>
                            ) : (
                              <Button
                                onClick={() =>
                                  act(
                                    `/admin/users/${u.id}/unsuspend`,
                                    "POST",
                                    undefined,
                                    "Account restored",
                                  )
                                }
                              >
                                Restore
                              </Button>
                            ))}
                        </div>
                      </Td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>
        )}
      </div>
    </Shell>
  );
}
