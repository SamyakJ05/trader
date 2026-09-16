"use client";

import { useEffect, useState } from "react";
import Shell from "@/components/Shell";
import { Button, EmptyState, PageHeader, Skeleton } from "@/components/ui";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { EgressBanner } from "@/components/broker/EgressBanner";
import { BrokerAccount, BrokerCapabilities } from "@/lib/types";
import {
  AccountAction,
  BrokerConnectionCard,
} from "@/components/broker/BrokerConnectionCard";
import {
  BrokerConnectWizard,
  SessionTokenModal,
} from "@/components/broker/BrokerConnectWizard";

export default function BrokersPage() {
  const { data: accounts, loading, reload } = useApi<BrokerAccount[]>("/brokers/accounts", 15000);
  const { data: capabilities } = useApi<Record<string, BrokerCapabilities>>(
    "/brokers/capabilities"
  );
  const { push } = useToast();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [tokenFor, setTokenFor] = useState<BrokerAccount | null>(null);
  const [tokenError, setTokenError] = useState<string | null>(null);
  const [pendingApiSession, setPendingApiSession] = useState<string | null>(null);

  // Zerodha callback lands back here with ?connected= or ?error=.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("connected")) {
      push("success", "Zerodha session stored — run “Verify read access” to confirm the read path.");
    } else if (params.get("error")) {
      push("error", `Broker connection failed: ${params.get("error")}`);
    }
    // Breeze has no per-path callback — ICICI redirects to whatever bare URL
    // was registered, which lands at / and forwards here (see app/page.tsx).
    // Hold the value until accounts have loaded, so it can be matched to the
    // right one rather than guessed at before the list exists.
    const apisession = params.get("apisession");
    if (apisession) setPendingApiSession(apisession);
    if (params.get("connected") || params.get("error") || apisession) {
      window.history.replaceState(null, "", "/brokers");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!pendingApiSession || !accounts) return;
    const breeze = accounts.filter((a) => a.broker === "icici_breeze");
    if (breeze.length === 1) {
      setTokenError(null);
      setTokenFor(breeze[0]);
      // pendingApiSession stays set here, deliberately: the modal reads it
      // via initialToken on the render this same effect triggers by calling
      // setTokenFor, and clearing it in the same batch raced that prop to
      // undefined before the modal ever saw it (confirmed live: the effect's
      // own log line showed pendingApiSession flip to null in the exact
      // render where the modal should have opened pre-filled, and it opened
      // empty instead). Clearing happens once the modal actually closes --
      // see the SessionTokenModal onClose/onSubmit handlers below.
    } else if (breeze.length === 0) {
      push("error", "Signed in to ICICI, but no icici_breeze account exists yet — add one, then paste the token manually.");
      setPendingApiSession(null);
    } else {
      push("info", "Signed in to ICICI — click “Set token” on the right Breeze account to paste it.");
      setPendingApiSession(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingApiSession, accounts]);

  async function handleAction(account: BrokerAccount, action: AccountAction) {
    setBusyId(account.id);
    try {
      switch (action) {
        case "connect": {
          const res = await api<{ login_url?: string; status?: string }>(
            `/brokers/accounts/${account.id}/connect`,
            { method: "POST" }
          );
          if (res.login_url) {
            window.location.href = res.login_url; // Kite login redirect flow
            return;
          }
          push("info", `Connection status: ${res.status}`);
          break;
        }
        case "reconnect":
          await api(`/brokers/accounts/${account.id}/refresh-session`, { method: "POST" });
          push("success", "Session refreshed");
          break;
        case "disconnect":
          await api(`/brokers/accounts/${account.id}/disconnect`, { method: "POST" });
          push("info", "Disconnected");
          break;
        case "verify": {
          const res = await api<{ verified: boolean; checks: Record<string, string> }>(
            `/brokers/accounts/${account.id}/verify`,
            { method: "POST" }
          );
          if (res.verified) push("success", "Read access verified (profile + funds).");
          else push("error", `Verification failed: ${JSON.stringify(res.checks)}`);
          break;
        }
        case "sync": {
          const res = await api<{ skipped: string[] }>(
            `/brokers/accounts/${account.id}/sync`,
            { method: "POST" }
          );
          if (res.skipped?.length) push("info", `Synced with skips: ${res.skipped.join("; ")}`);
          else push("success", "Synced");
          break;
        }
        case "sync-instruments": {
          // The instrument master maps a trading symbol to the broker's own
          // token, and nothing downstream works without it: the tick stream
          // has nothing to subscribe to and strategy creation rejects every
          // symbol as unknown. The nightly job does this too; this is for
          // someone who has just connected and does not want to wait.
          const res = await api<{ exchange: string; instruments: number }>(
            `/brokers/accounts/${account.id}/sync-instruments`,
            { method: "POST", body: JSON.stringify({ exchange: "NSE" }) }
          );
          push("success", `${res.instruments} NSE instruments synced`);
          break;
        }
        case "set-token":
          setTokenError(null);
          setTokenFor(account);
          break;
        case "toggle-live":
          await api(`/brokers/accounts/${account.id}/live`, {
            method: "PATCH",
            body: JSON.stringify({ live_enabled: !account.live_enabled }),
          });
          break;
        case "delete":
          if (!window.confirm(`Delete connection “${account.label}”?`)) break;
          await api(`/brokers/accounts/${account.id}`, { method: "DELETE" });
          push("info", "Connection deleted");
          break;
      }
      reload();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusyId(null);
    }
  }

  async function saveToken(token: string) {
    if (!tokenFor) return;
    setTokenError(null);
    try {
      await api(`/brokers/accounts/${tokenFor.id}/session-token`, {
        method: "PUT",
        body: JSON.stringify({ token }),
      });
      setTokenFor(null);
      setPendingApiSession(null);
      push("success", "Token stored (encrypted). Run “Verify read access” to confirm it works.");
      reload();
    } catch (err) {
      setTokenError(err instanceof Error ? err.message : "Failed");
    }
  }

  return (
    <Shell>
      <EgressBanner />

      <PageHeader
        title="Broker Connections"
        sub="Connect, verify and sync broker accounts — paper simulator or real brokers"
        action={
          <Button variant="primary" onClick={() => setWizardOpen(true)}>
            Connect broker
          </Button>
        }
      />

      {loading && !accounts ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Skeleton className="h-56" />
          <Skeleton className="h-56" />
        </div>
      ) : accounts?.length ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          {accounts.map((account) => (
            <BrokerConnectionCard
              key={account.id}
              account={account}
              capabilities={capabilities?.[account.broker]}
              onAction={(action) => handleAction(account, action)}
              busy={busyId === account.id}
            />
          ))}
        </div>
      ) : (
        <EmptyState
          title="No broker connections"
          hint="Start with the paper simulator — one click, virtual cash, no credentials."
          action={
            <Button variant="primary" onClick={() => setWizardOpen(true)}>
              Connect broker
            </Button>
          }
        />
      )}

      <BrokerConnectWizard
        open={wizardOpen}
        onClose={() => setWizardOpen(false)}
        capabilities={capabilities ?? undefined}
        onDone={reload}
      />
      <SessionTokenModal
        open={tokenFor !== null}
        broker={tokenFor?.broker ?? ""}
        onClose={() => {
          setTokenFor(null);
          setPendingApiSession(null);
        }}
        onSubmit={saveToken}
        error={tokenError}
        busy={false}
        initialToken={tokenFor?.broker === "icici_breeze" ? (pendingApiSession ?? undefined) : undefined}
      />
    </Shell>
  );
}
