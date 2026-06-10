"use client";

import { Pill } from "../ui";

export interface AuditEventLite {
  id: number;
  ts: string;
  event_type: string;
  entity_type: string | null;
  entity_id: string | null;
}

export function RecentAuditList({ events }: { events: AuditEventLite[] }) {
  if (!events.length) {
    return <p className="text-sm text-ink-faint">No events yet.</p>;
  }
  return (
    <ul className="space-y-2">
      {events.map((e) => (
        <li key={e.id} className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            <Pill value={e.event_type} />
            <span className="text-ink-dim">
              {e.entity_type}
              {e.entity_id ? ` · ${e.entity_id.slice(0, 8)}` : ""}
            </span>
          </span>
          <span className="text-xs text-ink-faint">
            {new Date(e.ts).toLocaleTimeString()}
          </span>
        </li>
      ))}
    </ul>
  );
}
