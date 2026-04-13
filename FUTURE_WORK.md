# MARA — Future Work Prompt

**Generated:** April 2026
**For use in:** next Claude Code session after 1–2 weeks of paper trading observation

Copy the section(s) below as the opening prompt for the next session.

---

## Prompt A — Session Re-entry + Health Check (start every session with this)

```
Continue work on MARA (Multi-Agent Risk-Adjusted Capital Orchestrator).
Read CLAUDE.md completely before touching any code.

Current state as of April 2026:
- 130 tests passing, 8 skipped, 0 failed
- Stack running in paper trading mode, WAR_PREMIUM regime at 80% confidence
- All 5 workers allocating correctly: arbitrader/nautilus/polymarket/autohedge/core_dividends
- Telegram bot live and polling
- Conflict index upgraded to 7 sources: market_proxy(60%) + gdelt(10%) + ucdp_ged(10%) +
  ais_chokepoint(10%) + nasa_firms(3%) + usgs_seismic(2%) + edgar_macro(5%)
  Keys needed: UCDP_API_TOKEN, AISSTREAM_API_KEY, NASA_FIRMS_API_KEY (all optional, weights
  redistribute to market_proxy if absent). USGS + EDGAR require no keys.
- SEC EDGAR watchlist scan runs every 30 min; GET /edgar-alerts on hypervisor
- backtest/strategy_comparison.py: vectorized swing_macd + range_mean_revert backtester
- backtest/optimizer.py: Bayesian hyperparameter optimizer (scikit-optimize GP)
  Run: python backtest/optimizer.py --n-calls 100 (overnight)
  Output: config/best_params_{swing,range,regime}.json
- workers/nautilus/indicators/adx.py: pure-Python ADX (no TA-Lib)
- workers/nautilus/strategies/range_mean_revert.py: BB + RSI + ADX ranging strategy

Re-entry sequence:
  wsl -d Ubuntu-24.04
  cd ~/mara && source .venv/bin/activate
  docker compose up -d
  curl -s http://localhost:8000/status | python3 -m json.tool

Run tests first to confirm baseline:
  ~/mara/.venv/bin/python -m pytest tests/ -v
  # Must see: 130 passed, 8 skipped, 0 failed

Then proceed with the task below.
```

---

## Prompt B — F-05: AutoHedge Binary Sanity Check

**Prerequisites:** phi3:mini has been running for 1+ weeks with coherent output observed in Telegram `/status` reports. No prerequisite credentials needed.

```
Implement F-05: AutoHedge binary sanity check.

Context: autohedge worker (port 8003) runs phi3:mini via Ollama for market advisory.
We want to validate whether phi3:mini is giving coherent output before we trust it
for any capital influence. This is a single yes/no question sent to phi3:mini, with
the result reported to Telegram. No automatic capital impact — purely observational.

Implementation plan:
1. In `hypervisor/main.py`, add a new APScheduler job that fires daily at 08:00
   (or on-demand via a new GET /autohedge/sanity endpoint).

2. The sanity check sends one question to the autohedge worker:
   POST /signal with {"regime": current_regime, "paper_trading": true}
   Then inspects the returned signal for:
   - Is the response a valid list?
   - Does each signal have "action", "rationale", "advisory_only" fields?
   - Is "advisory_only" True (required until Phase 3)?
   - Does "action" make directional sense for the current regime?
     (e.g., "BUY" in WAR_PREMIUM, not "SELL" on core defensive assets)

3. Format a one-line Telegram alert:
   "AutoHedge sanity [PASS|FAIL]: action={action}, regime={regime}, rationale snippet"
   Send via the existing direct Bot API call pattern in hypervisor/main.py
   (requests.post to api.telegram.org — do NOT call the telegram_bot container).

4. Store the last sanity result in the /status endpoint as:
   "autohedge_sanity": {"passed": bool, "timestamp": ISO, "action": str}

5. Add a test in test_integration_dryrun.py:
   - Mock the autohedge /signal endpoint to return a valid advisory signal
   - Call the sanity check logic
   - Assert the result dict has passed=True and the status endpoint reflects it

Constraints:
- Do NOT give phi3:mini output any automatic capital allocation weight (F-07 is parked)
- advisory_only=True must remain enforced — fail the sanity check if it's False
- If autohedge is unreachable, log and skip — do not halt the hypervisor
- Keep PAPER_TRADING=True, USE_LIVE_RATES=False (invariants)
- Run ~/mara/.venv/bin/python -m pytest tests/ -v after — must still pass
```

