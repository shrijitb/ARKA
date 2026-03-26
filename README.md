# MARA: Multi-Agent Risk-Adjusted Capital Trader

MARA is a multi-agent quantitative trading system designed for turbulent macro environments — geopolitical crises, regime shifts, commodity shocks, war. A Python FastAPI hypervisor coordinates specialised worker agents and allocates capital dynamically based on classified market regime.

## Table of Contents
- [Project Overview](#project-overview)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [Worker Map](#worker-map)
- [Configuration](#configuration)
- [How to Run](#how-to-run)
- [Test Suite](#test-suite)
- [Pending Work](#pending-work)
- [License](#license)

## Project Overview

MARA targets futures, crypto, forex, ETFs, and prediction markets. The system operates in a sequence: backtest → paper trading → live. The development environment uses Windows laptop → WSL2 Ubuntu-24.04 → Docker Desktop 4.63.0. The production target is Raspberry Pi 5 (16GB RAM) with Docker ARM64 via buildx + QEMU.

As of March 2026, the live classifier output shows WAR_PREMIUM with 80% confidence.

## Key Features

- Multi-agent orchestration with dynamic capital allocation
- Regime classifier that detects market states (WAR_PREMIUM, CRISIS_ACUTE, BEAR_RECESSION, etc.)
- Specialised workers for swing trading, arbitrage, prediction markets, and AI advisory
- Risk management at portfolio and worker levels
- RESTful API contracts for all workers
- Dockerized deployment for easy scaling
- Paper trading mode with deterministic synthetic data
- Live trading integration with OKX exchange (Binance and Bybit are geo-blocked)

## Architecture

The system consists of a central Hypervisor that manages worker agents. The Hypervisor includes:
- Capital allocator that distributes funds based on regime
- Risk manager that enforces drawdown limits and position caps
- Regime classifier that outputs market state with confidence

Workers are independent FastAPI processes that expose standard endpoints:
- GET /health → worker status
- GET /status → P&L, Sharpe ratio, allocated capital, open positions
- GET /metrics → Prometheus-compatible metrics
- POST /signal → trading signals
- POST /execute → trade execution or advisory
- POST /allocate → capital allocation
- POST /pause → halt new entries
- POST /resume → resume trading
- POST /regime → regime update

All workers must return metrics as plain text with media type "text/plain" for Prometheus compatibility.

### Regime States

| Regime | Meaning | Capital Bias |
|--------|---------|-------------|
| WAR_PREMIUM | Active geopolitical conflict | arbitrader 45%, polymarket 30%, nautilus 25% |
| CRISIS_ACUTE | Market panic, VIX explosion | arbitrader 40%, polymarket 20%, nautilus 10% |
| BEAR_RECESSION | Sustained downturn | nautilus 45%, arbitrader 25%, polymarket 20% |
| BULL_FROTHY | Euphoric bull | nautilus 45%, arbitrader 35%, polymarket 10% |
| REGIME_CHANGE | Transition state | arbitrader 40%, nautilus 30%, polymarket 20% |
| SHADOW_DRIFT | Hidden pressure, BDI moving | arbitrader 40%, nautilus 35%, polymarket 15% |
| BULL_CALM | Default, no stress | nautilus 45%, arbitrader 30%, polymarket 10% |

Priority order: WAR_PREMIUM > CRISIS_ACUTE > BEAR_RECESSION > BULL_FROTHY > REGIME_CHANGE > SHADOW_DRIFT > BULL_CALM

## Worker Map

| Worker | Key | Port | Technology | Role |
|--------|-----|------|------------|------|
| worker-nautilus | nautilus | 8001 | NautilusTrader | MACD+Fractals swing strategy on OKX perps |
| worker-polymarket | polymarket | 8002 | Python CLOB | Prediction market making |
| worker-autohedge | autohedge | 8003 | litellm + Ollama | AI advisory only (phi3:mini) |
| worker-arbitrader | arbitrader | 8004 | Java + Python sidecar | Cross-exchange price arbitrage |
| worker-stocksharp | stocksharp | 8005 | .NET 8 | IBKR order router (Phase 2 only) |

Exchange: OKX exclusively. Binance returns HTTP 451, Bybit returns 403 — both geo-blocked. OKX symbol format: `BTC-USDT-SWAP`.

## Configuration

### Config Structure

There is no `config.py` at the project root. Instead:

```
config/
  settings.yaml      # runtime settings
  regimes.yaml       # classifier thresholds (recalibrated March 2026)
  allocations.yaml   # worker weight documentation (mirrors capital.py)
config.py            # Python constants for workers + test suite (separate from config/)
```

### Safety Defaults (Do Not Change Without Explicit Intent)

```python
PAPER_TRADING   = True
USE_LIVE_RATES  = False
USE_LIVE_OHLCV  = False
EXCHANGES       = ["okx"]    # binance/bybit are geo-blocked
INITIAL_CAPITAL_USD = 200.0
```

### Environment Variables

| Key | Value |
|-----|-------|
| MARA_MODE | backtest |
| MARA_LIVE | false |
| FRED_API_KEY | Optional — yfinance fallback available |
| OLLAMA_HOST | http://ollama:11434 |
| OLLAMA_MODEL | phi3:mini |
| ACLED_EMAIL | Use institution/corporate domain |
| ACLED_PASSWORD | Configure |
| POLY_PRIVATE_KEY | Configure|
| IBKR_HOST/PORT | Needs implementing |

## How to Run

### Development

1. Start WSL2 Ubuntu-24.04:
   ```
   wsl -d Ubuntu-24.04
   cd ~/mara
   source .venv/bin/activate
   ```

2. Check Docker containers:
   ```
   docker compose ps
   ```

3. Run tests (always use venv Python):
   ```
   ~/mara/.venv/bin/python -m pytest tests/test_integration_dryrun.py -v
   ```

### Docker Compose

The `docker-compose.yml` file defines services for each worker and the hypervisor. To start the full stack:
```
docker compose up
```

### Paper Trading

Set `PAPER_TRADING = True` in `config.py`. The system uses deterministic synthetic OHLCV data for consistent signals.

### Live Trading

To enable live data, set:
- `USE_LIVE_OHLCV = True` in `config.py`
- `USE_LIVE_RATES = True` in `config.py`
- Ensure `MARA_LIVE = true` in `.env`

Note: Live trading requires OKX API keys and should only be attempted after thorough paper trading validation.

## Test Suite

Run the integration dry-run test:
```
~/mara/.venv/bin/python -m pytest tests/test_integration_dryrun.py -v
```

Expected output (as of March 2026):
```
38 passed | 2 failed | 17 skipped (7.22s)
```

The two real failures are related to the `/metrics endpoint returning a bare string that FastAPI JSON-encodes, breaking Prometheus parsing. Fixes are documented in the `3_12_MARA_dev_update.md` file.

Skipped tests are expected due to missing dependencies (autohedge requires litellm, polymarket requires py_clob_client). These workers will pass once their dependencies are installed.

## Pending Work

- Apply the `/metrics` response fix in `workers/nautilus/worker_api.py` and `workers/arbitrader/sidecar/main.py` (see Section 7 of `3_12_MARA_dev_update.md`)
- Execute `docker compose up` for full stack integration test
- Validate paper trading confirms capital flows from hypervisor to workers
- Deploy to Raspberry Pi 5 using `scripts/deploy_pi.sh` (requires finding Pi IP via `arp -a` in PowerShell, look for MAC `DC:A6:32` or `E4:5F:01`)
- Complete backtesting pipeline (currently requires additional work)
- Implement StockSharp IBKR wrapper (Phase 2)

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## Notes

- The work is not complete regarding deployment on PiOS and backtesting still needs to be done.
- Do not use em dashes in documentation.

## Files Reference

```
~/mara/
├── conftest.py                              ✅ venv guard + sys.modules stubs
├── config.py                                ✅ Python constants for workers + tests
├── pytest.ini
├── requirements.txt
├── docker-compose.yml
├── .env
├── config/
│   ├── settings.yaml
│   ├── regimes.yaml                         ✅ recalibrated
│   └── allocations.yaml
├── hypervisor/
│   ├── main.py                              ✅ rebuilt (10 bugs fixed)
│   ├── allocator/capital.py                 ✅ 4 worker keys
│   ├── regime/classifier.py                 ✅
│   └── risk/manager.py                      ✅
├── workers/
│   ├── nautilus/
│   │   ├── worker_api.py                    ✅ needs /metrics fix
│   │   └── strategies/swing_macd.py         ✅
│   ├── arbitrader/
│   │   └── sidecar/main.py                  ✅ needs /metrics fix
│   ├── autohedge/
│   │   ├── worker_api.py                    ✅ (skips until litellm installed)
│   │   ├── ollama_patch.py                  ✅
│   │   ├── Dockerfile                       ✅
│   │   └── requirements.txt                 ✅
│   ├── polymarket/
│   │   ├── adapter/main.py                  ✅ (skips until py_clob_client installed)
│   │   └── Dockerfile                       ✅
│   └── stocksharp/                          🔲 Phase 2
├── data/feeds/
│   ├── market_data.py                       ✅ 15/15 tests
│   └── conflict_index.py                    ✅ (ACLED + GDELT bugs known)
└── tests/
    └── test_integration_dryrun.py           ✅ 38 pass, 2 fail (metrics), 17 skip
```
