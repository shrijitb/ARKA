# ARKA: Agentic Risk Killing Algorithms

An open-source, autonomous trading system for turbulent macro environments. ARKA coordinates specialized agents and automations across crypto, futures, commodities, and prediction markets using dynamic regime-aware capital allocation.

**Status**: In development | **License**: LGPL-3.0
---

## Quick Start

### One-Line Install (Recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/shrijitb/MARA/arka-build-alpha/install.sh | bash
```

The installer will:
- Check and install dependencies (Docker, git, jq)
- Clone the repository
- Detect your hardware (Pi, laptop, desktop)
- Select the optimal LLM model and settings
- Generate `.env` with your hardware profile
- Build and launch the entire stack
- **Show a popup dialog asking if you want to launch the dashboard now**
- **Install the `arka` CLI command for easy management**

After installation, you can:
- **Launch dashboard popup**: `arka launch`
- **Check system status**: `arka status`
- **View logs**: `arka logs`
- **Stop services**: `arka stop`
- **Start services**: `arka start`

### Manual Install

```bash
git clone https://github.com/shrijitb/mara
cd mara && python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in credentials — never commit .env
docker compose up -d
```

- **Dashboard (Arka UI)**: http://localhost:3000 — guided setup wizard on first run
- **Hypervisor API**: `curl -s http://localhost:8000/status | python3 -m json.tool`

