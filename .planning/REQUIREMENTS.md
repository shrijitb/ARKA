# Requirements: Arka Stabilization & Hardening

**Defined:** 2026-04-15
**Core Value:** No silent failure reaches Phase 3 — every bug, race condition, and untested safety path is found and fixed before live trading begins.

---

## v1 Requirements

### Bugs — Critical Defects

- [x] **BUG-01**: `CYCLE_INTERVAL_SEC` default in `hypervisor/main.py` (line 95) is `3600`; `validate_config()` defaults to `"60"` and enforces `10–600`. These must be aligned to a single consistent default so any environment without `.env` behaves as documented (60s).
- [x] **BUG-02**: `GET /health/locks` writes a `"test_worker"` entry with `+$100 PnL` into live `HypervisorState.worker_pnl`. This persists across requests and inflates reported PnL in `/status` and `/dashboard/state`. The side-effecting lock test must be removed or isolated.
- [x] **BUG-03**: `GET /health/persistence` calls `await repo.log_regime("TEST", {}, False)` against the production database on every health check. This creates `regime="TEST"` rows in `data/arka.db`. Must use a rollback transaction or remove the write entirely.
- [x] **BUG-04**: `validate_config()` is called at module import time (`hypervisor/main.py` line 1012) and raises `SystemExit` if env vars are missing. Any tool or test that imports `hypervisor.main` without a full `.env` crashes immediately. Must be moved into the FastAPI `lifespan` context.
- [x] **BUG-05**: Watchlist endpoint rejects any ticker containing `.`, `-`, `=`, or `/` (e.g., `BRK.B`, `BTC-USDT-SWAP`, `GC=F`) due to `isalnum()` check. Validation must allow `[A-Z0-9.=/-]` up to 20 characters.
- [x] **BUG-06**: `_pull_worker_status()` in `hypervisor/main.py` polls 4 workers sequentially — worst-case 40 seconds per cycle. Must be refactored to `asyncio.gather()` matching the pattern in `_check_worker_health()`.
- [x] **BUG-07**: GDELT conflict index sleeps 3.5s × 3 queries = 10.5s synchronously in the main cycle. Must be moved to a background cached task so the main cycle reads the last cached score.
- [x] **BUG-08**: Deferred imports `from hypervisor.allocator.capital import HMM_STATE_LABELS` and `import numpy as np` inside `_run_cycle()` (called every cycle). Must be moved to module-level imports.

### Missing Critical Features

- [x] **FEAT-01**: `/api/dashboard/state` endpoint does not exist in `hypervisor/main.py`. The dashboard polls this every 10 seconds and receives HTTP 404. All live dashboard panels are non-functional. The endpoint must be implemented per the schema in CLAUDE.md section 15.
- [x] **FEAT-02**: Telegram bot does not validate `TELEGRAM_ALLOWED_USER_ID` before processing `/pause`, `/resume`, or portfolio commands when the env var is unset. Must fail-safe: if unset, reject all commands with a logged warning.
- [x] **FEAT-03**: Arbitrader worker (`workers/arbitrader/`) has a complete REST contract and Dockerfile but is absent from `docker-compose.yml` and `WORKER_REGISTRY`. Must either be formally registered as a worker or explicitly documented as Phase 3 only (and CLAUDE.md health check on port 8004 corrected).

### Safety Rails

- [x] **SAFE-01**: `hypervisor/risk/margin_reserve.py` has no test coverage. `tests/test_safety_rails.py` must exercise: reserve calculation, breach detection, and recovery paths.
- [x] **SAFE-02**: `hypervisor/risk/expiry_guard.py` has no test coverage. `tests/test_safety_rails.py` must exercise: near-expiry detection, physical delivery prevention, and forced position close logic.
- [x] **SAFE-03**: `data/feeds/circuit_breaker.py` and `hypervisor/circuit_breaker.py` have no test coverage. `tests/test_concurrency.py` must exercise: state transitions (CLOSED → OPEN → HALF_OPEN), failure threshold triggering, and reset behavior.
- [x] **SAFE-04**: `hypervisor/audit.py` must be verified as actively called for all state-changing events (regime change, capital allocation, worker pause/resume, profit sweep). Audit calls that are dead code must be wired in.
- [x] **SAFE-05**: `hypervisor/auth.py` API key exposure: `/setup/status` is unauthenticated and returns the master API key in plain JSON. Must restrict key disclosure to pre-setup state only (before `SETUP_COMPLETE=true`), or require a one-time setup token.

