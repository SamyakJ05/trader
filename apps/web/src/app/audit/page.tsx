"use client";

import { useState } from "react";
import Shell from "@/components/Shell";
import { Card, Pill, Td, Th } from "@/components/ui";
import { useApi } from "@/lib/useApi";
import { AuditEvent } from "@/lib/types";

const EVENT_TYPES = [
  "",
  "USER_ACTION",
  "SIGNAL_GENERATED",
  "RISK_CHECK",
  "ORDER_REQUESTED",
  "ORDER_STATE_CHANGED",
  "ORDER_FILL",
  "BROKER_RESPONSE",
  "KILL_SWITCH",
  "BROKER_SESSION",
  "BROKER_SYNC",
];

export default function AuditPage() {
  const [filter, setFilter] = useState("");
  const query = filter ? `&event_type=${filter}` : "";
  const { data } = useApi<{ events: AuditEvent[] }>(`/audit/events?limit=100${query}`, 10000);

  return (
    <Shell>
      <div className="mb-4 flex items-center gap-4">
        <h1 className="text-xl font-bold">Audit Log</h1>
        <select
          className="rounded-md border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-sm"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        >
          {EVENT_TYPES.map((t) => (
            <option key={t} value={t}>{t || "all events"}</option>
          ))}
        </select>
      </div>
      <Card>
        <table className="w-full">
          <thead>
            <tr className="border-b border-zinc-800">
              <Th>#</Th><Th>Time</Th><Th>Event</Th><Th>Entity</Th><Th>Payload</Th>
            </tr>
          </thead>
          <tbody>
            {data?.events.map((e) => (
              <tr key={e.id} className="border-b border-zinc-900 align-top">
                <Td className="text-zinc-600">{e.id}</Td>
                <Td className="whitespace-nowrap text-zinc-500">
                  {new Date(e.ts).toLocaleTimeString()}
                </Td>
                <Td><Pill value={e.event_type} /></Td>
                <Td className="text-zinc-400">
                  {e.entity_type}
                  {e.entity_id ? ` · ${e.entity_id.slice(0, 8)}` : ""}
                </Td>
                <Td>
                  <pre className="max-w-xl overflow-x-auto text-xs text-zinc-500">
                    {JSON.stringify(e.payload)}
                  </pre>
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </Shell>
  );
}
