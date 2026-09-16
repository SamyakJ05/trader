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
    exchange: "NSE",
    product: "MIS",
  });

  // Every account, not only paper. The form was paper-only by construction,
  // so a live account could be connected, verified and enabled and still have
  // no way to place an order through the UI.
  const allAccounts = accounts ?? [];
  const accountId = form.account || allAccounts[0]?.id || "";
  const selected = allAccounts.find((a) => a.id === accountId);
  const isLive = selected?.environment === "live";

  // Breeze accepts neither market orders nor MIS. Offering them would produce
  // a form that always fails at the adapter, which is what the strategy
  // runner used to do before its order shaping was fixed.
  // SL is deliberately absent: it needs a trigger price, which this form does
  // not collect, so offering it would produce an order the backend rejects
  // every time. Stop orders belong in a strategy, which can set one.
  const noMarketOrders = selected?.broker === "icici_breeze";
  const orderTypes = noMarketOrders ? ["LIMIT"] : ["MARKET", "LIMIT"];
  const products = noMarketOrders ? ["CNC", "NRML"] : ["MIS", "CNC", "NRML"];
  const effectiveType = orderTypes.includes(form.order_type)
    ? form.order_type
    : orderTypes[0];
  const effectiveProduct = products.includes(form.product) ? form.product : products[0];
  const needsPrice = effectiveType === "LIMIT";

  async function placeOrder(e: React.FormEvent) {
    e.preventDefault();
    // A live order spends real money and cannot be undone once it fills.
    // Paper and live otherwise look identical in this form, so the one
    // irreversible case asks first and names what it is about to do.
    if (isLive) {
      const summary =
        `${form.side} ${form.quantity} ${form.symbol.toUpperCase()} ` +
        `(${effectiveType}${needsPrice ? ` @ ${form.price}` : ""}, ${effectiveProduct})`;
      if (!window.confirm(`Place a REAL order on ${selected?.label}?\n\n${summary}`)) {
        return;
      }
    }
    try {
      await api("/orders", {
        method: "POST",
        body: JSON.stringify({
          broker_account_id: accountId,
          order: {
            symbol: form.symbol.toUpperCase(),
            exchange: form.exchange,
            side: form.side,
            order_type: effectiveType,
            product: effectiveProduct,
            quantity: Number(form.quantity),
            price: needsPrice ? form.price : null,
          },
        }),
      });
      push(
        "success",
        `${isLive ? "LIVE order" : "Paper order"} placed: ${form.side} ${form.quantity} ${form.symbol.toUpperCase()}`,
      );
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

      <Card title={isLive ? "Place LIVE order" : "Place paper order"}>
        {isLive && (
          <p className="mb-3 rounded border border-live/40 bg-live/10 px-3 py-2 text-xs text-live">
            This account is live. Orders placed here are sent to the broker and
            use real money.
          </p>
        )}
        <form onSubmit={placeOrder} className="flex flex-wrap items-end gap-3">
          <div>
            <label className={labelClass}>Account</label>
            <select
              className={inputClass}
              value={accountId}
              onChange={(e) => setForm({ ...form, account: e.target.value })}
            >
              {allAccounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.label} — {a.environment}
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
              value={effectiveType}
              onChange={(e) => setForm({ ...form, order_type: e.target.value })}
            >
              {orderTypes.map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
          </div>
          <div>
            <label className={labelClass}>Product</label>
            <select
              className={inputClass}
              value={effectiveProduct}
              onChange={(e) => setForm({ ...form, product: e.target.value })}
            >
              {products.map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
          </div>
          <div>
            <label className={labelClass}>Exchange</label>
            <select
              className={inputClass}
              value={form.exchange}
              onChange={(e) => setForm({ ...form, exchange: e.target.value })}
            >
              <option>NSE</option>
              <option>BSE</option>
            </select>
          </div>
          {needsPrice && (
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
