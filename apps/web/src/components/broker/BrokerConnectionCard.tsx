"use client";

import { Button, Card } from "../ui";
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
import { Pill } from "../ui";

export type AccountAction =
  | "connect"
  | "reconnect"
  | "disconnect"
  | "verify"
  | "sync"
  | "set-token"
  | "toggle-live"
  | "delete";

function credentialWarning(account: BrokerAccount): string | null {
  if (account.broker === "paper") return null;
  const c = account.credentials;
  if (!c.env_keys_configured && !c.env_access_token_configured) {
    return `API credentials missing — set ${account.credential_ref ?? "<REF>"}_API_KEY / _API_SECRET in the backend environment.`;
  }
  if (account.broker !== "zerodha" && !c.session_token_configured && !c.env_access_token_configured) {
    return "No session/access token configured — use “Set token”.";
  }
  if (c.session_expires_at && new Date(c.session_expires_at) < new Date()) {
    return "Stored session token has expired — reconnect or set a new token.";
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
          <p className="text-base font-semibold">
            {account.label}{" "}
            <span className="text-sm font-normal text-zinc-500">
              {capabilities?.display_name ?? account.broker}
              {account.broker_client_id ? ` · ${account.broker_client_id}` : ""}
            </span>
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
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
        <div className="mt-3">
          <p className="mb-1 text-xs uppercase text-zinc-500">
            Broker API capabilities (not our adapter status)
          </p>
          <CapabilityBadges capabilities={capabilities} />
        </div>
      )}

      {account.status_message && (
        <p className="mt-3 rounded border border-amber-800 bg-amber-950/40 p-2 text-xs text-amber-300">
          {account.status_message}
        </p>
      )}
      {warning && (
        <p className="mt-3 rounded border border-amber-800 bg-amber-950/40 p-2 text-xs text-amber-300">
          {warning}
        </p>
      )}
      {capabilities && capabilities.adapter_status !== "working" && (
        <p className="mt-2 text-xs text-zinc-500">{capabilities.notes}</p>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        {account.broker === "zerodha" && (
          <>
            <Button variant="primary" disabled={busy} onClick={() => onAction("connect")}>
              Connect
            </Button>
            <Button disabled={busy} onClick={() => onAction("reconnect")}>
              Reconnect
            </Button>
          </>
        )}
        {tokenBrokers && (
          <Button variant="primary" disabled={busy} onClick={() => onAction("set-token")}>
            Set token
          </Button>
        )}
        <Button disabled={busy} onClick={() => onAction("verify")}>
          Verify read access
        </Button>
        <Button disabled={busy} onClick={() => onAction("sync")}>
          Sync now
        </Button>
        {account.broker !== "paper" && (
          <>
            <Button disabled={busy} onClick={() => onAction("disconnect")}>
              Disconnect
            </Button>
            <Button
              variant={account.live_enabled ? "danger" : "default"}
              disabled={busy}
              onClick={() => onAction("toggle-live")}
            >
              {account.live_enabled ? "Disable live" : "Enable live"}
            </Button>
          </>
        )}
        <Button variant="danger" disabled={busy} onClick={() => onAction("delete")}>
          Delete
        </Button>
      </div>
    </Card>
  );
}
