# Codebase Concerns

**Analysis Date:** 2026-04-15

---

## Tech Debt

**Duplicate Orchestration Logic (DIContainer + main.py):**
- Issue: `hypervisor/di_container.py` contains a full `Hypervisor` class with an identical `_run_cycle()` / `orchestration_loop()` to `hypervisor/main.py`. The DI container was introduced for testability but the production code still uses module-level globals in `main.py`. The `Hypervisor` class in `di_container.py` is currently dead code.
- Files: `hypervisor/di_container.py` (lines 80–257), `hypervisor/main.py`
- Impact: Any bug fixed in `main.py` must also be fixed in `di_container.py` or divergence grows. The test isolation benefit of DI is not being realised.
- Fix approach: Wire `main.py` to use the `Hypervisor` class from `di_container.py`, or delete the duplicate and document that tests should use the module-level globals with monkeypatching.

**CYCLE_INTERVAL_SEC Default Inconsistency:**
- Issue: `hypervisor/main.py` line 95 defaults `CYCLE_INTERVAL_SEC` to `3600` (one hour). `validate_config()` at line 986 defaults to `"60"` and enforces `10–600` bounds. CLAUDE.md documents the value as `60`. A running hypervisor with no env var set will cycle every hour, not every minute.
- Files: `hypervisor/main.py` (lines 95, 986)
- Impact: Silent misconfiguration in any environment that relies on the code default rather than `.env`. Causes stale regime classifications.
- Fix approach: Align all defaults to a single value; `validate_config()` must read `os.environ.get("CYCLE_INTERVAL_SEC", "3600")` to match the module constant, or the module constant must be lowered.

**Phase 3 Stubs Commingles Production State:**
- Issue: `workers/core_dividends/worker_api.py` (lines 205–216) and `hypervisor/main.py` (lines 265–281) contain `# PHASE 3:` stubs for OKX redemption and broker wiring that are embedded in live code paths rather than feature-flagged. The quarterly profit sweep sends Telegram messages advertising a feature that doesn't exist.
- Files: `workers/core_dividends/worker_api.py`, `hypervisor/main.py`
- Impact: Operational confusion; sweep messages alarm users with "_PHASE 3: OKX/USDT redemption not yet wired_".
- Fix approach: Gate stub paths behind `if PHASE3_ENABLED` env flag, or remove the advisory text from production Telegram messages.

**Arbitrader Worker: Exists But Not Orchestrated:**
- Issue: `workers/arbitrader/` contains a full NautilusTrader-based pair arb sidecar (`workers/arbitrader/sidecar/main.py`, 515 lines) with a Dockerfile and REST contract. It is not present in `docker-compose.yml` and is not registered in `hypervisor/main.py`'s `WORKER_REGISTRY`. CLAUDE.md's health check command (`curl -s http://localhost:8004/metrics`) references port 8004, but no service runs there.
- Files: `workers/arbitrader/sidecar/main.py`, `workers/arbitrader/Dockerfile`, `docker-compose.yml`, `hypervisor/main.py`
- Impact: The arb strategy is silently absent from every production cycle. CLAUDE.md health checks targeting port 8004 will always fail.
- Fix approach: Add `worker-arbitrader` service to `docker-compose.yml` and register `"arbitrader": os.environ.get("ARBITRADER_URL", "http://worker-arbitrader:8004")` in `WORKER_REGISTRY`, or formally document arbitrader as Phase 3 only.

**`data.feeds` Unavailable Inside Nautilus Container:**
- Issue: `workers/nautilus/strategies/funding_arb.py` (line 52), `order_flow.py` (line 48), and `factor_model.py` (line 54) attempt `from data.feeds.funding_rates import ...` at runtime. The nautilus Dockerfile build context is `./workers/nautilus` — `data/feeds/` is never copied in. The `try/except ImportError` guard silently falls back to synthetic/deterministic data.
- Files: `workers/nautilus/strategies/funding_arb.py`, `workers/nautilus/strategies/order_flow.py`, `workers/nautilus/strategies/factor_model.py`, `workers/nautilus/Dockerfile`
- Impact: Live OKX funding rate and order book data is never consumed by nautilus strategies in production. The strategies always run on synthetic data, degrading signal quality.
- Fix approach: Either extend the nautilus Docker build context to include `data/feeds/` (matching the hypervisor Dockerfile pattern), or vendor the required feed modules into `workers/nautilus/`.

