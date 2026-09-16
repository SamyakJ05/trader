"use client";

import { useEffect, useRef, useState } from "react";
import { Button, Card, inputClass, labelClass } from "@/components/ui";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";

type Suspect = {
  date: string;
  previous_close: string;
  open: string;
  move_pct: string;
  likely_split: string | null;
};

type Report = {
  status: string;
  imported?: { symbol: string; candles: number; split_suspects?: Suspect[] }[];
  failed?: { symbol: string; error: string }[];
  error?: string;
};

/**
 * Import market history without a shell.
 *
 * A backtest replays stored candles and cannot invent bars it does not have,
 * so until this runs for a symbol the strategy trading it cannot be tested at
 * all. Importing was previously CLI-only, which made the first backtest
 * anyone tried fail on missing data with no way to fix it from here.
 *
 * The import runs as a worker job because the download is slow enough to time
 * out behind the proxy, so this queues it and polls.
 */
export function ImportHistoryPanel({ onImported }: { onImported?: () => void }) {
  const { push } = useToast();
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<Report | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const today = new Date().toISOString().slice(0, 10);
  const lastYear = new Date(Date.now() - 365 * 864e5).toISOString().slice(0, 10);
  const [form, setForm] = useState({
    symbols: "",
    interval: "1d",
    start: lastYear,
    end: today,
  });

  // A queued job outlives this component if the user navigates away; without
  // this the poll keeps firing against a dead tree.
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  async function poll(jobId: string, attempt = 0) {
    try {
      const res = await api<Report>(`/backtests/history/${jobId}`);
      if (res.status === "complete" || res.status === "failed") {
        setReport(res);
        setBusy(false);
        if (res.status === "complete") {
          const ok = res.imported?.length ?? 0;
          const bad = res.failed?.length ?? 0;
          push(
            bad ? "info" : "success",
            bad ? `${ok} imported, ${bad} failed` : `${ok} symbol(s) imported`,
          );
          onImported?.();
        } else {
          push("error", res.error ?? "Import failed");
        }
        return;
      }
      // Yahoo is slow and a multi-symbol job can take minutes. Give up after
      // ~5 minutes rather than polling forever: the job may still finish, and
      // the message says so instead of implying it failed.
      if (attempt > 100) {
        setBusy(false);
        push("info", "Still running — the job continues in the worker; check back shortly.");
        return;
      }
      timer.current = setTimeout(() => poll(jobId, attempt + 1), 3000);
    } catch (err) {
      setBusy(false);
      push("error", err instanceof Error ? err.message : "Could not read job status");
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const symbols = form.symbols
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    if (!symbols.length) {
      push("error", "Name at least one NSE symbol");
      return;
    }
    setBusy(true);
    setReport(null);
    try {
      const res = await api<{ job_id: string }>("/backtests/history", {
        method: "POST",
        body: JSON.stringify({ ...form, symbols }),
      });
      push("info", `Import queued for ${symbols.join(", ")}`);
      poll(res.job_id);
    } catch (err) {
      setBusy(false);
      push("error", err instanceof Error ? err.message : "Could not queue the import");
    }
  }

  return (
    <Card title="Import history">
      <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
        <div className="min-w-[16rem] flex-1">
          <label className={labelClass}>NSE symbols</label>
          <input
            className={inputClass}
            value={form.symbols}
            onChange={(e) => setForm({ ...form, symbols: e.target.value })}
            placeholder="RELIANCE, INFY, TCS"
            required
          />
        </div>
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
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? "Importing…" : "Import"}
        </Button>
      </form>

      <p className="mt-2 text-xs text-ink-faint">
        NSE tickers, not broker codes — a Breeze strategy naming RELIND is
        matched to RELIANCE history through the instrument master. Intraday
        history is retention-limited at the source, so a long 1m range returns
        nothing however it is asked for.
      </p>

      {report?.imported?.length ? (
        <div className="mt-3 space-y-2">
          {report.imported.map((r) => (
            <div key={r.symbol} className="text-sm">
              <span className="num font-semibold">{r.symbol}</span>{" "}
              <span className="text-ink-dim">{r.candles} candles</span>
              {r.split_suspects?.map((s) => (
                <p
                  key={s.date}
                  className="mt-1 rounded border border-warn/40 bg-warn/10 px-2 py-1 text-xs text-warn"
                >
                  {s.date}: {s.previous_close} → {s.open} ({s.move_pct}%)
                  {s.likely_split ? ` — looks like a ${s.likely_split} split` : ""}.
                  Unadjusted data does not correct corporate actions, so a
                  backtest spanning this date reads it as a price move.
                </p>
              ))}
            </div>
          ))}
        </div>
      ) : null}

      {report?.failed?.length ? (
        <div className="mt-3 space-y-1">
          {report.failed.map((f) => (
            <p key={f.symbol} className="text-xs text-loss">
              <span className="num font-semibold">{f.symbol}</span>: {f.error}
            </p>
          ))}
        </div>
      ) : null}
    </Card>
  );
}
