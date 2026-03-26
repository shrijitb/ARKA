# MARA: Multi-Agent Risk-Adjusted Capital Orchestrator

An open-source, autonomous trading system for turbulent macro environments. MARA coordinates specialized AI agents across crypto, futures, commodities, and prediction markets using dynamic regime-aware capital allocation.

**Status**: Paper trading MVP (March 2026) | **License**: LGPL-3.0

---

## Quick Start

```bash
# Clone and setup
git clone https://github.com/shrijitb/mara
cd mara && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run full stack
docker compose up -d

# Monitor hypervisor
docker compose logs -f hypervisor

# Check status
curl -s http://localhost:8000/status | python3 -m json.tool
```

---

## Project Overview

MARA targets **futures, crypto, forex, ETFs, and prediction markets** in regime-driven trading. The system operates in sequence: **backtest → paper trading → live**.

### Core Design

A **FastAPI Hypervisor** orchestrates five specialized worker agents:
- **Nautilus**: MACD+Fractals swing trading on OKX perps
- **Polymarket**: CLOB market-making on prediction markets
- **AutoHedge**: LLM-based advisory (phi3:mini on Ollama)
- **Arbitrader**: Cross-exchange spread arbitrage
- **StockSharp**: Interactive Brokers order routing (Phase 2)

Capital flows dynamically based on **7 market regimes** (WAR_PREMIUM, CRISIS_ACUTE, BEAR_RECESSION, etc.). The hypervisor includes:
- Regime classifier (market data + ACLED + GDELT)
- Capital allocator (regime → worker weights)
- Risk manager (drawdown, VaR, position limits)

### Environment

| Layer | Spec |
|-------|------|
| **Dev** | Windows laptop → WSL2 Ubuntu-24.04 → Docker Desktop 4.63.0 |
| **Production** | Raspberry Pi 5 (16GB) → Docker ARM64 (buildx + QEMU) |
| **Exchange** | OKX only (Binance 451, Bybit 403 — geo-blocked) |
| **Data** | yfinance, FRED, ACLED, GDELT, live order books |

---

## Architecture

### Hypervisor (Port 8000)

Orchestrates workers via REST, manages capital flow, enforces risk limits.

```
Regime Classifier (market data + conflict index)
         ↓
Capital Allocator (regime → worker allocations)
         ↓
Risk Manager (drawdown, VaR, cooldown, position caps)
         ↓
Worker Orchestrator (execute signals, monitor health)
```

### Workers (Ports 8001-8005)

All workers implement the same REST contract:

```
GET  /health     → {"status": "ok"}
GET  /status     → {"pnl": float, "sharpe": float, "allocated_usd": float, ...}
GET  /metrics    → Prometheus text (text/plain, not JSON)
POST /signal     → [{"side": "BUY", "price": 45000, "size": 0.1}, ...]
POST /execute    → {"order_id": "...", "status": "filled"}
POST /allocate   → {"amount_usd": 1000, "paper_trading": true}
POST /pause      → {"paused": true}
POST /resume     → {"paused": false}
POST /regime     → {"regime": "WAR_PREMIUM", "confidence": 0.80}
```

**Critical**: `/metrics` must return `Response(content=..., media_type="text/plain")` for Prometheus parsing.

### Regime States

| Regime | Trigger | Allocation |
|--------|---------|-----------|
| WAR_PREMIUM | geopolitical crisis, defense ETF momentum, gold/oil spike | arb 45%, poly 30%, nautilus 25% |
| CRISIS_ACUTE | VIX >40, yield inversion | arb 40%, poly 20%, nautilus 10% |
| BEAR_RECESSION | sustained downturn | nautilus 45%, arb 25%, poly 20% |
| BULL_FROTHY | low VIX, high funding rates | nautilus 45%, arb 35%, poly 10% |
| REGIME_CHANGE | transition signals | arb 40%, nautilus 30%, poly 20% |
| SHADOW_DRIFT | hidden pressure, BDI moving | arb 40%, nautilus 35%, poly 15% |
| BULL_CALM | default, no stress | nautilus 45%, arb 30%, poly 10% |

Detection logic:
- Market proxy (70-75%): VIX, DXY, gold/oil ratio, BDI, defense momentum
- ACLED CAST (20%): 21-day conflict forecast
- GDELT (5-25%): recent conflict sentiment (Goldstein score)

---

## Configuration

### Structure

```
config/
├── settings.yaml       # runtime parameters (CYCLE_INTERVAL_SEC, etc.)
├── regimes.yaml        # classifier thresholds (recalibrated March 2026)
└── allocations.yaml    # worker weight documentation (mirrors capital.py)

config.py              # Python constants (separate from config/ directory)
```

### Key Environment Variables

