"use client";

import Shell from "@/components/Shell";
import { Card, EmptyState, PageHeader, Pnl, Skeleton, Td, Th } from "@/components/ui";
import { useApi } from "@/lib/useApi";
import { BrokerAccount, Position } from "@/lib/types";

interface BrokerHolding {
  symbol: string;
  exchange: string;
  /** Sellable today — pledged and blocked stock excluded. */
  quantity: number;
  total_quantity?: number | null;
  /** null when the broker reports no cost basis (ICICI Breeze does not). */
  average_price?: string | null;
}

interface BrokerHoldingsResponse {
  holdings: BrokerHolding[];
  as_of: string | null;
  note?: string;
}

/** One broker's holdings, fetched only once its account id is known — a
 * hooks-in-a-loop issue otherwise, since the account list itself is data. */
function BrokerHoldingsCard({ account }: { account: BrokerAccount }) {
  const { data, loading } = useApi<BrokerHoldingsResponse>(
    `/portfolio/holdings?account_id=${account.id}`,
    15000
  );

  return (
    <Card title={`${account.label} — real holdings`}>
      {loading && !data ? (
        <Skeleton className="h-24" />
      ) : data?.holdings.length ? (
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr>
                <Th>Symbol</Th>
                <Th>Exchange</Th>
                <Th right>Sellable</Th>
                <Th right>Total</Th>
                <Th right>Average</Th>
              </tr>
            </thead>
            <tbody>
              {data.holdings.map((h) => (
                <tr key={`${h.exchange}:${h.symbol}`} className="border-t border-line">
                  <Td className="font-medium">{h.symbol}</Td>
                  <Td className="text-ink-dim">{h.exchange}</Td>
                  <Td right>{h.quantity}</Td>
                  {/* Differs from sellable when stock is pledged or blocked. */}
                  <Td right className="text-ink-dim">
                    {h.total_quantity ?? h.quantity}
                  </Td>
                  {/* Breeze reports no cost basis at all. Number(null) would
                      render 0.00, which reads as "free" rather than
                      "unknown" — the exact lie the backend stopped telling. */}
                  <Td right>
                    {h.average_price == null ? (
                      <span className="text-ink-faint">—</span>
                    ) : (
                      Number(h.average_price).toFixed(2)
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
          {/* No LTP or unrealized P&L column: this instance has no live quote
              feed wired up for broker accounts yet (that's the tick-stream
              stage of broker verification, separate from read-path
              verification), so nothing here is fabricated to fill the gap. */}
          <p className="mt-3 text-xs text-ink-faint">
            As of broker sync{data.as_of ? ` at ${new Date(data.as_of).toLocaleString()}` : ""}.
            Sellable excludes pledged and blocked stock. A dash means the broker reports
            no cost basis — ICICI Breeze does not send one. No live quote feed for this
            account yet, so no LTP or unrealized P&L either.
          </p>
        </div>
      ) : (
        <p className="text-sm text-ink-dim">
          {data?.note ?? "No holdings, or the account has not been synced yet."}
        </p>
      )}
    </Card>
  );
}

export default function PositionsPage() {
  const { data: accounts } = useApi<BrokerAccount[]>("/brokers/accounts", 15000);

  // Follow the accounts rather than assuming paper. This was pinned to
  // environment=paper, so a live position could never appear here however
  // many the broker reported — the table read "No open positions" while the
  // account held some.
  const hasLive = (accounts ?? []).some((a) => a.environment === "live");
  const environment = hasLive ? "live" : "paper";

  const { data: positions, loading } = useApi<Position[]>(
    `/portfolio/positions?environment=${environment}`,
    5000
  );

  const { data: holdings } = useApi<Position[]>("/portfolio/delivery", 5000);
  // Paper is its own settled-holdings section below; this is specifically
  // for a real broker account's own portfolio, pulled from its last sync.
  const brokerAccounts = accounts?.filter((a) => a.broker !== "paper") ?? [];

  return (
    <Shell>
      <PageHeader
        title="Positions"
        sub={
          hasLive
            ? "Open positions from your broker, and shares held in demat"
            : "Paper positions and settled delivery holdings, marked to the simulated feed"
        }
      />
      {brokerAccounts.length > 0 && (
        <div className="mb-5 space-y-4">
          {brokerAccounts.map((a) => (
            <BrokerHoldingsCard key={a.id} account={a} />
          ))}
        </div>
      )}
      {loading && !positions ? (
        <Skeleton className="h-48" />
      ) : positions?.length ? (
        <Card pad={false}>
          <div className="overflow-x-auto p-2">
            <table className="w-full">
              <thead>
                <tr className="border-b border-line">
                  <Th>Symbol</Th>
                  <Th>Exch</Th>
                  <Th>Product</Th>
                  <Th right>Qty</Th>
                  <Th right>Avg</Th>
                  <Th right>LTP</Th>
                  <Th right>Unrealized</Th>
                  <Th right>Realized</Th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.id} className="border-b border-line/50 last:border-0">
                    <Td className="font-medium">{p.symbol}</Td>
                    <Td className="text-ink-dim">{p.exchange}</Td>
                    <Td className="text-ink-dim">{p.product === "CNC" && p.quantity > 0 ? "CNC · pending T+1" : p.product}</Td>
                    <Td right className={p.quantity > 0 ? "text-gain" : p.quantity < 0 ? "text-loss" : ""}>
                      {p.quantity}
                    </Td>
                    <Td right>{Number(p.average_price).toFixed(2)}</Td>
                    <Td right>{Number(p.last_price).toFixed(2)}</Td>
                    <Td right>
                      <Pnl value={p.unrealized_pnl} />
                    </Td>
                    <Td right>
                      <Pnl value={p.realized_pnl} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : (
        <EmptyState
          title="No open positions"
          hint={
            hasLive
              ? "Positions are intraday and F&O exposure. Shares you own outright are holdings — shown above, from your broker — and an empty table here is normal when you hold only delivery stock."
              : "Place an order from the Orders page or start a strategy to build positions."
          }
        />
      )}
      <div className="mt-5">
        <Card title="Settled delivery holdings">
          {holdings?.length ? <div className="overflow-x-auto"><table className="w-full">
            <thead><tr><Th>Symbol</Th><Th>Exchange</Th><Th right>Settled qty</Th><Th right>Average</Th><Th right>LTP</Th><Th right>Unrealized</Th></tr></thead>
            <tbody>{holdings.map(h => <tr key={h.id} className="border-t border-line">
              <Td>{h.symbol}</Td><Td>{h.exchange}</Td><Td right>{h.quantity}</Td>
              <Td right>{Number(h.average_price).toFixed(2)}</Td><Td right>{Number(h.last_price).toFixed(2)}</Td>
              <Td right><Pnl value={h.unrealized_pnl} /></Td>
            </tr>)}</tbody>
          </table></div> : <p className="text-sm text-ink-dim">No settled holdings. CNC buys remain pending until the next trading session.</p>}
        </Card>
      </div>
    </Shell>
  );
}
