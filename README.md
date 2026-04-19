# Arka — Regime-Aware Multi-Agent Trading System

An autonomous trading system designed for turbulent macro environments. A Python FastAPI hypervisor orchestrates specialised worker agents and allocates capital dynamically using a 4-state Hidden Markov Model regime classifier.

**Status**: Paper trading | **License**: LGPL-3.0

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Regime Classification & Capital Allocation](#regime-classification--capital-allocation)
- [Workers](#workers)
- [Dashboard](#dashboard)
- [Setup](#setup)
- [Configuration](#configuration)
- [Running the Stack](#running-the-stack)
- [Tests](#tests)
- [Telegram Bot](#telegram-bot)
- [REST Contract](#rest-contract)
- [Risk Limits](#risk-limits)
- [Data Sources](#data-sources)
- [Development Guide](#development-guide)
- [Production Deploy (Raspberry Pi 5)](#production-deploy-raspberry-pi-5)
- [License](#license)

---

## Overview

Arka targets **crypto, futures, ETFs, and prediction markets** in regime-driven paper trading. The system is currently in the pre-Phase-3 stabilisation pass — all trading is simulated, no live orders are placed.

### Core sequence

```
Regime classification → Risk check → Capital allocation → Signal generation → Paper execution
```

This cycle runs every 60 seconds. The regime classifier uses a pre-trained 4-state Gaussian HMM on market features (VIX, yield curve, gold/oil, BTC funding rate, defense ETF momentum, GDELT conflict score) to determine the current macro state and route capital accordingly.

---

## Architecture

```
                    ┌──────────────────────────────┐
                    │   Hypervisor  (port 8000)    │
                    │   FastAPI orchestrator        │
                    │                              │
                    │  ┌────────────────────────┐  │
                    │  │  Regime Classifier     │  │
                    │  │  4-state HMM           │  │
                    │  ├────────────────────────┤  │
                    │  │  Capital Allocator     │  │
                    │  │  regime → worker USD   │  │
                    │  ├────────────────────────┤  │
                    │  │  Risk Manager          │  │
                    │  │  drawdown / VaR / caps │  │
                    │  └────────────────────────┘  │
                    └──┬───────────────────────────┘
                       │  HTTP (allocate / regime / signal / execute)
       ┌───────────────┼───────────────────┐
       ▼               ▼                   ▼
┌────────────┐  ┌──────────────┐  ┌────────────────┐
│  nautilus  │  │  prediction  │  │ core_dividends │
│  port 8001 │  │   markets    │  │   port 8006    │
│ paper sim  │  │  port 8002   │  │  SCHD+VYM hold │
└────────────┘  └──────────────┘  └────────────────┘

┌──────────────┐  ┌────────────┐  ┌─────────────────────┐
│ telegram-bot │  │   ollama   │  │       analyst       │
│  no port     │  │ port 11434 │  │      port 8003      │
│  polling     │  │ phi3:mini  │  │ phi3:mini + searxng │
└──────────────┘  └────────────┘  └─────────────────────┘
```

### Container map

| Container | Port | Tech | Mode |
|-----------|------|------|------|
| `arka-hypervisor` | 8000 | FastAPI | Orchestrator |
| `arka-nautilus` | 8001 | Python paper sim | ADX-routed strategies |
| `arka-prediction-markets` | 8002 | Python CLOB | Stub (needs `POLY_PRIVATE_KEY`) |
| `arka-analyst` | 8003 | litellm + Ollama | phi3:mini advisory |
| `arka-core-dividends` | 8006 | FastAPI | Paper hold (SCHD + VYM) |
| `arka-ollama` | 11434 | Ollama | phi3:mini CPU inference |
| `arka-telegram-bot` | none | python-telegram-bot | Polling, no inbound port |
| `arka-searxng` | 8080 | SearXNG | Local web search for Analyst |
| `workers/stocksharp` | 8005 | .NET 8 | Phase 3 only — not deployed |

---

## Regime Classification & Capital Allocation

### HMM classifier

The regime classifier loads a pre-trained 4-state Gaussian HMM from `hypervisor/regime/model_state/hmm_4state.pkl`. Each cycle, it extracts a feature vector and computes posterior probabilities over the four states. The capital allocator blends all four state profiles weighted by those probabilities, then applies a turnover filter (suppresses rebalancing if no weight shifts by more than 2%).

**Feature inputs** (all normalised):
- VIX level
- 10Y–2Y yield curve spread (FRED `T10Y2Y`)
- Gold/oil ratio (`GC=F` / `CL=F`)
- DXY proxy (UUP ETF)
- BDI proxy (BDRY ETF)
- BTC perpetual funding rate (OKX)
- Defense ETF momentum (NATO/SHLD/PPA/ITA basket)
- GDELT conflict sentiment score

### Regime profiles

| Regime | nautilus | prediction_markets | analyst | core_dividends | Max deploy |
|--------|----------|--------------------|---------|----------------|------------|
| `RISK_ON` | 44% | 12% | 8% | 36% | 80% |
| `RISK_OFF` | 34% | 18% | 8% | 40% | 75% |
| `CRISIS` | 10% | 20% | 0% | 30% | 50% |
| `TRANSITION` | 32% | 16% | 8% | 44% | 70% |

In `CRISIS`, 50% of capital stays in cash by design. Weights are normalised against the full profile sum, not just healthy workers — a single starting worker cannot absorb more than its intended share.

**Sharpe penalty:** if a worker's rolling Sharpe < 0.5, its allocation is halved. Fresh workers store `None` (not `0.0`) to avoid false penalisation on first cycle.

---

## Workers

### Nautilus (port 8001)

ADX-routed strategy engine. Runs five strategies simultaneously and routes capital based on market conditions:

| Strategy | Condition |
|----------|-----------|
| `swing_macd` | Trending (ADX ≥ 25) — MACD + Bullish Fractal long entries |
| `day_scalp` | Trending intraday — momentum bursts |
| `range_mean_revert` | Ranging (ADX < 20) — reversion to range midpoint |
| `factor_model` | Multi-factor quantitative — always active |
| `order_flow` | Order book imbalance signals |
| `funding_arb` | OKX perpetual funding rate arbitrage |

Pairs traded: `BTC/USDT`, `ETH/USDT`, `SOL/USDT`, `BNB/USDT`, `AVAX/USDT` on 4h timeframe.

If `nautilus_trader` import fails at startup, the worker falls back to an internal paper sim engine (`workers/nautilus/engine.py`).

### Prediction Markets (port 8002)

CLOB market-making stub. Returns valid signals but does not execute until `POLY_PRIVATE_KEY` (Polygon private key) is configured. Phase 3.

### Analyst (port 8003)

LLM-based trade thesis generation using phi3:mini via Ollama + SearXNG local web search. Produces Director / Quant / Risk three-stage advisory output. All signals are `advisory_only=True` — the hypervisor logs them but does not route to `/execute`.

### Core Dividends (port 8006)

Passive buy-and-hold sleeve: SCHD + VYM ETFs. Signals are `advisory_only=True` until IBKR is wired (Phase 3). Provides stable 36–44% allocation anchor in non-crisis regimes.

### Telegram Bot (no port)

Polling bot — no inbound port or webhook required. All commands are rejected for any Telegram user ID other than `TELEGRAM_ALLOWED_USER_ID`.

| Command | Description |
|---------|-------------|
| `/status` | Capital snapshot, regime, worker health, PnL |
| `/regime` | Current regime label + HMM confidence |
| `/watchlist` | Dynamic ticker watchlist |
| `/pause <worker>` | Halt new entries for a named worker |
| `/resume <worker>` | Resume a paused worker |
| `$TICKER` | Any message with e.g. `$BTC` adds the ticker to the watchlist |

---

## Dashboard

React 18 + Vite 5 + Tailwind CSS v4 frontend, served by nginx on port 3000. Proxies `/api/*` to the hypervisor at port 8000.

### Components

| Component | Description |
|-----------|-------------|
| `RegimeMood` | Weather metaphor (☀️ / ⛅ / 🌧 / ⛈) with animated HMM probability bars |
| `RiskMeter` | SVG semicircular gauge (green / amber / red) |
| `WorkerStory` | Character cards — plain-English activity summary per worker |
| `MoneyFlow` | Animated allocation bars — capital distribution in real time |
| `DomainMap` | OSINT domain entry/exit signals (GDELT-sourced) |
| `TimelineView` | Activity timeline — regime changes, allocations, alerts |
| `ThesisCard` | Director / Quant / Risk analyst pipeline output |
| `PortfolioView` | Positions list with SVG sparklines |
| `BacktestReport` | Nightly strategy results (PBO + DSR metrics) |
| `GlobalControls` | Emergency stop modal (requires typing "STOP" to confirm) |

### Setup wizard (first run)

On first launch before credentials are saved, the dashboard shows a 6-step guided wizard:

1. **Welcome** — overview of what Arka does
2. **Device** — detects Pi vs. laptop via `/api/system/hardware`
3. **Exchange** — OKX API key / secret / passphrase (paper trading works without them)
4. **Data Sources** — FRED, NASA FIRMS, AISstream, UCDP, Kalshi (all optional)
5. **Notifications** — Telegram bot token + User ID
6. **Review & Launch** — checklist + "Launch Arka" POST

### Build & run

```bash
docker compose build dashboard
docker compose up -d
# http://localhost:3000
```

### Dev mode

```bash
cd dashboard && npm install && npm run dev
# Vite dev server on :5173, /api proxied to localhost:8000
```

### Design tokens (Tailwind v4 custom theme in `src/styles/global.css`)

| Token | Value | Use |
|-------|-------|-----|
| `card` | `#0A0A0A` | Component card background |
| `surface` | `#111111` | Page background |
| `edge` / `rim` / `line` | `#1A1A1A` / `#2A2A2A` / `#333333` | Borders, graduated |
| `cream` | `#FFFDD0` | Primary text |
| `muted` | `#B0AE98` | Secondary text |
| `profit` | `#00E676` | Gains, success |
| `loss` | `#FF1744` | Losses, danger |
| `warn` | `#FFD740` | Caution |
| `purple` | `#E040FB` | AI/Analyst accent |
| `orange` | `#FF9100` | Arbitrader accent |

Never hardcode hex in JSX — always use token classes (`text-cream`, `bg-card`, etc.).

---

## Setup

### Prerequisites

- Docker + Docker Compose v2
- Python 3.12 + venv (for running tests locally)
- Git

### One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/shrijitb/ARKA/main/install.sh | bash
```

Automatically detects hardware (x86\_64 or ARM64/Pi 5), selects the optimal Ollama model, generates `.env` from your inputs, and launches the full stack.

### Quick start (manual, Ubuntu 24.04)

```bash
git clone https://github.com/shrijitb/ARKA.git
cd ARKA
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in credentials — never commit .env
docker compose up -d
```

- **Dashboard**: http://localhost:3000
- **Hypervisor API**: `curl -s http://localhost:8000/status | python3 -m json.tool`

### Windows 11 (WSL2)

```powershell
# PowerShell as Administrator
wsl --install   # installs Ubuntu 24.04; reboot when prompted
```

```bash
# Inside Ubuntu 24.04
sudo apt update && sudo apt install -y python3.12 python3.12-venv python3.12-dev git curl
git clone https://github.com/shrijitb/ARKA.git
cd ARKA
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
docker compose up -d
```

Install Docker Desktop for Windows, enable WSL2 integration for Ubuntu-24.04 in Settings → Resources → WSL Integration.

> Do **not** add `platform:` flags to `docker-compose.yml` — ARM64 pre-built binaries (e.g. `ollama/ollama`) exit with code 255 under QEMU on x86.

### macOS

```bash
# Install Docker Desktop for Mac, then:
brew install python@3.12 git
git clone https://github.com/shrijitb/ARKA.git
cd ARKA
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
docker compose up -d
```

### CLI (`arka-cli`)

The `arka-cli` script in the repo root provides shortcuts:

```bash
./arka-cli status     # worker health + regime
./arka-cli logs       # tail hypervisor logs
./arka-cli stop       # docker compose down
./arka-cli start      # docker compose up -d
./arka-cli restart    # stop + start
```

---

## Configuration

Copy `.env.example` to `.env`. **Never commit `.env`.**

```ini
# Cycle
CYCLE_INTERVAL_SEC=60

# Safety — do not change until paper trading is fully validated
PAPER_TRADING=true
USE_LIVE_RATES=false
USE_LIVE_OHLCV=false
INITIAL_CAPITAL_USD=200.0
EXCHANGES=["okx"]

# Ollama (local LLM for Analyst worker)
OLLAMA_HOST=http://ollama:11434
OLLAMA_MODEL=phi3:mini

# FRED (optional — yfinance fallback works without it)
FRED_API_KEY=

# OKX — leave empty for paper trading; required only for Phase 3 live execution
OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=

# Polymarket — Phase 3 only
POLY_PRIVATE_KEY=

# IBKR — Phase 3 only
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=1

# Telegram
TELEGRAM_BOT_TOKEN=        # from @BotFather
TELEGRAM_ALLOWED_USER_ID=  # from @userinfobot
```

### Regime thresholds (`config/regimes.yaml`)

```yaml
crisis_vix:            40.0
crisis_yield_curve:   -0.50
bear_yield_curve:      0.0
bear_vix:             25.0
frothy_vix:           15.0
frothy_funding_rate:   0.0003
```

### Capital allocations (`config/allocations.yaml`)

Documents the per-regime weight profiles. The authoritative values live in `hypervisor/allocator/capital.py` — this file is for human reference.

---

## Running the Stack

```bash
# Start everything
docker compose up -d

# Follow hypervisor logs
docker compose logs -f hypervisor

# Check container health
docker compose ps

# Rebuild a single service after a code change
docker compose build --no-cache <service-name>
docker compose up -d   # always restart the full stack after any rebuild

# Stop everything
docker compose down
```

> **Always restart with `docker compose up -d` after a rebuild.** Using `--force-recreate` on a single container disconnects it from the shared Docker network.

### Health check endpoints

```bash
curl -s http://localhost:8000/status | python3 -m json.tool
curl -s http://localhost:8000/regime
curl -s http://localhost:8000/watchlist
curl -s http://localhost:8001/health   # nautilus
curl -s http://localhost:8002/health   # prediction-markets
curl -s http://localhost:8003/health   # analyst
curl -s http://localhost:8006/health   # core_dividends
```

### Session re-entry

```bash
cd ~/ARKA
source .venv/bin/activate
docker compose up -d
curl -s http://localhost:8000/status | python3 -m json.tool
```

---

## Tests

Always use the venv Python directly — the system Python lacks `fastapi`, `httpx`, and other test dependencies.

```bash
.venv/bin/python -m pytest tests/ -v
# Expected: 120+ passed, 7 skipped, 0 failed
```

### Test classes

| Class | What it covers | Status |
|-------|---------------|--------|
| `TestWorkerContract` | nautilus + analyst REST contract endpoints | Pass |
| `TestCapitalAllocator` | Dollar splits, Sharpe penalty, cold-start single-worker cap | Pass |
| `TestRiskManagerIntegration` | All 7 risk limits, cooldown, peak-reset regression | 10/10 pass |
| `TestHypervisorCycle` | Registry keys, capital math, classifier shape | 4/4 pass |
| `TestEndToEndSignalSchema` | Signal format, `advisory_only` enforcement | 4/6 pass, 2 skip |
| `TestGdeltIntegration` | GDELT conflict query + scoring | 3/3 pass |
| `TestAdxCalculator` | Trending / ranging / ambiguous classification | 5/5 pass |
| `TestRangeMeanRevertStrategy` | Dead-market filter, insufficient bars, signal structure | 4/4 pass |
| `TestStrategyRouter` | swing-forced mode, auto default, `/strategy` runtime override | 3/3 pass |

### Expected skips

- `analyst` and `prediction_markets` contract tests — `litellm` / `py_clob_client` not in the local venv (pass in Docker)
- OKX live tests — paper trading only
- IBKR — Phase 3, not wired

---

## REST Contract

Every trading worker implements these endpoints. The hypervisor treats all workers identically through this contract.

```
GET  /health    → {"status": "ok", "paused": bool, ...worker-specific}
GET  /status    → {"pnl": float, "sharpe": float, "allocated_usd": float, "open_positions": int}
GET  /metrics   → Prometheus text format
POST /signal    → list of signal dicts (each has "action", "rationale", "advisory_only")
POST /execute   → executes trade or returns advisory
POST /allocate  → {"amount_usd": float, "paper_trading": bool}
POST /pause     → halt new entries
POST /resume    → resume entries
POST /regime    → {"regime": str, "confidence": float, "paper_trading": bool}
```

Hypervisor-only:

```
GET  /watchlist               → {"watchlist": ["BTC", ...]}
POST /watchlist               → {"ticker": "BTC"}
POST /workers/{worker}/pause  → pause a named worker
POST /workers/{worker}/resume → resume a named worker
```

> **`/metrics` pattern:** Always return `Response(content=text, media_type="text/plain")` from FastAPI. A bare string return causes JSON encoding that breaks Prometheus scraping.

---

## Risk Limits

The `RiskManager` (`hypervisor/risk/manager.py`) enforces these limits every cycle in priority order. A breach triggers a 1-hour halt cooldown.

| # | Limit | Value |
|---|-------|-------|
| 1 | Portfolio drawdown | 20% of peak capital |
| 2 | Cooldown state | 1 hour after any breach |
| 3 | PnL floor | −$40 realised |
| 4 | Max open positions | 6 |
| 5 | Min free capital | 15% of total |
| 6 | Max single worker allocation | 50% of total |
| 7 | Worker drawdown | 30% of worker peak capital |

`peak_capital` resets on every re-allocation. This prevents a hypervisor-initiated rebalance from being misread as worker drawdown.

Additional safety rails (separate modules):

- **Margin reserve** (`hypervisor/risk/margin_reserve.py`) — maintains a cash buffer before any leveraged position
- **Expiry guard** (`hypervisor/risk/expiry_guard.py`) — prevents physical delivery by closing futures positions before expiry

---

## Data Sources

### Market data

| Source | What | Notes |
|--------|------|-------|
| yfinance | OHLCV — equities, ETFs, crypto | Primary market data |
| FRED | Yield curve (`T10Y2Y`), macro | Optional — yfinance fallback available |
| OKX | Perpetual funding rates, order book depth | Paper mode only |
| UUP ETF | DXY proxy | `DX=F` returns empty frames intermittently |
| BDRY ETF | Baltic Dry Index proxy | `^BDI` delisted from yfinance |
| GC=F / CL=F | Gold / oil futures | Used in gold/oil ratio feature |

### OSINT / conflict data

| Source | What | Status |
|--------|------|--------|
| GDELT v2 | Global news conflict sentiment (Goldstein score) | Working, rate-limited |
| ACLED | Conflict event data | Not used — 403 on free tier (permanent) |
| AISstream | Maritime ship tracking | Optional, websocket feed |
| NASA FIRMS | Fire/disaster detection | Optional |
| SEC EDGAR | Insider buying (Form 4) | Optional |
| UCDP | Uppsala conflict data | Optional |

GDELT scores contribute ~25% to the conflict feature input. The remaining ~75% comes from market proxy signals (defense ETF momentum, gold/oil ratio, VIX).

---

## Development Guide

### Adding a worker

1. Create `workers/<name>/worker_api.py` implementing the [REST contract](#rest-contract)
2. Add `workers/<name>/Dockerfile` (no `platform:` flag)
3. Add the service to `docker-compose.yml`
4. Register the worker key + URL in `hypervisor/main.py` `WORKER_REGISTRY`
5. Add allocation weights to all 4 regime profiles in `hypervisor/allocator/capital.py` (weights must sum to 1.0 per regime)
6. Add tests to `tests/test_integration_dryrun.py`

### Inter-service URLs

Use Docker DNS names, overridable via env vars. Never hardcode `localhost` or IPs — they break on Pi and across Docker networks.

```
NAUTILUS_URL            http://worker-nautilus:8001
PREDICTION_MARKETS_URL  http://worker-prediction-markets:8002
ANALYST_URL             http://worker-analyst:8003
CORE_DIVIDENDS_URL      http://worker-core-dividends:8006
HYPERVISOR_URL          http://hypervisor:8000
```

### Key design decisions

- **OKX only** — Binance HTTP 451, Bybit HTTP 403 from this region. OKX perp symbol format: `BTC-USDT-SWAP`
- **`asyncio.gather` for health checks** — sequential awaits in a dict comprehension caused httpx shared-client state failures
- **`worker_sharpe = None` for fresh workers** — `0.0` would trigger the Sharpe gate on the first cycle
- **Ollama healthcheck uses `ollama list`** — the `ollama/ollama` image has no curl or wget
- **Telegram profit alerts use `requests.post` directly to `api.telegram.org`** — no inter-container dependency on the bot container
- **HMM bootstrap** — if no pre-trained model exists, the classifier trains on the first 30 days of live data and saves to `hypervisor/regime/model_state/hmm_4state.pkl`

### Hypervisor cycle (step by step)

Each cycle (default 60s):

1. **Fetch market data** — yfinance, FRED, OKX funding rates, GDELT
2. **Extract features** — normalise into HMM feature vector
3. **Classify regime** — HMM posterior → 4 state probabilities
4. **Risk check** — drawdown, positions, free capital, PnL floor
5. **Allocate capital** — blend profiles by HMM posteriors, apply Sharpe penalty, apply turnover filter
6. **Broadcast regime** — POST `/regime` to all workers
7. **Push allocations** — POST `/allocate` to all healthy workers
8. **Request signals** — POST `/signal` to all healthy workers
9. **Execute** — POST `/execute` for non-advisory signals

### Circuit breaker

`hypervisor/circuit_breaker.py` wraps all external data fetches. Opens after 3 consecutive failures, enters HALF_OPEN after a cooldown, resets on success. Fallback values in `hypervisor/regime/feature_pipeline.py` (`_FALLBACKS` dict) prevent HMM input from going `NaN` when a feed is down.

---

## Production Deploy (Raspberry Pi 5)

Target: Raspberry Pi 5, 8GB+ RAM, Docker ARM64. Docker pulls native ARM64 images automatically.

**Find the Pi on your network (from Linux/macOS):**

```bash
arp -a   # look for MAC prefix DC:A6:32 or E4:5F:01
```

**Deploy:**

```bash
# Edit scripts/deploy_pi.sh — set PI_IP, then:
bash scripts/deploy_pi.sh
```

**Verify on the Pi:**

```bash
ssh pi@<PI_IP>
cd ~/ARKA && docker compose ps
curl -s http://localhost:8000/status | python3 -m json.tool
```

---

## Repository Structure

```
ARKA/
├── LICENSE                              # LGPL-3.0
├── NOTICE.txt                           # Third-party attributions
├── LGPL3_COMPLIANCE.md                  # Compliance guide for distributors
├── README.md
├── config.py                            # Python constants (PAPER_TRADING, EXCHANGES, etc.)
├── conftest.py                          # Pytest venv guard + sys.modules stubs
├── pytest.ini                           # asyncio_mode=auto, integration mark
├── requirements.txt
├── docker-compose.yml
├── arka-cli                             # CLI management script
├── install.sh                           # One-shot installer (detects hardware)
├── config/
│   ├── settings.yaml                    # System config (heartbeat, signal TTL, etc.)
│   ├── regimes.yaml                     # HMM classifier thresholds
│   ├── allocations.yaml                 # Per-regime weight profiles (human reference)
│   └── searxng/settings.yml            # SearXNG config for Analyst worker
├── hypervisor/
│   ├── Dockerfile                       # Build context = project root (.)
│   ├── main.py                          # FastAPI app, orchestration loop, APScheduler sweep
│   ├── audit.py                         # Structured audit logging
│   ├── auth.py                          # API key middleware
│   ├── circuit_breaker.py               # Circuit breaker for external dependencies
│   ├── di_container.py                  # Dependency injection container
│   ├── errors.py                        # Custom exception types
│   ├── allocator/capital.py             # RegimeAllocator — 4 states, 4 workers
│   ├── regime/
│   │   ├── classifier.py                # HMM classification entry point
│   │   ├── feature_pipeline.py          # Feature extraction + fallbacks
│   │   ├── hmm_model.py                 # HMM lifecycle (load / bootstrap / retrain)
│   │   ├── circuit_breakers.py          # Per-regime halt conditions
│   │   └── model_state/hmm_4state.pkl   # Pre-trained model (4-state Gaussian HMM)
│   ├── risk/
│   │   ├── manager.py                   # RiskManager — 7 enforcement points
│   │   ├── margin_reserve.py            # Margin call reserve
│   │   └── expiry_guard.py              # Physical delivery prevention
│   └── db/
│       ├── engine.py                    # SQLite async engine (WAL mode)
│       ├── models.py                    # RegimeLog, Signal, Order ORM models
│       └── repository.py               # ArkaRepository — all DB I/O
├── workers/
│   ├── nautilus/
│   │   ├── Dockerfile
│   │   ├── worker_api.py                # FastAPI port 8001, paper sim
│   │   ├── engine.py                    # Internal paper sim (fallback if nautilus_trader fails)
│   │   ├── indicators/adx.py            # Pure Python ADX (Wilder's smoothing)
│   │   └── strategies/
│   │       ├── swing_macd.py            # MACD + Bullish Fractal
│   │       ├── day_scalp.py             # Intraday momentum
│   │       ├── range_mean_revert.py     # Mean reversion for ranging markets
│   │       ├── factor_model.py          # Multi-factor quant
│   │       ├── order_flow.py            # Order book imbalance
│   │       └── funding_arb.py           # OKX perpetual funding arb
│   ├── prediction_markets/
│   │   ├── Dockerfile
│   │   └── worker_api.py                # FastAPI port 8002, CLOB stub
│   ├── analyst/
│   │   ├── Dockerfile
│   │   └── worker_api.py                # FastAPI port 8003, phi3:mini + SearXNG
│   ├── core_dividends/
│   │   ├── Dockerfile
│   │   └── worker_api.py                # FastAPI port 8006, SCHD+VYM paper hold
│   ├── telegram_bot/
│   │   ├── Dockerfile
│   │   └── main.py                      # Polling bot, no inbound port
│   ├── arbitrader/                      # Java arbitrage engine sidecar (not in compose)
│   └── stocksharp/                      # Phase 3 only — .NET 8 IBKR router
├── data/
│   ├── db/schema.sql                    # SQLite schema source of truth
│   └── feeds/
│       ├── market_data.py               # yfinance, FRED wrappers; UUP/BDRY proxies
│       ├── conflict_index.py            # GDELT + market proxy conflict score (0–100)
│       ├── gdelt_client.py              # GDELT v2 Doc API client
│       ├── domain_router.py             # OSINT domain entry/exit routing
│       ├── osint_processor.py           # OSINT event aggregator
│       ├── edgar_client.py              # SEC EDGAR Form 4 insider buying
│       ├── maritime_client.py           # AISstream ship tracking
│       ├── environment_client.py        # NASA FIRMS fire/disaster detection
│       ├── ucdp_client.py               # Uppsala conflict data
│       ├── funding_rates.py             # OKX perpetual funding rates
│       ├── order_book.py                # OKX order book depth
│       └── circuit_breaker.py           # Circuit breaker for feed failures
├── dashboard/
│   ├── Dockerfile                       # node:20-alpine build + nginx:alpine serve
│   ├── nginx.conf                       # port 3000, /api/* → hypervisor:8000
│   ├── package.json                     # React 18, Vite 5, @tailwindcss/vite
│   ├── vite.config.js
│   └── src/
│       ├── App.jsx                      # SetupWizard ↔ Dashboard routing
│       ├── hooks/useArkaData.js          # 10s polling hook
│       ├── styles/global.css            # Tailwind v4 tokens + @keyframes
│       ├── utils/cn.js                  # class merge utility
│       ├── pages/
│       │   ├── Dashboard.jsx            # 3-col desktop / 2-col tablet / tab-nav mobile
│       │   └── SetupWizard.jsx          # 6-step guided setup
│       └── components/
│           ├── narrative/               # RegimeMood, RiskMeter, WorkerStory,
│           │                            #   MoneyFlow, DomainMap, TimelineView
│           ├── setup/                   # WelcomeStep … ReviewStep + StepIndicator
│           ├── education/               # Tooltip, glossary.js (23 plain-English terms)
│           ├── ThesisCard.jsx
│           ├── PortfolioView.jsx
│           ├── BacktestReport.jsx
│           ├── SystemMetrics.jsx
│           └── GlobalControls.jsx
├── tests/
│   ├── test_mara.py                     # Core unit + integration tests
│   ├── test_integration_dryrun.py       # Dry-run suite (120+ pass, 7 skip)
│   ├── test_safety_rails.py             # Margin reserve, expiry guard, liquidity
│   ├── test_concurrency.py              # Race conditions, circuit breaker transitions
│   ├── test_phase5_coverage.py          # Phase 5 coverage suite
│   └── test_phase6_quality.py           # Phase 6 quality suite
└── scripts/
    └── deploy_pi.sh                     # Pi deployment script
```

---

## License

Arka is distributed under the **GNU Lesser General Public License v3.0 (LGPL-3.0)**.

Arka uses **NautilusTrader** (also LGPL-3.0). This means:

- Use Arka for free, modify it, sell hardware running it, run it commercially
- Modifications to Arka itself must remain open under LGPL-3.0
- Source code is always auditable

### Third-party licenses

| Project | License |
|---------|---------|
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | LGPL-3.0 |
| [StockSharp](https://github.com/StockSharp/StockSharp) | Apache 2.0 |

See [LGPL3_COMPLIANCE.md](./LGPL3_COMPLIANCE.md) for full distributor requirements.

Full license text: [LICENSE](./LICENSE)

---

**Arka v0.1 — April 2026 — LGPL-3.0**
