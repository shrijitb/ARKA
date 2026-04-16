# Architecture

**Analysis Date:** 2026-04-15

## Pattern Overview

**Overall:** Regime-Aware Multi-Agent Orchestration over Docker Microservices

**Key Characteristics:**
- A central FastAPI hypervisor orchestrates 4 independent trading worker containers via HTTP REST
- Market regime is classified every cycle using a 4-state Gaussian HMM; regime drives capital allocation weights
- Workers are isolated behind a standard REST contract — the hypervisor never calls into worker internals
- All state mutations are asynchronously locked (asyncio.Lock) and persisted to SQLite via ArkaRepository
- OSINT data feeds (GDELT, ACLED, AIS, NASA, EDGAR) feed a conflict index and domain router that modify allocations

## Layers

**Orchestration Layer:**
- Purpose: Drive the classification→risk→allocation→execution cycle every `CYCLE_INTERVAL_SEC`
- Location: `hypervisor/main.py`
- Contains: `HypervisorState`, `orchestration_loop()`, `_run_cycle()`, worker HTTP communication helpers, setup wizard endpoints, quarterly profit sweep cron
- Depends on: `RegimeClassifier`, `RegimeAllocator`, `RiskManager`, `ArkaRepository`, `CircuitBreaker`
- Used by: Dashboard (REST), Telegram bot, tests

**Regime Classification Layer:**
- Purpose: Classify market into one of 4 states (RISK_ON, RISK_OFF, CRISIS, TRANSITION)
- Location: `hypervisor/regime/`
- Contains:
  - `classifier.py` — `RegimeClassifier` entry point; manages HMM lifecycle and context window
  - `feature_pipeline.py` — fetches 6-feature vector (VIX, yield curve, HY OAS, NFCI, equity momentum) from yfinance + FRED
  - `hmm_model.py` — `RegimeHMM` wrapping hmmlearn's GaussianHMM
  - `circuit_breakers.py` — overrides HMM posterior probabilities when hard thresholds are breached (VIX spike, war score)
  - `model_state/hmm_4state.pkl` — persisted model weights
- Depends on: `data/feeds/market_data.py`, `data/feeds/conflict_index.py`
- Used by: `hypervisor/main.py` (`classifier.classify_sync()` called via `asyncio.to_thread`)

**Capital Allocation Layer:**
- Purpose: Translate regime probabilities into dollar allocations per worker
- Location: `hypervisor/allocator/capital.py`
- Contains: `RegimeAllocator.compute()`, `blend_allocations()` (probability-weighted blending across 4 HMM states), `ALLOCATION_PROFILES`, `HMM_STATE_MAX_DEPLOY`, turnover filter
- Depends on: numpy (blending math)
- Used by: `hypervisor/main.py` (step 5 of each cycle)

**Risk Management Layer:**
- Purpose: Portfolio-level guardrails that can halt or trim workers independently of regime
- Location: `hypervisor/risk/manager.py`, `hypervisor/risk/margin_reserve.py`, `hypervisor/risk/expiry_guard.py`
- Contains: `RiskManager.assess()` (drawdown, per-worker cap, open positions, free capital floor, PnL floor checks), `RiskVerdict`, `WorkerRiskState`
- Depends on: `MarginReserveManager`, `ExpiryGuard`
- Used by: `hypervisor/main.py` (step 4 of each cycle, before allocation)

**Worker Layer:**
- Purpose: Strategy execution, paper simulation, advisory signals
- Location: `workers/`
- Contains (4 active trading workers + sidecar + telegram bot):
  - `workers/nautilus/worker_api.py` (port 8001) — ADX-routed swing/range/day/funding/order-flow strategies
  - `workers/prediction_markets/worker_api.py` (port 8002) — Kalshi/Polymarket CLOB stub
  - `workers/analyst/worker_api.py` (port 8003) — phi3:mini via Ollama + SearXNG advisory
  - `workers/core_dividends/worker_api.py` (port 8006) — SCHD/VYM passive hold
  - `workers/arbitrader/sidecar/main.py` (port 8004) — statistical pair-arb (BTC/ETH spread)
  - `workers/telegram_bot/main.py` — polling Telegram bot, no REST port
