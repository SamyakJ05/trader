"use client";

import { useState } from "react";
import { Button, Pill } from "../ui";
import { useToast } from "../toast";
import { api } from "@/lib/api";
import { AIProposal } from "@/lib/types";

export function ProposalCard({
  proposal,
  onDecided,
}: {
  proposal: AIProposal;
  onDecided?: () => void;
}) {
  const { push } = useToast();
  const [busy, setBusy] = useState(false);
  const [local, setLocal] = useState(proposal);

  async function decide(action: "approve" | "reject") {
    setBusy(true);
    try {
      const res = await api<{ proposal?: AIProposal; order_status?: string } & AIProposal>(
        `/ai/proposals/${local.id}/${action}`,
        { method: "POST" }
      );
      if (action === "approve") {
        const updated = (res.proposal ?? res) as AIProposal;
        setLocal(updated);
        push(
          res.order_status === "REJECTED_RISK" ? "error" : "success",
          res.order_status === "REJECTED_RISK"
            ? "Approved, but the risk engine rejected the order"
            : `Order placed (${res.order_status})`
        );
      } else {
        setLocal(res as AIProposal);
        push("info", "Proposal rejected");
      }
      onDecided?.();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }

  const pending = local.status === "PROPOSED";

  // A symbol alone does not name an F&O contract — the same underlying has
  // thousands — so approving one without seeing the expiry and strike is
  // approving a trade you cannot actually identify.
  const contract = local.expiry
    ? [
        local.expiry,
        local.strike ? `${Number(local.strike)}` : null,
        local.option_right && local.option_right !== "OTHERS"
          ? local.option_right
          : "FUT",
      ]
        .filter(Boolean)
        .join(" ")
    : null;

  return (
    <div className="rounded-xl border border-accent/30 bg-accent/5 p-3.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Pill value={local.side} />
          <span className="num text-sm font-semibold">
            {local.quantity} × {local.symbol}
            {contract && <span className="text-accent"> {contract}</span>}
          </span>
          <span className="text-xs text-ink-faint">
            {local.order_type}
            {local.limit_price ? ` @${Number(local.limit_price).toFixed(2)}` : ""} · {local.product} ·{" "}
            {local.exchange}
          </span>
        </div>
        <Pill value={local.status} />
      </div>
      <p className="mt-2 text-sm text-ink-dim">{local.rationale}</p>
      {pending && (
        <div className="mt-3 flex gap-2">
          <Button size="sm" variant="primary" disabled={busy} onClick={() => decide("approve")}>
            Approve &amp; place order
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => decide("reject")}>
            Reject
          </Button>
        </div>
      )}
    </div>
  );
}
