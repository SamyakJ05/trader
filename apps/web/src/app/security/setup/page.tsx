"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { api, setToken } from "@/lib/api";
import { Button, Card, ErrorNote, inputClass, labelClass } from "@/components/ui";

interface SetupResponse {
  secret: string;
  provisioning_uri: string;
}

/**
 * Forced two-factor enrolment. Two-factor is mandatory on this instance, so
 * every route except this one refuses a user who has not finished — the API
 * client redirects here on `totp_setup_required`.
 */
export default function TotpSetupPage() {
  const router = useRouter();
  const [setup, setSetup] = useState<SetupResponse | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<SetupResponse>("/auth/totp/setup", { method: "POST" })
      .then((res) => {
        setSetup(res);
        // Rendered locally: the provisioning URI contains the TOTP secret, so
        // it must never be sent to a third-party QR service.
        return QRCode.toDataURL(res.provisioning_uri, { width: 200, margin: 1 });
      })
      .then((dataUrl) => dataUrl && setQr(dataUrl))
      .catch((err) => setError(err instanceof Error ? err.message : "Failed"));
  }, []);

  async function enable(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api<{ recovery_codes: string[] }>("/auth/totp/enable", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      setCodes(res.recovery_codes);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  // Recovery codes are shown exactly once — they are hashed at rest and can
  // never be redisplayed, so the user must confirm they have saved them.
  if (codes) {
    return (
      <div className="mx-auto max-w-lg px-4 py-16">
        <Card title="Save your recovery codes">
          <p className="mb-4 text-sm text-ink-dim">
            These are shown once and cannot be retrieved later. Each works a single
            time, and they are the only way back in if you lose your authenticator.
          </p>
          <pre className="mb-4 rounded-lg border border-line bg-panel-2 p-4 font-mono text-sm leading-7">
            {codes.join("\n")}
          </pre>
          <div className="mb-5 flex gap-2">
            <Button
              onClick={() => navigator.clipboard?.writeText(codes.join("\n"))}
              type="button"
            >
              Copy
            </Button>
            <Button
              type="button"
              onClick={() => {
                const blob = new Blob([codes.join("\n")], { type: "text/plain" });
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = "trader-recovery-codes.txt";
                a.click();
                URL.revokeObjectURL(url);
              }}
            >
              Download
            </Button>
          </div>
          <label className="mb-5 flex items-center gap-2 text-sm text-ink-dim">
            <input
              type="checkbox"
              checked={saved}
              onChange={(e) => setSaved(e.target.checked)}
            />
            I have saved these somewhere safe
          </label>
          <Button
            variant="primary"
            disabled={!saved}
            onClick={() => router.replace("/dashboard")}
          >
            Continue
          </Button>
        </Card>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-lg px-4 py-16">
      <Card title="Set up two-factor authentication">
        <p className="mb-5 text-sm text-ink-dim">
          Required on this instance. These accounts connect to real broker APIs, so
          a password on its own is not enough.
        </p>

        {setup ? (
          <>
            <p className="mb-2 text-sm text-ink-dim">
              Scan this in your authenticator app, or enter the key by hand:
            </p>
            <div className="mb-5 break-all rounded-lg border border-line bg-panel-2 p-3 font-mono text-sm">
              {setup.secret}
            </div>
            {qr && (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                alt="QR code for authenticator app enrolment"
                className="mb-5 rounded-lg bg-white p-3"
                width={200}
                height={200}
                src={qr}
              />
            )}
            <form onSubmit={enable}>
              <label className={labelClass}>Enter the 6-digit code to confirm</label>
              <input
                className={`${inputClass} mb-5`}
                value={code}
                onChange={(e) => setCode(e.target.value)}
                inputMode="numeric"
                autoComplete="one-time-code"
                placeholder="123456"
                required
              />
              <Button type="submit" variant="primary" disabled={busy}>
                {busy ? "…" : "Enable"}
              </Button>
            </form>
          </>
        ) : (
          <p className="text-sm text-ink-dim">Preparing your secret…</p>
        )}

        <ErrorNote message={error} />
        <button
          type="button"
          className="mt-6 text-sm text-ink-faint hover:text-ink"
          onClick={() => {
            setToken(null);
            router.replace("/login");
          }}
        >
          Sign out instead
        </button>
      </Card>
    </div>
  );
}