→ Full setup per OS (Windows 11 / Ubuntu 24.04 / macOS): [Setup](#setup)

### CLI Commands

After installation, you can manage Arka using the `arka` command:

```bash
arka launch          # Open dashboard popup in browser
arka status          # Check system status and worker health
arka logs            # View service logs (last 100 lines)
arka stop            # Stop all services
arka start           # Start all services
arka restart         # Restart all services
arka install         # Run installation (alias for install.sh)
```

---

## Table of Contents

- [Project Overview](#project-overview)
- [Architecture](#architecture)
- [Regime-Gated Capital Allocation](#regime-gated-capital-allocation)
- [Conflict Index](#conflict-index)
- [Dashboard](#dashboard)
- [Setup](#setup)
  - [Windows 11](#windows-11)
  - [Ubuntu 24.04](#ubuntu-2404)
  - [macOS](#macos)
- [Configuration](#configuration)
- [Running the Stack](#running-the-stack)
- [Session Re-entry](#session-re-entry)
- [Tests](#test-suite)
- [Telegram Bot](#telegram-bot)
- [REST Contract](#rest-contract)
- [Risk Limits](#risk-limits)
- [Feature Status](#feature-status)
- [Known Constraints](#known-constraints)
- [Architecture Deep Dive](#architecture-deep-dive)
- [Data Sources](#data-sources)
- [Production Deploy](#production-deploy-raspberry-pi-5)
- [Development](#development)
- [License](#license)

---

## Project Overview

Arka targets **futures, crypto, forex, ETFs, and prediction markets** in regime-driven trading.

### Core Design

A **FastAPI Hypervisor** orchestrates five specialized worker agents:

- **Nautilus**: ADX-routed strategy engine — MACD+Fractals for trending, mean-reversion for ranging
- **Prediction Markets**: CLOB market-making on prediction markets (stub until Phase 3)
- **Analyst**: LLM-based advisory (phi3:mini via Ollama + SearXNG web search)
- **Core Dividends**: Passive SCHD + VYM ETF buy-and-hold sleeve

Capital flows dynamically based on **7 market regimes** (WAR_PREMIUM, CRISIS_ACUTE, BEAR_RECESSION, etc.). The hypervisor includes:
- Regime classifier (market data + ACLED + GDELT conflict index)
- Capital allocator (regime → per-worker weights, normalised)
- Risk manager (drawdown, VaR, cooldown, position limits)
- Quarterly profit sweep (APScheduler, Jan/Apr/Jul/Oct 7th @ 09:00)

### Environment

| Layer | Spec |
|-------|------|
| **Dev (pre-March 2026)** | Windows 11 → WSL2 Ubuntu-24.04 → Docker Desktop 4.63.0 |
| **Dev (March 2026+)** | Ubuntu 24.04 native |
| **Production** | Raspberry Pi 5 (16GB) → Docker ARM64 (buildx + QEMU) |
| **Exchange** | OKX only (Binance HTTP 451, Bybit HTTP 403 — geo-blocked) |
| **Data** | yfinance, FRED, ACLED, GDELT, live order books |

---

## Architecture

```
                        ┌────────────────────────────┐
                        │  Hypervisor (port 8000)    │
                        │  FastAPI orchestrator      │
                        │  ┌──────────────────────┐  │
                        │  │  Regime Classifier   │  │
                        │  │  Conflict Index      │  │
                        │  │  Capital Allocator   │  │
                        │  │  Risk Manager        │  │
                        │  └──────────────────────┘  │
                        └──┬─────────────────────────┘
                           │ allocates + commands
     ┌─────────────────────┼──────────────────────────┐
     ▼                     ▼                          ▼
┌─────────────┐  ┌──────────────────────┐  ┌────────────────┐
│   nautilus  │  │  prediction-markets  │  │ core_dividends │
│  port 8001  │  │      port 8002       │  │   port 8006    │
│ ADX-routed  │  │     CLOB stub        │  │  SCHD+VYM hold │
└─────────────┘  └──────────────────────┘  └────────────────┘

┌──────────────┐  ┌────────────┐  ┌─────────────────────┐
│ telegram-bot │  │   ollama   │  │       analyst       │
│  polling,    │  │ port 11434 │  │      port 8003      │
│   no port    │  │ phi3:mini  │  │ phi3:mini + searxng │
└──────────────┘  └────────────┘  └─────────────────────┘
```

### Worker Map

| Container | Key | Port | Tech | Mode |
|-----------|-----|------|------|------|
| arka-hypervisor | — | 8000 | FastAPI | Orchestrator |
| arka-nautilus | `nautilus` | 8001 | NautilusTrader | Paper sim (ADX-routed) |
| arka-prediction-markets | `prediction_markets` | 8002 | Python CLOB | Stub — needs `POLY_PRIVATE_KEY` |
| arka-analyst | `analyst` | 8003 | litellm + Ollama | phi3:mini + SearXNG advisory |
| arka-core-dividends | `core_dividends` | 8006 | FastAPI | Paper hold (SCHD + VYM) |
| arka-ollama | — | 11434 | Ollama | phi3:mini CPU inference |
| arka-telegram-bot | — | none | python-telegram-bot | Polling — no inbound port |
| arka-searxng | — | 8080 | SearXNG | Local web search for Analyst |
| workers/stocksharp | — | 8005 | .NET 8 | Phase 3 only (IBKR routing) |

---

## Regime-Gated Capital Allocation

The classifier outputs one of **4 HMM states** using a Hidden Markov Model. Capital is split across workers per profile, with a cash buffer enforced at all times. Weights normalise against the sum of all non-zero profile entries — not just healthy workers — so a single worker starting up cannot absorb more than its intended share.

| Regime | nautilus | prediction_markets | analyst | core_dividends | Max deploy |
|--------|---------|-------------------|---------|----------------|-----------|
| `RISK_ON` | 44% | 12% | 8% | 36% | 80% |
| `RISK_OFF` | 34% | 18% | 8% | 40% | 75% |
| `CRISIS` | 10% | 20% | 0% | 30% | 50% |
| `TRANSITION` | 32% | 16% | 8% | 44% | 70% |

Priority order (first match wins): `CRISIS > TRANSITION > RISK_OFF > RISK_ON`

In `CRISIS` state, 50% stays in cash by design.

Sharpe penalty: if a worker's rolling Sharpe < 0.5, its allocation is halved. Fresh workers store `None` Sharpe (not `0.0`) to avoid false penalisation.

---

## Conflict Index

A 0–100 war premium score that is the primary input to `WAR_PREMIUM` classification:

| Source | Weight | Status |
|--------|--------|--------|
| Market proxy (defense ETF momentum + gold/oil ratio + VIX) | 70–75% | Working |
| ACLED CAST lethal event forecasts | 20% | 403 on free tier — permanently unavailable |
| ACLED live conflict events | 5% | 403 on free tier — permanently unavailable |
| GDELT conflict queries | 5–25% | Working (rate-limited) |

`WAR_PREMIUM` fires when `war_premium_score > 25`. Score currently runs on market proxy + GDELT only.

**Data source decisions:**
- **DXY proxy**: `UUP` ETF (Invesco DB US Dollar Index Bullish Fund) — `DX=F` and `DX-Y.NYB` return empty frames intermittently via yfinance
- **BDI proxy**: `BDRY` ETF — `^BDI` delisted from yfinance
- **Gold/oil ratio threshold**: 45.0 — gold near $5,000 puts the ratio at ~57 baseline; old threshold of 20 false-triggered WAR_PREMIUM in peacetime commodity bull markets
- **Market proxy at 70–75%**: ensures no single geopolitical data source can unilaterally trigger a regime change

---

## Dashboard

**Arka** is the beginner-friendly visual interface for MARA. It runs as a separate Docker container on port 3000 and translates every data point into plain English and visual metaphors.

### What it looks like

- **Regime Mood** — weather icon (☀️ calm / ⛅ risk-off / 🌧 bear / ⛈ crisis) with animated stacked probability bars
- **Risk Meter** — SVG semicircular gauge (green/amber/red) updated every 10 seconds
- **Worker Stories** — character cards showing each agent's activity in plain English, with a pause modal for manual intervention
- **Money Flow** — animated allocation bars showing how capital is distributed across workers in real time
- **Domain Intelligence** — OSINT entry/exit signals color-coded by action (green = enter, red = exit, amber = watch)
- **Analyst Pipeline** — Director / Quant / Risk three-stage trade thesis review

### Setup wizard

On first run (before credentials are saved), Arka shows a 6-step guided wizard:

1. **Welcome** — overview of what Arka does
2. **Device** — detects Pi vs. laptop, shows hardware profile
3. **Exchange** — OKX API keys (optional — paper trading works without them)
4. **Data Sources** — FRED, NASA FIRMS, AISstream, UCDP, Kalshi (all optional)
5. **Notifications** — Telegram bot token + User ID, or ntfy.sh topic
6. **Review & Launch** — checklist of configured services, one-click launch

### Build

```bash
docker compose build dashboard
docker compose up -d
# http://localhost:3000
```

### Dev mode (live reload against local hypervisor)

```bash
cd dashboard
npm install
npm run dev
# Vite dev server on :5173, /api proxied to localhost:8000
```

### Stack

| Layer | Tech |
|-------|------|
| Framework | React 18 with functional components + hooks |
| Build | Vite 5 |
| Styles | Tailwind CSS v4 (`@tailwindcss/vite`, no config file) |
| Serve | nginx:alpine on port 3000 |
| Data | `useArkaData` hook polls `/api/dashboard/state` + `/api/setup/status` every 10s |

---

## Setup

### Windows 11

MARA runs in Docker containers. On Windows, all development happens inside WSL2.

**1. Install WSL2**

Open PowerShell as Administrator:
```powershell
wsl --install
# Reboot when prompted. Ubuntu 24.04 LTS is installed by default.
```

**2. Install Docker Desktop for Windows**

Download from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/).

During install: select "Use WSL 2 instead of Hyper-V". After install:
- Open Docker Desktop → Settings → Resources → WSL Integration
- Enable integration for Ubuntu-24.04
- Apply & Restart

**3. Open Ubuntu 24.04 and install Python 3.12**

```bash
# Start → Ubuntu 24.04  (or: wsl -d Ubuntu-24.04 in PowerShell)
sudo apt update && sudo apt install -y python3.12 python3.12-venv python3.12-dev git curl
```

**4. Clone and create the virtual environment**

```bash
cd ~
git clone https://github.com/shrijitb/mara.git
cd mara
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**5. Configure `.env`** — see [Configuration](#configuration) below.

**6. Start the stack**

```bash
docker compose up -d
```

> Docker Desktop on Windows includes buildx and QEMU. ARM64 images for Pi cross-build work automatically — do **not** add `platform:` flags to `docker-compose.yml`.

---

### Ubuntu 24.04

**1. Install Docker Engine**

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Run docker without sudo (log out and back in after)
sudo usermod -aG docker $USER
```

**2. Install Python 3.12**

```bash
sudo apt install -y python3.12 python3.12-venv python3.12-dev git curl
```

**3. Clone and set up**

```bash
cd ~
git clone https://github.com/shrijitb/mara.git
cd mara
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**4. QEMU for ARM64 cross-build** (optional — only needed to build Pi images on x86)

```bash
sudo apt install -y qemu-user-static
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
```

**5. Configure `.env`**, then start:

```bash
docker compose up -d
```

---

### macOS

**1. Install Docker Desktop for Mac**

Download from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/). Install and start it. Docker Desktop on Mac includes buildx, compose, and QEMU.

**2. Install Python 3.12 via Homebrew**

```bash
# Install Homebrew if needed:
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install python@3.12 git
```

**3. Clone and set up**

```bash
cd ~
git clone https://github.com/shrijitb/mara.git
cd mara
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**4. Configure `.env`**, then start:

```bash
docker compose up -d
```

> **Apple Silicon (M1/M2/M3):** Docker Desktop handles ARM64 natively. Do **not** add `platform:` flags — they cause `exec format error` when pre-built native binaries (e.g. `ollama/ollama`) are forced through QEMU.

---

## Configuration

Copy `.env.example` to `.env` and fill in credentials. **Never commit `.env` to git.**

```ini
# Trading mode
MARA_MODE=backtest
MARA_LIVE=false
CYCLE_INTERVAL_SEC=60

# Safety defaults — do not change until paper trading is validated
PAPER_TRADING=true
USE_LIVE_RATES=false
USE_LIVE_OHLCV=false
INITIAL_CAPITAL_USD=200.0
EXCHANGES=["okx"]

# Ollama (local LLM for AutoHedge)
OLLAMA_HOST=http://ollama:11434
OLLAMA_MODEL=phi3:mini

# Data sources
FRED_API_KEY=                         # optional — yfinance fallback works without it
ACLED_EMAIL=
ACLED_PASSWORD=                       # free tier: token works, data endpoints 403

# OKX (only active exchange)
OKX_API_KEY=                          # Phase 3 only — leave empty for paper trading
OKX_API_SECRET=
OKX_API_PASSPHRASE=

# Polymarket (Phase 3 — leave empty)
POLY_PRIVATE_KEY=
POLY_MAX_EXPOSURE_USD=1000.0
POLY_MIN_EXPOSURE_USD=-1000.0

# IBKR (Phase 3 — leave as-is)
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=1

# Telegram bot
TELEGRAM_BOT_TOKEN=                   # from @BotFather
TELEGRAM_ALLOWED_USER_ID=             # from @userinfobot
```

### Regime thresholds (`config/regimes.yaml`)

```yaml
war_defense_momentum:  0.08
war_premium_threshold: 25.0
war_gold_oil_ratio:    45.0   # gold ~$5000 → ratio ~57 at baseline
crisis_vix:            40.0
crisis_yield_curve:   -0.50
bear_yield_curve:      0.0
bear_vix:             25.0
frothy_vix:           15.0
frothy_funding_rate:   0.0003
```

---

## Running the Stack

```bash
# Start everything
docker compose up -d

# Tail hypervisor logs
docker compose logs -f hypervisor

# Check container health
docker compose ps

# Rebuild a service after a code change
docker compose build --no-cache <service-name>
docker compose up -d          # always restart the FULL stack after any rebuild

# Stop everything
docker compose down
```

> **Critical:** Always restart with `docker compose up -d` after any rebuild. Using `--force-recreate` on a single container disconnects it from the Docker network shared by other services.

### Check live state

```bash
curl -s http://localhost:8000/status | python3 -m json.tool
curl -s http://localhost:8000/regime
curl -s http://localhost:8000/watchlist
curl -s http://localhost:8001/health   # nautilus
curl -s http://localhost:8002/health   # prediction-markets
curl -s http://localhost:8003/health   # analyst
curl -s http://localhost:8006/health   # core_dividends
```

---

## Session Re-entry

### Windows 11 / WSL2

```powershell
wsl -d Ubuntu-24.04
```
```bash
cd ~/mara
source .venv/bin/activate
docker compose up -d
curl -s http://localhost:8000/status | python3 -m json.tool
```

### Ubuntu 24.04 / macOS

```bash
cd ~/mara
source .venv/bin/activate
docker compose up -d
curl -s http://localhost:8000/status | python3 -m json.tool
```

---

## Test Suite

**Always use the venv Python directly** — the system Python lacks `fastapi`, `httpx`, and other dependencies.

```bash
~/mara/.venv/bin/python -m pytest tests/ -v
# Expected: 120+ passed, 7 skipped, 0 failed
```

### Test breakdown

| Class | What it tests | Status |
|-------|--------------|--------|
| `TestWorkerContract` | nautilus + analyst REST contract endpoints | Pass |
| `TestCapitalAllocator` | dollar splits, Sharpe penalty, cold-start single-worker cap regression | Pass |
| `TestRiskManagerIntegration` | all 7 risk limits, cooldown, re-allocation peak-reset regression | 10/10 pass |
| `TestHypervisorCycle` | registry keys, capital math, classifier shape | 4/4 pass |
| `TestEndToEndSignalSchema` | signal format, `advisory_only` enforcement | 4/6 pass, 2 skip |
| `TestAcledIntegration` | token acquisition, CAST, live events | 1 pass, 2 skip (free tier) |
| `TestGdeltIntegration` | GDELT query, conflict scoring | 3/3 pass |
| `TestAdxCalculator` | trending/ranging/ambiguous classification, value range | 5/5 pass |
| `TestRangeMeanRevertStrategy` | dead-market filter, insufficient bars, signal structure | 4/4 pass |
| `TestStrategyRouter` | swing-forced mode, auto default, `/strategy` runtime override | 3/3 pass |

### Expected skips

- `analyst` and `prediction_markets` contract tests — `litellm` / `py_clob_client` not in venv (pass in Docker)
- ACLED CAST + live events — free tier 403 is permanent, not a bug

---

## Telegram Bot

Set `TELEGRAM_BOT_TOKEN` (from [@BotFather](https://t.me/BotFather)) and `TELEGRAM_ALLOWED_USER_ID` (from [@userinfobot](https://t.me/userinfobot)) in `.env`, then restart the stack.

The bot uses long-polling — no inbound port or webhook required. All commands are rejected for any Telegram user ID other than `TELEGRAM_ALLOWED_USER_ID`.

| Command | Description |
|---------|-------------|
| `/status` | Capital snapshot, regime, worker health, PnL |
| `/regime` | Current regime label + confidence |
| `/watchlist` | Dynamic ticker watchlist |
| `/pause <worker>` | Halt new entries for a specific worker |
| `/resume <worker>` | Resume a paused worker |
| `$TICKER` (free text) | Any message containing e.g. `$AAPL` adds the ticker to the watchlist |

---

## REST Contract

Every trading worker implements these endpoints:

```
GET  /health    → {"status": "ok", "paused": bool, ...worker-specific fields}
GET  /status    → {"pnl": float, "sharpe": float, "allocated_usd": float, "open_positions": int}
GET  /metrics   → Prometheus text format
POST /signal    → list of signal dicts (each has "action", "rationale", "advisory_only")
POST /execute   → executes trade or returns advisory
POST /allocate  → {"amount_usd": float, "paper_trading": bool}
POST /pause     → halt new entries
POST /resume    → resume entries
POST /regime    → {"regime": str, "confidence": float, "paper_trading": bool}
```

Hypervisor-only endpoints:

```
GET  /watchlist               → {"watchlist": ["AAPL", ...]}
POST /watchlist               → {"ticker": "AAPL"}
POST /workers/{worker}/pause  → pause a named worker
POST /workers/{worker}/resume → resume a named worker
```

> **`/metrics` pattern:** Always return `Response(content=text, media_type="text/plain")` from FastAPI. A bare string return causes JSON encoding that breaks Prometheus scraping.

---

## Risk Limits

The `RiskManager` enforces these limits every cycle. A breach triggers a 1-hour halt cooldown.

| Limit | Value |
|-------|-------|
| Max portfolio drawdown | 20% |
| Max single worker allocation | 50% of total capital |
| Max open positions | 6 |
| Min free capital | 15% of total capital |
| PnL floor | −$40 |
| Worker max drawdown | 30% of worker's peak capital |
| Cooldown after breach | 1 hour |

**Drawdown baseline:** `peak_capital` resets on every capital re-allocation. This prevents a hypervisor-initiated rebalance (more workers coming online mid-run) from being misread as a worker drawdown.

---

## Feature Status

| ID | Feature | Status | Notes |
|----|---------|--------|-------|
| F-01 | Telegram command bot | Done | Polling, auth-gated, all commands implemented |
| F-02 | Passive dividend sleeve (`core_dividends`) | Done | SCHD + VYM paper hold, port 8006 |
| F-03 | Inverse ETF recession signals (SH, PSQ) | Done | Advisory-only until IBKR wired (Phase 3) |
| F-04 | Quarterly profit sweep skeleton | Done | APScheduler cron Jan/Apr/Jul/Oct 7th @ 09:00 |
| F-05 | Analyst binary sanity check | Waiting | Implement after 1–2 weeks phi3:mini observation |
| F-06 | Prediction Markets far-book live test | Blocked | Requires `POLY_PRIVATE_KEY` |
| F-07 | Analyst sentiment multiplier (1.1×/0.5×) | Parked | High complexity; wait for F-05 stable |
| F-08 | Analyst + GDELT dynamic watchlist | Parked | High complexity |
| F-09 | NautilusTrader full backtest harness | Parked | After 4+ weeks paper trading data |
| F-10 | Arka Visual Dashboard | Done | React 18 + Tailwind v4 at `dashboard/` |

> **Session continuity:** [`FUTURE_WORK.md`](./FUTURE_WORK.md) contains copy-paste prompts for the next Claude Code session covering F-05, F-06, Pi deploy, Phase 3 wiring, optimizer wiring, and observability.

---

## Known Constraints

| Constraint | Detail |
|-----------|--------|
| **OKX only** | Binance HTTP 451, Bybit HTTP 403 from this region. OKX perp symbol format: `BTC-USDT-SWAP` |
| **ACLED free tier** | OAuth token acquired successfully. `/api/cast/read` and `/api/acled/read` return 403 — requires approved researcher account. Permanent. |
| **IBKR Phase 3** | StockSharp `.NET 8` scaffold exists in `workers/stocksharp/` but not wired until Phase 3 |
| **Polymarket stub** | No live CLOB trading until `POLY_PRIVATE_KEY` is configured |
| **`advisory_only=True`** | All recession pair (SH, PSQ) and dividend signals are logged but not executed until IBKR is connected |
| **No `platform:` flags** | Adding platform constraints causes `exec format error` for pre-built native binaries (e.g. `ollama/ollama`) under QEMU |
| **`ollama/ollama` has no curl** | Healthcheck uses `["CMD", "ollama", "list"]` — not curl |
| **`python:3.11-slim` has no curl** | Polymarket healthcheck uses `python3 -c "import urllib.request; ..."` |

---

## Architecture Deep Dive

### Hypervisor Loop

Each cycle (default 60 seconds):

1. **Fetch market data** — yfinance, FRED, ACLED, GDELT
2. **Classify regime** — market proxy (70–75%) + conflict scores
3. **Allocate capital** — regime profile → per-worker amounts
4. **Check health** — concurrent `asyncio.gather` to all workers (sequential awaits caused httpx client state failures)
5. **Request signals** — POST `/signal` to all healthy workers
6. **Risk check** — drawdown, VaR, position limits, cooldown state
7. **Execute** — POST `/execute` to eligible workers (skips `advisory_only` signals)
8. **Log metrics** — Prometheus scrape points updated
9. **Sleep** — until next cycle

### Capital Allocator

```python
# Normalise against total profile weight, not just healthy workers.
# A single worker starting up must not absorb all of max_deploy.
profile_nonzero_sum = sum(w for w in profile.values() if w > 0.0)
allocations = {
    worker: round(max_deploy * (weight / profile_nonzero_sum), 2)
    for worker, weight in eligible.items()
}
```

Sharpe penalty halves allocation if rolling Sharpe < 0.5. Fresh workers start with `None` Sharpe — not `0.0` — to avoid false penalisation.

### Risk Manager — Seven Enforcement Points

1. **Portfolio drawdown** — DD > 20% → cooldown
2. **Cooldown** — 1-hour halt after any violation
3. **PnL floor** — realised PnL < −$40 → cooldown
4. **Max positions** — open positions > 6 → reject signal
5. **Free capital** — free USD < 15% → reject signal
6. **Worker cap** — single worker > 50% → cap allocation
7. **Worker drawdown** — worker DD > 30% of peak → exclude worker

---

## Data Sources

### Market Data

- **OHLCV**: yfinance (equities, ETFs, crypto)
- **Macro**: FRED (fed funds rate, 10Y–2Y yield spread, DXY via UUP ETF)
- **Commodities**: yfinance (gold, oil, BDI proxy via BDRY ETF)
- **Sentiment**: VIX, BTC funding rates

### Conflict Index Sources

- **ACLED CAST**: 21-day lethal conflict forecasts — 403 on free tier (permanent)
- **ACLED live events**: Recent conflict events by country — 403 on free tier (permanent)
- **GDELT**: Global news conflict sentiment (Goldstein score) — working, rate-limited

Score composition (without ACLED): 75% market proxy + 25% GDELT

---

## Production Deploy (Raspberry Pi 5)

Target: Raspberry Pi 5, 16GB RAM, Docker ARM64. Docker pulls native ARM64 images automatically — no `platform:` flags needed.

**Find the Pi on your local network (from Windows):**

```powershell
arp -a   # look for MAC prefix DC:A6:32 or E4:5F:01
```

**Deploy:**

```bash
# Edit scripts/deploy_pi.sh — fill in the Pi's IP address, then:
bash ~/mara/scripts/deploy_pi.sh
```

**Verify on the Pi:**

```bash
ssh pi@<PI_IP>
cd ~/mara && docker compose ps
curl -s http://localhost:8000/status | python3 -m json.tool
```

---

## Development

### Adding a worker

1. Create `workers/<name>/worker_api.py` implementing the [REST contract](#rest-contract)
2. Add `workers/<name>/Dockerfile` (no `platform:` flag)
3. Add the service to `docker-compose.yml` with `depends_on: hypervisor: condition: service_healthy`
4. Register the worker key + URL in `hypervisor/main.py` `WORKER_REGISTRY`
5. Add allocation weights to all 7 regime profiles in `hypervisor/allocator/capital.py` (weights must sum to 1.0 per regime)
6. Add tests to `tests/test_integration_dryrun.py`

### Inter-service URL convention

Use Docker DNS names, overridable via env vars. Never hardcode `localhost` or IPs — they break on Pi and across Docker networks.

```
NAUTILUS_URL            http://worker-nautilus:8001
PREDICTION_MARKETS_URL  http://worker-prediction-markets:8002
ANALYST_URL             http://worker-analyst:8003
CORE_DIVIDENDS_URL      http://worker-core-dividends:8006
HYPERVISOR_URL          http://hypervisor:8000
```

### Key design decisions

- **OKX only** — Binance HTTP 451, Bybit HTTP 403. Symbol format: `BTC-USDT-SWAP`
- **Market proxy primary (70–75%)** — prevents commodity bull markets from false-triggering WAR_PREMIUM
- **2-of-N signal requirement per regime** — prevents single-indicator false positives
- **Workers are FastAPI processes** — not Python class hierarchies; pluggable and replaceable
- **`asyncio.gather` for health checks** — sequential awaits in a dict comprehension caused httpx shared-client state failures
- **`worker_sharpe` stores `None` for fresh workers** — `0.0` would trigger the Sharpe gate every cycle
- **Ollama healthcheck uses `ollama list`** — `ollama/ollama` image has no curl/wget
- **Telegram bot sends direct via Bot API** — hypervisor uses `requests.post` to `api.telegram.org` for notifications; no inter-container dependency on the bot container

---

## License

MARA is distributed under the **GNU Lesser General Public License v3.0 (LGPL-3.0)**.

### Why LGPL-3.0?

MARA uses **NautilusTrader** (also LGPL-3.0), which requires the same license. This is a feature:

- Use MARA for free, forever
- Modify it for your needs
- Sell hardware devices running MARA
- Run it commercially
- Keep modifications private
- Source code remains open and auditable

### Third-party licenses

| Project | License |
|---------|---------|
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | LGPL-3.0 |
| [StockSharp](https://github.com/StockSharp/StockSharp) | Apache 2.0 |
| [AutoHedge](https://github.com/The-Swarm-Corporation/AutoHedge) | MIT |

All licenses compatible with LGPL-3.0.

### Distribution requirements

If you **distribute** MARA (e.g. as a Pi device or hosted deployment):
- Include `LICENSE` and `NOTICE.txt`
- Provide a link to source code
- Document any modifications

See [LGPL3_COMPLIANCE.md](./LGPL3_COMPLIANCE.md) and [HARDWARE_DISTRIBUTION.md](./HARDWARE_DISTRIBUTION.md) for full requirements.

Full license text: [LICENSE](./LICENSE) | [LGPL-3.0](https://www.gnu.org/licenses/lgpl-3.0.txt)

---

## File Structure

```
mara/
├── LICENSE                              # LGPL-3.0
├── NOTICE.txt                           # Third-party attributions
├── LGPL3_COMPLIANCE.md                  # Compliance guide
├── HARDWARE_DISTRIBUTION.md             # Pi device distribution guide
├── README.md                            # This file
├── conftest.py                          # Pytest venv guard + sys.modules stubs
├── config.py                            # Python constants
├── pytest.ini                           # asyncio_mode=auto, integration mark
├── requirements.txt                     # Python dependencies
├── docker-compose.yml                   # Multi-container definition
├── config/
│   ├── settings.yaml
│   ├── regimes.yaml                     # Classifier thresholds (recalibrated March 2026)
│   └── allocations.yaml                 # Weight documentation (mirrors capital.py)
├── dashboard/                           # Arka Visual Dashboard
│   ├── Dockerfile                       # node:20-alpine build + nginx:alpine serve
│   ├── nginx.conf                       # port 3000, /api/ → hypervisor:8000
│   ├── package.json                     # React 18, Vite 5, Tailwind v4
│   ├── vite.config.js
│   └── src/
│       ├── App.jsx                      # SetupWizard ↔ Dashboard routing
│       ├── hooks/useArkaData.js          # 10s polling hook
│       ├── styles/global.css            # Tailwind v4 + custom color tokens
│       ├── utils/cn.js                  # class merge utility
│       ├── pages/
│       │   ├── Dashboard.jsx            # 3-col/2-col/mobile-tab layout
│       │   └── SetupWizard.jsx          # 6-step guided setup
│       └── components/
│           ├── narrative/               # RegimeMood, RiskMeter, WorkerStory,
│           │                            #   MoneyFlow, DomainMap, TimelineView
│           ├── setup/                   # WelcomeStep … ReviewStep + StepIndicator
│           ├── education/               # Tooltip, glossary (23 terms)
│           ├── ThesisCard.jsx
│           ├── PortfolioView.jsx
│           ├── BacktestReport.jsx
│           ├── SystemMetrics.jsx
│           └── GlobalControls.jsx       # emergency stop modal
├── hypervisor/
│   ├── Dockerfile                       # Build context is project root (.)
│   ├── main.py                          # FastAPI orchestrator + APScheduler sweep
│   ├── allocator/capital.py             # RegimeAllocator — 7 regimes, 5 workers
│   ├── regime/
│   │   ├── classifier.py                # Regime detector
│   │   ├── circuit_breakers.py
│   │   ├── feature_pipeline.py
│   │   └── hmm_model.py
│   └── risk/manager.py                  # Risk enforcement
├── workers/
│   ├── nautilus/
│   │   ├── worker_api.py                # FastAPI port 8001, paper sim
│   │   └── strategies/swing_macd.py     # MACD + Bullish Fractal strategy
│   ├── prediction_markets/worker_api.py # FastAPI port 8002, CLOB stub
│   ├── analyst/worker_api.py            # FastAPI port 8003, phi3:mini + SearXNG advisory
│   ├── core_dividends/worker_api.py     # FastAPI port 8006, SCHD+VYM hold
│   ├── telegram_bot/main.py             # Polling bot, no port
│   └── stocksharp/                      # Phase 3 only — .NET 8 IBKR router
├── data/feeds/
│   ├── market_data.py                   # yfinance, FRED wrappers; UUP/BDRY proxies
│   ├── conflict_index.py                # ACLED + GDELT + market proxy fusion
│   ├── domain_router.py                 # OSINT domain entry/exit routing
│   ├── osint_processor.py               # OSINT event aggregator
│   ├── edgar_client.py                  # SEC EDGAR insider buying
│   ├── gdelt_client.py                  # GDELT v2 conflict queries
│   ├── maritime_client.py               # AISstream ship tracking
│   ├── environment_client.py            # NASA FIRMS fire detection
│   ├── ucdp_client.py                   # Uppsala Conflict Data
│   ├── funding_rates.py                 # OKX perpetual funding rates
│   └── order_book.py                    # OKX order book depth
├── tests/
│   ├── test_mara.py                     # Unit + integration tests
│   └── test_integration_dryrun.py       # Dry-run integration suite (120+ pass, 7 skip)
└── scripts/
    └── deploy_pi.sh                     # Raspberry Pi deployment script
```

---

## Disclaimer

MARA is a research and development system. Past performance does not guarantee future results. Trading involves risk of loss. Start with paper trading and thoroughly validate before live deployment. No warranty. Use at your own risk.

---

**ARKA v0.1 | April 2026 | LGPL-3.0**
