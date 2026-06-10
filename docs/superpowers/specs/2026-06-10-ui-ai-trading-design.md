# UI Redesign + AI Trading + Broker Connection Wizard — Design

Date: 2026-06-10
Status: Approved (all three workstreams, AI phases A–C included)

## Goals

1. Full visual redesign of the web app (trading-terminal aesthetic, better hierarchy and feedback).
2. AI Trading, phased: (A) AI analyst chat with human-approved trade proposals, (B) natural-language strategy generator, (C) autonomous AI strategy kind running through the existing pipeline.
3. Replace the broker connection modal with a guided 3-step wizard and improve connection cards/flows.

Non-goals: live trading enablement, broker adapter changes, backtesting, mobile layout.

## Invariants (unchanged)

- Nothing outside `app/adapters/` talks to a broker.
- Every order goes through `services/orders.place_order` (idempotency → audit → risk → dispatch). AI never bypasses it.
- Paper is the only enabled execution mode. AI features are paper-only.
- `audit_events` stays append-only; AI actions emit audit events.

---

## Workstream 1 — UI redesign

**Design system** (`apps/web/src/app/globals.css` + `layout.tsx`):
- Fonts via `next/font/google`: Space Grotesk (UI/display), JetBrains Mono (numerals, symbols, ids). Tabular numbers for all money/qty cells.
- Tailwind v4 `@theme` tokens: near-black background (`#0B0E14` family), elevated panel surfaces, hairline borders, emerald (gain), red (loss), amber (warning), cyan (primary action).

**Component kit** (`apps/web/src/components/ui.tsx`, split if it grows past ~300 lines):
- Keep: `Card`, `Pill`, `Th`, `Td`, `Button`, `Pnl`, `ErrorNote` (restyled, API-compatible where possible).
- Add: `PageHeader`, `StatCard`, `Skeleton`, `EmptyState`, `Modal` (shared base for all dialogs), `Sparkline` (inline SVG, no chart dependency), `Toast` system (`ToastProvider` + `useToast`) replacing inline notice strings.

**Shell** (`components/Shell.tsx`): sidebar with inline SVG icons, nav grouped into Trading (Dashboard, Brokers, Positions, Orders), AI (AI Trading), System (Strategies, Risk, Audit Log); kill-switch state pill in sidebar; consistent page container.

**Pages restyled**: login, dashboard (StatCards + sparkline on funds, broker health table, kill switch, recent activity), brokers, positions, orders, strategies, risk, audit. Loading states use `Skeleton`; empty states use `EmptyState`.

Existing component tests (`BrokerConnectionCard.test.tsx`, `KillSwitchBanner.test.tsx`) updated to match new markup but keep asserting the same behavior.

## Workstream 2 — Broker connection wizard

Replace `BrokerConnectModal` with `BrokerConnectWizard` (3 steps, single modal shell):

1. **Pick broker** — card per broker (paper / zerodha / groww / icici_breeze) showing display name, adapter-status pill (working/scaffold), auth model. Data from existing `/brokers/capabilities`.
2. **Configure** — label input; for non-paper: credential_ref input plus per-broker setup help with a copyable `.env` snippet (`<REF>_API_KEY=` etc.); groww/breeze note the post-create "Set token" step.
3. **Create & verify** — creates the account (existing `POST /brokers/accounts`), then offers Connect (zerodha redirect) or Set token (groww/breeze, inline token field reusing the session-token endpoint), then Verify read access — each as a progress row with pass/fail state. Paper accounts: create → connect → verify automatically.

`SessionTokenModal` survives as a standalone for the card-level "Set token" action, restyled on the shared `Modal`.

`BrokerConnectionCard`: restyled; status pill + status message, credential checklist, verify/sync timestamps, grouped primary/secondary/danger actions. Zerodha `?connected=` callback toast suggests "Verify read access".

## Workstream 3 — AI Trading

**Backend foundation**:
- Dependencies: `anthropic` (Python SDK; also covers Bedrock via `AsyncAnthropicBedrock`) and `openai` (covers OpenAI, OpenRouter, and any OpenAI-compatible base URL).
- **Provider settings (in-app, per user)**: new table `ai_settings` — provider (`anthropic | openai | openrouter | bedrock`), model, base_url (nullable), `credentials_enc` (Fernet-encrypted JSON: `{api_key}` or `{aws_access_key_id, aws_secret_access_key, region}` for Bedrock), updated_at. Secrets never returned by the API — only `configured: true`. Env fallback: `ANTHROPIC_API_KEY` + `AI_MODEL` still work when no row exists.
- `app/services/ai/llm.py`: thin provider abstraction — `LLMClient` protocol with `chat(system, messages, tools) -> LLMReply {text, tool_calls}` and `generate_json(system, prompt, schema) -> dict`. Two impls: `AnthropicLLM` (anthropic + bedrock; native tool use, `output_config.format` structured output) and `OpenAILLM` (openai + openrouter + custom base_url; function calling, `response_format json_schema`).
- Endpoints: `GET /ai/settings` (provider/model/configured, no secrets), `PUT /ai/settings` (validates provider, encrypts credentials), `POST /ai/settings/test` (one tiny round-trip, returns ok/error), `GET /ai/status` → `{configured, provider, model}`.
- Frontend: AI Settings card on `/ai` — provider select, model input with per-provider placeholder (anthropic: `claude-opus-4-8`; openrouter: `anthropic/claude-opus-4.8`; bedrock: `anthropic.claude-opus-4-8`), API-key password field (write-only), base URL field for openrouter/custom (prefilled `https://openrouter.ai/api/v1`), AWS key/secret/region fields for bedrock, Test + Save buttons.
- All AI endpoints return 503 with a clear message when unconfigured.
- New route module `app/api/routes/ai.py` registered in the router.
- New `AuditEventType` members: `AI_PROPOSAL`, `AI_DECISION` (strings only; no migration needed for the enum).

