"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, setToken } from "@/lib/api";
import { Button, ErrorNote } from "@/components/ui";

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
    <div className="flex min-h-screen items-center justify-center">
      <form onSubmit={submit} className="w-96 rounded-lg border border-zinc-800 bg-zinc-900 p-6">
        <h1 className="mb-1 text-xl font-bold">trader</h1>
        <p className="mb-6 text-sm text-zinc-500">
          Paper-first algo trading. Seed login: demo@trader.local / demo1234
        </p>
        <label className="mb-1 block text-xs uppercase text-zinc-500">Email</label>
        <input
          className="mb-4 w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          type="email"
          required
        />
        <label className="mb-1 block text-xs uppercase text-zinc-500">Password</label>
        <input
          className="mb-6 w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          type="password"
          required
          minLength={8}
        />
        <Button type="submit" variant="primary" disabled={busy}>
          {mode === "login" ? "Log in" : "Create account"}
        </Button>
        <button
          type="button"
          className="ml-4 text-sm text-zinc-500 hover:text-zinc-300"
          onClick={() => setMode(mode === "login" ? "register" : "login")}
        >
          {mode === "login" ? "Need an account?" : "Have an account?"}
        </button>
        <ErrorNote message={error} />
      </form>
    </div>
  );
}
