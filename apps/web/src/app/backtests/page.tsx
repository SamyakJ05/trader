"use client";

import { FormEvent, useState } from "react";
import Shell from "@/components/Shell";
import { Button, Card, PageHeader } from "@/components/ui";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";

type Config = { symbol: string; interval: string; source: string; product: string };
type Summary = { id: string; created_at: string; config: Config; total_return: string };
type Run = { id: string; config: Config; results: {
  total_return: string; max_drawdown: string; win_rate: number; trade_count: number;
  total_charges: string; final_equity: string; open_quantity: number; rejected_signals: number;
  candle_count: number; source: string; limitations: string[];
  equity_curve: { ts: string; equity: string }[];
} };
const percent = (v: string | number) => `${(Number(v) * 100).toFixed(2)}%`;
const rupees = (v: string) => Number(v).toLocaleString("en-IN", { style: "currency", currency: "INR" });

export default function BacktestsPage() {
  const { data: runs, reload } = useApi<Summary[]>("/backtests");
  const [run, setRun] = useState<Run | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true); setError("");
    try {
      const body = Object.fromEntries(data.entries());
      const result = await api<Run>("/backtests", { method: "POST", body: JSON.stringify({
        ...body, kind: "sma_crossover", exchange: "NSE",
        symbol: String(body.symbol).trim().toUpperCase(),
        start: `${body.start}T00:00:00+05:30`, end: `${body.end}T00:00:00+05:30`,
        fast: Number(body.fast), slow: Number(body.slow), quantity: Number(body.quantity),
      }) });
      setRun(result); reload();
    } catch (err) { setError(err instanceof Error ? err.message : "Backtest failed"); }
    finally { setBusy(false); }
  }

  async function open(id: string) {
    setBusy(true); setError("");
    try { setRun(await api<Run>(`/backtests/${id}`)); }
    catch (err) { setError(err instanceof Error ? err.message : "Could not load run"); }
    finally { setBusy(false); }
  }

  const curve = run?.results.equity_curve ?? [];
  // Bound rendering work for long runs while always retaining the last point.
  const sampled = curve.filter((_, i) => i % Math.max(1, Math.ceil(curve.length / 500)) === 0 || i === curve.length - 1);
  const values = sampled.map(p => Number(p.equity));
  const low = values.length ? Math.min(...values) : 0;
  const high = values.length ? Math.max(...values) : 1;
  const points = values.map((value, i) => `${20 + i / Math.max(1, values.length - 1) * 760},${180 - (value-low) / (high-low || 1) * 160}`).join(" ");

  return <Shell>
    <PageHeader title="Backtests" sub="Replay SMA crossover on stored NSE candles, with next-open fills and trading charges" />
    <Card>
      <p className="mb-4 text-sm text-ink-dim">Import historical candles before running a replay. Simulator and Yahoo data stay separate. AI strategy replay is unavailable.</p>
      <form onSubmit={submit} className="grid gap-4 sm:grid-cols-3">
        <label>Symbol<input name="symbol" defaultValue="RELIANCE" required className="mt-1 block w-full rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm focus:border-accent focus:outline-none" /></label>
        <label>Source<select name="source" className="mt-1 block w-full rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm focus:border-accent focus:outline-none"><option value="yfinance_unadjusted">Yahoo (unadjusted)</option><option value="simulator">Simulator</option></select></label>
        <label>Interval<select name="interval" className="mt-1 block w-full rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm focus:border-accent focus:outline-none"><option value="1d">Daily</option><option value="5m">5 minutes</option><option value="1m">1 minute</option></select></label>
        <label>Start date (IST)<input name="start" type="date" required className="mt-1 block w-full rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm focus:border-accent focus:outline-none" /></label>
        <label>End date (exclusive, IST)<input name="end" type="date" required className="mt-1 block w-full rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm focus:border-accent focus:outline-none" /></label>
        <label>Product<select name="product" className="mt-1 block w-full rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm focus:border-accent focus:outline-none"><option value="MIS">MIS</option><option value="CNC">CNC (T+1)</option></select></label>
        <label>Initial cash (₹)<input name="initial_cash" type="number" min="1" max="1000000000" defaultValue="1000000" required className="mt-1 block w-full rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm focus:border-accent focus:outline-none" /></label>
        <label>Fast SMA<input name="fast" type="number" min="1" max="1000" defaultValue="5" required className="mt-1 block w-full rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm focus:border-accent focus:outline-none" /></label>
        <label>Slow SMA<input name="slow" type="number" min="2" max="2000" defaultValue="20" required className="mt-1 block w-full rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm focus:border-accent focus:outline-none" /></label>
        <label>Quantity<input name="quantity" type="number" min="1" max="100000" defaultValue="1" required className="mt-1 block w-full rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm focus:border-accent focus:outline-none" /></label>
        <div className="self-end"><Button type="submit" variant="primary" disabled={busy}>{busy ? "Working…" : "Run backtest"}</Button></div>
      </form>
      {error && <p role="alert" className="mt-4 text-red-400">{error}</p>}
    </Card>
    {run && <div className="mt-5 space-y-4">
      <Card>
        <h2 className="mb-3 text-lg font-semibold">{run.config.symbol} · {run.config.product} · {run.config.interval}</h2>
        <p className="mb-4 text-sm text-ink-dim">{run.results.candle_count.toLocaleString()} candles · {run.results.source}</p>
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-3">
          {[["Total return", percent(run.results.total_return)], ["Max drawdown", percent(run.results.max_drawdown)],
            ["Win rate (closed trades)", percent(run.results.win_rate)], ["Closed trades", run.results.trade_count],
            ["Charges paid", rupees(run.results.total_charges)], ["Final equity", rupees(run.results.final_equity)]].map(([label, value]) =>
            <div key={label}><dt className="text-sm text-ink-dim">{label}</dt><dd className="text-xl font-semibold">{value}</dd></div>)}
        </dl>
        <svg viewBox="0 0 800 200" className="mt-5 w-full" role="img" aria-label={`Equity curve, from ${curve[0]?.equity} to ${curve.at(-1)?.equity} rupees`}>
          <polyline points={points} fill="none" stroke="currentColor" strokeWidth="2" className="text-accent" />
        </svg>
        <div className="flex justify-between text-xs text-ink-dim"><span>{curve[0]?.ts.slice(0, 10)}</span><span>{curve.at(-1)?.ts.slice(0, 10)}</span></div>
        <p className="mt-3 text-sm">Open quantity: {run.results.open_quantity} · Rejected signals: {run.results.rejected_signals}</p>
      </Card>
      <Card><h2 className="mb-2 font-semibold">Model limitations</h2><ul className="list-disc space-y-1 pl-5 text-sm text-ink-dim">{run.results.limitations.map(text => <li key={text}>{text}</li>)}</ul></Card>
    </div>}
    <div className="mt-5"><Card><h2 className="mb-3 font-semibold">Saved runs</h2>
      {!runs?.length ? <p className="text-sm text-ink-dim">No saved runs yet.</p> : <ul className="space-y-2">{runs.map(saved => <li key={saved.id}>
        <button disabled={busy} onClick={() => open(saved.id)} className="text-sm text-accent underline">{saved.config.symbol} · {saved.config.interval} · {percent(saved.total_return)} · {new Date(saved.created_at).toLocaleString()}</button>
      </li>)}</ul>}
    </Card></div>
  </Shell>;
}
