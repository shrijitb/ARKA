# Codebase Structure

**Analysis Date:** 2026-04-15

## Directory Layout

```
arka/                               # Project root; also Docker build context for hypervisor
├── config.py                       # Python constants shared by workers + test suite
├── conftest.py                     # pytest venv guard + sys.modules stubs
├── pytest.ini                      # asyncio_mode=auto, integration mark
├── requirements.txt                # Python deps for hypervisor + workers (shared)
├── docker-compose.yml              # Full stack definition; no platform: flags
├── install.sh                      # One-shot installer (hardware detection + stack launch)
├── arka-cli                        # CLI helper script (executable)
├── CLAUDE.md                       # Master reference for Claude Code sessions
│
├── config/                         # Runtime YAML configuration (mounted read-only into containers)
│   ├── regimes.yaml                # Regime threshold values (VIX, yield curve, war premium, etc.)
│   ├── settings.yaml               # System-wide settings (heartbeat, drawdown, Kelly fraction)
│   ├── allocations.yaml            # Per-state capital allocation weights (mirrors capital.py)
│   └── searxng/                    # SearXNG config for local web search (analyst worker)
│
├── hypervisor/                     # FastAPI orchestrator; build context is project root (.)
│   ├── Dockerfile
│   ├── main.py                     # App + orchestration loop + all REST endpoints
│   ├── audit.py                    # Structured audit logging; writes data/audit.jsonl
│   ├── auth.py                     # APIKeyMiddleware + key lifecycle
│   ├── circuit_breaker.py          # CircuitBreaker class (CLOSED/OPEN/HALF_OPEN)
│   ├── di_container.py             # DIContainer for testability
│   ├── errors.py                   # Custom exception types (RegimeClassificationError, etc.)
│   ├── allocator/
│   │   └── capital.py              # RegimeAllocator, blend_allocations(), ALLOCATION_PROFILES
│   ├── regime/
│   │   ├── classifier.py           # RegimeClassifier — HMM lifecycle, classify_sync()
│   │   ├── feature_pipeline.py     # 6-feature extraction from yfinance + FRED
│   │   ├── hmm_model.py            # RegimeHMM wrapping hmmlearn GaussianHMM
│   │   ├── circuit_breakers.py     # Hard override of HMM probs on VIX/war threshold breach
│   │   └── model_state/
│   │       └── hmm_4state.pkl      # Persisted HMM weights (reloaded on startup)
│   ├── risk/
│   │   ├── manager.py              # RiskManager — 6-limit portfolio guard
│   │   ├── margin_reserve.py       # MarginReserveManager — per-position liquid buffer
│   │   └── expiry_guard.py         # ExpiryGuard — prevent holding expiring contracts
│   ├── ai/                         # AI-related hypervisor utilities
│   └── db/
│       ├── engine.py               # Async SQLAlchemy engine (aiosqlite); init_db()
│       ├── models.py               # ORM models: RegimeLog, PortfolioState, Signal, Order
│       └── repository.py           # ArkaRepository — all DB write/read operations
│
├── workers/
│   ├── nautilus/                   # Port 8001 — NautilusTrader swing/range/day strategies
│   │   ├── Dockerfile
│   │   ├── worker_api.py           # FastAPI entrypoint; ArkaEngine; ADX-routed strategy selection
│   │   ├── indicators/
│   │   │   └── adx.py             # Pure Python ADX (Wilder's smoothing), no TA-Lib
│   │   └── strategies/
│   │       ├── swing_macd.py       # 4H MACD swing (trending market, ADX > 25)
│   │       ├── range_mean_revert.py# Mean reversion (ranging market, ADX < 20)
│   │       ├── day_scalp.py        # 1m intraday momentum
│   │       ├── funding_arb.py      # OKX perpetual funding rate carry
│   │       ├── order_flow.py       # Order book imbalance signals
│   │       └── factor_model.py     # 3-factor cross-sectional model (momentum + carry + size)
│   ├── prediction_markets/         # Port 8002 — Kalshi/Polymarket CLOB stub
│   │   ├── Dockerfile
│   │   └── worker_api.py
│   ├── analyst/                    # Port 8003 — phi3:mini + SearXNG advisory
│   │   ├── Dockerfile
│   │   └── worker_api.py
│   ├── core_dividends/             # Port 8006 — SCHD + VYM passive hold
│   │   ├── Dockerfile
│   │   └── worker_api.py
│   ├── arbitrader/                 # Port 8004 — statistical pair arb (BTC/ETH spread)
│   │   ├── sidecar/
│   │   │   └── main.py            # FastAPI sidecar; NautilusTrader arb engine
│   │   └── src/                   # Vendored Java Arbitrader source (git submodule, Phase 3 only)
│   ├── polymarket/                 # Polymarket adapter (Phase 3)
│   │   └── adapter/
│   ├── telegram_bot/               # No REST port — polling bot only
│   │   ├── Dockerfile
│   │   └── main.py                # /status /regime /watchlist /pause /resume $TICKER
│   └── stocksharp/                 # Phase 3 ONLY — .NET 8 IBKR router; do not modify
│
├── data/
│   ├── feeds/
│   │   ├── market_data.py          # yfinance + FRED wrappers; UUP/BDRY proxies
│   │   ├── conflict_index.py       # 0–100 war premium score; AcledTokenManager
│   │   ├── domain_router.py        # DomainRouter.evaluate() → DomainDecision list
│   │   ├── osint_processor.py      # OSINT aggregator (GDELT, ACLED, AIS, NASA)
│   │   ├── circuit_breaker.py      # Circuit breaker for external data fetches
│   │   ├── funding_rates.py        # OKX perpetual funding rates
│   │   ├── order_book.py           # OKX order book depth
│   │   ├── edgar_client.py         # SEC EDGAR insider buying feed
│   │   ├── gdelt_client.py         # GDELT v2 conflict query client
│   │   ├── maritime_client.py      # AISstream ship tracking client
│   │   ├── environment_client.py   # NASA FIRMS fire detection
│   │   ├── ucdp_client.py          # Uppsala Conflict Data client
│   │   ├── aviation_client.py      # Aviation OSINT feed
│   │   ├── company_researcher.py   # Company-level OSINT research
│   │   └── searxng_client.py       # SearXNG web search client
│   ├── db/
│   │   └── schema.sql              # SQLite DDL; CREATE TABLE IF NOT EXISTS for all tables
│   ├── arka.db                     # SQLite database (runtime; gitignored)
│   └── audit.jsonl                 # Rotating JSONL audit log (runtime; gitignored)
│
├── dashboard/                      # React 18 + Vite 5 + Tailwind v4 SPA
│   ├── Dockerfile                  # Multi-stage: node:20-alpine build + nginx:alpine serve (port 3000)
│   ├── nginx.conf                  # /api/* → http://hypervisor:8000/
│   ├── package.json
│   ├── vite.config.js              # @tailwindcss/vite plugin; /api proxy for dev
│   ├── electron/                   # Electron desktop wrapper
│   ├── public/
│   └── src/
│       ├── main.jsx                # React root
│       ├── App.jsx                 # SetupWizard ↔ Dashboard routing
│       ├── hooks/
│       │   └── useArkaData.js      # Polls /api/dashboard/state + /api/setup/status every 10s
│       ├── styles/
│       │   └── global.css          # @import "tailwindcss"; @theme {} custom tokens
│       ├── utils/
│       │   └── cn.js               # Tailwind class merge utility
│       ├── pages/
│       │   ├── Dashboard.jsx       # 3-col desktop / 2-col tablet / tab-nav mobile
│       │   └── SetupWizard.jsx     # 6-step guided setup
│       └── components/
│           ├── narrative/          # Data → visual metaphors (RegimeMood, RiskMeter, etc.)
│           ├── setup/              # 6 wizard step components
│           └── education/          # Tooltip + glossary
│
├── tests/
│   ├── test_mara.py                # Unit + integration tests (120+ assertions)
│   ├── test_integration_dryrun.py  # Dry-run integration suite
│   ├── test_safety_rails.py        # Margin reserve, expiry guard, position liquidity
│   └── test_concurrency.py         # Race conditions, circuit breaker state transitions
│
└── scripts/
    └── deploy_pi.sh                # Pi deploy helper (placeholder IP)
```

