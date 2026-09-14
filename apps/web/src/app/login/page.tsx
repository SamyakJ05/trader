"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, setToken } from "@/lib/api";
import { Button, ErrorNote, Pill, inputClass, labelClass } from "@/components/ui";

interface AuthResponse {
  token: string;
  user: { email: string; totp_enabled: boolean };
  /** Session may only complete TOTP enrolment. */
  enrolment_only: boolean;
}

interface ChallengeResponse {
  challenge: string;
  totp_enrolled: boolean;
}

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [challenge, setChallenge] = useState<ChallengeResponse | null>(null);
  const [code, setCode] = useState("");
  const [useRecovery, setUseRecovery] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submitPassword(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api<ChallengeResponse>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      if (!res.totp_enrolled) {
        // Not enrolled yet — the state of every account when an instance first
        // makes 2FA mandatory. Exchange the challenge for an enrolment-only
        // session and send them to setup.
        const session = await api<AuthResponse>("/auth/login/verify", {
          method: "POST",
          body: JSON.stringify({ challenge: res.challenge }),
        });
        setToken(session.token);
        router.replace("/security/setup");
        return;
      }
      setChallenge(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  async function submitCode(e: React.FormEvent) {
    e.preventDefault();
    if (!challenge) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api<AuthResponse>("/auth/login/verify", {
        method: "POST",
        body: JSON.stringify({
          challenge: challenge.challenge,
          ...(useRecovery ? { recovery_code: code } : { code }),
        }),
      });
      setToken(res.token);
      router.replace(res.enrolment_only ? "/security/setup" : "/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  const awaitingCode = challenge !== null && challenge.totp_enrolled;

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={awaitingCode ? submitCode : submitPassword}
        className="w-full max-w-sm rounded-xl border border-line bg-panel p-7 shadow-2xl shadow-black/30"
      >
        <div className="mb-1 flex items-center gap-2">
          <h1 className="text-2xl font-bold tracking-tight">
            trader<span className="text-accent">_</span>
          </h1>
          <Pill value="paper" label="PAPER" />
        </div>

        {awaitingCode ? (
          <>
            <p className="mb-7 mt-1 text-sm text-ink-dim">
              {useRecovery
                ? "Enter one of your recovery codes."
                : "Enter the 6-digit code from your authenticator app."}
            </p>
            <label className={labelClass}>{useRecovery ? "Recovery code" : "Code"}</label>
            <input
              className={`${inputClass} mb-6`}
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoFocus
              autoComplete="one-time-code"
              inputMode={useRecovery ? "text" : "numeric"}
              placeholder={useRecovery ? "abcde-12345" : "123456"}
              required
            />
            <div className="flex items-center justify-between">
              <Button type="submit" variant="primary" disabled={busy}>
                {busy ? "…" : "Verify"}
              </Button>
              <button
                type="button"
                className="text-sm text-ink-faint hover:text-ink"
                onClick={() => {
                  setUseRecovery(!useRecovery);
                  setCode("");
                  setError(null);
                }}
              >
                {useRecovery ? "Use authenticator" : "Use a recovery code"}
              </button>
            </div>
          </>
        ) : (
          <>
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
            <div className="flex items-center justify-between">
              <Button type="submit" variant="primary" disabled={busy}>
                {busy ? "…" : "Continue"}
              </Button>
              <Link href="/forgot-password" className="text-sm text-ink-faint hover:text-ink">
                Forgot password?
              </Link>
            </div>
          </>
        )}
        <ErrorNote message={error} />
      </form>
    </div>
  );
}