**Deferred Imports Inside Hot Loop:**
- Issue: `hypervisor/main.py` lines 416–417 execute `from hypervisor.allocator.capital import HMM_STATE_LABELS` and `import numpy as np` inside `_run_cycle()` — a function called on every orchestration cycle (every `CYCLE_INTERVAL_SEC`). Python caches module imports so this is not a correctness bug, but it is an anti-pattern that obscures dependencies and makes profiling harder.
- Files: `hypervisor/main.py` (lines 416–417)
- Impact: Low performance impact; high readability/maintenance impact.
- Fix approach: Move these imports to the module top level with the other imports.

---

## Security Considerations

**CORS Wildcard Origin:**
- Risk: `hypervisor/main.py` (line 362) configures `allow_origins=["*"]`. Any web page on any domain can make credentialed requests to the hypervisor if a user with a valid browser session visits a malicious site.
- Files: `hypervisor/main.py` (lines 360–365)
- Current mitigation: Bearer token authentication via `APIKeyMiddleware` is present. The wildcard CORS alone does not bypass auth.
- Recommendations: Restrict `allow_origins` to `["http://localhost:3000", "http://arka-dashboard:3000"]` (or the Pi's LAN IP). A wildcard bypasses the same-origin protections that browsers otherwise enforce.

**API Key Exposed via Unauthenticated Endpoint:**
- Risk: `/setup/status` is in `EXEMPT_PATHS` (no auth required) and returns `"api_key": _api_key` in plain JSON (line 917). Any process that can reach port 8000 — including other Docker containers — can retrieve the hypervisor's master API key without credentials.
- Files: `hypervisor/main.py` (lines 896–918), `hypervisor/auth.py` (lines 44–48)
- Current mitigation: Port 8000 is not exposed to the public internet in normal deployment. Network-level isolation through Docker is relied upon.
- Recommendations: Return the API key only on the first `/setup/status` call (before `SETUP_COMPLETE=true`), or require a one-time setup token to retrieve it.

**Docker Socket Mounted in Hypervisor Container:**
- Risk: `/var/run/docker.sock` is bind-mounted into the `arka-hypervisor` container (`docker-compose.yml` line 49). Any code executing inside the hypervisor can start, stop, or inspect all containers on the host — equivalent to root access on the host machine.
- Files: `docker-compose.yml` (line 49), `hypervisor/main.py` (`_restart_container`, lines 794–804)
- Current mitigation: The socket is used only for `docker restart` after credential saves via the setup wizard.
- Recommendations: Replace the socket mount with a dedicated lightweight restart sidecar (e.g. `docker-socket-proxy`), or implement restart via Docker SDK with a tightly scoped proxy.

**Credential Write to `.env` Without Atomic Write:**
- Risk: `hypervisor/main.py` `save_credentials()` (line 954) calls `env_path.write_text(env_content)` — a non-atomic operation. A power loss mid-write corrupts `.env`, locking out all services on restart.
- Files: `hypervisor/main.py` (lines 921–965)
- Current mitigation: None.
- Recommendations: Write to a temp file first, then `os.replace()` for atomicity (POSIX atomic rename).

---

## Performance Bottlenecks

**HMM Bootstrap on First Run (3+ Minutes):**
- Problem: If `hypervisor/regime/model_state/hmm_4state.pkl` is absent or corrupted, `classifier.classify_sync()` fetches 3 years of daily data from yfinance + FRED before training the HMM. This blocks the orchestration thread for 3–5 minutes on first run or after model deletion.
- Files: `hypervisor/regime/classifier.py` (lines 167–171), `hypervisor/regime/feature_pipeline.py`
- Cause: Bootstrap is synchronous and happens inside `asyncio.to_thread()`, which prevents other coroutines from running (it's in a separate thread, so FastAPI itself still responds, but the first cycle takes minutes).
- Improvement path: Pre-bake the initial model into the Docker image via a build-time training step. The `.pkl` is already committed to git (`hypervisor/regime/model_state/hmm_4state.pkl`) — ensure the Dockerfile `COPY` includes the `model_state/` directory so the model ships with the image.

**Sequential Worker Status Polling:**
- Problem: `_pull_worker_status()` in `hypervisor/main.py` (lines 521–543) iterates workers sequentially with individual `await client.get()` calls inside a `for` loop. With 4 workers and a 10-second timeout, worst case is 40 seconds per cycle.
- Files: `hypervisor/main.py` (lines 521–543)
- Cause: Unlike `_check_worker_health()` which uses `asyncio.gather()`, status polling is sequential.
- Improvement path: Refactor to use `asyncio.gather(*[...], return_exceptions=True)` as done in `_check_worker_health()`.

**GDELT Rate Limiting (3.5s Sleep Per Query):**
- Problem: `data/feeds/conflict_index.py` defines `GDELT_SLEEP = 3.5`. With 3 queries (line 63–67), each conflict index computation takes at least 10.5 seconds blocking the calling thread.
- Files: `data/feeds/conflict_index.py` (lines 63, 68)
- Cause: Required to avoid GDELT rate limiting, but the sleep is synchronous.
- Improvement path: Move GDELT queries to a background task with cached results, so the main cycle reads the last cached score rather than blocking.

---

## Fragile Areas

**HMM Model State Labels Hardcoded in Two Places:**
- Files: `hypervisor/regime/hmm_model.py` (line 31), `hypervisor/allocator/capital.py` (line 42)
- Why fragile: `STATE_LABELS` in `hmm_model.py` and `HMM_STATE_LABELS` in `capital.py` both define the 4-state ordering `["RISK_ON", "RISK_OFF", "CRISIS", "TRANSITION"]`. The allocator builds `regime_probs_array` by indexing into `HMM_STATE_LABELS` order. If the state ordering changes in `hmm_model.py` after a retrain but `capital.py` is not updated, capital weights are silently assigned to wrong regimes.
- Safe modification: Any change to state ordering must update both files simultaneously and force a model retrain.
- Test coverage: No test asserts that the ordering is consistent between the two modules.

**`/health/locks` Endpoint Pollutes Live State:**
- Files: `hypervisor/main.py` (lines 876–893)
- Why fragile: The `GET /health/locks` diagnostic endpoint calls `state.update_worker_pnl("test_worker", 100.0)`, which writes a fake worker entry into the production `HypervisorState` dict. This `"test_worker"` entry persists across requests and will appear in `GET /status` responses, in capital reconciliation (`sum(self.worker_pnl.values())`), and in `/dashboard/state` output, inflating reported PnL by $100.
- Safe modification: Remove the side-effecting lock test or use a separate ephemeral state instance.
- Test coverage: No test catches this pollution.

**`/health/persistence` Writes Fake Regime Entries:**
- Files: `hypervisor/main.py` (lines 855–873)
- Why fragile: `persistence_health()` calls `await repo.log_regime("TEST", {}, False)` against the production database on every health check request. Docker healthchecks hit `/health` (not `/health/persistence`), but any monitoring system that polls this endpoint will create `regime="TEST"` rows in `data/arka.db`.
- Safe modification: Remove the regime write from the health check, or use a transaction that is immediately rolled back.
- Test coverage: Not covered in the test suite.

**`validate_config()` Called at Module Import Time:**
- Files: `hypervisor/main.py` (line 1012)
- Why fragile: `validate_config()` calls `raise SystemExit(...)` if required env vars are missing. This fires during import (not only during `uvicorn` startup), which means any test, migration script, or tool that imports `hypervisor.main` without a full `.env` will crash immediately. It also enforces `CYCLE_INTERVAL_SEC` bounds of `10–600` that conflict with the module-level default of `3600`.
- Safe modification: Move `validate_config()` into the FastAPI `lifespan` context so it only runs when the server actually starts.
- Test coverage: `conftest.py` stubs most env vars but does not exercise the validation path directly.

**Watchlist Accepts Only `isalnum()` Tickers:**
- Files: `hypervisor/main.py` (line 749)
- Why fragile: `ticker.isalnum()` rejects all tickers with hyphens, dots, or slashes (e.g., `BRK.B`, `BTC-USDT-SWAP`, `GC=F`). The watchlist is used by the Telegram bot and dashboard, but users who add crypto or futures tickers will get HTTP 400.
- Safe modification: Replace with a regex that allows `[A-Z0-9.=/-]` up to 20 characters.

---

## Scaling Limits

**SQLite for Production Persistence:**
- Current capacity: SQLite at `data/arka.db` with WAL mode supports concurrent reads well. Write throughput is gated by a single write lock.
- Limit: At the current cycle interval (3600s) SQLite is sufficient. If `CYCLE_INTERVAL_SEC` is reduced to seconds or multiple hypervisors are deployed, write contention will cause lock timeouts.
- Scaling path: Migrate to PostgreSQL (async via `asyncpg`) when moving to multi-hypervisor or sub-minute cycles.

**HypervisorState is In-Process Only:**
- Current capacity: All regime state, allocation history, and worker health is held in `HypervisorState()` in memory. A hypervisor restart loses all in-flight state.
- Limit: No state is shared across processes or replicas.
- Scaling path: The SQLite repository partially addresses this for regime and portfolio history but `worker_pnl`, `worker_health`, and `allocations` are not persisted between restarts.

---

## Dependencies at Risk

**`hmmlearn` Version Lock:**
- Risk: `hypervisor/regime/hmm_model.py` uses `hmmlearn.hmm.GaussianHMM`. The `hmm_4state.pkl` file is a pickle of this model. If `hmmlearn` is upgraded and the internal model representation changes (as it has between major versions), the pickle will fail to load silently (caught by bare `except Exception` in `hmm_model.py` line 165).
- Impact: Silent fallback to bootstrap training on every restart — 3–5 minute first-cycle delay and loss of historical calibration.
- Migration plan: Pin `hmmlearn` to an exact version in `requirements.txt`; add a model format version field to the pickle wrapper; write a migration test that loads the committed `.pkl`.

**`yfinance` MultiIndex Column Behaviour:**
- Risk: `data/feeds/market_data.py` documents that `yfinance >= 0.2.x` returns MultiIndex columns even for single tickers. The `_last_close()` helper works around this, but several call sites in `hypervisor/regime/feature_pipeline.py` use the same yfinance API. Any future yfinance API change can silently return NaN or crash feature extraction, causing the classifier to fall back to stale results.
- Impact: Stale regime classifications; undetected data quality degradation.
- Migration plan: Add a yfinance version pin and a data-quality assertion (e.g. assert returned close prices are within a plausible range) to `feature_pipeline.py`.

---

## Missing Critical Features

**No Dashboard `/api/dashboard/state` Implementation:**
- Problem: CLAUDE.md documents a detailed `/api/dashboard/state` endpoint schema consumed by `dashboard/src/hooks/useArkaData.js`. Grepping `hypervisor/main.py` shows no route handler for `/dashboard/state`. The dashboard polls this endpoint every 10 seconds but will always receive HTTP 404.
- Blocks: All dashboard panels that display live data (RegimeMood, RiskMeter, MoneyFlow, WorkerStory, DomainMap, TimelineView, PortfolioView, ThesisCard, BacktestReport, SystemMetrics).
- Files: `hypervisor/main.py`, `dashboard/src/hooks/useArkaData.js`

**Telegram Bot Has No Auth for Sensitive Commands:**
- Problem: `workers/telegram_bot/main.py` handles `/pause`, `/resume`, and portfolio commands. `TELEGRAM_ALLOWED_USER_ID` is the sole access control. If the env var is unset (common during setup), the bot accepts commands from any Telegram user.
- Files: `workers/telegram_bot/main.py`
- Blocks: Safe public deployment of the telegram bot.

---

## Test Coverage Gaps

**Dashboard Setup Endpoints Not Tested:**
- What's not tested: `/setup/credentials`, `/setup/status`, `/system/hardware`, and container restart behavior after credential saves are untested.
- Files: `hypervisor/main.py` (lines 758–965)
- Risk: Credential write logic (`save_credentials`) includes a non-atomic file write and a `subprocess.run(["docker", "restart", ...])` call. Bugs here can corrupt `.env` or restart wrong containers.
- Priority: High

**`/dashboard/state` Endpoint Missing and Untested:**
- What's not tested: The endpoint does not exist, so no test can cover it.
- Files: `hypervisor/main.py`, `tests/`
- Risk: Dashboard is entirely non-functional in any deployment until this endpoint is added.
- Priority: High

**Arbitrader Sidecar Integration:**
- What's not tested: `workers/arbitrader/sidecar/main.py` has no test file. The paper arb simulator (`_synthetic_spread`) and all REST endpoints are exercised only through manual inspection.
- Files: `workers/arbitrader/sidecar/main.py`
- Risk: Arb strategy correctness and REST contract compliance are unverified.
- Priority: Medium

**Regime State Label Ordering Consistency:**
- What's not tested: No test asserts that `STATE_LABELS` in `hmm_model.py` and `HMM_STATE_LABELS` in `capital.py` have the same ordering. A mismatch silently mis-routes capital.
- Files: `hypervisor/regime/hmm_model.py`, `hypervisor/allocator/capital.py`
- Risk: Silent capital mis-allocation to wrong strategy buckets during regime transitions.
- Priority: High

**`_health/locks` State Pollution:**
- What's not tested: No test verifies that calling `/health/locks` does not leave a `"test_worker"` entry in `worker_pnl` affecting subsequent capital reconciliation.
- Files: `hypervisor/main.py` (lines 876–893)
- Risk: In-production PnL reporting inflated by $100 after any health check call.
- Priority: Medium

---

*Concerns audit: 2026-04-15*