- Depends on: Standard REST contract implemented by each; OKX via env vars
- Used by: Hypervisor (health + status polls, regime broadcast, allocation push)

**Data Feeds Layer:**
- Purpose: Provide market data, macro signals, and OSINT intelligence to classification and workers
- Location: `data/feeds/`
- Contains:
  - `market_data.py` — yfinance, FRED wrappers; UUP/BDRY proxies for DXY/BDI
  - `conflict_index.py` — 0–100 war premium score from ACLED + GDELT + market proxies
  - `domain_router.py` — `DomainRouter.evaluate()` produces `DomainDecision` list; feeds `apply_domain_overrides()` in allocator
  - `osint_processor.py` — aggregates GDELT, ACLED, AIS, NASA FIRMS events
  - `circuit_breaker.py` — `CircuitBreaker` class for wrapping external calls (CLOSED/OPEN/HALF_OPEN)
  - `funding_rates.py`, `order_book.py` — OKX perp data
  - `edgar_client.py`, `gdelt_client.py`, `maritime_client.py`, `environment_client.py`, `ucdp_client.py` — OSINT clients
- Depends on: External APIs (yfinance, FRED, OKX, ACLED, GDELT, NASA, AIS)
- Used by: `hypervisor/regime/classifier.py`, `workers/nautilus/`

**Persistence Layer:**
- Purpose: Audit trail and queryable history of regime changes, allocations, signals, orders
- Location: `hypervisor/db/`, `data/db/`
- Contains:
  - `hypervisor/db/engine.py` — async SQLAlchemy engine backed by aiosqlite; database at `data/arka.db`
  - `hypervisor/db/models.py` — SQLAlchemy ORM models (`RegimeLog`, `PortfolioState`, `Signal`, `Order`)
  - `hypervisor/db/repository.py` — `ArkaRepository` with all DB write/read operations
  - `data/db/schema.sql` — DDL; run once via `init_db()` on startup
  - `data/audit.jsonl` — rotating JSONL audit log written by `hypervisor/audit.py`
- Depends on: SQLAlchemy async + aiosqlite
- Used by: `hypervisor/main.py` after each cycle step

**Dashboard Layer:**
- Purpose: React single-page app providing visual monitoring and setup wizard
- Location: `dashboard/`
- Contains: React 18 + Vite 5 + Tailwind CSS v4 frontend, served by nginx on port 3000; proxies `/api/*` to `http://hypervisor:8000/`
- Depends on: Hypervisor REST API (`/api/dashboard/state`, `/api/setup/*`, `/api/pause`, `/api/resume`)
- Used by: End user; polls every 10 seconds via `hooks/useArkaData.js`

## Data Flow

**Primary Orchestration Cycle (every CYCLE_INTERVAL_SEC):**

1. `orchestration_loop()` in `hypervisor/main.py` calls `_run_cycle()`
2. `_check_worker_health()` — concurrent GET `/health` to all workers via httpx; unhealthy workers excluded from allocation
3. `_pull_worker_status(healthy)` — GET `/status` per healthy worker; updates `HypervisorState.worker_pnl`, `worker_sharpe`
4. `state.reconcile_capital()` — recomputes `total_capital` from PnL sum (paper mode holds it constant at `INITIAL_CAPITAL_USD`)
5. `classifier.classify_sync()` in thread pool — `FeaturePipeline.extract_current()` fetches live market data, `RegimeHMM.predict_proba()` returns 4-state posterior, `apply_circuit_breakers()` may override
6. `RiskManager.assess()` — checks 6 portfolio limits; returns `RiskVerdict`; may broadcast pause to workers
7. `allocator.compute(regime, worker_health, worker_sharpe, probabilities)` — probability-weighted blend of `ALLOCATION_PROFILES`; Sharpe gate halves weight for underperformers
8. `_broadcast_regime(healthy)` — POST `/regime` to each healthy worker
9. `_send_allocations(allocations)` — POST `/allocate` to each worker with `amount_usd > MIN_TRADE_SIZE_USD`
10. `repo.snapshot_portfolio()` — persist portfolio state to SQLite
11. Resume workers with positive allocation if not in `CRISIS` regime

