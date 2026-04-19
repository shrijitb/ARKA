# Roadmap: Arka Stabilization & Hardening

## Overview

This milestone hardens the Arka trading system before Phase 3 live trading begins. Working from the existing paper-trading baseline (120+ tests, 0 failures), we fix critical bugs that corrupt production state, add the missing dashboard endpoint, implement safety rail test coverage, close security gaps, expand test coverage to 150+, remediate fragile patterns, and validate stability through a 24–48 hour paper trading stress test. Every phase delivers a verifiable, independently testable result.

## Phases

- [x] **Phase 1: Critical Bug Fixes** - Eliminate production state corruption, config inconsistency, and performance blockers in the hypervisor core
- [x] **Phase 2: Missing Critical Features** - Implement the missing dashboard state endpoint, Telegram auth guard, and resolve the arbitrader status ambiguity
- [x] **Phase 3: Safety Rails Coverage** - Achieve full test coverage for margin_reserve, expiry_guard, circuit_breaker, audit, and auth key exposure
- [x] **Phase 4: Security Hardening** - Restrict CORS origins, make credential writes atomic, and replace the Docker socket mount
- [x] **Phase 5: Test Coverage Expansion** - Reach 150+ passing tests covering DB layer, setup endpoints, HMM label consistency, health-check pollution, and arbitrader contract
- [x] **Phase 6: Code Quality & Fragile Pattern Remediation** - Fix nautilus data feed isolation, gate Phase 3 stubs, ship HMM model in Docker image, pin hmmlearn, and guard yfinance data quality
- [ ] **Phase 7: Stress Test & Sign-Off** - Run 24–48 hour paper trading stress test and produce a documented pass/fail verdict

## Phase Details

### Phase 1: Critical Bug Fixes
**Goal**: The hypervisor runs correctly — no state pollution from health checks, consistent configuration defaults, no import-time crashes, and the main cycle executes in under 5 seconds
**Depends on**: Nothing (first phase)
**Requirements**: BUG-01, BUG-02, BUG-03, BUG-04, BUG-05, BUG-06, BUG-07, BUG-08
**Success Criteria** (what must be TRUE):
  1. A hypervisor started with no `.env` file cycles every 60 seconds (not 3600), and `validate_config()` does not raise `SystemExit` at import time
  2. Calling `GET /health/locks` does not add a `"test_worker"` entry to `worker_pnl` — subsequent `/status` responses show no phantom $100 PnL
  3. Calling `GET /health/persistence` does not create a `regime="TEST"` row in `data/arka.db`
  4. Adding `BRK.B`, `BTC-USDT-SWAP`, and `GC=F` to the watchlist succeeds with HTTP 200 (not HTTP 400)
  5. `_pull_worker_status()` completes concurrently; worst-case cycle time with 4 workers is under 15 seconds, and GDELT scoring does not block the main cycle
**Plans**: TBD

### Phase 2: Missing Critical Features
**Goal**: The dashboard shows live data, the Telegram bot rejects commands when auth is unset, and the arbitrader's deployment status is unambiguous
**Depends on**: Phase 1
**Requirements**: FEAT-01, FEAT-02, FEAT-03
**Success Criteria** (what must be TRUE):
  1. `GET /api/dashboard/state` returns HTTP 200 with all required top-level keys (regime, conflict_score, risk, portfolio, workers, domain_signals, timeline, thesis, backtest, system)
  2. Dashboard panels (RegimeMood, RiskMeter, WorkerStory, MoneyFlow) display live data instead of loading/error state
  3. A Telegram bot deployed without `TELEGRAM_ALLOWED_USER_ID` set rejects all `/pause`, `/resume`, and portfolio commands with a logged warning rather than executing them
  4. The arbitrader's status is documented unambiguously: either it appears in `docker-compose.yml` and `WORKER_REGISTRY`, or CLAUDE.md removes the port 8004 health check command
**UI hint**: yes
**Plans**: TBD

### Phase 3: Safety Rails Coverage
**Goal**: Every safety mechanism (margin reserve, expiry guard, circuit breaker, audit logging, API key exposure) has test coverage and is confirmed to be wired into the production cycle
**Depends on**: Phase 2
**Requirements**: SAFE-01, SAFE-02, SAFE-03, SAFE-04, SAFE-05
**Success Criteria** (what must be TRUE):
  1. `tests/test_safety_rails.py` passes with tests covering margin reserve calculation, breach detection, and recovery; expiry guard near-expiry detection, physical delivery prevention, and forced close logic
  2. `tests/test_concurrency.py` passes with tests covering circuit breaker state transitions (CLOSED → OPEN → HALF_OPEN), failure threshold triggering, and reset behavior for both `hypervisor/circuit_breaker.py` and `data/feeds/circuit_breaker.py`
  3. Audit calls for regime change, capital allocation, worker pause/resume, and profit sweep are confirmed active (not dead code) via grep and/or test assertion
  4. `/setup/status` does not expose the master API key once `SETUP_COMPLETE=true` is set