---

## Prompt C — Pi Deploy

**Prerequisites:** Access to the Raspberry Pi 5 on the local network.

```
Deploy MARA to the Raspberry Pi 5 production target.

Context:
- Pi 5, 8GB RAM, Docker ARM64
- docker-compose.yml has no platform: flags — Docker pulls native ARM64 automatically
- Pi IP: find it via `arp -a` in PowerShell, MAC prefix DC:A6:32 or E4:5F:01
- scripts/deploy_pi.sh has a placeholder IP — fill it in first

Steps:
1. Find Pi IP:
   arp -a   # run in PowerShell on Windows host, look for DC:A6:32 or E4:5F:01

2. Edit scripts/deploy_pi.sh — replace the placeholder IP with the real one.

3. Copy .env to Pi (never commit .env to git):
   scp ~/mara/.env pi@<PI_IP>:~/mara/.env

4. Run deploy script:
   bash ~/mara/scripts/deploy_pi.sh

5. Verify on Pi:
   ssh pi@<PI_IP>
   cd ~/mara && docker compose ps
   curl -s http://localhost:8000/status | python3 -m json.tool
   curl -s http://localhost:8000/health

6. Confirm Telegram bot is polling (check /status from Telegram shows correct state).

7. Watch for ARM64-specific issues:
   - ollama/ollama: may need longer start_period on Pi (Pi is slower than x86)
   - Java arbitrader: JAVA_OPTS "-Xmx400m -Xms128m" is already set for Pi's 8GB
   - If any container exits 255, it's an architecture mismatch — check for hidden platform: flags

Do NOT add platform: flags to docker-compose.yml — Docker handles ARM64 automatically.
```

---

## Prompt D — F-06: Polymarket Far-Book Live Test

**Prerequisites:** `POLY_PRIVATE_KEY` (Polygon/Ethereum private key) and `POLY_MARKET_ID` in `.env`.

```
Implement F-06: Polymarket far-book live test.

Context:
- workers/polymarket/adapter/main.py runs in stub mode (no CLOB activity)
- POLY_PRIVATE_KEY is a Polygon/Ethereum private key for the CLOB API
- "Far-book" means quoting only at 20–30% spreads from mid (low fill probability)
- Goal: 48-hour sanity check that the CLOB API integration is functional before
  committing real capital

Implementation plan:
1. In workers/polymarket/adapter/main.py, detect if POLY_PRIVATE_KEY is set.
   If not set: continue in stub mode (current behavior). If set: activate live mode.

2. Live mode in /execute:
   - Connect to Polymarket CLOB using py_clob_client
   - Get the order book for POLY_MARKET_ID
   - Calculate mid price from best bid/best ask
   - Place a limit BUY at mid - 25% (far book)
   - Place a limit SELL at mid + 25% (far book)
   - Both orders should be small (POLY_MIN_EXPOSURE_USD to POLY_MAX_EXPOSURE_USD from env)
   - Log all orders placed; store in /status

3. Risk constraint: ensure total exposure never exceeds POLY_MAX_EXPOSURE_USD ($1000)
   and never goes below POLY_MIN_EXPOSURE_USD (-$1000).

4. Monitoring: /status should return:
   {"pnl": float, "sharpe": float, "open_positions": int, "live_mode": bool,
    "orders": [{"side": "BUY"/"SELL", "price": float, "size": float, "status": str}]}

5. After 48 hours, review fill rate and PnL. If coherent, proceed with Phase 3 wiring.

Constraints:
- Do NOT change PAPER_TRADING=True in config.py (polymarket has its own live_mode flag)
- Keep advisory_only=True on all non-polymarket signals
- Run tests after: ~/mara/.venv/bin/python -m pytest tests/ -v
```

