# Roadmap

## Shipped 2026-06: UI redesign + AI trading (paper-only)
- [x] Design system + component kit, broker connect wizard
- [x] AI analyst chat with human-approved proposals (multi-provider:
      Anthropic / OpenAI / OpenRouter / Bedrock, keys encrypted at rest)
- [x] NL strategy generator constrained to registered kinds
- [x] `ai_agent` strategy kind (interval + daily-cap guards, audited decisions)

## Phase A — harden the paper core (now)
- [x] Postgres integration tests for ledger, settlement, concurrent fills and isolation
- [ ] Full order-pipeline integration against real Redis (current tests use fakeredis)
- [x] Paper holdings model (T+1 settlement of CNC fills into holdings)
- [x] Equity charges engine: brokerage per broker plan, STT, exchange txn,
      SEBI, stamp duty, GST — make paper P&L honest
- [x] Exchange holiday calendar for the market-hours guard
- [x] Per-account paper cash as a proper ledger (credits/debits table) instead
      of latest-snapshot arithmetic

## Phase B — first real broker (Zerodha)
- [ ] Verify all Kite read endpoints against a live account
- [ ] Instruments sync job into market_instruments (+ symbol validation on orders)
- [ ] Kite WebSocket tick streaming -> replace sim feed for live symbols
- [ ] Postback checksum validation + order reconciliation
- [ ] Daily session-expiry handling job + UI re-login prompt
- [ ] Supervised 1-share live order test (see README playbook)
- [ ] Client-side rate limiting (token bucket per adapter, Kite ~3 rps)

## Phase C — Groww + Breeze
- [ ] Groww: verify endpoint paths, implement TOTP token mint, instruments CSV
- [ ] Breeze: verify endpoints, implement 100/min / 5000/day local throttle,
      socket.io streaming
- [ ] Per-broker symbol mapping table (same instrument, different tokens/codes)

## Phase D — strategy platform
- [x] Candle aggregation (1m/5m) from ticks instead of raw tick SMA
- [x] SMA crossover backtester over stored candles (next-open fills, saved results)
- [x] Historical NSE import via yfinance (daily/1m/5m)
- [ ] Strategy run lifecycle (strategy_runs populated, stats, error surfacing)
- [ ] Position sizing + per-strategy capital allocation
- [ ] More strategies (momentum, mean reversion) via the registry

## Phase E — production posture
- [ ] Real secret store (Vault / SOPS) instead of raw env
- [ ] Per-user encryption contexts; rotate Fernet keys
- [ ] Observability: Prometheus metrics, request tracing, alerting on risk HALTs
- [ ] Postgres backups, migration CI gate, blue-green deploy
- [ ] SEBI algo-trading compliance review before any multi-user live offering
