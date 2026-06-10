"use client";

import { KillSwitchStatus } from "@/lib/types";

export function KillSwitchBanner({ status }: { status: KillSwitchStatus | null }) {
  if (!status?.global_engaged) return null;
  return (
    <div
      role="alert"
      className="mb-4 rounded-md border border-red-700 bg-red-950/60 p-3 text-sm font-semibold text-red-200"
    >
      GLOBAL KILL SWITCH ENGAGED — all order placement halted.
      {status.global_reason ? ` Reason: ${status.global_reason}` : ""}
    </div>
  );
}
