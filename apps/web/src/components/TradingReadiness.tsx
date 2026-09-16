"use client";

import { Card, labelClass } from "@/components/ui";
import { useApi } from "@/lib/useApi";

type Check = { check: string; ok: boolean; detail: string; fix: string | null };
type Readiness = { ready: boolean; checks: Check[] };

/**
 * Everything that must be true before strategies trade unattended today.
 *
 * The daily routine is a browser login before the open — Breeze sessions die
 * at midnight IST and ICICI publish no way to renew one programmatically.
 * After that login nobody is watching, so "is it actually ready?" needs one
 * answer rather than four pages to check.
 *
 * Every failing check carries the next step. A readiness panel that says "not
 * ready" without saying what to do is a worse version of the dashboard the
 * operator already had.
 */
export function TradingReadiness() {
  const { data } = useApi<Readiness>("/brokers/readiness", 60000);
  if (!data) return null;

  const failing = data.checks.filter((c) => !c.ok);

  return (
    <Card
      title={data.ready ? "Ready to trade" : "Not ready to trade"}
      className={
        data.ready ? "border-gain/40" : failing.length ? "border-warn/40" : ""
      }
    >
      {data.ready ? (
        <p className="text-sm text-gain">
          Session live, gates open, strategies running. Nothing further is
          needed today.
        </p>
      ) : (
        <ul className="space-y-2">
          {failing.map((c) => (
            <li key={c.check} className="text-sm">
              <span className="font-semibold capitalize text-warn">{c.check}</span>
              <span className="text-ink-dim"> — {c.detail}</span>
              {c.fix && <p className="mt-0.5 text-xs text-ink-faint">{c.fix}</p>}
            </li>
          ))}
        </ul>
      )}

      {failing.length > 0 && (
        <p className="mt-3 text-xs text-ink-faint">
          {data.checks.length - failing.length} of {data.checks.length} checks
          pass.
        </p>
      )}

      <details className="mt-3">
        <summary className={`${labelClass} cursor-pointer`}>All checks</summary>
        <ul className="mt-2 space-y-1">
          {data.checks.map((c) => (
            <li key={c.check} className="text-xs">
              <span className={c.ok ? "text-gain" : "text-warn"}>
                {c.ok ? "ok" : "no"}
              </span>{" "}
              <span className="capitalize text-ink-dim">{c.check}</span>
              <span className="text-ink-faint"> — {c.detail}</span>
            </li>
          ))}
        </ul>
      </details>
    </Card>
  );
}
