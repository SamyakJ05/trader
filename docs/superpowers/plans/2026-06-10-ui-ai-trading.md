# UI Redesign + AI Trading + Broker Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the trader web UI, add a guided broker-connection wizard, and add Claude-powered AI trading (analyst chat + proposals, strategy generator, autonomous `ai_agent` strategy kind) per `docs/superpowers/specs/2026-06-10-ui-ai-trading-design.md`.

**Architecture:** Frontend stays Next.js 15 + Tailwind v4 with a custom component kit (no new UI deps). Backend adds `app/services/ai/` (AsyncAnthropic, model `claude-opus-4-8`), one new table `ai_proposals` (migration 0004), one new route module `/ai`, and an async strategy path in the runner. AI never places orders directly — only via `services/orders.place_order` after human approval (Phase A) or via the existing signal→risk→pipeline path (Phase C).

**Tech Stack:** Next.js 15, Tailwind v4, vitest; FastAPI, SQLAlchemy 2 async, Alembic, `anthropic` Python SDK, pytest + fakeredis.

---

## File map

Frontend (apps/web/src):
- Modify: `app/layout.tsx` (fonts), `app/globals.css` (theme), `components/Shell.tsx`, `components/ui.tsx`, all `app/*/page.tsx`, `components/broker/*`
- Create: `components/toast.tsx`, `components/icons.tsx`, `components/broker/BrokerConnectWizard.tsx`, `components/ai/ChatPanel.tsx`, `components/ai/ProposalCard.tsx`, `components/ai/GenerateStrategyModal.tsx`, `app/ai/page.tsx`
- Delete: `components/broker/BrokerConnectModal.tsx` (wizard replaces it; `SessionTokenModal` moves into wizard file)

Backend (apps/api):
- Modify: `pyproject.toml`, `app/core/config.py`, `app/domain/enums.py`, `app/db/models.py`, `app/api/router.py`, `app/api/routes/strategies.py` (paper guard), `app/engines/strategy/base.py`, `app/engines/strategy/runner.py`, `.env.example` (root)
- Create: `app/services/ai/__init__.py`, `app/services/ai/client.py`, `app/services/ai/tools.py`, `app/services/ai/analyst.py`, `app/services/ai/generator.py`, `app/engines/strategy/ai_agent.py`, `app/api/routes/ai.py`, `alembic/versions/0004_ai_proposals.py`, `tests/test_ai_proposals.py`, `tests/test_ai_generator.py`, `tests/test_ai_agent.py`

---

### Task 1: Design system (fonts + theme tokens)

**Files:** Modify `apps/web/src/app/layout.tsx`, `apps/web/src/app/globals.css`

- [ ] Load Space Grotesk + JetBrains Mono via `next/font/google` in `layout.tsx`, expose as CSS vars `--font-sans` / `--font-mono`, set on `<body>`.
- [ ] Rewrite `globals.css` with Tailwind v4 `@theme` tokens: surfaces (`--color-bg: #0B0E14`, `--color-panel: #11151D`, `--color-panel-2: #161B25`), hairline border `#1F2733`, text tiers, accent cyan `#22D3EE`, gain `#34D399`, loss `#F87171`, warn `#FBBF24`. `font-variant-numeric: tabular-nums` utility class `num`.
- [ ] `pnpm --filter web build` passes. Commit.

### Task 2: Component kit

**Files:** Modify `apps/web/src/components/ui.tsx`; Create `components/toast.tsx`, `components/icons.tsx`

- [ ] `ui.tsx`: restyle `Card` (optional `title`, `action`, `pad`), `Pill` (same status map, new tokens), `Button` (variants default/primary/danger/ghost, `size`), `Th`/`Td`, `Pnl` (mono + sign), `ErrorNote`. Add `PageHeader {title, sub?, action?}`, `StatCard {label, value, sub?, spark?}`, `Skeleton {className}`, `EmptyState {title, hint?, action?}`, `Modal {open, onClose, title, width?, children}` (fixed overlay, esc/overlay close), `Sparkline {points: number[], width?, height?}` (pure SVG polyline).
- [ ] `toast.tsx`: `ToastProvider` (context + fixed stack, auto-dismiss 5s, kinds info/success/error) and `useToast()` returning `push(kind, message)`. Mount provider in `layout.tsx`.
- [ ] `icons.tsx`: small inline SVG set (dashboard, link/broker, layers/positions, list/orders, bot/ai, strategy, shield/risk, scroll/audit, logout, spark/generate, send, check, x).
- [ ] Build passes. Commit.

### Task 3: Shell redesign

**Files:** Modify `apps/web/src/components/Shell.tsx`

