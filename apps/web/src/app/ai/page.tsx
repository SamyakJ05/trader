"use client";

import { useState } from "react";
import Shell from "@/components/Shell";
import { Button, Card, EmptyState, PageHeader, Pill, Skeleton } from "@/components/ui";
import { AISettingsCard } from "@/components/ai/AISettingsCard";
import { ChatPanel } from "@/components/ai/ChatPanel";
import { ProposalCard } from "@/components/ai/ProposalCard";
import { useApi } from "@/lib/useApi";
import { AIProposal, AIStatus, BrokerAccount } from "@/lib/types";

export default function AIPage() {
  const { data: status, loading, reload: reloadStatus } = useApi<AIStatus>("/ai/status");
  const { data: accounts } = useApi<BrokerAccount[]>("/brokers/accounts");
  const { data: proposals, reload: reloadProposals } = useApi<AIProposal[]>(
    "/ai/proposals?status_filter=PROPOSED",
    10000
  );
  const [showSettings, setShowSettings] = useState(false);

  const configured = status?.configured ?? false;

  return (
    <Shell>
      <PageHeader
        title="AI Trading"
        sub="Analyst chat, human-approved trade proposals, AI strategies — paper only"
        action={
          configured ? (
            <div className="flex items-center gap-2">
              <Pill value="connected" label={`${status?.provider} · ${status?.model}`} />
              <Button size="sm" variant="ghost" onClick={() => setShowSettings(!showSettings)}>
                {showSettings ? "Hide settings" : "Settings"}
              </Button>
            </div>
          ) : undefined
        }
      />

      {loading && !status ? (
        <Skeleton className="h-64" />
      ) : !configured || showSettings ? (
        <div className="mb-4">
          <AISettingsCard
            onSaved={() => {
              reloadStatus();
              setShowSettings(false);
            }}
          />
        </div>
      ) : null}

      {configured && (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <div className="xl:col-span-2">
            <ChatPanel accounts={accounts ?? []} onProposalsChanged={reloadProposals} />
          </div>
          <div className="space-y-4">
            <Card title="Pending proposals">
              {proposals?.length ? (
                <div className="space-y-3">
                  {proposals.map((p) => (
                    <ProposalCard key={p.id} proposal={p} onDecided={reloadProposals} />
                  ))}
                </div>
              ) : (
                <p className="text-sm text-ink-faint">
                  None pending. Ask the analyst for a trade idea — every proposal needs your
                  approval before any order is placed.
                </p>
              )}
            </Card>
            <Card title="How it works">
              <ul className="space-y-2 text-xs leading-relaxed text-ink-dim">
                <li>1. The analyst reads your paper portfolio through read-only tools.</li>
                <li>2. Trade ideas become proposals — nothing executes on its own.</li>
                <li>3. Approval routes through the same risk engine as manual orders.</li>
                <li>4. Every AI proposal and decision lands in the audit log.</li>
              </ul>
            </Card>
          </div>
        </div>
      )}

      {!configured && !loading && (
        <EmptyState
          title="Configure an AI provider to start"
          hint="Anthropic Claude, OpenAI, OpenRouter or Amazon Bedrock — your key is encrypted at rest. Alternatively set ANTHROPIC_API_KEY in the backend env."
        />
      )}
    </Shell>
  );
}