---

## Prompt E — Phase 3 Wiring (IBKR + Live OKX)

**Prerequisites:** Live IBKR connection (`IBKR_HOST`, `IBKR_PORT=7497`), live OKX API keys, 4+ weeks paper trading data, F-05 and F-06 complete.

```
Wire Phase 3 live execution paths in MARA.

Context: MARA has been running in paper trading mode. All workers use advisory_only=True
or paper sim. Phase 3 enables real order execution. Proceed carefully — this moves
real money.

Tasks (implement in order, one at a time):

TASK 1 — Remove advisory_only from core_dividends SCHD+VYM signals:
  File: workers/core_dividends/worker_api.py
  - /signal currently returns advisory_only=True on all SCHD/VYM signals
  - Remove advisory_only gate for the two passive ETF positions
  - /execute should place real IBKR buy orders via StockSharp (worker-stocksharp, port 8005)
  - Constraint: positions are buy-and-hold only, no short selling

TASK 2 — Wire StockSharp IBKR router (workers/stocksharp/):
  - workers/stocksharp/ contains a .NET 8 scaffold (do not touch until this task)
  - Wire the REST contract: GET /health, POST /execute with {symbol, side, qty, paper}
  - Use IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID from env
  - paper=True → log only; paper=False → real IBKR order
  - docker-compose profile: "live" (already in docker-compose.yml)

TASK 3 — Enable RECESSION_PAIRS signals (SH, PSQ):
  File: workers/nautilus/worker_api.py
  - /signal in BEAR_RECESSION and CRISIS_ACUTE regimes currently returns advisory_only=True
    for SH and PSQ (inverse ETF signals)
  - Remove advisory_only for these when IBKR is connected and paper=False

TASK 4 — Wire quarterly sweep stubs:
  File: hypervisor/main.py
  - Find the two "# PHASE 3:" comment stubs in the quarterly_profit_sweep function
  - Wire IBKR redemption: sell ETF positions via StockSharp when surplus > $220
  - Wire USDT swap: convert surplus to USDT via OKX (use ccxt, OKX symbol format BTC-USDT-SWAP)

For each task:
- Run ~/mara/.venv/bin/python -m pytest tests/ -v after the task — must not regress
- Test with paper=True first before enabling paper=False
- Never change INITIAL_CAPITAL_USD=200.0 or EXCHANGES=["okx"]
```

---

## Prompt F — F-09: NautilusTrader Backtest Harness

**Prerequisites:** 4+ weeks of paper trading PnL data. F-05 complete.

```
Implement F-09: Full NautilusTrader backtest harness for the swing_macd strategy.

Context:
- workers/nautilus/strategies/swing_macd.py is the MACD + Bullish Fractal strategy
- It currently runs live paper sim in nautilus worker_api.py
- We want a standalone backtest that replays historical OHLCV data through the strategy
  and produces a performance report: Sharpe ratio, max drawdown, win rate, equity curve

Implementation plan:
1. Create workers/nautilus/backtest.py (standalone script, not a web service).

2. Load historical OHLCV data:
   - Use ccxt or yfinance for historical OHLCV
   - Pairs: BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT, AVAX/USDT (from config.SWING_PAIRS)
   - Timeframe: 4h (from config.SWING_TIMEFRAME)
   - Period: minimum 90 days

3. Run the backtest through NautilusTrader's BacktestEngine:
   - Use the existing swing_macd.py strategy class (do not modify it)
   - Initial capital: config.INITIAL_CAPITAL_USD ($200)
   - Apply stop-loss: config.SWING_STOP_LOSS_PCT (2%)
   - Apply take-profit: config.SWING_TAKE_PROFIT_RATIO (2.0×)

4. Output a performance report:
   - Sharpe ratio (annualised)
   - Max drawdown (%)
   - Win rate (%)
   - Total trades
   - Equity curve plot (optional, if matplotlib available)
   - Save report to data/backtest_results/ as JSON + optional PNG

5. Add a test in test_mara.py:
   TestNautilusBacktest:
   - Run backtest on synthetic OHLCV data (not live data — keep USE_LIVE_OHLCV=False in tests)
   - Assert Sharpe > 0 on a strongly trending synthetic series
   - Assert max_drawdown < 0.50 (sanity bound)

Constraints:
- USE_LIVE_OHLCV=False in config.py — use synthetic data in tests, real data only in backtest.py
- Do not modify swing_macd.py strategy
- Keep PAPER_TRADING=True
- Run ~/mara/.venv/bin/python -m pytest tests/ -v after
```

