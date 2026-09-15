"use client";

import { Button, Card, Pill } from "../ui";
import { BrokerAccount, BrokerCapabilities } from "@/lib/types";
import {
  AdapterStatusBadge,
  CapabilityBadges,
  EnvironmentPill,
  LiveEnabledChip,
  ReadPathChip,
  SyncStatusChip,
  TradePathChip,
} from "./badges";

export type AccountAction =
  | "connect"
  | "reconnect"
  | "disconnect"
  | "verify"
  | "sync"
  | "set-token"
  | "toggle-live"
  | "delete";

// Brokers like Breeze have no programmatic session refresh — the session
// dies on the broker's own schedule (Breeze: midnight IST or 24h from issue,
// whichever first, per SEBI's daily-reset rule) regardless of whether
// anything here notices. This is the difference between finding that out
// when the next order silently fails, and finding it out with time to log
// in again first.
const SESSION_WARNING_WINDOW_MS = 2 * 60 * 60 * 1000; // 2 hours

function formatTimeRemaining(ms: number): string {
  const hours = Math.floor(ms / (60 * 60 * 1000));
  const minutes = Math.floor((ms % (60 * 60 * 1000)) / (60 * 1000));
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${Math.max(minutes, 1)}m`;
}

function credentialWarning(account: BrokerAccount): string | null {
  if (account.broker === "paper") return null;
  const c = account.credentials;
  if (!c.env_keys_configured && !c.env_access_token_configured) {
    return `API credentials missing — set ${account.credential_ref ?? "<REF>"}_API_KEY / _API_SECRET in the backend environment.`;
  }
  if (account.broker !== "zerodha" && !c.session_token_configured && !c.env_access_token_configured) {
    return "No session/access token configured — use “Set token”.";
  }
  if (c.session_expires_at) {
    const remaining = new Date(c.session_expires_at).getTime() - Date.now();
    if (remaining <= 0) {
      return "Session expired — log in to resume trading.";
    }
    if (remaining <= SESSION_WARNING_WINDOW_MS) {
      return `Breeze session expires in ${formatTimeRemaining(remaining)} — no automatic refresh, log in again before it lapses.`;
    }
  }
  return null;
}

export function BrokerConnectionCard({
  account,
  capabilities,
  onAction,
  busy = false,
}: {
  account: BrokerAccount;
  capabilities?: BrokerCapabilities;
  onAction: (action: AccountAction) => void;
  busy?: boolean;
}) {
  const warning = credentialWarning(account);
  const tokenBrokers = account.broker === "groww" || account.broker === "icici_breeze";

  return (
    <Card>
      <div className="flex items-start justify-between">
        <div>
          <p className="text-base font-semibold tracking-tight">
            {account.label}{" "}
            <span className="text-sm font-normal text-ink-faint">
              {capabilities?.display_name ?? account.broker}
              {account.broker_client_id ? ` · ${account.broker_client_id}` : ""}
            </span>
          </p>
          <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
            <EnvironmentPill environment={account.environment} />
            <Pill value={account.status} />
            <AdapterStatusBadge status={account.adapter_status} />
            <ReadPathChip readVerifiedAt={account.read_verified_at} />
            <TradePathChip account={account} />
            <LiveEnabledChip liveEnabled={account.live_enabled} />
            <SyncStatusChip lastSyncAt={account.last_sync_at} />
          </div>
        </div>
      </div>

      {capabilities && (
        <div className="mt-3.5">
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-ink-faint">
            Broker API capabilities (not our adapter status)
          </p>
          <CapabilityBadges capabilities={capabilities} />
        </div>
      )}

      {account.status_message && (
        <p className="mt-3 rounded-lg border border-warn/30 bg-warn/10 p-2.5 text-xs text-warn">
          {account.status_message}
        </p>
      )}
      {warning && (
        <p className="mt-3 rounded-lg border border-warn/30 bg-warn/10 p-2.5 text-xs text-warn">
          {warning}
        </p>
      )}
      {capabilities && capabilities.adapter_status !== "working" && (
        <p className="mt-2 text-xs text-ink-faint">{capabilities.notes}</p>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-3.5">
        {account.broker === "zerodha" && (
          <>
            <Button size="sm" variant="primary" disabled={busy} onClick={() => onAction("connect")}>
              Connect
            </Button>
            <Button size="sm" disabled={busy} onClick={() => onAction("reconnect")}>
              Reconnect
            </Button>
          </>
        )}
        {tokenBrokers && (
          <Button size="sm" variant="primary" disabled={busy} onClick={() => onAction("set-token")}>
            Set token
          </Button>
        )}
        <Button size="sm" disabled={busy} onClick={() => onAction("verify")}>
          Verify read access
        </Button>
        <Button size="sm" disabled={busy} onClick={() => onAction("sync")}>
          Sync now
        </Button>
        <span className="flex-1" />
        {account.broker !== "paper" && (
          <>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => onAction("disconnect")}>
              Disconnect
            </Button>
            <Button
              size="sm"
              variant={account.live_enabled ? "danger" : "ghost"}
              disabled={busy}
              onClick={() => onAction("toggle-live")}
            >
              {account.live_enabled ? "Disable live" : "Enable live"}
            </Button>
          </>
        )}
        <Button size="sm" variant="danger" disabled={busy} onClick={() => onAction("delete")}>
          Delete
        </Button>
      </div>
    </Card>
  );
}
