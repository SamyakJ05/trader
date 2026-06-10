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
  const [email, setEmail] = useState("demo@trader.local");
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState<"login" | "register">("login");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api<AuthResponse>(`/auth/${mode}`, {
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
          Paper-first algo trading. Seed login: demo@trader.local / demo1234
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
        <div className="flex items-center justify-between">
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? "…" : mode === "login" ? "Log in" : "Create account"}
          </Button>
          <button
            type="button"
            className="text-sm text-ink-faint hover:text-ink"
            onClick={() => setMode(mode === "login" ? "register" : "login")}
          >
            {mode === "login" ? "Need an account?" : "Have an account?"}
          </button>
        </div>
        <ErrorNote message={error} />
      </form>
    </div>
  );
}