**Phase A — AI analyst + proposals**:
- New table `ai_proposals` (migration 0004): id, user_id, broker_account_id, symbol, exchange, side, order_type, product, quantity, limit_price (nullable), rationale, status (`PROPOSED | APPROVED | REJECTED`), order_id (nullable FK), created_at, decided_at.
- `app/services/ai/tools.py`: read-only tools over the DB/redis — `get_positions`, `get_orders`, `get_funds`, `get_quotes` (sim prices), `get_strategies`, `get_risk_rules` — plus `propose_trade`, which inserts an `ai_proposals` row + `AI_PROPOSAL` audit event and returns the proposal id to the model. Tools never place orders.
- `app/services/ai/analyst.py`: manual tool-use loop with `AsyncAnthropic`, `thinking={"type": "adaptive"}`, system prompt describing the paper-trading context and that proposals require human approval. Chat is stateless server-side: the client sends the transcript, gets back new turns + any proposals created.
- Endpoints: `POST /ai/analyst/chat`, `GET /ai/proposals`, `POST /ai/proposals/{id}/approve` (places the order via `order_service.place_order` with `client_order_id = ai-{proposal_id_hex}`, links order, audits `USER_ACTION`), `POST /ai/proposals/{id}/reject`.
- Frontend `/ai` page: setup card when unconfigured (env instructions); chat panel; proposal cards inline + pending-proposals list with Approve/Reject.

**Phase B — strategy generator**:
- `app/services/ai/generator.py`: one structured-output call (`output_config={"format": {"type": "json_schema", ...}}`) producing `{name, kind, symbols, params, rationale}` with `kind` constrained to `STRATEGY_REGISTRY` keys; server-side validation re-checks kind and params shape.
- `POST /ai/strategies/generate` returns the draft; nothing is persisted — the UI shows a preview card and creates via the existing `POST /strategies`.
- Strategies page gets a "Generate with AI" action opening a prompt modal → preview → create.

**Phase C — autonomous AI strategy**:
- `app/engines/strategy/base.py` gains `AsyncStrategyBase` (async `evaluate_async`); the runner awaits it when the impl is async, sync path unchanged.
- `app/engines/strategy/ai_agent.py`: kind `ai_agent`, registered in `STRATEGY_REGISTRY`. Per symbol it calls Claude with price history, current position, and `params.instructions`; structured output `{action: BUY|SELL|HOLD, quantity, reason}` mapped to existing `SignalType`s (BUY → ENTRY_LONG / EXIT_SHORT, SELL → EXIT_LONG / ENTRY_SHORT based on current position sign). Signals flow through the unchanged runner path: signal persisted → audit → risk engine → order pipeline → kill switches.
- Guardrails: returns no signals when AI unconfigured; redis-backed `params.min_interval_seconds` (default 300) between calls and `params.max_decisions_per_day` (default 10); paper environment enforced at strategy start for kind `ai_agent`; every model decision (including HOLD) audited as `AI_DECISION`.

## Error handling

- Missing API key: 503 from AI endpoints with actionable detail; `/ai` page shows setup instructions; `ai_agent` logs and skips.
- Anthropic API errors: caught via typed SDK exceptions, surfaced as 502 with a short message; chat UI shows a retry-able error bubble. `ai_agent` treats errors as no-signal (does not flip strategy to ERROR for transient API failures).
- Proposal approve on a non-PROPOSED proposal → 409. Approve failures from the risk engine still mark the proposal APPROVED with the rejected order linked — the order status tells the story.

## Testing

- Backend (pytest, mocked Anthropic client): proposal lifecycle (create via tool → approve places order through pipeline → reject), generator output validation (bad kind rejected), ai_agent decision→signal mapping + interval/day-cap guards + unconfigured behavior.
- Frontend (vitest): updated existing component tests; new tests for wizard step flow and proposal card actions.
- Manual: `make` dev stack, walk dashboard → wizard (paper account end-to-end) → AI page (unconfigured state) → strategies generate (with key if available).
