"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { api, setToken } from "@/lib/api";
import { Wordmark } from "@/components/Wordmark";
import { Button, ErrorNote, inputClass, labelClass } from "@/components/ui";

interface AuthResponse {
  token: string;
  user: { email: string };
}

interface InvitePreview {
  email: string;
  full_name: string | null;
}

function InviteForm() {
  const router = useRouter();
  const token = useSearchParams().get("token") ?? "";

  const [preview, setPreview] = useState<InvitePreview | null>(null);
  const [checking, setChecking] = useState(true);
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) {
      setError("This link is missing its invite token.");
      setChecking(false);
      return;
    }
    api<InvitePreview>(`/auth/invite/${encodeURIComponent(token)}`)
      .then((p) => {
        setPreview(p);
        setFullName(p.full_name ?? "");
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Invalid invite"))
      .finally(() => setChecking(false));
  }, [token]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await api<AuthResponse>("/auth/register", {
        method: "POST",
        body: JSON.stringify({ token, password, full_name: fullName || null }),
      });
      setToken(res.token);
      router.replace("/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-xl border border-line bg-panel p-7 shadow-2xl shadow-black/30"
      >
        <div className="mb-1 flex items-center gap-2">
          <h1><Wordmark size="lg" /></h1>
        </div>

        {checking ? (
          <p className="mt-6 text-sm text-ink-dim">Checking your invite…</p>
        ) : preview ? (
          <>
            <p className="mb-7 mt-1 text-sm text-ink-dim">
              Setting up <span className="text-ink">{preview.email}</span>. Choose a
              password to finish.
            </p>
            <label className={labelClass}>Name</label>
            <input
              className={`${inputClass} mb-4`}
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Optional"
            />
            <label className={labelClass}>Password</label>
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
              className={`${inputClass} mb-6`}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              type="password"
              required
              minLength={8}
            />
            <Button type="submit" variant="primary" disabled={busy}>
              {busy ? "Creating…" : "Create account"}
            </Button>
          </>
        ) : (
          <p className="mb-4 mt-1 text-sm text-ink-dim">
            Ask your instance operator for a fresh invite link.
          </p>
        )}
        <ErrorNote message={error} />
      </form>
    </div>
  );
}

export default function InvitePage() {
  return (
    <Suspense fallback={null}>
      <InviteForm />
    </Suspense>
  );
}
