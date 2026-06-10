"use client";

import { useState } from "react";
import { Button, ErrorNote } from "../ui";

const BROKER_HELP: Record<string, { title: string; body: string; needsRef: boolean }> = {
  paper: {
    title: "Paper Simulator",
    body: "One click. Internal simulator with virtual cash — no credentials needed.",
    needsRef: false,
  },
  zerodha: {
    title: "Zerodha Kite Connect (scaffolded — unverified)",
    body:
      "Requires a Kite Connect app (developers.kite.trade) with redirect URL set to " +
      "http://localhost:8000/api/v1/brokers/zerodha/callback. Put the app's API key/secret " +
      "in the backend env as <REF>_API_KEY / <REF>_API_SECRET, then use Connect to run the " +
      "Kite login → request_token → checksum exchange. Access token expires daily.",
    needsRef: true,
  },
  groww: {
    title: "Groww Trading API (scaffolded — unverified)",
    body:
      "Token-based auth. Generate a daily access token from the Groww trade API dashboard and " +
      "paste it via “Set token” after creating the connection (stored encrypted, never " +
      "re-displayed), or set <REF>_ACCESS_TOKEN in the backend env. Trading methods are " +
      "deliberately disabled until verified.",
    needsRef: true,
  },
  icici_breeze: {
    title: "ICICI Direct Breeze (scaffolded — unverified)",
    body:
      "Not OAuth. Requires three things: API key and secret key in the backend env " +
      "(<REF>_API_KEY / <REF>_API_SECRET), plus a session token obtained by logging in at " +
      "api.icicidirect.com/apiuser/login?api_key=... — paste it via “Set token” " +
      "(stored encrypted). Trading methods are deliberately disabled until verified.",
    needsRef: true,
  },
};

export function BrokerConnectModal({
  open,
  onClose,
  onCreate,
  error,
  busy,
}: {
  open: boolean;
  onClose: () => void;
  onCreate: (data: { broker: string; label: string; credential_ref: string | null }) => void;
  error: string | null;
  busy: boolean;
}) {
  const [broker, setBroker] = useState("paper");
  const [label, setLabel] = useState("");
  const [ref, setRef] = useState("");
  const help = BROKER_HELP[broker];

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="w-[34rem] rounded-lg border border-zinc-700 bg-zinc-900 p-6">
        <h2 className="mb-4 text-lg font-semibold">Create broker connection</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onCreate({ broker, label, credential_ref: help.needsRef ? ref || null : null });
          }}
        >
          <label className="mb-1 block text-xs uppercase text-zinc-500">Broker</label>
          <select
            className="mb-3 w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
            value={broker}
            onChange={(e) => setBroker(e.target.value)}
          >
            <option value="paper">paper</option>
            <option value="zerodha">zerodha</option>
            <option value="groww">groww</option>
            <option value="icici_breeze">icici_breeze</option>
          </select>

          <div className="mb-3 rounded border border-zinc-800 bg-zinc-950 p-3">
            <p className="text-sm font-medium">{help.title}</p>
            <p className="mt-1 text-xs leading-relaxed text-zinc-400">{help.body}</p>
          </div>

          <label className="mb-1 block text-xs uppercase text-zinc-500">Label</label>
          <input
            className="mb-3 w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            required
            maxLength={64}
          />

          {help.needsRef && (
            <>
              <label className="mb-1 block text-xs uppercase text-zinc-500">
                Credential ref (env-var prefix, e.g. ZERODHA_MAIN)
              </label>
              <input
                className="mb-3 w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
                value={ref}
                onChange={(e) => setRef(e.target.value.toUpperCase())}
                placeholder="ZERODHA_MAIN"
              />
            </>
          )}

          <ErrorNote message={error} />
          <div className="mt-4 flex justify-end gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={busy}>
              Create
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

export function SessionTokenModal({
  open,
  broker,
  onClose,
  onSubmit,
  error,
  busy,
}: {
  open: boolean;
  broker: string;
  onClose: () => void;
  onSubmit: (token: string) => void;
  error: string | null;
  busy: boolean;
}) {
  const [token, setToken] = useState("");
  if (!open) return null;

  const hint =
    broker === "groww"
      ? "Paste the daily access token from the Groww trade API dashboard."
      : "Paste the Breeze session token from the api.icicidirect.com login redirect.";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="w-[30rem] rounded-lg border border-zinc-700 bg-zinc-900 p-6">
        <h2 className="mb-2 text-lg font-semibold">Set session token</h2>
        <p className="mb-3 text-xs text-zinc-400">
          {hint} Stored encrypted on the backend and never displayed again — only
          “configured” status is shown.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onSubmit(token);
            setToken("");
          }}
        >
          <input
            type="password"
            autoComplete="off"
            className="mb-3 w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            required
            minLength={8}
            placeholder="token"
          />
          <ErrorNote message={error} />
          <div className="mt-4 flex justify-end gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={busy}>
              Save token
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
