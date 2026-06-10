"use client";

import { useEffect, useState } from "react";
import { Button, Card, Pill, inputClass, labelClass } from "../ui";
import { useToast } from "../toast";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { AISettingsInfo } from "@/lib/types";

const PROVIDER_LABELS: Record<string, string> = {
  anthropic: "Anthropic Claude",
  openai: "OpenAI",
  openrouter: "OpenRouter",
  bedrock: "Amazon Bedrock",
};

const KEY_PLACEHOLDERS: Record<string, string> = {
  anthropic: "sk-ant-…",
  openai: "sk-…",
  openrouter: "sk-or-…",
};

export function AISettingsCard({ onSaved }: { onSaved: () => void }) {
  const { data: settings, reload } = useApi<AISettingsInfo>("/ai/settings");
  const { push } = useToast();
  const [provider, setProvider] = useState("anthropic");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [awsKeyId, setAwsKeyId] = useState("");
  const [awsSecret, setAwsSecret] = useState("");
  const [region, setRegion] = useState("us-east-1");
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    if (!settings) return;
    if (settings.source === "settings" && settings.provider) {
      setProvider(settings.provider);
      setModel(settings.model ?? "");
      setBaseUrl(settings.base_url ?? "");
    }
  }, [settings]);

  function pickProvider(p: string) {
    setProvider(p);
    setModel(settings?.default_models?.[p] ?? "");
    setBaseUrl(p === "openrouter" ? "https://openrouter.ai/api/v1" : "");
  }

  async function save() {
    setBusy(true);
    try {
      await api("/ai/settings", {
        method: "PUT",
        body: JSON.stringify({
          provider,
          model: model || settings?.default_models?.[provider] || "",
          base_url: provider === "openrouter" || provider === "openai" ? baseUrl || null : null,
          api_key: apiKey || null,
          aws_access_key_id: provider === "bedrock" ? awsKeyId : null,
          aws_secret_access_key: provider === "bedrock" ? awsSecret : null,
          region: provider === "bedrock" ? region : null,
        }),
      });
      setApiKey("");
      setAwsKeyId("");
      setAwsSecret("");
      push("success", "AI settings saved — key stored encrypted");
      reload();
      onSaved();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Failed to save settings");
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setTesting(true);
    try {
      const res = await api<{ ok: boolean; error?: string; model?: string }>(
        "/ai/settings/test",
        { method: "POST" }
      );
      if (res.ok) push("success", `Connection OK (${res.model})`);
      else push("error", `Test failed: ${res.error ?? "unknown error"}`);
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Test failed");
    } finally {
      setTesting(false);
    }
  }

  const isBedrock = provider === "bedrock";
  const configured = settings?.configured ?? false;

  return (
    <Card
      title="AI provider"
      action={
        configured ? (
          <Pill
            value="connected"
            label={settings?.source === "env" ? "configured (env)" : "configured"}
          />
        ) : (
          <Pill value="pending_auth" label="not configured" />
        )
      }
    >
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {(settings?.providers ?? Object.keys(PROVIDER_LABELS)).map((p) => (
          <button
            key={p}
            type="button"
            onClick={() => pickProvider(p)}
            className={`rounded-lg border px-3 py-2.5 text-sm font-medium transition-colors ${
              provider === p
                ? "border-accent/60 bg-accent/10 text-accent"
                : "border-line-2 text-ink-dim hover:border-ink-faint"
            }`}
          >
            {PROVIDER_LABELS[p] ?? p}
          </button>
        ))}
      </div>

      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label className={labelClass}>Model</label>
          <input
            className={`${inputClass} num`}
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={settings?.default_models?.[provider] ?? "model id"}
          />
        </div>

        {!isBedrock && (
          <div>
            <label className={labelClass}>API key {configured && "(leave blank to keep)"}</label>
            <input
              type="password"
              autoComplete="off"
              className={inputClass}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={KEY_PLACEHOLDERS[provider] ?? "api key"}
            />
          </div>
        )}

        {(provider === "openrouter" || provider === "openai") && (
          <div className="sm:col-span-2">
            <label className={labelClass}>Base URL (optional for OpenAI-compatible hosts)</label>
            <input
              className={`${inputClass} num`}
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={provider === "openrouter" ? "https://openrouter.ai/api/v1" : "https://api.openai.com/v1"}
            />
          </div>
        )}

        {isBedrock && (
          <>
            <div>
              <label className={labelClass}>AWS access key ID</label>
              <input
                type="password"
                autoComplete="off"
                className={inputClass}
                value={awsKeyId}
                onChange={(e) => setAwsKeyId(e.target.value)}
                placeholder="AKIA…"
              />
            </div>
            <div>
              <label className={labelClass}>AWS secret access key</label>
              <input
                type="password"
                autoComplete="off"
                className={inputClass}
                value={awsSecret}
                onChange={(e) => setAwsSecret(e.target.value)}
              />
            </div>
            <div>
              <label className={labelClass}>Region</label>
              <input
                className={`${inputClass} num`}
                value={region}
                onChange={(e) => setRegion(e.target.value)}
                placeholder="us-east-1"
              />
            </div>
          </>
        )}
      </div>

      <p className="mt-3 text-xs text-ink-faint">
        Keys are stored Fernet-encrypted on the backend and never shown again. The backend
        ANTHROPIC_API_KEY env var works as a zero-config fallback.
      </p>

      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={test} disabled={testing || !configured}>
          {testing ? "Testing…" : "Test connection"}
        </Button>
        <Button variant="primary" onClick={save} disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </Button>
      </div>
    </Card>
  );
}
