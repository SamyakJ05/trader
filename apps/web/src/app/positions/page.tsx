"use client";

import Shell from "@/components/Shell";
import { Card, Pill, Pnl, Td, Th } from "@/components/ui";
import { useApi } from "@/lib/useApi";
import { Position } from "@/lib/types";

export default function PositionsPage() {
  const { data: positions } = useApi<Position[]>("/portfolio/positions?environment=paper", 5000);

  return (
    <Shell>
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-xl font-bold">Positions</h1>
        <Pill value="paper" />
      </div>
      <Card>
        <table className="w-full">
          <thead>
            <tr className="border-b border-zinc-800">
              <Th>Symbol</Th><Th>Exch</Th><Th>Product</Th><Th>Qty</Th>
              <Th>Avg</Th><Th>LTP</Th><Th>Unrealized</Th><Th>Realized</Th>
            </tr>
          </thead>
          <tbody>
            {positions?.length ? (
              positions.map((p) => (
                <tr key={p.id} className="border-b border-zinc-900">
                  <Td className="font-medium">{p.symbol}</Td>
                  <Td>{p.exchange}</Td>
                  <Td>{p.product}</Td>
                  <Td className={p.quantity > 0 ? "text-emerald-400" : p.quantity < 0 ? "text-red-400" : ""}>
                    {p.quantity}
                  </Td>
                  <Td>{Number(p.average_price).toFixed(2)}</Td>
                  <Td>{Number(p.last_price).toFixed(2)}</Td>
                  <Td><Pnl value={p.unrealized_pnl} /></Td>
                  <Td><Pnl value={p.realized_pnl} /></Td>
                </tr>
              ))
            ) : (
              <tr><Td className="text-zinc-500">No positions</Td></tr>
            )}
          </tbody>
        </table>
      </Card>
    </Shell>
  );
}