## Directory Purposes

**`hypervisor/`:**
- Purpose: Central orchestrator — regime classification, capital allocation, risk management, all REST endpoints
- Contains: FastAPI app, HMM classifier pipeline, risk guardrails, SQLite persistence, audit logging, auth middleware
- Key files: `main.py`, `allocator/capital.py`, `regime/classifier.py`, `risk/manager.py`, `db/repository.py`

**`workers/`:**
- Purpose: Isolated trading strategy containers; each exposes the standard Arka REST contract
- Contains: 5 worker packages (nautilus, prediction_markets, analyst, core_dividends, telegram_bot) + arbitrader sidecar
- Key files: `nautilus/worker_api.py`, `nautilus/strategies/*.py`, `nautilus/indicators/adx.py`

**`data/feeds/`:**
- Purpose: All external data acquisition — market prices, macro data, OSINT intelligence
- Contains: yfinance/FRED wrappers, OSINT clients, conflict index scorer, domain router, circuit breakers
- Key files: `market_data.py`, `conflict_index.py`, `domain_router.py`, `osint_processor.py`

**`data/db/`:**
- Purpose: Database schema definition
- Contains: `schema.sql` — bootstrapped by `hypervisor/db/engine.py:init_db()` on every startup

**`dashboard/src/`:**
- Purpose: React SPA for monitoring and setup
- Contains: Pages (Dashboard, SetupWizard), narrative components, setup wizard steps, data hooks
- Key files: `App.jsx`, `hooks/useArkaData.js`, `styles/global.css`

