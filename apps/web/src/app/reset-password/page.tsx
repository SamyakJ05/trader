"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { api } from "@/lib/api";
import { Button, Card, ErrorNote, inputClass, labelClass } from "@/components/ui";

function ResetForm() {
  const router = useRouter();
  const token = useSearchParams().get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api("/auth/password/reset", {
        method: "POST",
        body: JSON.stringify({ token, password }),
      });
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-md px-4 py-16">
      <Card title="Choose a new password">
        {done ? (
          <>
            <p className="mb-5 text-sm text-ink-dim">
              Password changed. Every device has been signed out — sign in again with
              your new password and your authenticator code.
            </p>
            <Button variant="primary" onClick={() => router.replace("/login")}>
              Sign in
            </Button>
          </>
        ) : !token ? (
          <p className="text-sm text-ink-dim">This link is missing its reset token.</p>
        ) : (
          <form onSubmit={submit}>
            <label className={labelClass}>New password</label>
            <input
              className={`${inputClass} mb-4`}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              type="password"
              required
              minLength={8}
            />
            <label className={labelClass}>Confirm password</label>
            <input
              className={`${inputClass} mb-5`}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              type="password"
              required
              minLength={8}
            />
            <Button type="submit" variant="primary" disabled={busy}>
              {busy ? "…" : "Set password"}
            </Button>
            <ErrorNote message={error} />
          </form>
        )}
      </Card>
    </div>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={null}>
      <ResetForm />
    </Suspense>
  );
}
