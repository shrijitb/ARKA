# External Integrations

**Analysis Date:** 2026-04-15

## APIs & External Services

### Market Data

**yfinance (Yahoo Finance):**
- Used for: equities, ETFs (VOO, VT, BND, GLDM, NATO, SHLD, etc.), VIX (`^VIX`), commodity futures (GC=F, CL=F, SI=F, HG=F, NG=F), DXY proxy (UUP), BDI proxy (BDRY)
- SDK: `yfinance >=0.2.40`
- Auth: None (public)
- File: `data/feeds/market_data.py`
- Notes: MultiIndex column bug workaround via `_last_close()` helper; `^BDI` delisted — use BDRY ETF

**FRED (Federal Reserve Economic Data):**
- Used for: yield curve (T10Y2Y), CPI, unemployment, macro indicators
- SDK: `fredapi >=0.5.1`
- Auth: `FRED_API_KEY` env var (optional — yfinance fallback available if missing)
- File: `data/feeds/market_data.py`

**OKX Exchange:**
- Used for: crypto OHLCV, perpetual funding rates, order book depth, live trading (Phase 3)
- SDK: `ccxt >=4.3.0` for OHLCV; raw `requests` for public OKX REST for funding rates
- Auth: OKX API key/secret/passphrase (stored in `.env`; injected via setup wizard)
- Files: `data/feeds/market_data.py`, `data/feeds/funding_rates.py`, `data/feeds/order_book.py`
- Endpoints: `https://www.okx.com/api/v5/public/funding-rate`, `/api/v5/public/funding-rate-history`
- Symbol format: `BTC-USDT-SWAP` (OKX perp notation)
- Notes: Binance (HTTP 451) and Bybit (HTTP 403) are geo-blocked from this region; OKX is the sole exchange

### OSINT Intelligence Sources

**GDELT (Global Database of Events, Language, and Tone):**
- Used for: conflict event news scoring, geopolitical sentiment (Goldstein score)
- SDK: `gdeltdoc >=1.5.0` (falls back to raw urllib on import failure)
- Auth: None (public)
- Endpoint: `https://api.gdeltproject.org/api/v2/doc/doc`
- Files: `data/feeds/gdelt_client.py`, `data/feeds/conflict_index.py`
- Rate limit: 3.5s sleep between queries (`GDELT_SLEEP = 3.5`)

**SEC EDGAR:**
- Used for: 8-K material event filings, Form 4 insider trading, 10-Q/10-K earnings calendar
- SDK: `edgartools >=3.0.0` (wraps `data.sec.gov`)
- Auth: None (User-Agent header only per SEC Fair Access Policy)
- File: `data/feeds/edgar_client.py`

**ACLED (Armed Conflict Location and Event Data):**
- Used for: lethal conflict forecasts (CAST), live conflict events by country
- Auth: `ACLED_EMAIL` + `ACLED_PASSWORD` env vars; token managed by `AcledTokenManager` class
- File: `data/feeds/conflict_index.py`
- Notes: Free tier returns HTTP 403 permanently for CAST and live events endpoints — both are skipped in tests

**UCDP (Uppsala Conflict Data Program):**
- Used for: georeferenced armed conflict events with fatality counts
- Auth: None (free REST API, polite 1 req/s)
- Endpoint: `https://ucdpapi.pcr.uu.se/api/gedevents/23.1`
- File: `data/feeds/ucdp_client.py`

**AISstream (Maritime Vessel Tracking):**
- Used for: vessel density anomaly detection at 6 strategic chokepoints (Hormuz, Malacca, Suez, Panama, Bosphorus, Taiwan Strait)
- Auth: `AIS_API_KEY` env var (free tier at aisstream.io)
- Protocol: WebSocket (`websockets >=12.0`)
- File: `data/feeds/maritime_client.py`

**NASA FIRMS (Fire Information for Resource Management System):**
- Used for: thermal anomaly detection near energy infrastructure
- Auth: `NASA_FIRMS_API_KEY` env var (free at firms.modaps.eosdis.nasa.gov)
- Endpoint: `https://firms.modaps.eosdis.nasa.gov/api/area/csv/{MAP_KEY}/VIIRS_SNPP_NRT/world/1`
- File: `data/feeds/environment_client.py`

**USGS Earthquake Hazards Program:**
- Used for: M4.5+ earthquakes near critical infrastructure / energy chokepoints
- Auth: None (public)
- Endpoint: `https://earthquake.usgs.gov/fdsnws/event/1/query`
- File: `data/feeds/environment_client.py`

**OpenSky Network (Aviation Tracking):**
- Used for: military/VIP/ISR aircraft activity in conflict zones (early warning signal)
- Auth: `OPENSKY_USERNAME` + `OPENSKY_PASSWORD` env vars (optional; raises quota from 400 to 4000 req/day)
- Endpoint: `https://opensky-network.org/api/states/all`
- File: `data/feeds/aviation_client.py`

## AI / LLM

**Ollama (Self-Hosted LLM):**
- Used for: advisory-only trade signals in the Analyst worker
- Model: `phi3:mini` default (configurable via `OLLAMA_MODEL` env var; qwen3:4b also referenced)
- Connection: `OLLAMA_HOST` env var (default: `http://ollama:11434`)
- SDK: Direct HTTP via `workers/analyst/ollama_patch.py`; `instructor >=1.7.0` for structured output
- Docker service: `arka-ollama` (port 11434, volume `ollama_data`)
- Healthcheck: `ollama list` command (not `/api/tags` — empty body breaks curl `-f`)
- File: `workers/analyst/worker_api.py`, `workers/analyst/ollama_patch.py`