**Regime Classification Sub-flow:**

1. `FeaturePipeline.extract_current()` fetches VIX, VIX3M, 10Y-2Y spread, HY OAS (FRED BAMLH0A0HYM2), NFCI, SPY 60-day momentum via yfinance/FRED
2. Features are z-scored using running stats; appended to rolling 252-bar context window
3. `RegimeHMM.predict_proba(context)` runs HMM forward-filter; returns 4-dim probability vector
4. `apply_circuit_breakers(probs, raw, war_score)` may hard-override probabilities when VIX > 40 or conflict index > 25
5. Argmax of modified probs → regime label + confidence; full probability dict passed to allocator

**Signal Flow (worker → hypervisor):**

1. Worker generates signal internally (strategy logic, ADX routing, funding rate threshold, etc.)
2. Hypervisor may pull via POST `/signal` (advisory) or push execute via POST `/execute`
3. All signals tagged `advisory_only=True` for `analyst` and `core_dividends` until Phase 3

**State Management:**

- `HypervisorState` is a single shared object protected by `asyncio.Lock`
- All mutations use locked helper methods (`update_worker_pnl`, `update_regime`, `update_allocations`)
- `get_snapshot()` returns an atomic copy of all mutable state for use in REST responses
- `DIContainer` (`hypervisor/di_container.py`) enables dependency injection for testing

## Key Abstractions

**RegimeClassifier (`hypervisor/regime/classifier.py`):**
- Purpose: Single entry point for market state; wraps HMM lifecycle
- Pattern: Loads persisted model on init; bootstraps and trains on first call if missing; retrained monthly via APScheduler
- Key method: `classify_sync()` → `RegimeResult` (regime, confidence, probabilities dict, circuit_breaker_active)

**RegimeAllocator (`hypervisor/allocator/capital.py`):**
- Purpose: Translate HMM posterior probabilities into USD allocations
- Pattern: `blend_allocations(probs)` computes weighted average of `ALLOCATION_PROFILES`; turnover filter (`_TURNOVER_THRESHOLD = 0.02`) suppresses rebalancing on tiny shifts
- Key method: `compute(regime, worker_health, worker_sharpe, probabilities)` → `AllocationResult`

**RiskManager (`hypervisor/risk/manager.py`):**
- Purpose: Binary gate before capital deployment; produces `RiskVerdict`
- Pattern: Stateful (`WorkerRiskState` per worker tracks peak capital); checks 6 limits in priority order; 1-hour cooldown on breach
- Key method: `assess(total_capital, free_capital, open_positions, worker_pnl, worker_allocated)` → `RiskVerdict`

**Worker REST Contract:**
- Purpose: Uniform interface across all trading workers; hypervisor treats all workers identically
- Pattern: Every trading worker exposes `GET /health`, `GET /status`, `GET /metrics`, `POST /regime`, `POST /allocate`, `POST /pause`, `POST /resume`, `POST /signal`, `POST /execute`
- Critical rule: `/metrics` must return `Response(content=text, media_type="text/plain")` — bare string return breaks Prometheus

**CircuitBreaker (`hypervisor/circuit_breaker.py`, `data/feeds/circuit_breaker.py`):**
- Purpose: Prevent cascading failures when external APIs (yfinance, FRED, OKX, GDELT) degrade
- Pattern: CLOSED → OPEN after `failure_threshold` consecutive failures; HALF_OPEN after `cooldown_seconds`; used by all external data fetches