```bash
# Execution
MARA_MODE=backtest                    # or: live
MARA_LIVE=false
PAPER_TRADING=true

# Safety Defaults (DO NOT CHANGE)
USE_LIVE_RATES=false
USE_LIVE_OHLCV=false
EXCHANGES=["okx"]
INITIAL_CAPITAL_USD=200.0
CYCLE_INTERVAL_SEC=60

# Data Sources
FRED_API_KEY=<optional>               # Federal Reserve Economic Data
ACLED_EMAIL=<your-email>              # Armed Conflict Location & Event Data
ACLED_PASSWORD=<configure>
GDELT_API_KEY=<optional>              # Global Database of Events, Language, and Tone

# LLM & Inference
OLLAMA_HOST=http://ollama:11434
OLLAMA_MODEL=phi3:mini

# Trading
POLY_PRIVATE_KEY=<Phase 3>            # Polymarket signing key
IBKR_HOST=127.0.0.1                   # Interactive Brokers (Phase 2)
IBKR_PORT=7497
IBKR_CLIENT_ID=1
```

### Safety Defaults

```python
PAPER_TRADING       = True    # Never set to False in development
USE_LIVE_RATES      = False   # Enable only after paper trading validation
USE_LIVE_OHLCV      = False   # Enable only after paper trading validation
EXCHANGES           = ["okx"]  # Binance/Bybit geo-blocked
INITIAL_CAPITAL_USD = 200.0   # Paper trading amount
```

---

## How to Run

### Development (WSL2 Ubuntu)

```bash
# Start WSL session
wsl -d Ubuntu-24.04
cd ~/mara
source .venv/bin/activate

# Check stack health
docker compose ps

# Run tests (always use venv Python)
~/mara/.venv/bin/python -m pytest tests/test_integration_dryrun.py -v

# Start full stack
docker compose up -d

# View hypervisor logs
docker compose logs -f hypervisor

# Check orchestration status
curl -s http://localhost:8000/status | python3 -m json.tool

# Stop stack
docker compose down
```

### Docker Compose Stack

The `docker-compose.yml` defines:
- `mara-ollama`: Ollama LLM inference (phi3:mini)
- `mara-hypervisor`: FastAPI orchestrator (port 8000)
- `mara-nautilus`: NautilusTrader swing worker (port 8001)
- `mara-polymarket`: CLOB market maker (port 8002)
- `mara-autohedge`: LLM advisory (port 8003)
- `mara-arbitrader`: Cross-exchange arb (port 8004)

**Note**: Always restart full stack together (`docker compose up -d`), never recreate single containers.

### Paper Trading

Default mode. System uses deterministic synthetic OHLCV for consistent signal generation.

```bash
# Run one orchestration cycle
docker compose logs hypervisor | grep "Cycle complete"

# Monitor capital allocation
curl http://localhost:8000/metrics | grep mara_allocations
```

### Live Trading

Only after thorough paper trading validation (2-4 weeks minimum).

```bash
# In .env, set:
MARA_LIVE=true
USE_LIVE_RATES=true
USE_LIVE_OHLCV=true

# Provide OKX API keys
OKX_API_KEY=<your-key>
OKX_API_SECRET=<your-secret>
OKX_API_PASSPHRASE=<your-passphrase>

# Restart stack
docker compose restart
```

---

## Test Suite

### Run Integration Tests

```bash
~/mara/.venv/bin/python -m pytest tests/test_integration_dryrun.py -v
```

Expected output (March 2026):
```
38 passed | 2 failed | 17 skipped
```

### Test Coverage

| Test Class | What | Status |
|---|---|---|
| TestWorkerContract | 8 REST endpoints (nautilus + arbitrader) | 10/12 pass, 2 fail (metrics), 8 skip |
| TestCapitalAllocator | Dollar splits per regime, key alignment | 10/10 pass |
| TestRiskManagerIntegration | Drawdown, cooldown, PnL floor, position cap | 8/8 pass |
| TestHypervisorCycle | WORKER_REGISTRY, capital math, position count | 4/4 pass |
| TestEndToEndSignalSchema | Signal format, required fields | 4/6 pass, 2 skip |

### Known Failures

Two `/metrics` endpoint failures due to FastAPI JSON-encoding bare strings. Fix in both workers:

```python
from fastapi.responses import Response

@app.get("/metrics")
def metrics():
    content = (
        f'mara_worker_active{{worker="nautilus"}} {active}\n'
        f'mara_nautilus_pnl_usd {state.realised_pnl:.4f}\n'
    )
    return Response(content=content, media_type="text/plain")
```

### Skipped Tests (Expected)

- AutoHedge: `litellm` not in venv
- Polymarket: `py_clob_client` not in venv

These workers pass once dependencies are installed.

---

## Pending Work

