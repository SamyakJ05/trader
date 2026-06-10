"use client";

import Shell from "@/components/Shell";
import { Button, Card, EmptyState, PageHeader, Pill, Skeleton, Td, Th } from "@/components/ui";
import { useToast } from "@/components/toast";
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
  const { data: rules, loading, reload: reloadRules } = useApi<RiskRule[]>("/risk/rules");
  const { data: events, reload: reloadEvents } = useApi<RiskEvent[]>("/risk/events?limit=50", 10000);
  const { data: kill, reload: reloadKill } = useApi<KillSwitchStatus>("/system/killswitch", 5000);
  const { push } = useToast();

  async function toggleGlobalKill(engaged: boolean) {
    try {
      await api("/system/killswitch", {
        method: "POST",
        body: JSON.stringify({ scope: "global", engaged, reason: "manual from risk page" }),
      });
      push(engaged ? "error" : "success", engaged ? "Kill switch ENGAGED" : "Kill switch released");
      reloadKill();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Failed");
    }
  }

  async function toggleRule(rule: RiskRule) {
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
      push("error", err instanceof Error ? err.message : "Failed");
    }
  }

  return (
    <Shell>
      <PageHeader title="Risk Controls" sub="Pre-trade checks, kill switches, and recent decisions" />

      <Card title="Global kill switch">
        <div className="flex flex-wrap items-center gap-4">
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
          <p className="text-sm text-ink-faint">
            Halts every order (paper and live) and kills running strategies.
          </p>
        </div>
      </Card>

      <div className="mt-4">
        {loading && !rules ? (
          <Skeleton className="h-40" />
        ) : (
          <Card title="Risk rules" pad={false}>
            <div className="overflow-x-auto p-2">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-line">
                    <Th>Rule</Th>
                    <Th>Env</Th>
                    <Th>Params</Th>
                    <Th>Enabled</Th>
                    <Th>{""}</Th>
                  </tr>
                </thead>
                <tbody>
                  {rules?.map((r) => (
                    <tr key={r.id} className="border-b border-line/50 last:border-0">
                      <Td className="font-medium">{r.rule_type}</Td>
                      <Td>
                        <Pill value={r.environment} />
                      </Td>
                      <Td className="num text-xs text-ink-dim">{JSON.stringify(r.params)}</Td>
                      <Td>
                        <span className={r.enabled ? "text-gain" : "text-ink-faint"}>
                          {r.enabled ? "yes" : "no"}
                        </span>
                      </Td>
                      <Td>
                        <Button size="sm" onClick={() => toggleRule(r)}>
                          {r.enabled ? "Disable" : "Enable"}
                        </Button>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </div>

      <div className="mt-4">
        {events?.length ? (
          <Card title="Recent risk decisions" pad={false}>
            <div className="overflow-x-auto p-2">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-line">
                    <Th>Time</Th>
                    <Th>Decision</Th>
                    <Th>Reason</Th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((e) => (
                    <tr key={e.id} className="border-b border-line/50 last:border-0">
                      <Td className="num whitespace-nowrap text-ink-faint">
                        {new Date(e.ts).toLocaleString()}
                      </Td>
                      <Td>
                        <Pill value={e.decision === "ALLOW" ? "connected" : "error"} label={e.decision} />
                      </Td>
                      <Td className="text-ink-dim">{e.reason}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        ) : (
          <EmptyState title="No risk decisions yet" hint="Decisions appear here as orders flow through the risk engine." />
        )}
      </div>
    </Shell>
  );
}
