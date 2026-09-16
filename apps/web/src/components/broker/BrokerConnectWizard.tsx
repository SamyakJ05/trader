"use client";

import { useEffect, useState } from "react";
import { Button, ErrorNote, Modal, Pill, inputClass, labelClass } from "../ui";
import { IconCheck, IconCopy } from "../icons";
import { useToast } from "../toast";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { BrokerAccount, BrokerCapabilities } from "@/lib/types";

type TokenFlow = "none" | "redirect" | "paste";

interface BrokerSetup {
  needsRef: boolean;
  tokenFlow: TokenFlow;
  note: string;
  envSnippet?: (ref: string) => string;
}

const BROKER_ORDER = ["paper", "zerodha", "groww", "icici_breeze"];

const SETUP: Record<string, BrokerSetup> = {
  paper: {
    needsRef: false,
    tokenFlow: "none",
    note: "Internal simulator with virtual cash. No credentials needed — connected instantly.",
  },
  zerodha: {
    needsRef: true,
    tokenFlow: "redirect",
    note:
      "Requires a Kite Connect app (developers.kite.trade) with redirect URL " +
      "http://localhost:8000/api/v1/brokers/zerodha/callback. Connect runs the Kite login → " +
      "request_token → checksum exchange. Access token expires daily.",
    envSnippet: (ref) => `${ref}_API_KEY=your-kite-api-key\n${ref}_API_SECRET=your-kite-api-secret`,
  },
  groww: {
    needsRef: true,
    tokenFlow: "paste",
    note:
      "Token-based auth. Generate a daily access token from the Groww trade API dashboard and " +
      "paste it in the next step (stored encrypted, never re-displayed).",
    envSnippet: (ref) => `${ref}_API_KEY=your-groww-api-key\n${ref}_API_SECRET=your-groww-api-secret`,
  },
  icici_breeze: {
    needsRef: true,
    tokenFlow: "paste",
    note:
      "Not OAuth. Needs API key + secret in the backend env, plus a session token from " +
      "api.icicidirect.com/apiuser/login?api_key=… pasted in the next step (stored encrypted).",
    envSnippet: (ref) => `${ref}_API_KEY=your-breeze-api-key\n${ref}_API_SECRET=your-breeze-secret-key`,
  },
};

type Step = "broker" | "configure" | "activate";
type RowState = "idle" | "busy" | "done" | "failed";

