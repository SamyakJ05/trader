"use client";

import { useState } from "react";
import Shell from "@/components/Shell";
import { Button, Card, ErrorNote, Pill, Td, Th } from "@/components/ui";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { KillSwitchStatus, RiskRule } from "@/lib/types";

interface RiskEvent {
  id: string;
  ts: string;
  environment: string;
  decision: string;
  reason: string;
}

export default function RiskPage() {
  const { data: rules, reload: reloadRules } = useApi<RiskRule[]>("/risk/rules");
  const { data: events, reload: reloadEvents } = useApi<RiskEvent[]>("/risk/events?limit=50", 10000);
  const { data: kill, reload: reloadKill } = useApi<KillSwitchStatus>("/system/killswitch", 5000);
  const [error, setError] = useState<string | null>(null);

  async function toggleGlobalKill(engaged: boolean) {
    setError(null);
    try {
      await api("/system/killswitch", {
        method: "POST",
        body: JSON.stringify({ scope: "global", engaged, reason: "manual from risk page" }),
      });
      reloadKill();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function toggleRule(rule: RiskRule) {
    setError(null);
    try {
      await api(`/risk/rules/${rule.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          rule_type: rule.rule_type,
          environment: rule.environment,
          params: rule.params,
          enabled: !rule.enabled,
        }),
      });
      reloadRules();
      reloadEvents();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  return (
    <Shell>
      <h1 className="mb-4 text-xl font-bold">Risk Controls</h1>
      <ErrorNote message={error} />

      <Card title="Global kill switch">
        <div className="flex items-center gap-4">
          <Pill
            value={kill?.global_engaged ? "error" : "connected"}
            label={kill?.global_engaged ? "ENGAGED" : "ARMED / NORMAL"}
          />
          {kill?.global_engaged ? (
            <Button onClick={() => toggleGlobalKill(false)}>Release</Button>
          ) : (
            <Button variant="danger" onClick={() => toggleGlobalKill(true)}>
              ENGAGE — halt all trading
            </Button>
          )}
          <p className="text-sm text-zinc-500">
            Halts every order (paper and live) and kills running strategies.
          </p>
        </div>
      </Card>

      <div className="mt-4">
        <Card title="Risk rules">
          <table className="w-full">
            <thead>
              <tr className="border-b border-zinc-800">
                <Th>Rule</Th><Th>Env</Th><Th>Params</Th><Th>Enabled</Th><Th>{""}</Th>
              </tr>
            </thead>
            <tbody>
              {rules?.map((r) => (
                <tr key={r.id} className="border-b border-zinc-900">
                  <Td className="font-medium">{r.rule_type}</Td>
                  <Td><Pill value={r.environment} /></Td>
                  <Td className="text-zinc-400">{JSON.stringify(r.params)}</Td>
                  <Td>{r.enabled ? "yes" : "no"}</Td>
                  <Td>
                    <Button onClick={() => toggleRule(r)}>{r.enabled ? "Disable" : "Enable"}</Button>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>

      <div className="mt-4">
        <Card title="Recent risk decisions">
          <table className="w-full">
            <thead>
              <tr className="border-b border-zinc-800">
                <Th>Time</Th><Th>Decision</Th><Th>Reason</Th>
              </tr>
            </thead>
            <tbody>
              {events?.map((e) => (
                <tr key={e.id} className="border-b border-zinc-900">
                  <Td className="text-zinc-500">{new Date(e.ts).toLocaleString()}</Td>
                  <Td>
                    <Pill
                      value={e.decision === "ALLOW" ? "connected" : "error"}
                      label={e.decision}
                    />
                  </Td>
                  <Td className="text-zinc-400">{e.reason}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
    </Shell>
  );
}
