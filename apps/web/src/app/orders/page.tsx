"use client";

import { useState } from "react";
import Shell from "@/components/Shell";
import { Button, Card, ErrorNote, Pill, Td, Th } from "@/components/ui";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { BrokerAccount, Order } from "@/lib/types";

const TERMINAL = ["FILLED", "CANCELLED", "REJECTED", "REJECTED_RISK", "FAILED"];

export default function OrdersPage() {
  const { data: orders, reload } = useApi<Order[]>("/orders?limit=100", 5000);
  const { data: accounts } = useApi<BrokerAccount[]>("/brokers/accounts");
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    symbol: "RELIANCE",
    side: "BUY",
    order_type: "MARKET",
    quantity: 10,
    price: "",
    account: "",
  });

  const paperAccounts = accounts?.filter((a) => a.environment === "paper") ?? [];
  const accountId = form.account || paperAccounts[0]?.id || "";

  async function placeOrder(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api("/orders", {
        method: "POST",
        body: JSON.stringify({
          broker_account_id: accountId,
          order: {
            symbol: form.symbol.toUpperCase(),
            exchange: "NSE",
            side: form.side,
            order_type: form.order_type,
            product: "MIS",
            quantity: Number(form.quantity),
            price: form.order_type === "LIMIT" ? form.price : null,
          },
        }),
      });
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function cancel(id: string) {
    setError(null);
    try {
      await api(`/orders/${id}/cancel`, { method: "POST" });
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  return (
    <Shell>
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-xl font-bold">Orders</h1>
        <Pill value="paper" />
      </div>
      <ErrorNote message={error} />

      <Card title="Place paper order">
        <form onSubmit={placeOrder} className="flex items-end gap-3">
          <div>
            <label className="mb-1 block text-xs uppercase text-zinc-500">Account</label>
            <select
              className="rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
              value={accountId}
              onChange={(e) => setForm({ ...form, account: e.target.value })}
            >
              {paperAccounts.map((a) => (
                <option key={a.id} value={a.id}>{a.label}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase text-zinc-500">Symbol</label>
            <input
              className="w-32 rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
              value={form.symbol}
              onChange={(e) => setForm({ ...form, symbol: e.target.value })}
              required
            />
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase text-zinc-500">Side</label>
            <select
              className="rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
              value={form.side}
              onChange={(e) => setForm({ ...form, side: e.target.value })}
            >
              <option>BUY</option>
              <option>SELL</option>
            </select>
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase text-zinc-500">Type</label>
            <select
              className="rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
              value={form.order_type}
              onChange={(e) => setForm({ ...form, order_type: e.target.value })}
            >
              <option>MARKET</option>
              <option>LIMIT</option>
            </select>
          </div>
          {form.order_type === "LIMIT" && (
            <div>
              <label className="mb-1 block text-xs uppercase text-zinc-500">Price</label>
              <input
                className="w-24 rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
                value={form.price}
                onChange={(e) => setForm({ ...form, price: e.target.value })}
                required
              />
            </div>
          )}
          <div>
            <label className="mb-1 block text-xs uppercase text-zinc-500">Qty</label>
            <input
              type="number"
              min={1}
              className="w-20 rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
              value={form.quantity}
              onChange={(e) => setForm({ ...form, quantity: Number(e.target.value) })}
            />
          </div>
          <Button type="submit" variant="primary" disabled={!accountId}>Place</Button>
        </form>
      </Card>

      <div className="mt-4">
        <Card title="Order history">
          <table className="w-full">
            <thead>
              <tr className="border-b border-zinc-800">
                <Th>Time</Th><Th>Symbol</Th><Th>Side</Th><Th>Type</Th><Th>Qty</Th>
                <Th>Filled</Th><Th>Avg Px</Th><Th>Status</Th><Th>Source</Th><Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {orders?.map((o) => (
                <tr key={o.id} className="border-b border-zinc-900">
                  <Td className="text-zinc-500">{new Date(o.placed_at).toLocaleTimeString()}</Td>
                  <Td className="font-medium">{o.symbol}</Td>
                  <Td className={o.side === "BUY" ? "text-emerald-400" : "text-red-400"}>{o.side}</Td>
                  <Td>{o.order_type}{o.price ? ` @${o.price}` : ""}</Td>
                  <Td>{o.quantity}</Td>
                  <Td>{o.filled_quantity}</Td>
                  <Td>{o.average_fill_price ? Number(o.average_fill_price).toFixed(2) : "—"}</Td>
                  <Td>
                    <Pill value={o.status} />
                    {o.status_message && (
                      <p className="mt-1 max-w-48 text-xs text-zinc-500">{o.status_message}</p>
                    )}
                  </Td>
                  <Td className="text-zinc-500">{o.strategy_id ? "strategy" : "manual"}</Td>
                  <Td>
                    {!TERMINAL.includes(o.status) && (
                      <Button variant="danger" onClick={() => cancel(o.id)}>Cancel</Button>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
    </Shell>
  );
}
