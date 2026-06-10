"use client";

import Shell from "@/components/Shell";
import { Card, Pill, Td, Th } from "@/components/ui";
import { ReadPathChip, SyncStatusChip } from "@/components/broker/badges";
import { RecentAuditList } from "@/components/broker/RecentAuditList";
import { useApi } from "@/lib/useApi";
import { DashboardSummary } from "@/lib/types";

function Stat({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <Card title={label}>
      <p className="text-2xl font-bold">{value}</p>
      {sub && <p className="mt-1 text-xs text-zinc-500">{sub}</p>}
    </Card>
  );
}

export default function Dashboard() {
  const { data: s } = useApi<DashboardSummary>("/dashboard/summary", 10000);

  const strategyLine = s
    ? Object.entries(s.strategies)
        .map(([k, v]) => `${v} ${k.toLowerCase()}`)
        .join(" · ") || "none"
    : "—";

  return (
    <Shell>
      <h1 className="mb-4 text-xl font-bold">Dashboard</h1>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label="Broker accounts"
          value={s ? `${s.accounts.connected}/${s.accounts.total}` : "—"}
          sub={
            s
              ? `connected · ${s.accounts.by_environment["paper"] ?? 0} paper, ` +
                `${s.accounts.live_configured} live-configured`
              : undefined
          }
        />
        <Stat
          label="Funds (latest snapshots)"
          value={s ? `₹${Number(s.funds.total_available_cash).toLocaleString("en-IN")}` : "—"}
          sub={s && !s.funds.complete ? "partial — some accounts never synced" : "all accounts"}
        />
        <Stat label="Open positions" value={s?.open_positions ?? "—"} />
        <Stat label="Open orders" value={s?.open_orders ?? "—"} sub={s ? `strategies: ${strategyLine}` : undefined} />
      </div>

      <div className="mt-4">
        <Card title="Broker health">
          <table className="w-full">
            <thead>
              <tr className="border-b border-zinc-800">
                <Th>Account</Th><Th>Broker</Th><Th>Env</Th><Th>Status</Th>
                <Th>Read path</Th><Th>Sync</Th><Th>Cash</Th><Th>Holdings</Th>
              </tr>
            </thead>
            <tbody>
              {s?.per_account.map((a) => (
                <tr key={a.id} className="border-b border-zinc-900">
                  <Td className="font-medium">{a.label}</Td>
                  <Td className="text-zinc-400">{a.broker}</Td>
                  <Td><Pill value={a.environment} /></Td>
                  <Td><Pill value={a.status} /></Td>
                  <Td><ReadPathChip readVerifiedAt={a.read_verified_at} /></Td>
                  <Td><SyncStatusChip lastSyncAt={a.last_sync_at} /></Td>
                  <Td>
                    {a.available_cash !== null
                      ? `₹${Number(a.available_cash).toLocaleString("en-IN")}`
                      : <span className="text-zinc-600">no snapshot</span>}
                  </Td>
                  <Td>
                    {a.holdings_count !== null
                      ? a.holdings_count
                      : <span className="text-zinc-600">—</span>}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
          {s && (s.accounts.never_synced.length > 0 || s.accounts.read_unverified.length > 0) && (
            <p className="mt-3 text-xs text-amber-400/80">
              Partial data: {s.accounts.never_synced.length} account(s) never synced,{" "}
              {s.accounts.read_unverified.length} with unverified read access. Totals above
              exclude what brokers haven&apos;t reported.
            </p>
          )}
        </Card>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-4">
        <Card title="Kill switch">
          {s ? (
            <div className="flex items-center gap-3">
              <Pill
                value={s.killswitch.global_engaged ? "error" : "connected"}
                label={s.killswitch.global_engaged ? "ENGAGED" : "NORMAL"}
              />
              {s.killswitch.killed_strategies.length > 0 && (
                <span className="text-sm text-amber-400">
                  {s.killswitch.killed_strategies.length} strategy kill switch(es) engaged
                </span>
              )}
            </div>
          ) : (
            "—"
          )}
        </Card>
        <Card title="Recent activity">
          <RecentAuditList events={s?.recent_events ?? []} />
        </Card>
      </div>
    </Shell>
  );
}
