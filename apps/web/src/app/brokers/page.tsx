"use client";

import { useEffect, useState } from "react";
import Shell from "@/components/Shell";
import { Button, ErrorNote } from "@/components/ui";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { BrokerAccount, BrokerCapabilities } from "@/lib/types";
import {
  AccountAction,
  BrokerConnectionCard,
} from "@/components/broker/BrokerConnectionCard";
import {
  BrokerConnectModal,
  SessionTokenModal,
} from "@/components/broker/BrokerConnectModal";

export default function BrokersPage() {
  const { data: accounts, reload } = useApi<BrokerAccount[]>("/brokers/accounts", 15000);
  const { data: capabilities } = useApi<Record<string, BrokerCapabilities>>(
    "/brokers/capabilities"
  );
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [tokenFor, setTokenFor] = useState<BrokerAccount | null>(null);
  const [tokenError, setTokenError] = useState<string | null>(null);

  // Zerodha callback lands back here with ?connected= or ?error=.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("connected")) {
      setNotice("Zerodha session stored — run “Verify read access” to confirm the read path.");
    } else if (params.get("error")) {
      setError(`Broker connection failed: ${params.get("error")}`);
    }
    if (params.get("connected") || params.get("error")) {
      window.history.replaceState(null, "", "/brokers");
    }
  }, []);

  async function handleAction(account: BrokerAccount, action: AccountAction) {
    setError(null);
    setNotice(null);
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
          setNotice(`Connection status: ${res.status}`);
          break;
        }
        case "reconnect":
          await api(`/brokers/accounts/${account.id}/refresh-session`, { method: "POST" });
          break;
        case "disconnect":
          await api(`/brokers/accounts/${account.id}/disconnect`, { method: "POST" });
          break;
        case "verify": {
          const res = await api<{ verified: boolean; checks: Record<string, string> }>(
            `/brokers/accounts/${account.id}/verify`,
            { method: "POST" }
          );
          setNotice(
            res.verified
              ? "Read access verified (profile + funds)."
              : `Verification failed: ${JSON.stringify(res.checks)}`
          );
          break;
        }
        case "sync": {
          const res = await api<{ skipped: string[] }>(
            `/brokers/accounts/${account.id}/sync`,
            { method: "POST" }
          );
          if (res.skipped?.length) setNotice(`Synced with skips: ${res.skipped.join("; ")}`);
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
          break;
      }
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusyId(null);
    }
  }

  async function createAccount(data: {
    broker: string;
    label: string;
    credential_ref: string | null;
  }) {
    setCreateError(null);
    try {
      await api("/brokers/accounts", {
        method: "POST",
        body: JSON.stringify({ ...data, environment: "paper" }),
      });
      setCreateOpen(false);
      reload();
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Failed");
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
      setNotice("Token stored (encrypted). Run “Verify read access” to confirm it works.");
      reload();
    } catch (err) {
      setTokenError(err instanceof Error ? err.message : "Failed");
    }
  }

  return (
    <Shell>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-bold">Broker Connections</h1>
        <Button variant="primary" onClick={() => setCreateOpen(true)}>
          Create connection
        </Button>
      </div>

      <ErrorNote message={error} />
      {notice && (
        <p className="mb-3 rounded border border-sky-800 bg-sky-950/50 p-2 text-sm text-sky-300">
          {notice}
        </p>
      )}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {accounts?.map((account) => (
          <BrokerConnectionCard
            key={account.id}
            account={account}
            capabilities={capabilities?.[account.broker]}
            onAction={(action) => handleAction(account, action)}
            busy={busyId === account.id}
          />
        ))}
      </div>
      {!accounts?.length && (
        <p className="text-sm text-zinc-500">
          No broker connections. Create a paper account to start.
        </p>
      )}

      <BrokerConnectModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreate={createAccount}
        error={createError}
        busy={false}
      />
      <SessionTokenModal
        open={tokenFor !== null}
        broker={tokenFor?.broker ?? ""}
        onClose={() => setTokenFor(null)}
        onSubmit={saveToken}
        error={tokenError}
        busy={false}
      />
    </Shell>
  );
}
