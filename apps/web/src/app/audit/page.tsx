"use client";

import { useState } from "react";
import Shell from "@/components/Shell";
import { Card, EmptyState, PageHeader, Pill, Skeleton, Td, Th, inputClass } from "@/components/ui";
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
  "AI_PROPOSAL",
  "AI_DECISION",
];

export default function AuditPage() {
  const [filter, setFilter] = useState("");
  const query = filter ? `&event_type=${filter}` : "";
  const { data, loading } = useApi<{ events: AuditEvent[] }>(
    `/audit/events?limit=100${query}`,
    10000
  );

  return (
    <Shell>
      <PageHeader
        title="Audit Log"
        sub="Append-only event stream — signals, risk checks, orders, sessions"
        action={
          <select
            className={`${inputClass} w-auto`}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            {EVENT_TYPES.map((t) => (
              <option key={t} value={t}>
                {t || "all events"}
              </option>
            ))}
          </select>
        }
      />
      {loading && !data ? (
        <Skeleton className="h-64" />
      ) : data?.events.length ? (
        <Card pad={false}>
          <div className="overflow-x-auto p-2">
            <table className="w-full">
              <thead>
                <tr className="border-b border-line">
                  <Th>#</Th>
                  <Th>Time</Th>
                  <Th>Event</Th>
                  <Th>Entity</Th>
                  <Th>Payload</Th>
                </tr>
              </thead>
              <tbody>
                {data.events.map((e) => (
                  <tr key={e.id} className="border-b border-line/50 align-top last:border-0">
                    <Td className="num text-ink-faint">{e.id}</Td>
                    <Td className="num whitespace-nowrap text-ink-faint">
                      {new Date(e.ts).toLocaleTimeString()}
                    </Td>
                    <Td>
                      <Pill value={e.event_type} />
                    </Td>
                    <Td className="text-ink-dim">
                      {e.entity_type}
                      {e.entity_id ? ` · ${e.entity_id.slice(0, 8)}` : ""}
                    </Td>
                    <Td>
                      <pre className="num max-w-xl overflow-x-auto text-xs text-ink-faint">
                        {JSON.stringify(e.payload)}
                      </pre>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : (
        <EmptyState title="No events" hint="Try a different event-type filter." />
      )}
    </Shell>
  );
}
