"use client";

import { useState } from "react";
import { Button, Modal, Pill, inputClass, labelClass } from "../ui";
import { IconSpark } from "../icons";
import { useToast } from "../toast";
import { api } from "@/lib/api";
import { BrokerAccount, StrategyDraft } from "@/lib/types";

export function GenerateStrategyModal({
  open,
  onClose,
  accounts,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  accounts: BrokerAccount[];
  onCreated: () => void;
}) {
  const { push } = useToast();
  const paperAccounts = accounts.filter((a) => a.environment === "paper");
  const [prompt, setPrompt] = useState("");
  const [accountId, setAccountId] = useState("");
  const [draft, setDraft] = useState<StrategyDraft | null>(null);
  const [busy, setBusy] = useState(false);

  const selected = accountId || paperAccounts[0]?.id || "";

  function reset() {
    setPrompt("");
    setDraft(null);
    setBusy(false);
  }

  async function generate(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await api<StrategyDraft>("/ai/strategies/generate", {
        method: "POST",
        body: JSON.stringify({ prompt }),
      });
      setDraft(res);
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Generation failed");
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    if (!draft || !selected) return;
    setBusy(true);
    try {
      await api("/strategies", {
        method: "POST",
        body: JSON.stringify({
          name: draft.name,
          kind: draft.kind,
          broker_account_id: selected,
          environment: "paper",
          symbols: draft.symbols,
          params: draft.params,
        }),
      });
      push("success", `Strategy “${draft.name}” created (draft — start it when ready)`);
      onCreated();
      reset();
      onClose();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Create failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
      title="Generate strategy with AI"
      width="w-[36rem]"
    >
      {!draft ? (
        <form onSubmit={generate}>
          <label className={labelClass}>Describe the strategy</label>
          <textarea
            className={`${inputClass} mb-3 h-28 resize-none`}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="e.g. Trade RELIANCE and TCS on short-term momentum: buy small dips in an uptrend, exit quickly, max 5 shares per trade."
            required
            minLength={8}
            maxLength={2000}
          />
          <label className={labelClass}>Paper account</label>
          <select
            className={`${inputClass} mb-4`}
            value={selected}
            onChange={(e) => setAccountId(e.target.value)}
          >
            {paperAccounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label}
              </option>
            ))}
          </select>
          <div className="flex justify-end gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={busy || !selected}>
              {busy ? "Generating…" : (
                <span className="flex items-center gap-1.5">
                  <IconSpark width={14} height={14} /> Generate
                </span>
              )}
            </Button>
          </div>
        </form>
      ) : (
        <div>
          <div className="rounded-xl border border-line bg-bg p-4">
            <div className="flex items-center justify-between">
              <p className="text-sm font-semibold">{draft.name}</p>
              <Pill value={draft.kind} />
            </div>
            <p className="mt-1.5 text-xs text-ink-dim">{draft.symbols.join(", ")}</p>
            <pre className="num mt-3 overflow-x-auto rounded-lg bg-panel-2 p-3 text-xs text-ink-dim">
              {JSON.stringify(draft.params, null, 2)}
            </pre>
            {draft.rationale && (
              <p className="mt-3 text-xs italic text-ink-faint">{draft.rationale}</p>
            )}
          </div>
          <div className="mt-4 flex justify-between">
            <Button variant="ghost" onClick={() => setDraft(null)}>
              ← Edit prompt
            </Button>
            <div className="flex gap-2">
              <Button onClick={onClose}>Cancel</Button>
              <Button variant="primary" disabled={busy} onClick={create}>
                {busy ? "Creating…" : "Create strategy"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </Modal>
  );
}