**`config/`:**
- Purpose: Runtime YAML configuration mounted read-only into all containers
- Contains: Regime thresholds, system settings, allocation weights
- Key files: `regimes.yaml`, `settings.yaml`, `allocations.yaml`

**`tests/`:**
- Purpose: Test suite; always run via `~/.venv/bin/python -m pytest tests/ -v`
- Contains: Unit tests, integration dry-runs, safety rail tests, concurrency tests

## Key File Locations

**Entry Points:**
- `hypervisor/main.py`: Hypervisor FastAPI app; `app` object; `orchestration_loop()` background task
- `workers/nautilus/worker_api.py`: Nautilus worker FastAPI app (port 8001)
- `workers/arbitrader/sidecar/main.py`: Arb worker FastAPI app (port 8004)
- `dashboard/src/main.jsx`: React root
- `dashboard/src/App.jsx`: Top-level routing (SetupWizard vs Dashboard)

**Configuration:**
- `config.py`: Python constants (capital, exchange, strategy parameters); imported by workers and tests
- `config/regimes.yaml`: HMM circuit breaker thresholds
- `config/settings.yaml`: System heartbeat, Kelly fraction, drawdown limits, data tickers
- `config/allocations.yaml`: Per-state allocation weights (duplicated in `hypervisor/allocator/capital.py` as code)
- `.env.example`: Template for required environment variables

**Core Logic:**
- `hypervisor/allocator/capital.py`: `ALLOCATION_PROFILES`, `RegimeAllocator`, `blend_allocations()`
- `hypervisor/regime/classifier.py`: `RegimeClassifier`, `RegimeResult`, `Regime` enum
- `hypervisor/regime/feature_pipeline.py`: 6-feature vector extraction (VIX, yield curve, HY OAS, NFCI, equity momentum)
- `hypervisor/risk/manager.py`: `RiskManager`, `RiskVerdict`, `WorkerRiskState`
- `data/feeds/conflict_index.py`: War premium scoring
- `data/feeds/domain_router.py`: `DomainRouter`, `DomainDecision`, `apply_domain_overrides()`

**Persistence:**
- `hypervisor/db/engine.py`: `async_session`, `init_db()`
- `hypervisor/db/repository.py`: `ArkaRepository`
- `hypervisor/db/models.py`: ORM models
- `data/db/schema.sql`: Table DDL
- `hypervisor/audit.py`: `audit()` async function; audit event types

**Testing:**
- `conftest.py`: venv guard, sys.modules stubs for optional heavy deps (nautilus_trader, ccxt)
- `pytest.ini`: `asyncio_mode=auto`; `integration` mark registration

## Naming Conventions

**Files:**
- Python modules: `snake_case.py` throughout
- React components: `PascalCase.jsx` (e.g., `RegimeMood.jsx`, `WorkerStory.jsx`)
- React hooks: `camelCase.js` with `use` prefix (e.g., `useArkaData.js`)
- React utilities: `camelCase.js` (e.g., `cn.js`)

**Directories:**
- Python packages: `snake_case/` (e.g., `core_dividends/`, `prediction_markets/`)
- React component groups: `lowercase/` directories (e.g., `narrative/`, `setup/`, `education/`)

**Classes:**
- Services/managers: PascalCase noun + role suffix (e.g., `RegimeClassifier`, `RegimeAllocator`, `RiskManager`, `ArkaRepository`)
- Data containers: PascalCase (e.g., `RegimeResult`, `AllocationResult`, `RiskVerdict`, `DomainDecision`)
- FastAPI apps: `app = FastAPI(...)` at module level