### Immediate (This Week)
- [ ] Apply `/metrics` Response fix (nautilus + arbitrader workers)
- [ ] Confirm capital allocation numbers in hypervisor logs
- [ ] Run `docker compose up` full stack test

### Short Term (1-2 Weeks)
- [ ] Paper trading validation (PnL, Sharpe, positions updating)
- [ ] NautilusTrader backtest harness (OHLCV backtesting)
- [ ] Regime classifier firing WAR_PREMIUM correctly with live data

### Medium Term (3-4 Weeks)
- [ ] Pi deployment (`scripts/deploy_pi.sh`)
- [ ] Backtesting pipeline (historical validation)
- [ ] ACLED token refresh (password reset)

### Phase 2 (2-3 Months)
- [ ] StockSharp IBKR wrapper (.NET 8)
- [ ] Multi-regime stress testing
- [ ] Live trading on OKX

---

## Architecture Deep Dive

### Hypervisor Loop

Each cycle (default 60 seconds):

1. **Fetch market data** (yfinance, FRED, ACLED, GDELT)
2. **Classify regime** (market proxy + conflict scores)
3. **Allocate capital** (regime → worker weights)
4. **Check health** (concurrent requests to all workers)
5. **Request signals** (POST /signal to all workers)
6. **Risk check** (drawdown, VaR, position limits)
7. **Execute** (POST /execute to eligible workers)
8. **Log metrics** (Prometheus scrape points)
9. **Sleep** (until next cycle)

### Capital Allocator

Given a regime, distributes capital as:
```python
allocation = {
    "nautilus": 0.45,      # Regime-specific weight
    "polymarket": 0.30,
    "arbitrader": 0.25,
    "autohedge": 0.0,      # Advisory only
}

capital_per_worker = allocation[worker] * available_capital
```

Sharpe penalty: If worker sharpe < 0.5, reduce allocation by 50%.  
Unhealthy worker: If drawdown > limit, exclude from allocation.

### Risk Manager

Seven enforcement points:

1. **Drawdown**: Portfolio DD > MAX_DRAWDOWN_PCT (20%) → cooldown
2. **Cooldown**: 1-hour halt after violation
3. **PnL Floor**: Realised PnL < PNL_FLOOR_USD (-$40) → cooldown
4. **Max Positions**: Open positions > MAX_OPEN_POSITIONS (6) → reject signal
5. **Free Capital**: Free USD < MIN_FREE_PCT (15%) → reject signal
6. **Worker Cap**: Single worker allocation > MAX_SINGLE_WORKER_PCT (50%) → cap
7. **Worker Drawdown**: Worker DD > WORKER_MAX_DRAWDOWN_PCT (30%) → exclude worker

---

## Key Design Decisions

- **OKX only**: Binance (451), Bybit (403) are geo-blocked. Symbol format: `BTC-USDT-SWAP`
- **BDRY ETF**: Used as BDI proxy (^BDI delisted from yfinance)
- **Market proxy primary (70-75%)**: Prevents commodity bull false triggers into WAR_PREMIUM
- **2-of-N signal requirement**: Prevents single-indicator false positives
- **AutoHedge advisory-only**: Director + Quant + Risk agents; Execution agent stripped
- **Workers as FastAPI services**: Pluggable architecture, easy to swap engines
- **Hypervisor proprietary-ready**: REST interface means NautilusTrader can be replaced

---

## Data Sources

### Market Data

- **OHLCV**: yfinance (equities, ETFs, crypto)
- **Macro**: FRED (fed rates, 10Y-2Y yield, DXY)
- **Commodities**: yfinance (gold, oil, BDI proxy via BDRY)
- **Sentiment**: VIX, BTC funding rates

### Conflict Index

- **ACLED CAST**: 21-day conflict forecast (100/100 for high activity)
- **ACLED Live**: Recent conflict events per country
- **GDELT**: Global news sentiment (Goldstein scale)

Weights:
- Market proxy: 70-75%
- ACLED CAST: 20%
- ACLED live: 5%
- GDELT: 5-25% (inverse, rate-limited)

---

## Deployment

### Local Docker

```bash
docker compose up -d
docker compose logs -f
```

### Raspberry Pi 5

```bash
# Find Pi IP
arp -a | grep -E "DC:A6:32|E4:5F:01"

# Deploy
./scripts/deploy_pi.sh <pi-ip>

# Verify
ssh pi@<pi-ip>
docker ps
curl http://localhost:8000/health
```

### Scaling Considerations

- Hypervisor: single instance (coordination bottleneck)
- Workers: horizontally scalable (REST clients)
- Ollama: single instance (shared inference)
- Data feeds: cache locally to reduce API calls

---

## 📜 License

MARA is distributed under the **GNU Lesser General Public License v3.0 (LGPL-3.0)**.

