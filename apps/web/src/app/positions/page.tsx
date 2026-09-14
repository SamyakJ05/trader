"use client";

import Shell from "@/components/Shell";
import { Card, EmptyState, PageHeader, Pnl, Skeleton, Td, Th } from "@/components/ui";
import { useApi } from "@/lib/useApi";
import { Position } from "@/lib/types";

export default function PositionsPage() {
  const { data: positions, loading } = useApi<Position[]>(
    "/portfolio/positions?environment=paper",
    5000
  );

  const { data: holdings } = useApi<Position[]>("/portfolio/delivery", 5000);

  return (
    <Shell>
      <PageHeader title="Positions" sub="Paper positions and settled delivery holdings, marked to the simulated feed" />
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
          hint="Place a paper order from the Orders page or start a strategy to build positions."
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
