"use client";

import { useState } from "react";
import Shell from "@/components/Shell";
import { Button, Card, PageHeader, Pill, inputClass, labelClass } from "@/components/ui";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";

interface TotpStatus {
  totp_enabled: boolean;
  recovery_codes_remaining: number;
}

export default function SecurityPage() {
  const { data: status, reload } = useApi<TotpStatus>("/auth/totp/status");
  const { push } = useToast();

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);

  async function changePassword(e: React.FormEvent) {
    e.preventDefault();
    if (next !== confirm) {
      push("error", "Passwords do not match");
      return;
    }
    setBusy(true);
    try {
      await api("/auth/password/change", {
        method: "POST",
        body: JSON.stringify({ current_password: current, password: next }),
      });
      push("success", "Password changed. Other devices have been signed out.");
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  async function regenerate(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await api<{ recovery_codes: string[] }>("/auth/totp/recovery-codes", {
        method: "POST",
        body: JSON.stringify({ code: totpCode }),
      });
      setCodes(res.recovery_codes);
      setTotpCode("");
      reload();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  const low = (status?.recovery_codes_remaining ?? 0) <= 3;

  return (
    <Shell>
      <PageHeader title="Security" sub="Two-factor authentication and password" />

      <Card title="Two-factor authentication">
        <div className="mb-4 flex items-center gap-3">
          <Pill
            value={status?.totp_enabled ? "connected" : "warn"}
            label={status?.totp_enabled ? "ENABLED" : "NOT SET UP"}
          />
          <span className="text-sm text-ink-dim">
            {status?.recovery_codes_remaining ?? 0} recovery codes left
          </span>
        </div>
        {low && status?.totp_enabled && (
          <p className="mb-4 text-sm text-warn">
            You are running low on recovery codes. Generate a new set — they are the
            only way back in if you lose your authenticator.
          </p>
        )}

        {codes ? (
          <>
            <p className="mb-3 text-sm text-ink-dim">
              Your new codes. These replace the old set and are shown only once.
            </p>
            <pre className="mb-4 rounded-lg border border-line bg-panel-2 p-4 font-mono text-sm leading-7">
              {codes.join("\n")}
            </pre>
            <Button onClick={() => setCodes(null)}>Done</Button>
          </>
        ) : (
          <form onSubmit={regenerate} className="flex flex-wrap items-end gap-3">
            <div className="min-w-[12rem]">
              <label className={labelClass}>Authenticator code</label>
              <input
                className={inputClass}
                value={totpCode}
                onChange={(e) => setTotpCode(e.target.value)}
                inputMode="numeric"
                placeholder="123456"
                required
              />
            </div>
            <Button type="submit" disabled={busy}>
              {busy ? "…" : "Generate new recovery codes"}
            </Button>
          </form>
        )}
      </Card>

      <div className="mt-4">
        <Card title="Password">
          <form onSubmit={changePassword} className="max-w-sm">
            <label className={labelClass}>Current password</label>
            <input
              className={`${inputClass} mb-4`}
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              type="password"
              required
            />
            <label className={labelClass}>New password</label>
            <input
              className={`${inputClass} mb-4`}
              value={next}
              onChange={(e) => setNext(e.target.value)}
              type="password"
              required
              minLength={8}
            />
            <label className={labelClass}>Confirm new password</label>
            <input
              className={`${inputClass} mb-5`}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              type="password"
              required
              minLength={8}
            />
            <Button type="submit" variant="primary" disabled={busy}>
              {busy ? "…" : "Change password"}
            </Button>
            <p className="mt-3 text-sm text-ink-faint">
              Changing your password signs out every other device. This one stays
              signed in.
            </p>
          </form>
        </Card>
      </div>
    </Shell>
  );
}