### Why LGPL-3.0?

MARA uses **NautilusTrader** (also LGPL-3.0), which requires the same license. This is a feature:

- ✅ Use MARA for free, forever
- ✅ Modify it for your needs
- ✅ Sell hardware devices running MARA
- ✅ Run it commercially
- ✅ Keep modifications private
- ✅ Source code remains open and auditable

### Third-Party Licenses

| Project | License | Status |
|---------|---------|--------|
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | LGPL-3.0 | ✅ Core engine |
| [StockSharp](https://github.com/StockSharp/StockSharp) | Apache 2.0 | ✅ Reference |
| [AutoHedge](https://github.com/The-Swarm-Corporation/AutoHedge) | MIT | ✅ Agent framework |
| Polymarket MM Bot | Unlicensed | ✅ Reference patterns |

All licenses compatible. No conflicts.

### What This Means

**If you use MARA:**
- No restrictions on commercial use
- No restrictions on trading profits
- You can modify the code
- You must provide source access if you distribute it

**If you distribute MARA (Pi device, deployment, etc.):**
- Include LICENSE and NOTICE.txt files
- Provide a GitHub link to source code
- Document any modifications

See [LGPL3_COMPLIANCE.md](./LGPL3_COMPLIANCE.md) for detailed distribution requirements.

### Full License Text

- **LICENSE**: [GNU Lesser General Public License v3.0](./LICENSE)
- **Third-Party Notices**: [NOTICE.txt](./NOTICE.txt)
- **Compliance Guide**: [LGPL3_COMPLIANCE.md](./LGPL3_COMPLIANCE.md)
- **Hardware Distribution**: [HARDWARE_DISTRIBUTION.md](./HARDWARE_DISTRIBUTION.md)
- **LGPL-3.0 Full Text**: https://www.gnu.org/licenses/lgpl-3.0.txt

---

## File Structure

```
mara/
├── LICENSE                              # LGPL-3.0 license
├── NOTICE.txt                           # Third-party attributions
├── LGPL3_COMPLIANCE.md                  # Compliance guide
├── HARDWARE_DISTRIBUTION.md             # Pi device sales guide
├── README.md                            # This file
├── conftest.py                          # Pytest venv guard + stubs
├── config.py                            # Python constants
├── pytest.ini                           # Pytest configuration
├── requirements.txt                     # Python dependencies
├── docker-compose.yml                   # Multi-container definition
├── .env                                 # Environment variables (gitignore)
├── config/
│   ├── settings.yaml
│   ├── regimes.yaml                     # Classifier thresholds
│   └── allocations.yaml
├── hypervisor/
│   ├── Dockerfile
│   ├── main.py                          # FastAPI orchestrator
│   ├── allocator/capital.py             # Capital allocator
│   ├── regime/classifier.py             # Regime detector
│   └── risk/manager.py                  # Risk enforcement
├── workers/
│   ├── nautilus/
│   │   ├── Dockerfile
│   │   ├── worker_api.py                # FastAPI server
│   │   └── strategies/swing_macd.py     # MACD+Fractals strategy
│   ├── polymarket/
│   │   ├── Dockerfile
│   │   └── adapter/main.py              # CLOB market maker
│   ├── autohedge/
│   │   ├── Dockerfile
│   │   ├── worker_api.py                # LLM advisory
│   │   ├── ollama_patch.py
│   │   └── requirements.txt
│   ├── arbitrader/
│   │   ├── Dockerfile
│   │   └── sidecar/main.py              # Cross-exchange arb
│   └── stocksharp/                      # Phase 2 (IBKR routing)
├── data/feeds/
│   ├── market_data.py                   # yfinance, FRED wrapper
│   └── conflict_index.py                # ACLED + GDELT fusion
├── tests/
│   ├── test_integration_dryrun.py       # Full integration test
│   └── test_mara.py                     # Unit tests
└── scripts/
    └── deploy_pi.sh                     # Raspberry Pi deployment
```

---

## Support & Community

- **Documentation**: [Wiki](https://github.com/shrijitb/mara/wiki)
- **Issues**: [GitHub Issues](https://github.com/shrijitb/mara/issues)
- **Discussions**: [GitHub Discussions](https://github.com/shrijitb/mara/discussions)

---

## Contributing

Contributions welcome! Please see [CONTRIBUTING.md](./CONTRIBUTING.md) for guidelines.

This project uses LGPL-3.0, so all contributions are under the same license.

---

## Disclaimer

MARA is a research/development system. Past performance does not guarantee future results. Trading involves risk of loss. Start with paper trading and thoroughly validate before live deployment.

No warranty. Use at your own risk.

---

**MARA v1.0 | March 2026 | LGPL-3.0 Licensed**