export function BrokerConnectWizard({
  open,
  onClose,
  capabilities,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  capabilities?: Record<string, BrokerCapabilities>;
  onDone: () => void;
}) {
  const { push } = useToast();
  // A live-only instance refuses to create paper accounts, so offering one is
  // offering a 409. The flag comes from the server rather than being inferred
  // from the account list: having no paper accounts left is not the same as
  // refusing to make them.
  const { data: config } = useApi<{ paper_trading_enabled: boolean }>("/system/config");
  const paperEnabled = config?.paper_trading_enabled !== false;
  const brokers = paperEnabled
    ? BROKER_ORDER
    : BROKER_ORDER.filter((b) => b !== "paper");

  const [step, setStep] = useState<Step>("broker");
  const [broker, setBroker] = useState("paper");
  const [label, setLabel] = useState("");
  const [ref, setRef] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [account, setAccount] = useState<BrokerAccount | null>(null);
  const [connectState, setConnectState] = useState<RowState>("idle");
  const [verifyState, setVerifyState] = useState<RowState>("idle");
  const [verifyDetail, setVerifyDetail] = useState<string | null>(null);
  const [token, setTokenValue] = useState("");
  const [busy, setBusy] = useState(false);

  // Without this the wizard opens on "paper" and submits it, because the
  // default is set before the config arrives.
  useEffect(() => {
    if (!paperEnabled && broker === "paper") setBroker(brokers[0]);
  }, [paperEnabled, broker, brokers]);

  const setup = SETUP[broker];

  function reset() {
    setStep("broker");
    setBroker(paperEnabled ? "paper" : brokers[0]);
    setLabel("");
    setRef("");
    setError(null);
    setAccount(null);
    setConnectState("idle");
    setVerifyState("idle");
    setVerifyDetail(null);
    setTokenValue("");
    setBusy(false);
  }

  function close() {
    reset();
    onClose();
  }

  async function createAccount(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const created = await api<BrokerAccount>("/brokers/accounts", {
        method: "POST",
        body: JSON.stringify({
          broker,
          label,
          credential_ref: setup.needsRef ? ref || null : null,
          environment: "paper",
        }),
      });
      setAccount(created);
      setStep("activate");
      if (broker === "paper") {
        // paper connects instantly — run connect + verify automatically
        await runConnect(created);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create connection");
    } finally {
      setBusy(false);
    }
  }

  async function runConnect(acc: BrokerAccount) {
    setConnectState("busy");
    setError(null);
    try {
      const res = await api<{ login_url?: string; status?: string }>(
        `/brokers/accounts/${acc.id}/connect`,
        { method: "POST" }
      );
      if (res.login_url) {
        window.location.href = res.login_url; // Kite login redirect flow
        return;
      }
      setConnectState("done");
    } catch (err) {
      setConnectState("failed");
      setError(err instanceof Error ? err.message : "Connect failed");
    }
  }

  async function saveTokenAndConnect() {
    if (!account) return;
    setConnectState("busy");
    setError(null);
    try {
      await api(`/brokers/accounts/${account.id}/session-token`, {
        method: "PUT",
        body: JSON.stringify({ token }),
      });
      setTokenValue("");
      setConnectState("done");
      push("success", "Token stored (encrypted). Verify read access to confirm it works.");
    } catch (err) {
      setConnectState("failed");
      setError(err instanceof Error ? err.message : "Failed to store token");
    }
  }

  async function runVerify() {
    if (!account) return;
    setVerifyState("busy");
    setError(null);
    try {
      const res = await api<{ verified: boolean; checks: Record<string, string> }>(
        `/brokers/accounts/${account.id}/verify`,
        { method: "POST" }
      );
      setVerifyState(res.verified ? "done" : "failed");
      setVerifyDetail(
        res.verified
          ? "Read access verified (profile + funds)."
          : Object.entries(res.checks)
              .map(([k, v]) => `${k}: ${v}`)
              .join(" · ")
      );
    } catch (err) {
      setVerifyState("failed");
      setVerifyDetail(err instanceof Error ? err.message : "Verification failed");
    }
  }

  function copySnippet() {
    if (!setup.envSnippet) return;
    navigator.clipboard.writeText(setup.envSnippet(ref || "ZERODHA_MAIN"));
    push("info", "Env snippet copied to clipboard");
  }

  const stepIndex = { broker: 0, configure: 1, activate: 2 }[step];

  return (
    <Modal open={open} onClose={close} title="Connect a broker" width="w-[38rem]">
      {/* progress */}
      <div className="mb-5 flex items-center gap-2">
        {["Broker", "Configure", "Activate"].map((s, i) => (
          <div key={s} className="flex items-center gap-2">
            <span
              className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold ${
                i < stepIndex
                  ? "bg-gain/20 text-gain"
                  : i === stepIndex
                    ? "bg-accent/20 text-accent"
                    : "bg-panel-2 text-ink-faint"
              }`}
            >
              {i < stepIndex ? <IconCheck width={10} height={10} /> : i + 1}
            </span>
            <span
              className={`text-xs font-medium ${i === stepIndex ? "text-ink" : "text-ink-faint"}`}
            >
              {s}
            </span>
            {i < 2 && <span className="mx-1 h-px w-6 bg-line-2" />}
          </div>
        ))}
      </div>

      {step === "broker" && (
        <div className="space-y-2">
          {brokers.map((b) => {
            const caps = capabilities?.[b];
            const active = broker === b;
            return (
              <button
                key={b}
                type="button"
                onClick={() => setBroker(b)}
                className={`flex w-full items-center justify-between rounded-xl border p-3.5 text-left transition-colors ${
                  active ? "border-accent/60 bg-accent/5" : "border-line-2 hover:border-ink-faint"
                }`}
              >
                <div>
                  <p className="text-sm font-semibold">{caps?.display_name ?? b}</p>
                  <p className="mt-0.5 text-xs text-ink-faint">
                    {caps?.auth_model || SETUP[b].note.slice(0, 80)}
                  </p>
                </div>
                <Pill value={caps?.adapter_status ?? (b === "paper" ? "working" : "scaffold")} />
              </button>
            );
          })}
          <div className="flex justify-end gap-2 pt-3">
            <Button onClick={close}>Cancel</Button>
            <Button variant="primary" onClick={() => setStep("configure")}>
              Continue
            </Button>
          </div>
        </div>
      )}

      {step === "configure" && (
        <form onSubmit={createAccount}>
          <div className="mb-4 rounded-lg border border-line bg-bg p-3">
            <p className="text-xs leading-relaxed text-ink-dim">{setup.note}</p>
          </div>

          <label className={labelClass}>Label</label>
          <input
            className={`${inputClass} mb-4`}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="e.g. Main paper account"
            required
            maxLength={64}
          />

          {setup.needsRef && (
            <>
              <label className={labelClass}>Credential ref (env-var prefix)</label>
              <input
                className={`${inputClass} num mb-2`}
                value={ref}
                onChange={(e) => setRef(e.target.value.toUpperCase())}
                placeholder={`${broker.toUpperCase().replace("ICICI_BREEZE", "BREEZE")}_MAIN`}
                required
              />
              {setup.envSnippet && (
                <div className="mb-4 rounded-lg border border-line bg-bg p-3">
                  <div className="mb-1 flex items-center justify-between">
                    <p className="text-[10px] font-semibold uppercase tracking-wider text-ink-faint">
                      Add to backend .env
                    </p>
                    <button
                      type="button"
                      onClick={copySnippet}
                      className="flex items-center gap-1 text-xs text-accent hover:underline"
                    >
                      <IconCopy width={12} height={12} /> copy
                    </button>
                  </div>
                  <pre className="num overflow-x-auto text-xs text-ink-dim">
                    {setup.envSnippet(ref || "ZERODHA_MAIN")}
                  </pre>
                </div>
              )}
            </>
          )}

          <ErrorNote message={error} />
          <div className="mt-4 flex justify-between">
            <Button variant="ghost" onClick={() => setStep("broker")}>
              ← Back
            </Button>
            <div className="flex gap-2">
              <Button onClick={close}>Cancel</Button>
              <Button type="submit" variant="primary" disabled={busy}>
                {busy ? "Creating…" : "Create connection"}
              </Button>
            </div>
          </div>
        </form>
      )}

      {step === "activate" && account && (
        <div className="space-y-3">
          <ActivateRow state="done" title={`Connection “${account.label}” created`} />

          {setup.tokenFlow === "redirect" && (
            <ActivateRow
              state={connectState}
              title="Connect via Kite login"
              detail="Redirects to Zerodha — you'll land back on the Brokers page."
              action={
                connectState !== "done" && (
                  <Button size="sm" variant="primary" disabled={connectState === "busy"} onClick={() => runConnect(account)}>
                    Connect
                  </Button>
                )
              }
            />
          )}

          {setup.tokenFlow === "paste" && (
            <ActivateRow
              state={connectState}
              title="Set session token"
              detail="Stored encrypted on the backend, never displayed again."
              action={
                connectState !== "done" && (
                  <div className="flex gap-2">
                    <input
                      type="password"
                      autoComplete="off"
                      className={`${inputClass} w-44`}
                      value={token}
                      onChange={(e) => setTokenValue(e.target.value)}
                      placeholder="token"
                      minLength={8}
                    />
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={connectState === "busy" || token.length < 8}
                      onClick={saveTokenAndConnect}
                    >
                      Save
                    </Button>
                  </div>
                )
              }
            />
          )}

          {setup.tokenFlow === "none" && (
            <ActivateRow state={connectState === "idle" ? "busy" : connectState} title="Connect paper simulator" />
          )}

          <ActivateRow
            state={verifyState}
            title="Verify read access"
            detail={verifyDetail ?? "Confirms profile + funds can be read."}
            action={
              verifyState !== "done" && (
                <Button size="sm" disabled={verifyState === "busy"} onClick={runVerify}>
                  Verify
                </Button>
              )
            }
          />

          <ErrorNote message={error} />
          <div className="flex justify-end gap-2 pt-2">
            <Button
              variant="primary"
              onClick={() => {
                onDone();
                close();
              }}
            >
              Done
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}

function ActivateRow({
  state,
  title,
  detail,
  action,
}: {
  state: RowState;
  title: string;
  detail?: string;
  action?: React.ReactNode;
}) {
  const dot = {
    idle: "bg-ink-faint",
    busy: "animate-pulse bg-warn",
    done: "bg-gain",
    failed: "bg-loss",
  }[state];
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-line bg-bg p-3">
      <div className="flex min-w-0 items-start gap-2.5">
        <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${dot}`} />
        <div className="min-w-0">
          <p className="text-sm font-medium">{title}</p>
          {detail && <p className="mt-0.5 text-xs text-ink-faint">{detail}</p>}
        </div>
      </div>
      {action}
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
  initialToken,
}: {
  open: boolean;
  broker: string;
  onClose: () => void;
  onSubmit: (token: string) => void;
  error: string | null;
  busy: boolean;
  /** Pre-fills the field — Breeze's redirect lands with the token already in
   * the URL, so requiring it be retyped by hand would just be friction. */
  initialToken?: string;
}) {
  const [token, setToken] = useState(initialToken ?? "");

  useEffect(() => {
    if (open && initialToken) setToken(initialToken);
  }, [open, initialToken]);

  const hint =
    broker === "groww"
      ? "Paste the daily access token from the Groww trade API dashboard."
      : "Paste the Breeze session token from the api.icicidirect.com login redirect.";

  return (
    <Modal open={open} onClose={onClose} title="Set session token" width="w-[30rem]">
      <p className="mb-3 text-xs text-ink-dim">
        {hint} Stored encrypted on the backend and never displayed again — only “configured”
        status is shown.
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
          className={`${inputClass} mb-3`}
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
    </Modal>
  );
}
