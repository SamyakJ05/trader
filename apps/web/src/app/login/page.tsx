"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, setToken } from "@/lib/api";
import { Button, ErrorNote, Pill, inputClass, labelClass } from "@/components/ui";

interface AuthResponse {
  token: string;
  user: { email: string };
}

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api<AuthResponse>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
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
          <h1 className="text-2xl font-bold tracking-tight">
            trader<span className="text-accent">_</span>
          </h1>
          <Pill value="paper" label="PAPER" />
        </div>
        <p className="mb-7 text-sm text-ink-dim">
          Paper-first algo trading. Accounts are invite-only — ask your
          instance operator for an invite link.
        </p>
        <label className={labelClass}>Email</label>
        <input
          className={`${inputClass} mb-4`}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          type="email"
          required
        />
        <label className={labelClass}>Password</label>
        <input
          className={`${inputClass} mb-6`}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          type="password"
          required
          minLength={8}
        />
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? "…" : "Log in"}
        </Button>
        <ErrorNote message={error} />
      </form>
    </div>
  );
}
