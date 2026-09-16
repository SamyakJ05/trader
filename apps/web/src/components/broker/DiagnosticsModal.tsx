"use client";

import { useEffect, useState } from "react";
import { Button, Modal, labelClass } from "@/components/ui";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";
import { BrokerAccount } from "@/lib/types";

type Read = {
  status: "ok" | "error" | "unsupported" | "session_expired";
  detail?: string;
  count?: number;
  sample?: Record<string, string | number | null>[];
  client_id?: string | null;
  name?: string | null;
  available_cash?: string;
  margin_used?: string | null;
};

type Report = {
  broker: string;
  environment: string;
  reads: Record<string, Read>;
  instrument_coverage: { symbols: number; resolved: number; missing: string[] };
};

const TONE: Record<Read["status"], string> = {
  ok: "text-gain",
  error: "text-loss",
  session_expired: "text-warn",
  unsupported: "text-ink-faint",
};

/**
 * Every broker read, side by side, so they can be compared against the
 * broker's own dashboard.
 *
 * The verification playbooks otherwise require an async REPL for exactly
 * this. Read-only: it never stamps read_verified_at, which stays the meaning
 * of the Verify button and the gate on enabling live trading.
 */
export function DiagnosticsModal({
  account,
  onClose,
}: {
  account: BrokerAccount | null;
  onClose: () => void;
}) {
  const { push } = useToast();
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!account) {
      setReport(null);
      return;
    }
    let cancelled = false;
    setBusy(true);
    api<Report>(`/brokers/accounts/${account.id}/diagnostics`, { method: "POST" })
      .then((res) => {
        if (!cancelled) setReport(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          push("error", err instanceof Error ? err.message : "Diagnostics failed");
        }
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [account, push]);

  const coverage = report?.instrument_coverage;

  return (
    <Modal
      open={!!account}
      onClose={onClose}
      title={`Diagnostics — ${account?.label ?? ""}`}
      width="w-[42rem]"
    >
      {busy && <p className="text-sm text-ink-dim">Calling the broker…</p>}

      {report && (
        <div className="space-y-3">
          {Object.entries(report.reads).map(([name, read]) => (
            <div key={name} className="rounded border border-line bg-panel-2 px-3 py-2">
              <div className="flex items-center justify-between">
                <span className="text-sm font-semibold capitalize">{name}</span>
                <span className={`text-xs ${TONE[read.status]}`}>{read.status}</span>
              </div>

              {read.detail && (
                <p className="mt-1 text-xs text-ink-dim">{read.detail}</p>
              )}

              {name === "profile" && read.status === "ok" && (
                <p className="num mt-1 text-xs text-ink-dim">
                  {read.client_id || "no client id reported"}
                  {read.name ? ` · ${read.name}` : ""}
                </p>
              )}

              {name === "funds" && read.status === "ok" && (
                <p className="num mt-1 text-xs text-ink-dim">
                  ₹{read.available_cash}
                  {read.margin_used ? ` · ₹${read.margin_used} used` : ""}
                </p>
              )}

              {read.count !== undefined && (
                <p className="mt-1 text-xs text-ink-dim">
                  {read.count} row{read.count === 1 ? "" : "s"}
                  {read.count > (read.sample?.length ?? 0)
                    ? ` (showing ${read.sample?.length})`
                    : ""}
                </p>
              )}

              {read.sample?.length ? (
                <div className="mt-2 overflow-x-auto">
                  <table className="w-full text-xs">
                    <tbody>
                      {read.sample.map((row, i) => (
                        <tr key={i} className="border-t border-line">
                          {Object.entries(row).map(([key, value]) => (
                            <td key={key} className="num py-1 pr-3 text-ink-dim">
                              <span className="text-ink-faint">{key}</span>{" "}
                              {value === null ? "—" : String(value)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}
            </div>
          ))}

          {coverage && coverage.symbols > 0 && (
            <div
              className={`rounded border px-3 py-2 ${
                coverage.missing.length
                  ? "border-warn/40 bg-warn/10 text-warn"
                  : "border-line bg-panel-2 text-ink-dim"
              }`}
            >
              <p className={labelClass}>Instrument coverage</p>
              <p className="text-xs">
                {coverage.resolved} of {coverage.symbols} strategy symbols resolve
                to a broker token.
                {coverage.missing.length ? (
                  <>
                    {" "}
                    <span className="num">{coverage.missing.join(", ")}</span> have
                    none — the tick stream subscribes by token, so those receive no
                    prices and their strategies will not trade live. Sync
                    instruments, then check the symbols are the broker&rsquo;s own
                    codes.
                  </>
                ) : null}
              </p>
            </div>
          )}

          <p className="text-xs text-ink-faint">
            Read-only. This does not change the account&rsquo;s verification
            state — use Verify for that.
          </p>
        </div>
      )}

      <div className="mt-4 flex justify-end">
        <Button onClick={onClose}>Close</Button>
      </div>
    </Modal>
  );
}