**SearXNG (Self-Hosted Metasearch):**
- Used for: company intelligence, event research via local metasearch (no external API key, no data leakage)
- Connection: `SEARXNG_URL` env var (default: `http://searxng:8080`)
- Docker service: `arka-searxng` (port 8080)
- File: `data/feeds/searxng_client.py`

## Messaging / Notifications

**Telegram Bot API:**
- Used for: command bot (`/status`, `/regime`, `/watchlist`, `/pause`, `/resume`, `$TICKER`); quarterly profit sweep alerts
- SDK: `python-telegram-bot >=21.0` (polling mode — no inbound port)
- Auth: `TELEGRAM_BOT_TOKEN` env var (from @BotFather); `TELEGRAM_ALLOWED_USER_ID` for access control
- Direct API call for profit sweep: `requests.post` to `https://api.telegram.org/bot{token}/sendMessage`
- Files: `workers/telegram_bot/main.py`, `hypervisor/main.py` (profit sweep)

## Data Storage

**Database:**
- SQLite (file-based, async)
- Location: `data/arka.db` (local dev); `/app/data/arka.db` (Docker, mounted volume)
- Client: SQLAlchemy 2.x async ORM + aiosqlite driver
- Schema: `data/db/schema.sql` (source of truth; WAL mode + NORMAL sync)
- ORM models: `hypervisor/db/models.py` (RegimeLog, Signal, Order)

**File Storage:**
- Local filesystem only
- Audit log: `data/audit.jsonl` (append-only JSONL via `hypervisor/audit.py`)
- Portfolio state snapshot: `logs/portfolio_state.json`
- HMM model: `hypervisor/regime/model_state/hmm_4state.pkl` (pre-trained, committed to git)

**Caching:**
- In-process Python dicts with TTL timestamps
- Funding rate cache: 5 min (current) / 8 h (history) in `data/feeds/funding_rates.py`
- Swing OHLCV cache: `SWING_CACHE_TTL_SEC = 14400` (4 hours)

## Authentication & Identity

**Hypervisor API Auth:**
- Mechanism: Bearer token (`Authorization: Bearer <key>`)
- Implementation: `APIKeyMiddleware` in `hypervisor/auth.py`
- Key lifecycle: auto-generated 32-byte random token on first startup; written to `.env`; read via `ARKA_API_KEY` env var
- Exempt paths: `/health`, `/metrics`, `/setup/status`, `/system/hardware`

**Exchange Auth:**
- OKX: API key/secret/passphrase stored in `.env`; setup via dashboard wizard (`/setup/credentials` POST)

## CI/CD & Deployment

**Hosting:**
- Production: Raspberry Pi 5 (ARM64 Docker)
- Dev: WSL2 Ubuntu-24.04 (Docker Desktop)

**CI Pipeline:**
- None detected (GitHub repository, no CI config found)

**Installer:**
- One-shot bash installer: `install.sh` (hardware detection, LLM model selection, config generation, stack launch)

## Phase 3 Integrations (Not Yet Active)

**Interactive Brokers (IBKR):**
- Purpose: US equity ETFs, inverse ETFs (SH, PSQ), live order execution
- Connection: `IBKR_HOST`, `IBKR_PORT` (7497), `IBKR_CLIENT_ID` env vars
- Implementation: `workers/stocksharp/` (.NET 8 router — do not modify)
- Status: Stubbed; all IBKR signals have `advisory_only=True`

**Polymarket (Prediction Markets):**
- Purpose: CLOB market making, binary event trading
- Auth: `POLY_PRIVATE_KEY` env var (Polygon/Ethereum private key)
- Worker: `workers/prediction_markets/worker_api.py` (port 8002, currently stub mode)

**Kalshi:**
- Purpose: US-regulated prediction markets (referenced in setup wizard `DataStep`)
- Auth: `KALSHI_API_KEY` env var
- Status: Optional data source in setup wizard; not wired to any feed client

## Webhooks & Callbacks

**Incoming:** None — all external data is polled or subscribed via WebSocket outbound connection

**Outgoing:**
- Telegram Bot API: profit sweep alerts sent via `requests.post` from hypervisor cron job
- OKX REST: funding rate and order book polling (public endpoints, no auth)

## Environment Variables Reference

**Required for core operation:**
- `ARKA_API_KEY` — auto-generated Bearer token (written by hypervisor on first run)
- `TELEGRAM_BOT_TOKEN` — Telegram bot token from @BotFather
- `TELEGRAM_ALLOWED_USER_ID` — numeric Telegram user ID

**Optional (graceful fallback if absent):**
- `FRED_API_KEY` — FRED macro data (yfinance fallback available)
- `AIS_API_KEY` — aisstream.io maritime feed (source disabled if absent)
- `NASA_FIRMS_API_KEY` — NASA FIRMS thermal anomaly feed (source disabled if absent)
- `OPENSKY_USERNAME` / `OPENSKY_PASSWORD` — raises quota from 400 to 4000 req/day
- `OLLAMA_HOST` / `OLLAMA_MODEL` — LLM backend (defaults: `http://ollama:11434` / `phi3:mini`)

**Exchange (Phase 3 live trading):**
- `OKX_API_KEY` / `OKX_SECRET` / `OKX_PASSPHRASE` — OKX live trading credentials
- `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID` — IBKR connection
- `POLY_PRIVATE_KEY` — Polymarket Ethereum private key

**Secrets location:** `.env` file at project root (gitignored; mounted into containers as volume)

---

*Integration audit: 2026-04-15*