**Plans**: TBD

### Phase 4: Security Hardening
**Goal**: The hypervisor's attack surface is reduced: CORS is restricted to known origins, `.env` writes survive power loss, and the Docker socket is not accessible inside the hypervisor container
**Depends on**: Phase 3
**Requirements**: SEC-01, SEC-02, SEC-03
**Success Criteria** (what must be TRUE):
  1. A browser request from an arbitrary origin (e.g., `http://evil.example.com`) to the hypervisor is blocked by CORS policy
  2. A simulated power-loss mid-write to `.env` (kill during write) leaves the previous valid `.env` intact — no partial/corrupt file on restart
  3. The hypervisor container no longer has `/var/run/docker.sock` mounted directly; container restarts triggered by the setup wizard go through a scoped proxy or alternative mechanism
**Plans**: TBD

### Phase 5: Test Coverage Expansion
**Goal**: Test count reaches 150+ passing with 0 new failures; HMM label ordering, health-check state pollution, database layer, setup endpoints, and arbitrader contract are all covered
**Depends on**: Phase 4
**Requirements**: COVER-01, COVER-02, COVER-03, COVER-04, COVER-05, COVER-06, COVER-07, COVER-08
**Success Criteria** (what must be TRUE):
  1. `~/.venv/bin/python -m pytest tests/ -v` reports 150 or more passed with 0 failures
  2. A test asserts that `STATE_LABELS` in `hmm_model.py` and `HMM_STATE_LABELS` in `capital.py` have identical ordering — the test fails if they diverge
  3. Tests confirm that calling `GET /health/locks` and `GET /health/persistence` in sequence leaves no `"test_worker"` entry in `worker_pnl` and no `regime="TEST"` row in the database
  4. `hypervisor/db/` repository layer has integration tests covering regime logging, portfolio writes, and read-back
  5. `hypervisor/di_container.py` `Hypervisor` class is either wired into `main.py` with a test, or deleted — it is not dead code
**Plans**: TBD

### Phase 6: Code Quality & Fragile Pattern Remediation
**Goal**: Nautilus strategies consume live OKX feed data (not synthetic fallback), Phase 3 stub messages are silenced, the HMM model ships inside the Docker image, and critical dependencies are version-pinned
**Depends on**: Phase 5
**Requirements**: QUAL-01, QUAL-02, QUAL-03, QUAL-04, QUAL-05
**Success Criteria** (what must be TRUE):
  1. `workers/nautilus/strategies/funding_arb.py`, `order_flow.py`, and `factor_model.py` successfully import from `data.feeds` inside the running container — the `except ImportError` fallback is not triggered during a live cycle
  2. The quarterly profit sweep Telegram message no longer contains `# PHASE 3:` advisory text; Phase 3 stub paths are gated behind `PHASE3_ENABLED=false` (default)
  3. A `docker compose build hypervisor` produces an image that starts without triggering HMM bootstrap training (model loads from the committed `.pkl` in under 30 seconds)
  4. `hmmlearn` is pinned to an exact version in `requirements.txt`; a test loads the committed `hmm_4state.pkl` and asserts it deserializes without error
  5. `hypervisor/regime/feature_pipeline.py` raises a clear error (not a silent NaN) when yfinance returns implausible close prices; `yfinance` version is pinned in `requirements.txt`
**Plans**: TBD

### Phase 7: Stress Test & Sign-Off
**Goal**: Arka runs continuously for 24–48 hours in paper trading mode without crashes, OOM, silent failures, or PnL drift — and the result is documented as a pass/fail verdict
**Depends on**: Phase 6
**Requirements**: STRESS-01, STRESS-02
**Success Criteria** (what must be TRUE):
  1. The hypervisor runs for 24 hours minimum with all workers healthy, regime cycles completing, no unhandled exceptions in logs, and no PnL drift attributable to health-check state pollution
  2. A written stress test report exists at `.planning/STRESS-TEST-REPORT.md` documenting: start/end timestamps, regime states observed, any anomalies found, final pass/fail verdict — signed off before Phase 3 work begins
**Plans**: TBD

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Critical Bug Fixes | Done | Complete | ✅ |
| 2. Missing Critical Features | Done | Complete | ✅ |
| 3. Safety Rails Coverage | Done | Complete | ✅ |
| 4. Security Hardening | Done | Complete | ✅ |
| 5. Test Coverage Expansion | 38 tests | Complete | ✅ |
| 6. Code Quality & Fragile Patterns | 18 tests | Complete | ✅ |
| 7. Stress Test & Sign-Off | 0/TBD | Not started | - |