- [ ] Sidebar: brand block (`trader` + PAPER pill), grouped nav — Trading: Dashboard, Brokers, Positions, Orders; AI: AI Trading (`/ai`); System: Strategies, Risk, Audit Log — each item icon + label, active state. Kill-switch pill at bottom (reads existing poll), logout.
- [ ] Main column: keep `KillSwitchBanner`, page container `max-w-[1400px] px-8 py-6`.
- [ ] Update `KillSwitchBanner.test.tsx` if markup changed; `pnpm --filter web test` green. Commit.

### Task 4: Restyle core pages

**Files:** Modify `app/login/page.tsx`, `app/dashboard/page.tsx`, `app/positions/page.tsx`, `app/orders/page.tsx`, `app/risk/page.tsx`, `app/audit/page.tsx`

- [ ] Login: centered card, brand, error note, loading state on submit.
- [ ] Dashboard: `PageHeader`, 4 `StatCard`s (funds card gets `Sparkline` of per-account cash where available), broker-health table, kill-switch + recent-activity cards. `Skeleton` while `!data`.
- [ ] Positions/Orders/Risk/Audit: `PageHeader`, table styling (mono numerals, right-aligned numbers), `EmptyState` for empty lists, `Skeleton` rows while loading, toasts for action errors where actions exist.
- [ ] Build + tests green. Commit per page or batch.

### Task 5: Broker wizard + cards + flows

**Files:** Create `components/broker/BrokerConnectWizard.tsx`; Modify `components/broker/BrokerConnectionCard.tsx`, `app/brokers/page.tsx`; Delete `components/broker/BrokerConnectModal.tsx`; Test `components/broker/BrokerConnectionCard.test.tsx`

- [ ] Wizard on shared `Modal`, internal state `step: "broker" | "configure" | "activate"`, `account: BrokerAccount | null`.
  - Step broker: capability cards (from `/brokers/capabilities` prop) with adapter-status pill, auth model line.
  - Step configure: label; non-paper → credential_ref + copyable env snippet (`{REF}_API_KEY=…` per broker), groww/breeze token note.
  - Step activate: rows Create ✓ → (zerodha: Connect button = login redirect; groww/breeze: token input → PUT session-token; paper: auto Connect) → Verify button (POST verify, show per-check result). Done closes + `reload()`.
- [ ] `SessionTokenModal` re-exported from wizard file on shared `Modal` (card-level Set token still works).
- [ ] `BrokerConnectionCard`: header (label, broker, env+status pills), status_message line, credential checklist (env keys / token configured), verify+sync timestamps, action groups; keep `AccountAction` union unchanged.
- [ ] Brokers page: toasts replace notice strings; `?connected=` triggers success toast suggesting Verify.
- [ ] Update card test for new markup (same behavioral asserts: actions fire, busy disables). `pnpm --filter web test` green. Commit.

### Task 6: AI backend foundation — multi-provider settings + LLM abstraction

**Files:** Modify `apps/api/pyproject.toml`, `app/core/config.py`, root `.env.example`; Create `app/services/ai/__init__.py`, `app/services/ai/llm.py`, `app/api/routes/ai.py`; Modify `app/api/router.py`

- [ ] Add `"anthropic>=0.40"` and `"openai>=1.50"` to dependencies.
- [ ] Settings: `anthropic_api_key: str | None = None`, `ai_model: str = "claude-opus-4-8"` (env fallback when no ai_settings row). `.env.example`: `ANTHROPIC_API_KEY=`, `AI_MODEL=claude-opus-4-8` under a `── AI ──` section, noting the in-app settings override.
- [ ] `ai_settings` table (in Task 7 migration): user_id unique FK, provider String(16), model String(128), base_url String(255) nullable, credentials_enc Text (Fernet JSON), updated_at.
- [ ] `llm.py`: `LLMReply` (pydantic: `text: str`, `tool_calls: list[LLMToolCall {id, name, args}]`, `raw_blocks: list | None` for anthropic continuation), `LLMClient` protocol with `async chat(system, messages, tools) -> LLMReply` and `async generate_json(system, prompt, schema) -> dict`; `AnthropicLLM` (AsyncAnthropic or AsyncAnthropicBedrock when provider=bedrock; adaptive thinking for anthropic provider; structured output via `output_config={"format": {"type": "json_schema", "schema": …}}`) and `OpenAILLM` (AsyncOpenAI with base_url; tools mapped to function-calling; `response_format={"type": "json_schema", …}`); `async resolve_llm(db, user_id) -> LLMClient | None` — reads ai_settings row, decrypts credentials, falls back to env `ANTHROPIC_API_KEY`.
- [ ] `routes/ai.py`: `GET /ai/status` → `{configured, provider, model}`; `GET /ai/settings` (no secrets, `configured` flag); `PUT /ai/settings {provider, model, base_url?, api_key?, aws_access_key_id?, aws_secret_access_key?, region?}` — validates provider, encrypts credential JSON via `core/security`, upserts row; `POST /ai/settings/test` — one tiny `generate_json`/`chat` round-trip, returns `{ok, error?}`; `require_ai` helper → 503 when `resolve_llm` returns None. Register router.
- [ ] Commit.

