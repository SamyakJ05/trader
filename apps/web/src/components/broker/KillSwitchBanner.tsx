"use client";

import { KillSwitchStatus } from "@/lib/types";

export function KillSwitchBanner({ status }: { status: KillSwitchStatus | null }) {
  if (!status?.global_engaged) return null;
  return (
    <div
      role="alert"
      className="mb-4 rounded-lg border border-loss/50 bg-loss/10 p-3 text-sm font-semibold text-loss"
    >
      GLOBAL KILL SWITCH ENGAGED — all order placement halted.
      {status.global_reason ? ` Reason: ${status.global_reason}` : ""}
    </div>
  );
}