---

## Prompt H — Apply Optimizer Results to Live Strategies

**Prerequisites:** `python backtest/optimizer.py --n-calls 100` completed and output files
exist at `config/best_params_swing.json`, `config/best_params_range.json`,
`config/best_params_regime.json`.

```
Wire the Bayesian optimizer output into the live MARA strategies.

Context:
- backtest/optimizer.py ran BO and saved best params to config/best_params_*.json
- Currently workers/nautilus/strategies/swing_macd.py and range_mean_revert.py use
  hardcoded defaults matching config.py constants
- hypervisor/regime/classifier.py uses hardcoded thresholds from config/regimes.yaml

Tasks:
1. workers/nautilus/strategies/swing_macd.py:
   - On module load, check if config/best_params_swing.json exists
   - If found, override MACD fast/slow/signal, fractal_lookback, stop_loss_pct,
     take_profit_ratio, adx thresholds with values from best_params["best_params"]
   - Log the source ("from optimizer" vs "default") at startup

2. workers/nautilus/strategies/range_mean_revert.py:
   - Same pattern: load config/best_params_range.json if present
   - Override bb_period, bb_std, rsi_period, stop_loss_pct, take_profit_ratio

3. hypervisor/regime/classifier.py:
   - Load config/best_params_regime.json if present
   - Override war_premium_threshold, crisis_vix, war_gold_oil_ratio, market_proxy_weight
   - These override the values in config/regimes.yaml

4. Add tests in test_mara.py:
   - Test that each module reads the JSON if present (mock json.load)
   - Test that it falls back cleanly to defaults if file absent/corrupt

Constraints:
- JSON loading must be fault-tolerant: any exception → fall back to defaults, log warning
- Do not change config.py constants (they remain the hardcoded baseline)
- PAPER_TRADING=True, all invariants unchanged
- Run ~/mara/.venv/bin/python -m pytest tests/ -v after
```

---

## Prompt G — Observability: Prometheus + Grafana

**Prerequisites:** Stack stable for 2+ weeks. Optional enhancement.

```
Add Prometheus scraping and a Grafana dashboard to MARA.

Context:
- All trading workers expose GET /metrics in Prometheus text format
- arbitrader (port 8004) has the most detailed metrics
- hypervisor (port 8000) does not yet expose /metrics

Tasks:
1. Add GET /metrics to hypervisor/main.py:
   - Expose: regime label, conflict_score, total_capital, free_capital, cycle_count
   - Per-worker: allocated_usd, pnl, sharpe (as gauges)
   - Format: plain Prometheus text (use Response(content=..., media_type="text/plain"))

2. Add prometheus + grafana services to docker-compose.yml:
   - prometheus: prom/prometheus:latest, scrape all worker /metrics endpoints
   - grafana: grafana/grafana:latest, port 3000
   - Both should be in a "monitoring" compose profile to keep default stack lean:
     docker compose --profile monitoring up -d

3. Create config/prometheus.yml with scrape targets for all 5 worker ports + hypervisor.

4. Create a Grafana dashboard JSON (config/grafana_dashboard.json):
   - Capital allocation per worker (stacked bar)
   - PnL per worker over time (line chart)
   - Regime label as a stat panel
   - Conflict score gauge

5. Test: after `docker compose --profile monitoring up -d`:
   curl -s http://localhost:9090/targets   # all targets should be UP
   curl -s http://localhost:3000           # Grafana login page

Constraints:
- No platform: flags
- Monitoring services must not affect the trading stack health checks
- Keep existing worker /metrics endpoints unchanged
```