### Task 7: ai_proposals table + enums

**Files:** Modify `app/db/models.py`, `app/domain/enums.py`; Create `alembic/versions/0004_ai_proposals.py`

- [ ] Enums: add `AI_PROPOSAL = "AI_PROPOSAL"`, `AI_DECISION = "AI_DECISION"` to `AuditEventType`; new `class AIProposalStatus(StrEnum): PROPOSED, APPROVED, REJECTED`.
- [ ] Model:

```python
class AIProposal(Base):
    __tablename__ = "ai_proposals"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="CASCADE")
    )
    symbol: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(8), default="NSE")
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(8), default="MARKET")
    product: Mapped[str] = mapped_column(String(8), default="MIS")
    quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    rationale: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PROPOSED", index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] Model `AISettings`: user_id unique FK, provider String(16), model String(128), base_url String(255) nullable, credentials_enc Text nullable, updated_at.
- [ ] Migration 0004 mirroring 0003 style — creates BOTH `ai_proposals` and `ai_settings` (revision chain 0003→0004).
- [ ] Commit.

### Task 8: AI read-only tools + propose_trade (TDD)

**Files:** Create `app/services/ai/tools.py`, `tests/test_ai_proposals.py` (first test)

- [ ] Failing test: `run_tool("propose_trade", …)` inserts PROPOSED row + AI_PROPOSAL audit event; `run_tool("get_positions")` returns serialized list.
- [ ] Implement `TOOLS: list[dict]` (JSON schemas) for `get_positions`, `get_orders` (last 20), `get_funds` (latest snapshot per account), `get_quotes {symbols}` (via `market_sim.get_price`), `get_strategies`, `get_risk_rules`, `propose_trade {symbol, exchange?, side, quantity, order_type?, product?, limit_price?, rationale}`; `async def run_tool(db, redis, user_id, account, name, args) -> str` returning JSON strings; Decimals via `str()`. `propose_trade` validates side/qty>0, inserts `AIProposal`, emits audit, returns `{"proposal_id": …, "status": "PROPOSED"}`.
- [ ] `pytest tests/test_ai_proposals.py -v` green. Commit.

### Task 9: Analyst loop + proposal endpoints (TDD)

**Files:** Create `app/services/ai/analyst.py`; Modify `app/api/routes/ai.py`; Test `tests/test_ai_proposals.py`

- [ ] Failing tests: approve on PROPOSED → calls `order_service.place_order` (monkeypatched), links order, sets APPROVED + decided_at, emits USER_ACTION audit; approve on APPROVED → 409-equivalent error; reject sets REJECTED.
- [ ] `analyst.py`: manual tool-use loop —

```python
async def chat(db, redis, user, account, messages: list[dict]) -> dict:
    client = resolve_llm(db, user.id)
    convo = list(messages)
    proposals: list[uuid.UUID] = []
    for _ in range(8):  # tool-round cap
        resp = await client.messages.create(
            model=get_settings().ai_model, max_tokens=4096,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT, tools=TOOLS, messages=convo)
        convo.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            break
        results = []
        for tu in tool_uses:
            out = await run_tool(db, redis, user.id, account, tu.name, tu.input)
            if tu.name == "propose_trade":
                proposals.append(json.loads(out)["proposal_id"])
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
        convo.append({"role": "user", "content": results})
    return {"messages": convo[len(messages):], "proposal_ids": proposals}
