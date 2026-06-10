# Roadmap

## Phase A — harden the paper core (now)
- [ ] Integration tests for the order pipeline (testcontainers: pg + redis)
- [ ] Paper holdings model (T+1 settlement of CNC fills into holdings)
- [ ] Real charges engine: brokerage per broker plan, STT/CTT, exchange txn,
      SEBI, stamp duty, GST — make paper P&L honest
- [ ] Exchange holiday calendar for the market-hours guard
- [ ] Per-account paper cash as a proper ledger (credits/debits table) instead
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
- [ ] Candle aggregation (1m/5m) from ticks instead of raw tick SMA
- [ ] Strategy backtester over stored candles
- [ ] Strategy run lifecycle (strategy_runs populated, stats, error surfacing)
- [ ] Position sizing + per-strategy capital allocation
- [ ] More strategies (momentum, mean reversion) via the registry

## Phase E — production posture
- [ ] Real secret store (Vault / SOPS) instead of raw env
- [ ] Per-user encryption contexts; rotate Fernet keys
- [ ] Observability: Prometheus metrics, request tracing, alerting on risk HALTs
- [ ] Postgres backups, migration CI gate, blue-green deploy
- [ ] SEBI algo-trading compliance review before any multi-user live offering
