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
} from "@/components/ui";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { KillSwitchStatus, RiskRule, User } from "@/lib/types";

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
  const { data: me } = useApi<User>("/auth/me");
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

  // The limit's value, edited as a single number. Raw JSON would let a typo
  // in a key name silently remove a limit rather than change it — on rules
  // that gate real money.
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  async function saveLimit(rule: RiskRule) {
    if (!rule.editable_field) return;
    const value = Number(draft);
    if (!Number.isFinite(value) || value <= 0) {
      push("error", "Enter a number greater than zero");
      return;
    }
    try {
      await api(`/risk/rules/${rule.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          rule_type: rule.rule_type,
          environment: rule.environment,
          params: { ...rule.params, [rule.editable_field]: value },
          enabled: rule.enabled,
        }),
      });
      push("success", "Limit updated");
      setEditing(null);
      reloadRules();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Could not update the limit");
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
          {me?.is_admin ? (
            kill?.global_engaged ? (
              <Button onClick={() => toggleGlobalKill(false)}>Release</Button>
            ) : (
              <Button variant="danger" onClick={() => toggleGlobalKill(true)}>
                ENGAGE — halt all trading
              </Button>
            )
          ) : null}
          <p className="text-sm text-ink-faint">
            {me?.is_admin
              ? "Halts every order (paper and live) and kills running strategies, for every user on this instance."
              : "Operator-controlled: halts trading for every user on this instance. Use a per-strategy kill switch on the Strategies page to stop your own strategies."}
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
                      {/* The description in words, with the raw params
                          underneath: "MAX_TOTAL_EXPOSURE {"max_exposure":
                          100000}" is not a sentence anyone reads under
                          pressure, but the exact values still matter when
                          editing. */}
                      <Td className="text-xs">
                        <span className="text-ink-dim">{r.description || "—"}</span>
                        <span className="num ml-2 text-ink-faint">
                          {JSON.stringify(r.params)}
                        </span>
                      </Td>
                      <Td>
                        <span className={r.enabled ? "text-gain" : "text-ink-faint"}>
                          {r.enabled ? "yes" : "no"}
                        </span>
                      </Td>
                      <Td>
                        <div className="flex items-center gap-2">
                          {editing === r.id ? (
                            <>
                              <input
                                autoFocus
                                className={`${inputClass} num w-28`}
                                value={draft}
                                onChange={(e) => setDraft(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === "Enter") saveLimit(r);
                                  if (e.key === "Escape") setEditing(null);
                                }}
                              />
                              <Button size="sm" variant="primary" onClick={() => saveLimit(r)}>
                                Save
                              </Button>
                              <Button size="sm" variant="ghost" onClick={() => setEditing(null)}>
                                Cancel
                              </Button>
                            </>
                          ) : (
                            <>
                              {r.editable_field && (
                                <Button
                                  size="sm"
                                  onClick={() => {
                                    setEditing(r.id);
                                    setDraft(String(r.params[r.editable_field!] ?? ""));
                                  }}
                                >
                                  Edit limit
                                </Button>
                              )}
                              <Button size="sm" onClick={() => toggleRule(r)}>
                                {r.enabled ? "Disable" : "Enable"}
                              </Button>
                            </>
                          )}
                        </div>
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
