"use client";

import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { Button, Card, ErrorNote, inputClass, labelClass } from "@/components/ui";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/password/forgot", {
        method: "POST",
        body: JSON.stringify({ email }),
      });
      setSent(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-md px-4 py-16">
      <Card title="Reset your password">
        {sent ? (
          // Deliberately does not say whether the address exists — that would
          // make this an account-existence oracle.
          <p className="text-sm text-ink-dim">
            If that address has an account, a reset link is on its way. It expires in
            30 minutes.
          </p>
        ) : (
          <form onSubmit={submit}>
            <label className={labelClass}>Email</label>
            <input
              className={`${inputClass} mb-5`}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              type="email"
              required
            />
            <Button type="submit" variant="primary" disabled={busy}>
              {busy ? "Sending…" : "Send reset link"}
            </Button>
            <ErrorNote message={error} />
          </form>
        )}
        <Link
          href="/login"
          className="mt-6 inline-block text-sm text-ink-faint hover:text-ink"
        >
          Back to sign in
        </Link>
      </Card>
    </div>
  );
}
