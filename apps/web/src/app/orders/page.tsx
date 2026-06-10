"use client";

import { useState } from "react";
import Shell from "@/components/Shell";
import {
  Button,
  Card,
  EmptyState,
  PageHeader,
  Pill,
  Skeleton,
  Td,
  Th,
  inputClass,
  labelClass,
} from "@/components/ui";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { BrokerAccount, Order } from "@/lib/types";

const TERMINAL = ["FILLED", "CANCELLED", "REJECTED", "REJECTED_RISK", "FAILED"];

export default function OrdersPage() {
  const { data: orders, loading, reload } = useApi<Order[]>("/orders?limit=100", 5000);
  const { data: accounts } = useApi<BrokerAccount[]>("/brokers/accounts");
  const { push } = useToast();
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
      push("success", `Order placed: ${form.side} ${form.quantity} ${form.symbol.toUpperCase()}`);
      reload();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Order failed");
    }
  }

  async function cancel(id: string) {
    try {
      await api(`/orders/${id}/cancel`, { method: "POST" });
      push("info", "Cancel requested");
      reload();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Cancel failed");
    }
  }

  return (
    <Shell>
      <PageHeader title="Orders" sub="Manual paper orders and full order history" />

      <Card title="Place paper order">
        <form onSubmit={placeOrder} className="flex flex-wrap items-end gap-3">
          <div>
            <label className={labelClass}>Account</label>
            <select
              className={inputClass}
              value={accountId}
              onChange={(e) => setForm({ ...form, account: e.target.value })}
            >
              {paperAccounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className={labelClass}>Symbol</label>
            <input
              className={`${inputClass} w-32`}
              value={form.symbol}
              onChange={(e) => setForm({ ...form, symbol: e.target.value })}
              required
            />
          </div>
          <div>
            <label className={labelClass}>Side</label>
            <select
              className={inputClass}
              value={form.side}
              onChange={(e) => setForm({ ...form, side: e.target.value })}
            >
              <option>BUY</option>
              <option>SELL</option>
            </select>
          </div>
          <div>
            <label className={labelClass}>Type</label>
            <select
              className={inputClass}
              value={form.order_type}
              onChange={(e) => setForm({ ...form, order_type: e.target.value })}
            >
              <option>MARKET</option>
              <option>LIMIT</option>
            </select>
          </div>
          {form.order_type === "LIMIT" && (
            <div>
              <label className={labelClass}>Price</label>
              <input
                className={`${inputClass} w-24 num`}
                value={form.price}
                onChange={(e) => setForm({ ...form, price: e.target.value })}
                required
              />
            </div>
          )}
          <div>
            <label className={labelClass}>Qty</label>
            <input
              type="number"
              min={1}
              className={`${inputClass} w-20 num`}
              value={form.quantity}
              onChange={(e) => setForm({ ...form, quantity: Number(e.target.value) })}
            />
          </div>
          <Button type="submit" variant="primary" disabled={!accountId}>
            Place
          </Button>
        </form>
      </Card>

      <div className="mt-4">
        {loading && !orders ? (
          <Skeleton className="h-64" />
        ) : orders?.length ? (
          <Card title="Order history" pad={false}>
            <div className="overflow-x-auto p-2">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-line">
                    <Th>Time</Th>
                    <Th>Symbol</Th>
                    <Th>Side</Th>
                    <Th>Type</Th>
                    <Th right>Qty</Th>
                    <Th right>Filled</Th>
                    <Th right>Avg Px</Th>
                    <Th>Status</Th>
                    <Th>Source</Th>
                    <Th>{""}</Th>
                  </tr>
                </thead>
                <tbody>
                  {orders.map((o) => (
                    <tr key={o.id} className="border-b border-line/50 last:border-0">
                      <Td className="num whitespace-nowrap text-ink-faint">
                        {new Date(o.placed_at).toLocaleTimeString()}
                      </Td>
                      <Td className="font-medium">{o.symbol}</Td>
                      <Td>
                        <Pill value={o.side} />
                      </Td>
                      <Td className="text-ink-dim">
                        {o.order_type}
                        {o.price ? ` @${o.price}` : ""}
                      </Td>
                      <Td right>{o.quantity}</Td>
                      <Td right>{o.filled_quantity}</Td>
                      <Td right>
                        {o.average_fill_price ? Number(o.average_fill_price).toFixed(2) : "—"}
                      </Td>
                      <Td>
                        <Pill value={o.status} />
                        {o.status_message && (
                          <p className="mt-1 max-w-48 text-xs text-ink-faint">{o.status_message}</p>
                        )}
                      </Td>
                      <Td className="text-ink-faint">{o.strategy_id ? "strategy" : "manual"}</Td>
                      <Td>
                        {!TERMINAL.includes(o.status) && (
                          <Button size="sm" variant="danger" onClick={() => cancel(o.id)}>
                            Cancel
                          </Button>
                        )}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        ) : (
          <EmptyState
            title="No orders yet"
            hint="Use the form above to place your first paper order."
          />
        )}
      </div>
    </Shell>
  );
}