```

  SYSTEM_PROMPT: paper-trading analyst for Indian markets; data via tools only; trades only via propose_trade; proposals require human approval; be concise.
- [ ] Endpoints in `routes/ai.py`: `POST /analyst/chat {account_id, messages}` (require_ai, 502 on `anthropic.APIError`), `GET /proposals?status=`, `POST /proposals/{id}/approve` (status guard → place_order with `client_order_id=f"ai-{p.id.hex[:18]}"` → link/audit), `POST /proposals/{id}/reject`.
- [ ] `pytest tests/ -v` green. Commit.

### Task 10: Strategy generator (TDD)

**Files:** Create `app/services/ai/generator.py`, `tests/test_ai_generator.py`; Modify `app/api/routes/ai.py`

- [ ] Failing test: mocked client returning valid JSON → draft dict; invalid kind → `ValueError`.
- [ ] `generator.py`: schema `{name, kind: enum(list(STRATEGY_REGISTRY)), symbols: [str], params: object, rationale}`; call `client.messages.create(..., output_config={"format": {"type": "json_schema", "schema": SCHEMA}})`, parse first text block JSON, re-validate kind ∈ registry, qty/params sanity.
- [ ] `POST /ai/strategies/generate {prompt}` → draft (no persist).
- [ ] Tests green. Commit.

### Task 11: Async strategy base + ai_agent (TDD)

**Files:** Modify `app/engines/strategy/base.py`, `app/engines/strategy/runner.py`, `app/api/routes/strategies.py`; Create `app/engines/strategy/ai_agent.py`, `tests/test_ai_agent.py`

- [ ] Failing tests: decision→signal mapping (BUY flat→ENTRY_LONG, SELL long→EXIT_LONG, SELL flat→ENTRY_SHORT, BUY short→EXIT_SHORT, HOLD→[]); unconfigured → []; interval guard via fakeredis; day cap.
- [ ] `base.py`: add

```python
class AsyncStrategyBase(StrategyBase):
    async def evaluate_async(self, ctx, redis) -> list[Signal]: ...
    def evaluate(self, ctx): raise NotImplementedError("async strategy")
```

- [ ] `ai_agent.py`: kind `"ai_agent"`, `min_history` = `params.get("min_history", 20)`; guards (configured, redis `ai_agent:last:{id}` interval default 300s, `ai_agent:count:{id}:{date}` cap default 10); structured-output decision call (prices tail, position, `params.instructions`); map per table; emit `AI_DECISION` audit for every decision incl. HOLD (strategy id available via ctx.params injection of `_strategy_id` from runner — runner passes it).
- [ ] `runner.py`: register `AiAgent()`; evaluation branch `signals = await impl.evaluate_async(ctx, redis) if isinstance(impl, AsyncStrategyBase) else impl.evaluate(ctx)`; inject `params={**strategy.params, "_strategy_id": str(strategy.id), "_user_id": str(strategy.user_id)}` into ctx.
- [ ] `strategies.py` start route: reject `kind == "ai_agent"` when account env != paper or AI unconfigured (400/503).
- [ ] `pytest` green. Commit.

### Task 12: /ai page (chat + proposals + setup)

**Files:** Create `app/ai/page.tsx`, `components/ai/ChatPanel.tsx`, `components/ai/ProposalCard.tsx`; Modify `lib/types.ts`

- [ ] Types: `AIStatus {configured, model}`, `AIProposal {…fields…}`, `ChatMessage {role, content}` (content blocks typed loosely).
- [ ] `components/ai/AISettingsCard.tsx`: provider select (Anthropic Claude / OpenAI / OpenRouter / Amazon Bedrock), model input with per-provider placeholder, write-only API-key password field (shows "configured" pill when set), base URL field (openrouter/custom, prefilled `https://openrouter.ai/api/v1`), AWS access key/secret/region fields when bedrock, Test + Save buttons → PUT `/ai/settings`, POST `/ai/settings/test`, toasts.
- [ ] Page: `useApi("/ai/status")`; unconfigured → settings card front-and-center; configured → settings collapsible + two-column: `ChatPanel` (account selector from `/brokers/accounts`, transcript state client-side, POST `/ai/analyst/chat`, render text blocks, inline `ProposalCard` for returned proposal ids) + pending proposals list (`/ai/proposals?status=PROPOSED`, 10s poll) with Approve/Reject → toasts.
- [ ] `ProposalCard`: symbol/side/qty/type/rationale, status pill, Approve (primary) / Reject (ghost), busy states.
- [ ] Build green. Commit.

### Task 13: Strategies page restyle + Generate with AI

**Files:** Modify `app/strategies/page.tsx`; Create `components/ai/GenerateStrategyModal.tsx`

- [ ] Restyle cards (kit), `PageHeader` action: "Generate with AI" (disabled w/ tooltip when `/ai/status` unconfigured).
- [ ] Modal: prompt textarea + account select → POST `/ai/strategies/generate` → preview (name, kind pill, symbols, params JSON, rationale) → Create calls existing `POST /strategies` → toast + reload.
- [ ] Build + tests green. Commit.

### Task 14: Final verification

- [ ] `cd apps/api && python -m pytest tests/ -v` — all green.
- [ ] `pnpm --filter web test` and `pnpm --filter web build` — green.
- [ ] `ruff check apps/api/app` clean.
- [ ] README: add AI section blurb + env vars; ROADMAP: note AI phase shipped (paper-only).
- [ ] Final commit.
