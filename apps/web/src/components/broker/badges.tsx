"use client";

import { Pill } from "../ui";
import { BrokerAccount, BrokerCapabilities } from "@/lib/types";

export function EnvironmentPill({ environment }: { environment: string }) {
  return <Pill value={environment} />;
}

export function AdapterStatusBadge({ status }: { status: string }) {
  return <Pill value={status} label={`adapter: ${status}`} />;
}

export function SyncStatusChip({ lastSyncAt }: { lastSyncAt: string | null }) {
  if (!lastSyncAt) {
    return <Pill value="pending_auth" label="never synced" />;
  }
  const age = Date.now() - new Date(lastSyncAt).getTime();
  const fresh = age < 15 * 60 * 1000;
  return (
    <Pill
      value={fresh ? "connected" : "pending_auth"}
      label={`synced ${new Date(lastSyncAt).toLocaleTimeString()}`}
    />
  );
}

export function ReadPathChip({ readVerifiedAt }: { readVerifiedAt: string | null }) {
  return readVerifiedAt ? (
    <Pill value="working" label={`read verified ${new Date(readVerifiedAt).toLocaleDateString()}`} />
  ) : (
    <Pill value="scaffold" label="read unverified" />
  );
}

export function TradePathChip({ account }: { account: BrokerAccount }) {
  if (account.broker === "paper") {
    return <Pill value="working" label="trade path: working (paper)" />;
  }
  return account.adapter_status === "working" ? (
    <Pill value="working" label="trade path: verified" />
  ) : (
    <Pill value="error" label="trade path unverified" />
  );
}

export function LiveEnabledChip({ liveEnabled }: { liveEnabled: boolean }) {
  return liveEnabled ? (
    <Pill value="live" label="LIVE ENABLED" />
  ) : (
    <Pill value="disconnected" label="live disabled" />
  );
}

const CAPABILITY_FIELDS: [keyof BrokerCapabilities, string][] = [
  ["place_order", "Orders"],
  ["holdings", "Holdings"],
  ["positions", "Positions"],
  ["funds", "Funds"],
  ["instruments_dump", "Instruments"],
  ["websocket_ticks", "Ticks"],
  ["order_postbacks", "Postbacks"],
  ["amo_orders", "AMO"],
  ["bracket_gtt", "GTT"],
];

export function CapabilityBadges({ capabilities }: { capabilities: BrokerCapabilities }) {
  return (
    <div className="flex flex-wrap gap-1">
      {CAPABILITY_FIELDS.map(([field, label]) => (
        <span
          key={field}
          title={`Broker API ${capabilities[field] ? "supports" : "does not support"} ${label}. This describes the broker, not our adapter.`}
          className={`rounded px-2 py-0.5 text-xs ${
            capabilities[field]
              ? "bg-gain/10 text-gain"
              : "bg-panel-2 text-ink-faint line-through"
          }`}
        >
          {label}
        </span>
      ))}
    </div>
  );
}
