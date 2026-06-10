"use client";

import { useEffect, useRef, useState } from "react";
import { Button, Card, inputClass } from "../ui";
import { IconSend } from "../icons";
import { useToast } from "../toast";
import { api } from "@/lib/api";
import { AIProposal, BrokerAccount, ChatTurn } from "@/lib/types";
import { ProposalCard } from "./ProposalCard";

const SUGGESTIONS = [
  "How is my portfolio doing?",
  "Quote RELIANCE and TCS with recent trend",
  "Find me one momentum trade idea",
];

export function ChatPanel({
  accounts,
  onProposalsChanged,
}: {
  accounts: BrokerAccount[];
  onProposalsChanged: () => void;
}) {
  const { push } = useToast();
  const paperAccounts = accounts.filter((a) => a.environment === "paper");
  const [accountId, setAccountId] = useState("");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  const selected = accountId || paperAccounts[0]?.id || "";

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);

  async function send(text: string) {
    const content = text.trim();
    if (!content || busy || !selected) return;
    const nextTurns: ChatTurn[] = [...turns, { role: "user", content }];
    setTurns(nextTurns);
    setInput("");
    setBusy(true);
    try {
      const res = await api<{ reply: string; proposals: AIProposal[] }>("/ai/analyst/chat", {
        method: "POST",
        body: JSON.stringify({
          broker_account_id: selected,
          messages: nextTurns.map((t) => ({ role: t.role, content: t.content })),
        }),
      });
      setTurns([
        ...nextTurns,
        { role: "assistant", content: res.reply, proposals: res.proposals },
      ]);
      if (res.proposals.length) onProposalsChanged();
    } catch (err) {
      setTurns(nextTurns.slice(0, -1));
      setInput(content);
      push("error", err instanceof Error ? err.message : "AI request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card pad={false} className="flex h-[36rem] flex-col">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-ink-dim">
          Analyst chat
        </h2>
        <select
          className={`${inputClass} w-auto py-1 text-xs`}
          value={selected}
          onChange={(e) => setAccountId(e.target.value)}
        >
          {paperAccounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.label}
            </option>
          ))}
        </select>
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {turns.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center gap-3">
            <p className="text-sm text-ink-faint">
              Ask about positions, funds, quotes — or for a trade idea.
            </p>
            <div className="flex flex-wrap justify-center gap-2">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  onClick={() => send(s)}
                  className="rounded-full border border-line-2 px-3 py-1 text-xs text-ink-dim transition-colors hover:border-accent/50 hover:text-accent"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className={t.role === "user" ? "flex justify-end" : ""}>
            <div
              className={`max-w-[85%] rounded-xl px-3.5 py-2.5 text-sm leading-relaxed ${
                t.role === "user"
                  ? "bg-accent/15 text-ink"
                  : "border border-line bg-panel-2 text-ink"
              }`}
            >
              <p className="whitespace-pre-wrap">{t.content}</p>
              {t.proposals?.map((p) => (
                <div key={p.id} className="mt-2">
                  <ProposalCard proposal={p} onDecided={onProposalsChanged} />
                </div>
              ))}
            </div>
          </div>
        ))}
        {busy && (
          <div className="flex items-center gap-2 text-xs text-ink-faint">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
            analyzing…
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <form
        className="flex gap-2 border-t border-line p-3"
        onSubmit={(e) => {
          e.preventDefault();
          send(input);
        }}
      >
        <input
          className={inputClass}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={selected ? "Ask the analyst…" : "Create a paper broker account first"}
          disabled={busy || !selected}
        />
        <Button type="submit" variant="primary" disabled={busy || !selected || !input.trim()}>
          <IconSend width={14} height={14} />
        </Button>
      </form>
    </Card>
  );
}
