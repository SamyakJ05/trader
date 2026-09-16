"use client";

import { useState } from "react";
import Shell from "@/components/Shell";
import { Button, Card, EmptyState, PageHeader, Pill, Skeleton } from "@/components/ui";
import { IconSpark } from "@/components/icons";
import { GenerateStrategyModal } from "@/components/ai/GenerateStrategyModal";
import { CreateStrategyModal } from "@/components/strategy/CreateStrategyModal";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { AIStatus, BrokerAccount, Strategy } from "@/lib/types";

export default function StrategiesPage() {
  const { data: strategies, loading, reload } = useApi<Strategy[]>("/strategies", 10000);
  const { data: accounts } = useApi<BrokerAccount[]>("/brokers/accounts");
  const { data: aiStatus } = useApi<AIStatus>("/ai/status");
  const { push } = useToast();
  const [generateOpen, setGenerateOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const { data: kinds } = useApi<{ kinds: string[] }>("/strategies/kinds");

  async function act(id: string, action: "start" | "stop") {
    try {
      await api(`/strategies/${id}/${action}`, { method: "POST" });
      push("success", action === "start" ? "Strategy started" : "Strategy stopped");
      reload();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Failed");
    }
  }

  async function kill(id: string, engaged: boolean) {
    try {
      await api("/system/killswitch", {
        method: "POST",
        body: JSON.stringify({
          scope: "strategy",
          strategy_id: id,
          engaged,
          reason: engaged ? "manual kill from UI" : "released from UI",
        }),
      });
      reload();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Failed");
    }
  }

  const accountLabel = (id: string | null) =>
    accounts?.find((a) => a.id === id)?.label ?? "—";

  return (
    <Shell>
      <PageHeader
        title="Strategies"
        sub="Signal generators routed through the risk engine and order pipeline"
        action={
          <div className="flex gap-2">
            <Button onClick={() => setCreateOpen(true)}>New strategy</Button>
            <Button
              variant="primary"
              onClick={() => setGenerateOpen(true)}
              disabled={!aiStatus?.configured}
            >
              <span className="flex items-center gap-1.5">
                <IconSpark width={14} height={14} /> Generate with AI
              </span>
            </Button>
          </div>
        }
      />
      {!aiStatus?.configured && (
        <p className="mb-4 text-xs text-ink-faint">
          AI generation needs a provider — configure one on the AI Trading page.
        </p>
      )}

      {loading && !strategies ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Skeleton className="h-48" />
          <Skeleton className="h-48" />
        </div>
      ) : strategies?.length ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {strategies.map((s) => (
            <Card key={s.id} title={s.name}>
              <div className="mb-2 flex flex-wrap gap-1.5">
                <Pill value={s.status} />
                <Pill value={s.environment} />
                {s.kind === "ai_agent" && <Pill value="PROPOSED" label="AI AGENT" />}
                {s.killed && <Pill value="KILLED" label="KILL SWITCH" />}
              </div>
              <p className="text-sm text-ink-dim">
                {s.kind} · {s.symbols.join(", ")} · account: {accountLabel(s.broker_account_id)}
              </p>
              <pre className="num mt-2 overflow-x-auto rounded-lg bg-bg p-2.5 text-xs text-ink-faint">
                {JSON.stringify(s.params, null, 2)}
              </pre>
              <div className="mt-3 flex gap-2">
                {s.status !== "RUNNING" ? (
                  <Button size="sm" variant="primary" onClick={() => act(s.id, "start")}>
                    Start
                  </Button>
                ) : (
                  <Button size="sm" onClick={() => act(s.id, "stop")}>
                    Stop
                  </Button>
                )}
                {s.killed ? (
                  <Button size="sm" onClick={() => kill(s.id, false)}>
                    Release kill
                  </Button>
                ) : (
                  <Button size="sm" variant="danger" onClick={() => kill(s.id, true)}>
                    Kill
                  </Button>
                )}
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState
          title="No strategies yet"
          hint="Create one by hand, or generate one with AI if a provider is configured."
          action={
            <div className="flex gap-2">
              <Button onClick={() => setCreateOpen(true)}>New strategy</Button>
              {aiStatus?.configured && (
                <Button variant="primary" onClick={() => setGenerateOpen(true)}>
                  Generate with AI
                </Button>
              )}
            </div>
          }
        />
      )}

      <CreateStrategyModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        accounts={accounts ?? []}
        kinds={kinds?.kinds ?? ["sma_crossover"]}
        onCreated={reload}
      />

      <GenerateStrategyModal
        open={generateOpen}
        onClose={() => setGenerateOpen(false)}
        accounts={accounts ?? []}
        onCreated={reload}
      />
    </Shell>
  );
}
