# Technology Stack

**Analysis Date:** 2026-04-15

## Languages

**Primary:**
- Python 3.12 — all backend services (hypervisor, workers, data feeds)

**Secondary:**
- JavaScript/JSX (ES Modules) — dashboard frontend (`dashboard/src/`)
- SQL — database schema (`data/db/schema.sql`)

## Runtime

**Environment:**
- Docker containers (python:3.12-slim base for all Python services)
- Node 20 (Alpine) for dashboard build stage; nginx:alpine for serving

**Package Manager:**
- pip + venv (Python) — lockfile via `requirements.txt`
- npm (JavaScript) — lockfile via `package-lock.json`

**Target Platforms:**
- Dev: WSL2 Ubuntu-24.04, x86_64
- Production: Raspberry Pi 5, ARM64, Docker

## Frameworks

**Core (Python):**
- FastAPI `>=0.110.0` — REST API for hypervisor (`hypervisor/main.py`) and all workers
- uvicorn `>=0.29.0` — ASGI server; all services bind `0.0.0.0:{PORT}`
- Pydantic v2 — request/response models (field validators, BaseModel)
- Starlette middleware — `APIKeyMiddleware` for Bearer-token auth

**Frontend:**
- React `^18.3.0` — UI framework (`dashboard/src/`)
- Vite `^5.4.0` — dev server and build tool (`dashboard/vite.config.js`)
- Tailwind CSS `^4.0.0` — utility CSS via `@tailwindcss/vite` plugin (no `tailwind.config.js`)
- Electron `^33.0.0` — optional desktop wrapper (`dashboard/electron/main.cjs`)
- Capacitor `^7.0.0` — optional iOS/Android wrapper

**ML / Quantitative:**
- hmmlearn `>=0.3.2` — 4-state Gaussian HMM for regime classification (`hypervisor/regime/hmm_model.py`, pre-trained model at `hypervisor/regime/model_state/hmm_4state.pkl`)
- numpy `>=1.26.0` — feature engineering, HMM inference, VaR simulation
- pandas `>=2.0.0` — OHLCV manipulation, time-series indexing

**Testing:**
- pytest `>=8.0.0` — test runner; config in `pytest.ini`
- pytest-asyncio `>=0.23.0` — async test support (`asyncio_mode=auto`)

**Scheduling:**
- APScheduler `>=3.10.0` — quarterly profit sweep cron in `hypervisor/main.py` (Jan/Apr/Jul/Oct 7th @ 09:00)

**Logging:**
- structlog `>=24.0.0` — structured JSON logging in workers
- Python stdlib `logging` — standard logging in hypervisor

**Messaging / Async:**
- pyzmq `>=25.0.0` — ZeroMQ bindings (available; not the primary IPC path)
- asyncio — native async throughout hypervisor and worker APIs
- httpx `>=0.27.0` — async HTTP client used by hypervisor for worker calls
- requests `>=2.31.0` — sync HTTP client used in data feeds and Telegram profit sweep

**Dependency Injection:**
- Custom DI container at `hypervisor/di_container.py` — instantiated at startup, injected into route handlers

## Key Dependencies

**Critical:**
- `ccxt >=4.3.0` — crypto exchange connectivity; configured for OKX only (`EXCHANGES = ["okx"]`). Binance/Bybit geo-blocked.
- `yfinance >=0.2.40` — equities/ETFs/futures OHLCV; primary market data source
- `fredapi >=0.5.1` — FRED macro data (yield curve, CPI); optional, falls back gracefully
- `hmmlearn >=0.3.2` — regime classification; model loaded from pickle on startup
- `python-telegram-bot >=21.0` — Telegram command bot (`workers/telegram_bot/main.py`)
- `sqlalchemy[asyncio] >=2.0.0` + `aiosqlite >=0.19.0` — async SQLite ORM

**OSINT / Intelligence:**
- `edgartools >=3.0.0` — SEC EDGAR 8-K, Form 4, 10-Q parsing; no API key needed
- `gdeltdoc >=1.5.0` — GDELT v2 Doc API wrapper; falls back to raw urllib
- `instructor >=1.7.0` — structured LLM output (used with Ollama)
- `websockets >=12.0` — AISstream maritime websocket feed

**Optional / Conditional:**
- `nautilus_trader` — imported in `workers/nautilus/worker_api.py`; build failure falls back to internal paper sim
- `scrapegraphai` — optional deep web research; falls back to snippet extraction when absent

## Configuration

**Environment:**
- Primary: `.env` file (mounted into Docker containers; gitignored)
- Runtime constants: `config.py` (Python module; committed)
- Regime thresholds: `config/regimes.yaml`
- System settings: `config/settings.yaml`
- Capital allocations per regime: `config/allocations.yaml`
- SearXNG settings: `config/searxng/`

**Build:**
- Python services: `hypervisor/Dockerfile` (build context = project root `.`)
- Worker Dockerfiles: each worker's own `Dockerfile` (no `platform:` flags)
- Dashboard: `dashboard/Dockerfile` (multi-stage: node:20-alpine build + nginx:alpine serve)
- Compose: `docker-compose.yml` (base); `docker-compose.pi.yml` (Pi ARM64 override)

## Database

**Engine:** SQLite (file-based, async)
- Client: SQLAlchemy 2.x async ORM + aiosqlite driver
- Connection: `sqlite+aiosqlite:///data/arka.db`
- Schema source of truth: `data/db/schema.sql`
- ORM models: `hypervisor/db/models.py` (RegimeLog, Signal, Order tables)
- Session factory: `hypervisor/db/engine.py`
- WAL mode enabled via PRAGMA on init

## Platform Requirements

**Development:**
- WSL2 Ubuntu-24.04 (Ryzen 7, 16GB RAM — CPU only)
- Docker Desktop 4.63.0 with buildx + QEMU for ARM64 emulation
- Python venv at `~/.venv` (always use `.venv/bin/python -m pytest`, not bare `pytest`)

**Production:**
- Raspberry Pi 5, 8GB RAM, ARM64, Docker
- All containers pull native arch automatically (no `platform:` flags)

---

*Stack analysis: 2026-04-15*
