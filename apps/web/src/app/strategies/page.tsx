"use client";

import { useState } from "react";
import Shell from "@/components/Shell";
import { Button, Card, ErrorNote, Pill } from "@/components/ui";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { BrokerAccount, Strategy } from "@/lib/types";

export default function StrategiesPage() {
  const { data: strategies, reload } = useApi<Strategy[]>("/strategies", 10000);
  const { data: accounts } = useApi<BrokerAccount[]>("/brokers/accounts");
  const [error, setError] = useState<string | null>(null);

  async function act(id: string, action: "start" | "stop") {
    setError(null);
    try {
      await api(`/strategies/${id}/${action}`, { method: "POST" });
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function kill(id: string, engaged: boolean) {
    setError(null);
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
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  const accountLabel = (id: string | null) =>
    accounts?.find((a) => a.id === id)?.label ?? "—";

  return (
    <Shell>
      <h1 className="mb-4 text-xl font-bold">Strategies</h1>
      <ErrorNote message={error} />
      <div className="grid grid-cols-2 gap-4">
        {strategies?.map((s) => (
          <Card key={s.id} title={s.name}>
            <div className="mb-2 flex gap-2">
              <Pill value={s.status} />
              <Pill value={s.environment} />
              {s.killed && <Pill value="KILLED" label="KILL SWITCH" />}
            </div>
            <p className="text-sm text-zinc-400">
              {s.kind} · {s.symbols.join(", ")} · account: {accountLabel(s.broker_account_id)}
            </p>
            <pre className="mt-2 rounded bg-zinc-950 p-2 text-xs text-zinc-500">
              {JSON.stringify(s.params, null, 2)}
            </pre>
            <div className="mt-3 flex gap-2">
              {s.status !== "RUNNING" ? (
                <Button variant="primary" onClick={() => act(s.id, "start")}>Start</Button>
              ) : (
                <Button onClick={() => act(s.id, "stop")}>Stop</Button>
              )}
              {s.killed ? (
                <Button onClick={() => kill(s.id, false)}>Release kill</Button>
              ) : (
                <Button variant="danger" onClick={() => kill(s.id, true)}>Kill</Button>
              )}
            </div>
          </Card>
        ))}
        {!strategies?.length && (
          <p className="text-sm text-zinc-500">No strategies. Seed script creates a demo one.</p>
        )}
      </div>
    </Shell>
  );
}
