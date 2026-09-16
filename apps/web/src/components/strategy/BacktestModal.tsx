"use client";

import { useState } from "react";
import { Button, Modal, inputClass, labelClass } from "@/components/ui";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";
import { Strategy } from "@/lib/types";

/**
 * Replay a saved strategy over stored history.
 *
 * Backtesting previously meant retyping an SMA configuration by hand, so the
 * strategy you actually intended to trade was never the thing tested. This
 * runs the strategy itself — its symbols, params and broker — so the result
 * describes it.
 */
export function BacktestModal({
  strategy,
  onClose,
}: {
  strategy: Strategy | null;
  onClose: () => void;
}) {
  const { push } = useToast();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);

  // A year of daily bars: enough for a 20-period warmup with room to trade.
  const today = new Date().toISOString().slice(0, 10);
  const lastYear = new Date(Date.now() - 365 * 864e5).toISOString().slice(0, 10);
  const [form, setForm] = useState({
    start: lastYear,
    end: today,
    interval: "1d",
    symbol: "",
    initial_cash: "1000000",
  });

  const isAi = strategy?.kind === "ai_agent";

  async function run(e: React.FormEvent) {
    e.preventDefault();
    if (!strategy) return;
    setBusy(true);
    setResult(null);
    try {
      const res = await api<Record<string, unknown>>("/backtests/strategy", {
        method: "POST",
        body: JSON.stringify({
          strategy_id: strategy.id,
          start: `${form.start}T00:00:00+05:30`,
          end: `${form.end}T23:59:59+05:30`,
          interval: form.interval,
          initial_cash: form.initial_cash,
          symbol: form.symbol || null,
        }),
      });
      setResult(res);
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Backtest failed");
    } finally {
      setBusy(false);
    }
  }

  const results = (result?.results ?? {}) as Record<string, string | number>;

  return (
    <Modal open={!!strategy} onClose={onClose} title={`Backtest — ${strategy?.name ?? ""}`}>
      {isAi ? (
        <p className="rounded border border-line bg-panel-2 px-3 py-2 text-sm text-ink-dim">
          An AI strategy decides with an LLM using context that no longer
          exists, and its past decisions were never recorded — so replaying it
          would mean inventing them. Run it on paper to build a real track
          record instead.
        </p>
      ) : (
        <form onSubmit={run} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={labelClass}>From</label>
              <input
                type="date"
                className={inputClass}
                value={form.start}
                onChange={(e) => setForm({ ...form, start: e.target.value })}
                required
              />
            </div>
            <div>
              <label className={labelClass}>To</label>
              <input
                type="date"
                className={inputClass}
                value={form.end}
                onChange={(e) => setForm({ ...form, end: e.target.value })}
                required
              />
            </div>
          </div>

          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className={labelClass}>Interval</label>
              <select
                className={inputClass}
                value={form.interval}
                onChange={(e) => setForm({ ...form, interval: e.target.value })}
              >
                <option value="1d">1d</option>
                <option value="5m">5m</option>
                <option value="1m">1m</option>
              </select>
            </div>
            <div>
              <label className={labelClass}>Symbol</label>
              <select
                className={inputClass}
                value={form.symbol}
                onChange={(e) => setForm({ ...form, symbol: e.target.value })}
              >
                {(strategy?.symbols ?? []).map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className={labelClass}>Capital</label>
              <input
                className={`${inputClass} num`}
                value={form.initial_cash}
                onChange={(e) => setForm({ ...form, initial_cash: e.target.value })}
              />
            </div>
          </div>

          <p className="text-xs text-ink-faint">
            Replays on stored candles. Import history first if the range has
            none — a backtest cannot invent bars it does not have.
          </p>

          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              Close
            </Button>
            <Button type="submit" variant="primary" disabled={busy}>
              {busy ? "Running…" : "Run backtest"}
            </Button>
          </div>
        </form>
      )}

      {result && (
        <div className="mt-4 rounded border border-line bg-panel-2 p-3">
          <div className="grid grid-cols-3 gap-3 text-sm">
            {[
              ["Return", results.total_return],
              ["Sharpe", results.sharpe],
              ["Max drawdown", results.max_drawdown],
              ["Trades", results.trades],
              ["Win rate", results.win_rate],
              ["Charges", results.charges_total],
            ].map(([label, value]) => (
              <div key={String(label)}>
                <div className={labelClass}>{label}</div>
                <div className="num">{value === undefined ? "—" : String(value)}</div>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs text-ink-faint">
            {String(results.candle_count ?? "?")} bars from{" "}
            {String(result.config && (result.config as Record<string, unknown>).history_symbol)}.
            Costs are modelled, not guaranteed; see the engine&rsquo;s stated
            limitations before trusting a number here.
          </p>
        </div>
      )}
    </Modal>
  );
}
