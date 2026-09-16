"use client";

import Shell from "@/components/Shell";
import { Card, PageHeader, Pill, Skeleton, StatCard, Td, Th } from "@/components/ui";
import { TradingReadiness } from "@/components/TradingReadiness";
import { ReadPathChip, SyncStatusChip } from "@/components/broker/badges";
import { RecentAuditList } from "@/components/broker/RecentAuditList";
import { useApi } from "@/lib/useApi";
import { DashboardSummary } from "@/lib/types";

export default function Dashboard() {
  const { data: s } = useApi<DashboardSummary>("/dashboard/summary", 10000);

  const strategyLine = s
    ? Object.entries(s.strategies)
        .map(([k, v]) => `${v} ${k.toLowerCase()}`)
        .join(" · ") || "none"
    : undefined;

  return (
    <Shell>
      <PageHeader title="Dashboard" sub="Trading overview across connected brokers" />

      <div className="mb-4">
        <TradingReadiness />
      </div>

      {!s ? (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-28" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <StatCard
            label="Broker accounts"
            value={`${s.accounts.connected}/${s.accounts.total}`}
            sub={`connected · ${s.accounts.by_environment["paper"] ?? 0} paper, ${s.accounts.live_configured} live-configured`}
          />
          <StatCard
            label="Available funds"
            value={`₹${Number(s.funds.total_available_cash).toLocaleString("en-IN")}`}
            sub={s.funds.complete ? "all accounts reporting" : "partial — some accounts never synced"}
          />
          <StatCard label="Open positions" value={s.open_positions} />
          <StatCard label="Open orders" value={s.open_orders} sub={`strategies: ${strategyLine}`} />
        </div>
      )}

      <div className="mt-4">
        <Card title="Broker health" pad={false}>
          <div className="overflow-x-auto p-2">
            <table className="w-full">
              <thead>
                <tr className="border-b border-line">
                  <Th>Account</Th>
                  <Th>Broker</Th>
                  <Th>Env</Th>
                  <Th>Status</Th>
                  <Th>Read path</Th>
                  <Th>Sync</Th>
                  <Th right>Cash</Th>
                  <Th right>Holdings</Th>
                </tr>
              </thead>
              <tbody>
                {s?.per_account.map((a) => (
                  <tr key={a.id} className="border-b border-line/50 last:border-0">
                    <Td className="font-medium">{a.label}</Td>
                    <Td className="text-ink-dim">{a.broker}</Td>
                    <Td>
                      <Pill value={a.environment} />
                    </Td>
                    <Td>
                      <Pill value={a.status} />
                    </Td>
                    <Td>
                      <ReadPathChip readVerifiedAt={a.read_verified_at} />
                    </Td>
                    <Td>
                      <SyncStatusChip lastSyncAt={a.last_sync_at} />
                    </Td>
                    <Td right>
                      {a.available_cash !== null ? (
                        `₹${Number(a.available_cash).toLocaleString("en-IN")}`
                      ) : (
                        <span className="text-ink-faint">no snapshot</span>
                      )}
                    </Td>
                    <Td right>
                      {a.holdings_count !== null ? (
                        a.holdings_count
                      ) : (
                        <span className="text-ink-faint">—</span>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
            {s && (s.accounts.never_synced.length > 0 || s.accounts.read_unverified.length > 0) && (
              <p className="px-3 py-3 text-xs text-warn/90">
                Partial data: {s.accounts.never_synced.length} account(s) never synced,{" "}
                {s.accounts.read_unverified.length} with unverified read access. Totals above
                exclude what brokers haven&apos;t reported.
              </p>
            )}
          </div>
        </Card>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Kill switch">
          {s ? (
            <div className="flex items-center gap-3">
              <Pill
                value={s.killswitch.global_engaged ? "error" : "connected"}
                label={s.killswitch.global_engaged ? "ENGAGED" : "NORMAL"}
              />
              {s.killswitch.killed_strategies.length > 0 && (
                <span className="text-sm text-warn">
                  {s.killswitch.killed_strategies.length} strategy kill switch(es) engaged
                </span>
              )}
            </div>
          ) : (
            <Skeleton className="h-6 w-32" />
          )}
        </Card>
        <Card title="Recent activity">
          <RecentAuditList events={s?.recent_events ?? []} />
        </Card>
      </div>
    </Shell>
  );
}