### Security

- [x] **SEC-01**: CORS `allow_origins=["*"]` in `hypervisor/main.py` (line 362). Must be restricted to `["http://localhost:3000", "http://arka-dashboard:3000"]` (or LAN IP for Pi deployment).
- [x] **SEC-02**: `save_credentials()` uses non-atomic `Path.write_text()` — a power loss mid-write corrupts `.env`. Must be replaced with write-to-temp + `os.replace()` (POSIX atomic rename).
- [x] **SEC-03**: Docker socket (`/var/run/docker.sock`) is bind-mounted into the hypervisor container, granting full host root access. Must be replaced with a tightly scoped restart mechanism (docker-socket-proxy or equivalent).

### Test Coverage

- [x] **COVER-01**: HMM state label ordering must be tested for consistency between `hypervisor/regime/hmm_model.py` (`STATE_LABELS`) and `hypervisor/allocator/capital.py` (`HMM_STATE_LABELS`). A mismatch silently routes capital to wrong regimes.
- [x] **COVER-02**: `/health/locks` state pollution must be tested: calling the endpoint must not leave `"test_worker"` in `worker_pnl` or affect subsequent capital reconciliation results.
- [x] **COVER-03**: `/health/persistence` database pollution must be tested: calling the endpoint must not create `regime="TEST"` rows in `data/arka.db`.
- [x] **COVER-04**: `hypervisor/db/` repository layer must have integration tests covering regime logging, portfolio writes, and read-back.
- [x] **COVER-05**: Dashboard setup endpoints (`/setup/credentials`, `/setup/status`, `/system/hardware`) must have tests covering credential write atomicity, partial updates, and container restart behavior.
- [x] **COVER-06**: `hypervisor/di_container.py` `Hypervisor` class must either be wired into `main.py` and tested, or deleted. Dead code that duplicates production logic is not acceptable.
- [x] **COVER-07**: `workers/arbitrader/sidecar/main.py` paper arb simulator and REST contract must have at least smoke-test coverage.
- [x] **COVER-08**: Overall test count must reach 150+ passing with 0 new failures. All new safety/concurrency test files (`test_safety_rails.py`, `test_concurrency.py`) must contribute substantively.

### Code Quality & Fragile Patterns

- [ ] **QUAL-01**: `data/feeds/` is unavailable inside the nautilus container — `funding_arb.py`, `order_flow.py`, and `factor_model.py` silently fall back to synthetic data. Build context must be extended to include `data/feeds/` or feed modules vendored into `workers/nautilus/`.
- [ ] **QUAL-02**: Phase 3 stubs (`# PHASE 3:` comments in `workers/core_dividends/worker_api.py` and `hypervisor/main.py`) produce confusing Telegram messages advertising features that don't exist. Must be gated behind `PHASE3_ENABLED` env flag or advisory text removed from production messages.
- [ ] **QUAL-03**: HMM model `.pkl` is committed to git but the Dockerfile may not `COPY` the `model_state/` directory, causing 3–5 minute bootstrap on first run. Must verify the Dockerfile ships the model and add a Docker build test.
- [ ] **QUAL-04**: `hmmlearn` is not pinned to an exact version in `requirements.txt`. A version bump can silently corrupt the `.pkl` and cause silent fallback to bootstrap training. Must pin `hmmlearn==<current>` and add a model load test.
- [ ] **QUAL-05**: `yfinance` MultiIndex column behavior is worked around in `market_data.py` but not guarded in `hypervisor/regime/feature_pipeline.py`. Must add a yfinance version pin and data-quality assertions (plausible close price range) to `feature_pipeline.py`.