**Constants:**
- Module-level Python constants: `ALL_CAPS_SNAKE` (e.g., `INITIAL_CAPITAL_USD`, `MAX_DRAWDOWN_PCT`)
- Dict profiles: `ALL_CAPS` (e.g., `ALLOCATION_PROFILES`, `HMM_STATE_MAX_DEPLOY`)

**Worker Registry Keys:**
- Must exactly match keys in `hypervisor/allocator/capital.py:ALLOCATION_PROFILES` and `hypervisor/main.py:WORKER_REGISTRY`
- Current keys: `nautilus`, `prediction_markets`, `analyst`, `core_dividends`

## Where to Add New Code

**New Trading Worker:**
- Worker code: `workers/<worker_name>/worker_api.py` (FastAPI, implement full REST contract)
- Docker: `workers/<worker_name>/Dockerfile` (no `platform:` flag)
- Register in: `hypervisor/main.py:WORKER_REGISTRY` and `hypervisor/allocator/capital.py:ALLOCATION_PROFILES`
- Add to: `docker-compose.yml` with health check + `depends_on: hypervisor`
- Add allocation weights to: all 4 states in `ALLOCATION_PROFILES` (must sum to ≤ 1.0)

**New Strategy (Nautilus worker):**
- Implementation: `workers/nautilus/strategies/<strategy_name>.py`
- Wire into ADX router: `workers/nautilus/worker_api.py` strategy selection logic
- Tests: `tests/test_mara.py`

**New Data Feed / OSINT Client:**
- Implementation: `data/feeds/<source>_client.py`
- Wrap external calls with `CircuitBreaker` from `data/feeds/circuit_breaker.py`
- Integrate into: `data/feeds/osint_processor.py` for OSINT; `data/feeds/market_data.py` for market data

**New Hypervisor Endpoint:**
- Add to: `hypervisor/main.py` after existing route groups
- Add auth exemption if needed: `hypervisor/auth.py:EXEMPT_PATHS`
- Add audit logging: call `await audit("<event_type>", ...)` from `hypervisor/audit.py`

**New HMM Feature:**
- Add to feature vector: `hypervisor/regime/feature_pipeline.py` (update `N_FEATURES` constant and `_FALLBACKS` dict)
- Retrain model: delete `hypervisor/regime/model_state/hmm_4state.pkl`; restart hypervisor to auto-bootstrap

**New React Component:**
- Narrative/visualization: `dashboard/src/components/narrative/<ComponentName>.jsx`
- Setup wizard step: `dashboard/src/components/setup/<StepName>Step.jsx`
- General UI: `dashboard/src/components/<ComponentName>.jsx`
- Use `cn()` from `dashboard/src/utils/cn.js` for conditional class merging
- Colors: Always use custom token classes (`text-cream`, `bg-card`, `text-profit`), never raw hex values

**New Database Table:**
- Add DDL to: `data/db/schema.sql` (use `CREATE TABLE IF NOT EXISTS`)
- Add ORM model to: `hypervisor/db/models.py`
- Add repository methods to: `hypervisor/db/repository.py`

**Test Files:**
- Unit + integration: `tests/test_mara.py` (existing; extend with new test classes)
- New test file: `tests/test_<area>.py`
- Always run with: `~/.venv/bin/python -m pytest tests/ -v` (never bare `pytest`)

## Special Directories

**`hypervisor/regime/model_state/`:**
- Purpose: Persisted HMM model weights and feature normalization statistics
- Generated: Yes (by `RegimeHMM.train()` and `FeaturePipeline.normalize()`)
- Committed: `hmm_4state.pkl` is committed as baseline; regenerated on monthly retrain

**`workers/arbitrader/src/`:**
- Purpose: Vendored Java Arbitrader source (originally third-party, now managed as git submodule)
- Generated: No
- Committed: Yes (git submodule)
- Note: Phase 3 only; do not modify

**`workers/stocksharp/`:**
- Purpose: .NET 8 IBKR router for Phase 3 live trading
- Generated: No
- Committed: Yes (placeholder)
- Note: Phase 3 only; do not modify

**`data/db/`:**
- Purpose: SQL schema definition; runtime SQLite database created alongside it as `data/arka.db`
- Generated: `arka.db` is generated at runtime by `init_db()`
- Committed: `schema.sql` yes; `arka.db` no (gitignored)

**`.planning/codebase/`:**
- Purpose: Codebase analysis documents consumed by GSD planning commands
- Generated: Yes (by `/gsd-map-codebase`)
- Committed: As needed

---

*Structure analysis: 2026-04-15*
