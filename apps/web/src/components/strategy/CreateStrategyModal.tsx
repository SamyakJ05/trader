"use client";

import { useState } from "react";
import { Button, Modal, inputClass, labelClass } from "@/components/ui";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";
import { BrokerAccount } from "@/lib/types";

/**
 * Create a strategy by hand.
 *
 * The only creation path in the UI was the AI generator, which is disabled
 * until an LLM provider is configured — so a user without one could not
 * create a strategy at all, and the empty state told them to run a seed
 * script.
 *
 * The fields here are the ones the runner actually reads. Execution
 * parameters (order type, product, limit buffer) are included because they
 * are per-strategy decisions the backend now honours, and because their
 * defaults differ per broker: Breeze cannot place a market order or an MIS
 * order at all, so a strategy created against it with the old hardcoded
 * defaults would have failed on every signal.
 */
export function CreateStrategyModal({
  open,
  onClose,
  accounts,
  kinds,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  accounts: BrokerAccount[];
  kinds: string[];
  onCreated: () => void;
}) {
  const { push } = useToast();
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    name: "",
    kind: "sma_crossover",
    account: "",
    symbols: "",
    exchange: "NSE",
    fast: 5,
    slow: 20,
    quantity: 1,
    order_type: "",
    product: "",
    limit_buffer_pct: "",
    expiry: "",
    strike: "",
    right: "",
  });

  const accountId = form.account || accounts[0]?.id || "";
  const selected = accounts.find((a) => a.id === accountId);
  const isBreeze = selected?.broker === "icici_breeze";
  const isDerivative = form.exchange === "NFO";
  const isLive = selected?.environment === "live";

  // Mirrors the backend's own defaults so the form does not imply a choice
  // the runner would override.
  const defaultOrderType = isBreeze ? "LIMIT" : "MARKET";
  const defaultProduct = isBreeze ? "CNC" : "MIS";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const symbols = form.symbols
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    if (!symbols.length) {
      push("error", "Name at least one symbol");
      return;
    }

    // Only send what was explicitly chosen. An empty string here means "use
    // the backend default", and sending one would pin the strategy to a value
    // the user never picked.
    const params: Record<string, unknown> = {
      exchange: form.exchange,
      fast: Number(form.fast),
      slow: Number(form.slow),
      quantity: Number(form.quantity),
    };
    if (form.order_type) params.order_type = form.order_type;
    if (form.product) params.product = form.product;
    if (form.limit_buffer_pct) params.limit_buffer_pct = form.limit_buffer_pct;
    // A symbol alone does not name an F&O contract — Breeze lists 3,350 NIFTY
    // contracts under that one code — so an NFO strategy must carry an expiry
    // or every order it places is refused.
    if (form.expiry) params.expiry = form.expiry;
    if (form.strike) params.strike = form.strike;
    if (form.right) params.right = form.right;

    setBusy(true);
    try {
      await api("/strategies", {
        method: "POST",
        body: JSON.stringify({
          name: form.name,
          kind: form.kind,
          broker_account_id: accountId,
          environment: selected?.environment ?? "paper",
          symbols,
          params,
        }),
      });
      push("success", `Strategy “${form.name}” created as a draft`);
      onCreated();
      onClose();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Could not create strategy");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="New strategy">
      <form onSubmit={submit} className="space-y-4">
        <div>
          <label className={labelClass}>Name</label>
          <input
            className={inputClass}
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="RELIANCE 5/20 crossover"
            required
          />
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelClass}>Kind</label>
            <select
              className={inputClass}
              value={form.kind}
              onChange={(e) => setForm({ ...form, kind: e.target.value })}
            >
              {kinds.map((k) => (
                <option key={k}>{k}</option>
              ))}
            </select>
          </div>
          <div>
            <label className={labelClass}>Account</label>
            <select
              className={inputClass}
              value={accountId}
              onChange={(e) => setForm({ ...form, account: e.target.value })}
            >
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.label} — {a.environment}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div>
          <label className={labelClass}>Symbols</label>
          <input
            className={inputClass}
            value={form.symbols}
            onChange={(e) => setForm({ ...form, symbols: e.target.value })}
            placeholder={isBreeze ? "RELIND, INFTEC" : "RELIANCE, INFY"}
            required
          />
          <p className="mt-1 text-xs text-ink-faint">
            Comma separated, in {selected?.broker ?? "the broker"}&rsquo;s own codes.
            {isBreeze && " Breeze uses its own: RELIANCE is RELIND."} Sync the
            instrument master first, or unknown symbols are rejected.
          </p>
        </div>

        <div className="grid grid-cols-4 gap-3">
          <div>
            <label className={labelClass}>Exchange</label>
            <select
              className={inputClass}
              value={form.exchange}
              onChange={(e) => setForm({ ...form, exchange: e.target.value })}
            >
              <option>NSE</option>
              {/* Breeze's own docs: BSE and MCX are not available on the
                  Breeze API, so offering BSE there is a segment every order
                  would be rejected on. */}
              {!isBreeze && <option>BSE</option>}
              <option>NFO</option>
            </select>
          </div>
          <div>
            <label className={labelClass}>Fast</label>
            <input
              type="number"
              min={1}
              className={`${inputClass} num`}
              value={form.fast}
              onChange={(e) => setForm({ ...form, fast: Number(e.target.value) })}
            />
          </div>
          <div>
            <label className={labelClass}>Slow</label>
            <input
              type="number"
              min={2}
              className={`${inputClass} num`}
              value={form.slow}
              onChange={(e) => setForm({ ...form, slow: Number(e.target.value) })}
            />
          </div>
          <div>
            <label className={labelClass}>Qty</label>
            <input
              type="number"
              min={1}
              className={`${inputClass} num`}
              value={form.quantity}
              onChange={(e) => setForm({ ...form, quantity: Number(e.target.value) })}
            />
          </div>
        </div>

        {isDerivative && (
          <div className="rounded border border-line bg-panel-2 px-3 py-2">
            <p className={labelClass}>Contract</p>
            <div className="grid grid-cols-3 gap-3">
              <div>
                <label className={labelClass}>Expiry</label>
                <input
                  type="date"
                  className={inputClass}
                  value={form.expiry}
                  onChange={(e) => setForm({ ...form, expiry: e.target.value })}
                  required
                />
              </div>
              <div>
                <label className={labelClass}>Right</label>
                <select
                  className={inputClass}
                  value={form.right}
                  onChange={(e) => setForm({ ...form, right: e.target.value })}
                >
                  <option value="">Future</option>
                  <option value="call">Call</option>
                  <option value="put">Put</option>
                </select>
              </div>
              <div>
                <label className={labelClass}>Strike</label>
                <input
                  className={`${inputClass} num`}
                  value={form.strike}
                  onChange={(e) => setForm({ ...form, strike: e.target.value })}
                  placeholder={form.right ? "25000" : "—"}
                  disabled={!form.right}
                  required={!!form.right}
                />
              </div>
            </div>
            <p className="mt-2 text-xs text-ink-faint">
              A symbol alone does not name a contract: the same underlying has
              many expiries and strikes. Leave the right as Future for a
              futures contract, which needs no strike.
            </p>
          </div>
        )}

        <details className="rounded border border-line bg-panel-2 px-3 py-2">
          <summary className="cursor-pointer text-xs uppercase tracking-wider text-ink-faint">
            Execution
          </summary>
          <div className="mt-3 grid grid-cols-3 gap-3">
            <div>
              <label className={labelClass}>Order type</label>
              <select
                className={inputClass}
                value={form.order_type}
                onChange={(e) => setForm({ ...form, order_type: e.target.value })}
              >
                <option value="">Default ({defaultOrderType})</option>
                {/* SL omitted: it needs a trigger price, which a strategy
                    sets per signal rather than as a fixed parameter. Offering
                    it here would pin every order to a type the runner could
                    not price. */}
                {(isBreeze ? ["LIMIT"] : ["MARKET", "LIMIT"]).map((t) => (
                  <option key={t}>{t}</option>
                ))}
              </select>
            </div>
            <div>
              <label className={labelClass}>Product</label>
              <select
                className={inputClass}
                value={form.product}
                onChange={(e) => setForm({ ...form, product: e.target.value })}
              >
                <option value="">Default ({defaultProduct})</option>
                {(isBreeze ? ["CNC", "NRML"] : ["MIS", "CNC", "NRML"]).map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </select>
            </div>
            <div>
              <label className={labelClass}>Limit buffer</label>
              <input
                className={`${inputClass} num`}
                value={form.limit_buffer_pct}
                onChange={(e) =>
                  setForm({ ...form, limit_buffer_pct: e.target.value })
                }
                placeholder="0.003"
              />
            </div>
          </div>
          <p className="mt-2 text-xs text-ink-faint">
            {isBreeze
              ? "Breeze accepts no market orders, so a market signal is placed as a limit through the last price. The buffer is how far through — 0.003 is 0.30%."
              : "Leave blank to use this broker’s defaults. The buffer applies only when an order is placed as a limit."}
          </p>
        </details>

        {isLive && (
          <p className="rounded border border-live/40 bg-live/10 px-3 py-2 text-xs text-live">
            This account is live. The strategy is created as a draft and will
            not trade until you start it, and starting it requires live
            trading to be enabled on the account.
          </p>
        )}

        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={busy || !accountId}>
            {busy ? "Creating…" : "Create draft"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