### Stress Test

- [ ] **STRESS-01**: Paper trading must run continuously for 24–48 hours without crashes, OOM, or silent failures. Log monitoring must confirm: regime cycles completing, worker health returning OK, no unhandled exceptions, no PnL drift from health check pollution.
- [ ] **STRESS-02**: Stress test run must be documented with timestamps, regime states observed, any anomalies found, and a pass/fail verdict before Phase 3 work begins.

---

## v2 Requirements

### Scaling

- **SCALE-01**: Migrate `HypervisorState` volatile state (worker_pnl, allocations, worker_health) to SQLite so it survives hypervisor restarts
- **SCALE-02**: Evaluate PostgreSQL migration path for sub-minute cycle intervals or multi-hypervisor deployment
- **SCALE-03**: Add model format versioning to the HMM pickle wrapper for safe `hmmlearn` upgrades

### Observability

- **OBS-01**: Structured log aggregation — ship hypervisor logs to a persistent store (currently stdout-only)
- **OBS-02**: Prometheus metrics for regime classification latency, worker poll latency, and PnL deltas

---

## Out of Scope

| Feature | Reason |
|---------|--------|
| `workers/stocksharp/` changes | Phase 3 only — IBKR wiring not started |
| Dashboard UI (`dashboard/`) | F-10 complete — no frontend changes this milestone |
| Live trading enablement | `PAPER_TRADING = True` stays until stress test passes |
| New trading strategies | Hardening only — no net-new functionality |
| Binance / Bybit integration | Geo-blocked from this region permanently |
| Polymarket live execution | Requires `POLY_PRIVATE_KEY` — Phase 3 |

---

## Traceability

*Populated by roadmapper agent — 2026-04-15*

| Requirement | Phase | Status |
|-------------|-------|--------|
| BUG-01 | Phase 1 | Pending |
| BUG-02 | Phase 1 | Pending |
| BUG-03 | Phase 1 | Pending |
| BUG-04 | Phase 1 | Pending |
| BUG-05 | Phase 1 | Pending |
| BUG-06 | Phase 1 | Pending |
| BUG-07 | Phase 1 | Pending |
| BUG-08 | Phase 1 | Pending |
| FEAT-01 | Phase 2 | Pending |
| FEAT-02 | Phase 2 | Pending |
| FEAT-03 | Phase 2 | Pending |
| SAFE-01 | Phase 3 | Pending |
| SAFE-02 | Phase 3 | Pending |
| SAFE-03 | Phase 3 | Pending |
| SAFE-04 | Phase 3 | Pending |
| SAFE-05 | Phase 3 | Pending |
| SEC-01 | Phase 4 | Pending |
| SEC-02 | Phase 4 | Pending |
| SEC-03 | Phase 4 | Pending |
| COVER-01 | Phase 5 | Pending |
| COVER-02 | Phase 5 | Pending |
| COVER-03 | Phase 5 | Pending |
| COVER-04 | Phase 5 | Pending |
| COVER-05 | Phase 5 | Pending |
| COVER-06 | Phase 5 | Pending |
| COVER-07 | Phase 5 | Pending |
| COVER-08 | Phase 5 | Pending |
| QUAL-01 | Phase 6 | Pending |
| QUAL-02 | Phase 6 | Pending |
| QUAL-03 | Phase 6 | Pending |
| QUAL-04 | Phase 6 | Pending |
| QUAL-05 | Phase 6 | Pending |
| STRESS-01 | Phase 7 | Pending |
| STRESS-02 | Phase 7 | Pending |

**Coverage:**
- v1 requirements: 34 total
- Mapped to phases: 34/34
- Unmapped: 0 ✓

---
*Requirements defined: 2026-04-15*
*Last updated: 2026-04-15 — traceability populated by roadmapper*
