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

  return (
    <Shell>
      <PageHeader title="Positions" sub="Open paper positions, marked to the simulated feed" />
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
                    <Td className="text-ink-dim">{p.product}</Td>
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
    </Shell>
  );
}