**ArkaRepository (`hypervisor/db/repository.py`):**
- Purpose: All SQLite I/O; each method opens its own session (no session leaking to callers)
- Pattern: `async with self.session_factory() as session:` pattern throughout; models in `hypervisor/db/models.py`

## Entry Points

**Hypervisor (FastAPI app):**
- Location: `hypervisor/main.py` — `app = FastAPI(title="Arka Hypervisor", version="2.0.0", lifespan=lifespan)`
- Triggers: `uvicorn hypervisor.main:app --host 0.0.0.0 --port 8000` (Docker CMD) or Docker Compose service `hypervisor`
- Responsibilities: Starts orchestration loop, APScheduler (quarterly sweep + monthly retrain), SQLite init, API key middleware, all REST endpoints

**Orchestration Loop:**
- Location: `hypervisor/main.py` — `async def orchestration_loop()` spawned as `asyncio.create_task` in lifespan
- Triggers: Runs immediately on startup (after 5s boot delay), then every `CYCLE_INTERVAL_SEC`
- Responsibilities: Full 9-step cycle per iteration

**Worker Entry Points:**
- `workers/nautilus/worker_api.py` — `app = FastAPI()` port 8001; `lifespan` starts ArkaEngine and background rebalance loop
- `workers/prediction_markets/worker_api.py` — FastAPI port 8002
- `workers/analyst/worker_api.py` — FastAPI port 8003; connects to Ollama at `OLLAMA_HOST`
- `workers/core_dividends/worker_api.py` — FastAPI port 8006
- `workers/arbitrader/sidecar/main.py` — FastAPI port 8004; pair arb on BTC/ETH OKX spread
- `workers/telegram_bot/main.py` — polling bot; no inbound port; calls hypervisor `/status`, `/regime`, `/watchlist`

**Dashboard:**
- Location: `dashboard/src/main.jsx` — React root; `App.jsx` routes SetupWizard ↔ Dashboard based on `setupComplete` flag
- Triggers: nginx serves on port 3000; Vite dev server on `npm run dev`

## Error Handling

**Strategy:** Defensive — every external call is wrapped in try/except; failures degrade gracefully rather than crash

**Patterns:**
- Regime classification failure: holds last known regime at 80% of previous confidence (`_held_result()`); falls back to `TRANSITION` (uniform 0.25) if no history
- Worker health failure: worker is excluded from allocation for that cycle; logged as warning
- Worker API call failure (allocate/regime/pause): logged as warning; cycle continues for other workers
- Database failure on `init_db()`: logged as error, persistence disabled; system continues in stateless mode
- External data feed failure: `CircuitBreaker` opens after 3 consecutive failures; `_FALLBACKS` dict in `feature_pipeline.py` supplies default feature values

## Cross-Cutting Concerns

**Logging:** Python stdlib `logging` for operational logs; `structlog` for audit events. Audit events written to `data/audit.jsonl` via rotating handler. All state-changing events covered by named audit functions in `hypervisor/audit.py`.

**Authentication:** `APIKeyMiddleware` (`hypervisor/auth.py`) wraps all hypervisor endpoints except `/health`, `/metrics`, `/setup/status`, `/system/hardware`. API key auto-generated on first run, stored in `.env`, returned by `/setup/status` for dashboard bootstrap.

**Configuration:** Python constants in `config.py` (worker-internal); YAML files in `config/` (`regimes.yaml`, `settings.yaml`, `allocations.yaml`); runtime overrides via environment variables. Never hardcode localhost or IPs in inter-service URLs — use Docker DNS names via env var overrides.

**Paper Trading Guard:** `PAPER_TRADING = True` in `config.py` and `ARKA_LIVE` env var control all trade execution paths. All `advisory_only=True` signals are logged but never sent to `/execute` until Phase 3.

**Scheduling:** APScheduler `AsyncIOScheduler` in `hypervisor/main.py` lifespan: quarterly profit sweep (Jan/Apr/Jul/Oct 7th @ 09:00), monthly HMM retrain (1st of month @ 03:00).

---

*Architecture analysis: 2026-04-15*
