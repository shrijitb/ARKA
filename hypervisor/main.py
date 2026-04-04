"""
hypervisor/main.py

MARA Hypervisor — The Regime-Aware Orchestrator.

Cycle (every CYCLE_INTERVAL_SEC, default 3600s):
  1. Health-check all workers via GET /health
  2. Pull status (pnl, sharpe, open_positions) via GET /status
  3. Classify market regime via RegimeClassifier
  4. Run portfolio risk checks via RiskManager
  5. Compute capital allocation via RegimeAllocator
  6. Broadcast regime to all workers via POST /regime
  7. Send capital allocations via POST /allocate
  8. Resume paused workers that now have allocation

Workers implement:
  GET  /health     → {"status": "ok"}
  GET  /status     → {"pnl": float, "sharpe": float, "allocated_usd": float,
                       "open_positions": int, ...}
  GET  /metrics    → Prometheus text
  POST /regime     → {"regime": str, "confidence": float, "paper_trading": bool}
  POST /allocate   → {"amount_usd": float, "paper_trading": bool}
  POST /pause      → halt new entries
  POST /resume     → resume trading
  POST /signal     → (optional) pull advisory signal

Run (from ~/mara with venv active):
  uvicorn hypervisor.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import httpx
import requests as _requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _APSCHEDULER_AVAILABLE = True
except ImportError:
    _APSCHEDULER_AVAILABLE = False

from hypervisor.allocator.capital import RegimeAllocator
from hypervisor.regime.classifier import RegimeClassifier
from hypervisor.risk.manager import RiskManager
from hypervisor.risk.execution_risk import ExecutionRiskChecker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
INITIAL_CAPITAL_USD = float(os.environ.get("INITIAL_CAPITAL_USD", 200.0))
CYCLE_INTERVAL_SEC  = int(os.environ.get("CYCLE_INTERVAL_SEC", 3600))
WORKER_TIMEOUT_SEC  = int(os.environ.get("WORKER_TIMEOUT_SEC", 10))
MIN_TRADE_SIZE_USD  = float(os.environ.get("MIN_TRADE_SIZE_USD", 10.0))
PAPER_TRADING       = os.environ.get("MARA_LIVE", "false").lower() != "true"

# ── Worker Registry ───────────────────────────────────────────────────────────
# Keys MUST match capital.py REGIME_PROFILES keys exactly.
# docker-compose service names resolve via Docker DNS (e.g. worker-nautilus).
# Override with env vars for local dev or Pi deploy.
WORKER_REGISTRY: Dict[str, str] = {
    "nautilus":       os.environ.get("NAUTILUS_URL",        "http://worker-nautilus:8001"),
    "polymarket":     os.environ.get("POLYMARKET_URL",      "http://worker-polymarket:8002"),
    "analyst":        os.environ.get("ANALYST_URL",         "http://worker-analyst:8003"),
    "arbitrader":     os.environ.get("ARBITRADER_URL",      "http://worker-arbitrader:8004"),
    "core_dividends": os.environ.get("CORE_DIVIDENDS_URL",  "http://worker-core-dividends:8006"),
}

# Regimes that force all directional workers to pause new entries
DEFENSIVE_REGIMES = {"CRISIS_ACUTE"}


# ── Global State ──────────────────────────────────────────────────────────────
class HypervisorState:
    def __init__(self):
        self.total_capital:     float            = INITIAL_CAPITAL_USD
        self.free_capital:      float            = INITIAL_CAPITAL_USD
        self.current_regime:    str              = "BULL_CALM"
        self.regime_confidence: float            = 0.0
        self.worker_health:     Dict[str, bool]  = {}
        self.worker_status:     Dict[str, Dict]  = {}
        self.worker_sharpe:     Dict[str, float] = {}
        self.worker_pnl:        Dict[str, float] = {}
        self.worker_allocated:  Dict[str, float] = {}
        self.cycle_count:       int              = 0
        self.last_cycle_at:     float            = 0.0
        self.started_at:        float            = time.time()
        self.halted:            bool             = False
        self.halt_reason:       str              = ""
        self.allocations:       Dict[str, float] = {}
        self.risk_verdict:      str              = "OK"
        self.watchlist:         List[str]        = []
        self.execution_risk_state: Dict         = {}
        self.war_premium_score: float            = 0.0
        self.cycle_duration_ms: float            = 0.0
        self.last_analyst_signal: dict           = {}
        self.last_edgar_alerts:  List[dict]     = []


state           = HypervisorState()
classifier      = RegimeClassifier()
allocator       = RegimeAllocator(total_capital=INITIAL_CAPITAL_USD)
risk_mgr        = RiskManager(initial_capital=INITIAL_CAPITAL_USD)
execution_risk  = ExecutionRiskChecker(paper_trading=PAPER_TRADING)


# ── Telegram notification helper ──────────────────────────────────────────────

_TG_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_TG_CHAT_ID = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")


def _tg_send(text: str) -> None:
    """Send a message to the configured Telegram chat. Fire-and-forget."""
    if not _TG_TOKEN or not _TG_CHAT_ID:
        logger.info(f"[tg_notify skipped — no token] {text}")
        return
    try:
        _requests.post(
            f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
            json={"chat_id": _TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=8,
        )
    except Exception as exc:
        logger.warning(f"Telegram notify failed: {exc}")


# ── Quarterly profit sweep ─────────────────────────────────────────────────────

PROFIT_TARGET_MULTIPLIER = 1.10   # 10% above initial capital before sweep


def _run_quarterly_sweep() -> None:
    """
    Fires on the 7th of Jan / Apr / Jul / Oct.
    Calculates surplus above INITIAL_CAPITAL_USD * 1.10, logs it, and
    sends a Telegram alert.

    # PHASE 3: wire IBKR redemption — transfer surplus to bank account.
    # PHASE 3: wire USDT redemption — swap surplus USDT → fiat via OKX.
    """
    target     = INITIAL_CAPITAL_USD * PROFIT_TARGET_MULTIPLIER
    surplus    = round(state.total_capital - target, 2)
    total      = round(state.total_capital, 2)
    regime     = state.current_regime
    cycle      = state.cycle_count

    if surplus > 0:
        msg = (
            f"📈 *MARA Quarterly Profit Sweep*\n"
            f"Total capital: ${total:.2f}\n"
            f"Target floor:  ${target:.2f} (initial × 1.10)\n"
            f"Surplus:       *${surplus:.2f}*\n"
            f"Regime: `{regime}` | Cycles: {cycle}\n\n"
            f"_PHASE 3: IBKR/USDT redemption not yet wired._"
        )
        logger.info(f"Quarterly sweep: surplus=${surplus:.2f} above ${target:.2f} floor")
    else:
        shortfall = round(target - state.total_capital, 2)
        msg = (
            f"📊 *MARA Quarterly Sweep — No Surplus*\n"
            f"Total capital: ${total:.2f}\n"
            f"Target floor:  ${target:.2f}\n"
            f"Shortfall:     ${shortfall:.2f}\n"
            f"Regime: `{regime}` | Cycles: {cycle}"
        )
        logger.info(f"Quarterly sweep: no surplus — ${shortfall:.2f} below target")

    _tg_send(msg)


# ── EDGAR watchlist filing monitor ───────────────────────────────────────────

EDGAR_SCAN_INTERVAL_SEC = int(os.environ.get("EDGAR_SCAN_INTERVAL_SEC", 1800))  # 30 min
EDGAR_ALERT_MIN_SCORE   = 50


async def _edgar_watchlist_scan() -> None:
    """
    Scan EDGAR for significant recent filings across all watchlist tickers.
    High-scoring filings (>= EDGAR_ALERT_MIN_SCORE) trigger Telegram alerts.
    Results stored in state.last_edgar_alerts for the /edgar-alerts endpoint.
    """
    try:
        from data.feeds.edgar_feed import (
            EdgarWatchlistMonitor, MONITORED_FORM_TYPES, parse_8k_with_llm,
        )
    except ImportError:
        logger.warning("edgar_feed not available — EDGAR watchlist scan skipped")
        return

    if not state.watchlist:
        return

    monitor = EdgarWatchlistMonitor()
    alerts: List[dict] = []

    for ticker in list(state.watchlist):
        try:
            cik = await asyncio.to_thread(monitor.get_cik_for_ticker, ticker)
            if cik is None:
                continue   # crypto or unknown ticker

            filings = await asyncio.to_thread(
                monitor.get_recent_filings, cik, MONITORED_FORM_TYPES, 2
            )
            for filing in filings:
                acc = filing["accession_number"]
                excerpt = await asyncio.to_thread(
                    monitor.get_filing_text_excerpt, acc, cik
                )
                significance = monitor.score_filing_significance(
                    filing["form_type"], excerpt
                )
                if significance["score"] >= EDGAR_ALERT_MIN_SCORE:
                    alert = {
                        "ticker":   ticker,
                        "form_type": filing["form_type"],
                        "date":     filing["filing_date"],
                        "score":    significance["score"],
                        "keywords": significance["keywords_matched"],
                        "excerpt":  excerpt[:200],
                    }
                    # LLM enrichment for high-scoring 8-Ks
                    if filing["form_type"] == "8-K" and significance["score"] >= 60:
                        try:
                            llm_context = await asyncio.to_thread(
                                parse_8k_with_llm, excerpt, ticker, acc
                            )
                            alert["llm_context"] = llm_context
                        except Exception:
                            pass
                    alerts.append(alert)
        except Exception as exc:
            logger.warning(f"EDGAR scan failed for {ticker}: {exc}")

    state.last_edgar_alerts = alerts

    if alerts:
        top = sorted(alerts, key=lambda x: x["score"], reverse=True)[:3]
        for a in top:
            kw_str = ", ".join(a["keywords"]) if a["keywords"] else "—"
            llm_info = ""
            if "llm_context" in a:
                ctx = a["llm_context"]
                llm_info = f"\nEvent: {ctx.get('event_type')} | {ctx.get('price_direction')} | {ctx.get('magnitude')}"
            _tg_send(
                f"EDGAR ALERT [{a['ticker']}] {a['form_type']} "
                f"(score: {a['score']:.0f})\n"
                f"Date: {a['date']}\n"
                f"Keywords: {kw_str}"
                f"{llm_info}\n"
                f"{a['excerpt']}"
            )
        logger.info(f"EDGAR watchlist scan: {len(alerts)} significant filings found")
    else:
        logger.debug("EDGAR watchlist scan: no significant filings")


async def _edgar_watchlist_loop() -> None:
    """Background loop running _edgar_watchlist_scan every EDGAR_SCAN_INTERVAL_SEC."""
    await asyncio.sleep(30)   # brief delay after startup
    while True:
        try:
            await _edgar_watchlist_scan()
        except Exception as exc:
            logger.error(f"EDGAR watchlist loop error: {exc}", exc_info=True)
        await asyncio.sleep(EDGAR_SCAN_INTERVAL_SEC)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("  MARA HYPERVISOR STARTING")
    logger.info(f"  Capital  : ${INITIAL_CAPITAL_USD:.2f}")
    logger.info(f"  Cycle    : {CYCLE_INTERVAL_SEC}s")
    logger.info(f"  Mode     : {'PAPER' if PAPER_TRADING else 'LIVE — REAL MONEY'}")
    logger.info(f"  Workers  : {list(WORKER_REGISTRY.keys())}")
    logger.info("=" * 60)

    task         = asyncio.create_task(orchestration_loop())
    edgar_task   = asyncio.create_task(_edgar_watchlist_loop())

    # Quarterly profit sweep — 7th of Jan / Apr / Jul / Oct
    scheduler = None
    if _APSCHEDULER_AVAILABLE:
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            _run_quarterly_sweep,
            CronTrigger(month="1,4,7,10", day=7, hour=9, minute=0),
            id="quarterly_sweep",
            replace_existing=True,
        )
        scheduler.start()
        logger.info("Quarterly profit sweep scheduler started (Jan/Apr/Jul/Oct 7th @ 09:00)")
    else:
        logger.warning("APScheduler not installed — quarterly sweep disabled. "
                       "Add apscheduler to requirements.txt.")

    yield

    task.cancel()
    edgar_task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    try:
        await edgar_task
    except asyncio.CancelledError:
        pass
    if scheduler:
        scheduler.shutdown(wait=False)
    logger.info("MARA Hypervisor shut down cleanly.")


app = FastAPI(title="MARA Hypervisor", version="1.1.0", lifespan=lifespan)


# ── Orchestration Loop ────────────────────────────────────────────────────────

async def orchestration_loop():
    await asyncio.sleep(5)   # Give workers time to start on first boot
    while True:
        cycle_start = time.time()
        state.cycle_count += 1
        try:
            await _run_cycle()
        except Exception as exc:
            logger.error(f"Cycle {state.cycle_count} failed: {exc}", exc_info=True)
        elapsed   = time.time() - cycle_start
        state.cycle_duration_ms = elapsed * 1000.0
        sleep_for = max(0, CYCLE_INTERVAL_SEC - elapsed)
        logger.info(f"Cycle {state.cycle_count} done in {elapsed:.1f}s. Next in {sleep_for:.0f}s.")
        await asyncio.sleep(sleep_for)


async def _run_cycle():
    logger.info(f"─── Hypervisor Cycle {state.cycle_count} ───")
    state.last_cycle_at = time.time()

    # Step 1: Health-check
    await _check_worker_health()
    healthy = [w for w, h in state.worker_health.items() if h]
    logger.info(f"  Healthy workers: {healthy}")

    # Step 2: Pull status
    await _pull_worker_status(healthy)
    _reconcile_capital()

    # Step 2.5: Pre-fetch data sources with per-source timing for latency monitoring.
    # Populates the market_data 5-min cache so classifier.classify_sync hits cache.
    await _prefetch_and_time_data_sources()

    # Step 3: Classify regime
    try:
        result = await asyncio.to_thread(classifier.classify_sync)
        state.current_regime    = result.regime.value
        state.regime_confidence = result.confidence
        logger.info(f"  Regime: {state.current_regime} ({state.regime_confidence:.0%})")
    except Exception as exc:
        logger.error(f"Regime classification failed: {exc} — holding {state.current_regime}")

    # Step 4: Risk check
    verdict = risk_mgr.assess(
        total_capital    = state.total_capital,
        free_capital     = state.free_capital,
        open_positions   = _count_open_positions(),
        worker_pnl       = state.worker_pnl,
        worker_allocated = state.allocations,  # authoritative; worker_allocated lags one cycle
    )
    state.risk_verdict = verdict.reason

    if not verdict:
        logger.warning(f"  Risk gate FAIL: {verdict.reason}")
        state.halted     = True
        state.halt_reason = verdict.reason
        if verdict.action == "halt_all":
            await _broadcast_pause(healthy)
            return
        if verdict.action in ("halt_worker", "trim_worker") and verdict.affected_worker:
            await _pause_worker(verdict.affected_worker)
            healthy = [w for w in healthy if w != verdict.affected_worker]
    else:
        if state.halted:
            logger.info("  Risk gate: CLEAR — resuming")
            state.halted     = False
            state.halt_reason = ""

    # Step 5: Allocate
    alloc = allocator.compute(
        regime          = state.current_regime,
        worker_health   = state.worker_health,
        worker_sharpe   = state.worker_sharpe,
        registered_only = healthy,
    )
    state.allocations = alloc.allocations
    logger.info(f"  {alloc.summary()}")

    # Step 6: Broadcast regime
    await _broadcast_regime(healthy, state.current_regime, state.regime_confidence)

    # Step 6.5: Pull signals and run pre-execution risk checks
    worker_signals  = await _pull_worker_signals(healthy, alloc.allocations)
    blocked_workers = _run_execution_risk_checks(worker_signals, alloc.allocations)
    lat_result      = execution_risk.check_latency()
    state.execution_risk_state = {
        "slippage":        dict(execution_risk.last_slippage_results),
        "liquidity":       dict(execution_risk.last_liquidity_results),
        "latency":         lat_result,
        "blocked_workers": list(blocked_workers),
        "cycle_count":     state.cycle_count,
    }

    # Step 7: Send allocations — skip blocked workers (live mode) or all on latency BLOCK
    if lat_result["flag"] == "BLOCK":
        logger.warning("execution_risk: latency BLOCK — deferring all allocations this cycle")
        allocations_to_send: Dict[str, float] = {}
    elif blocked_workers:
        allocations_to_send = {
            w: a for w, a in alloc.allocations.items() if w not in blocked_workers
        }
    else:
        allocations_to_send = alloc.allocations
    await _send_allocations(allocations_to_send)

    # Step 8: Resume workers that have allocation
    if state.current_regime not in DEFENSIVE_REGIMES:
        for worker in healthy:
            if alloc.allocations.get(worker, 0) > 0:
                await _resume_worker(worker)


# ── Worker Communication ──────────────────────────────────────────────────────

async def _check_worker_health():
    """Ping every registered worker /health endpoint concurrently."""
    workers = list(WORKER_REGISTRY.keys())
    urls    = [WORKER_REGISTRY[w] for w in workers]
    async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_SEC) as client:
        results = await asyncio.gather(
            *[client.get(f"{url}/health") for url in urls],
            return_exceptions=True,
        )
    for worker, result in zip(workers, results):
        if isinstance(result, Exception):
            state.worker_health[worker] = False
            logger.warning(f"  {worker}: health check failed ({type(result).__name__}: {result})")
        else:
            ok = result.status_code == 200
            state.worker_health[worker] = ok
            if not ok:
                logger.warning(f"  {worker}: health returned HTTP {result.status_code}")


async def _pull_worker_status(workers: List[str]):
    async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_SEC) as client:
        for worker in workers:
            url = WORKER_REGISTRY.get(worker)
            if not url:
                continue
            try:
                resp = await client.get(f"{url}/status")
                if resp.status_code == 200:
                    data = resp.json()
                    state.worker_status[worker]    = data
                    state.worker_pnl[worker]       = float(data.get("pnl", 0.0))
                    # Use None when sharpe is 0.0 (no trade history yet).
                    # capital.py treats None as "no data" and skips the Sharpe gate,
                    # allowing fresh workers to receive allocations on first cycle.
                    _sharpe = float(data.get("sharpe", 0.0))
                    state.worker_sharpe[worker] = _sharpe if _sharpe != 0.0 else None
                    state.worker_allocated[worker] = float(data.get("allocated_usd", 0.0))
            except Exception as exc:
                logger.warning(f"  {worker} /status failed: {exc}")


async def _broadcast_regime(workers: List[str], regime: str, confidence: float):
    payload = {"regime": regime, "confidence": confidence, "paper_trading": PAPER_TRADING}
    async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_SEC) as client:
        for worker in workers:
            url = WORKER_REGISTRY.get(worker)
            if url:
                try:
                    await client.post(f"{url}/regime", json=payload)
                except Exception as exc:
                    logger.warning(f"  {worker} /regime failed: {exc}")


async def _send_allocations(allocations: Dict[str, float]):
    async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_SEC) as client:
        for worker, amount in allocations.items():
            if amount < MIN_TRADE_SIZE_USD:
                continue
            url = WORKER_REGISTRY.get(worker)
            if not url:
                continue
            try:
                await client.post(f"{url}/allocate", json={
                    "amount_usd":    amount,
                    "paper_trading": PAPER_TRADING,
                })
                risk_mgr.record_worker_allocation(worker, amount)
            except Exception as exc:
                logger.warning(f"  {worker} /allocate failed: {exc}")


async def _broadcast_pause(workers: List[str]):
    async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_SEC) as client:
        for worker in workers:
            url = WORKER_REGISTRY.get(worker)
            if url:
                try:
                    await client.post(f"{url}/pause")
                except Exception:
                    pass


async def _pause_worker(worker: str):
    url = WORKER_REGISTRY.get(worker)
    if url:
        try:
            async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_SEC) as client:
                await client.post(f"{url}/pause")
            logger.info(f"  Paused: {worker}")
        except Exception as exc:
            logger.warning(f"  {worker} /pause failed: {exc}")


async def _resume_worker(worker: str):
    url = WORKER_REGISTRY.get(worker)
    if url:
        try:
            async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_SEC) as client:
                await client.post(f"{url}/resume")
        except Exception:
            pass


async def _prefetch_and_time_data_sources() -> None:
    """
    Warm the market-data cache and record per-source fetch latency.
    Runs four asyncio.to_thread calls sequentially so each source is
    timed independently. On cache hit every call returns in <10 ms.
    """
    import time as _t

    async def _timed(source: str, fn) -> None:
        t0 = _t.monotonic()
        try:
            await asyncio.to_thread(fn)
        except Exception as exc:
            logger.debug("data prefetch source=%s err=%s", source, exc)
        execution_risk.record_source_latency(source, (_t.monotonic() - t0) * 1000.0)

    def _yf():
        from data.feeds.market_data import (
            get_vix, get_dxy, get_bdi_slope, get_defense_momentum, get_gold_oil_ratio,
        )
        get_vix(); get_dxy(); get_bdi_slope(); get_defense_momentum(); get_gold_oil_ratio()

    def _fred():
        from data.feeds.market_data import get_yield_curve
        get_yield_curve()

    def _gdelt():
        # get_gdelt_tension_score is cached at 600 s TTL in market_data._cached.
        # Timing this call captures actual GDELT network latency on cache miss.
        from data.feeds.market_data import get_gdelt_tension_score
        get_gdelt_tension_score()

    def _okx():
        from data.feeds.market_data import get_crypto_funding_rate
        get_crypto_funding_rate("BTC-USDT-SWAP")

    await _timed("yfinance", _yf)
    await _timed("fred",     _fred)
    await _timed("gdelt",    _gdelt)
    await _timed("okx",      _okx)


async def _pull_worker_signals(
    workers: List[str], allocations: Dict[str, float]
) -> Dict[str, List[dict]]:
    """
    POST /signal on each healthy worker that has a non-trivial allocation.
    The analyst worker receives a full context payload; all others get {}.
    Returns {worker: [signal_dict, ...]}. Failures are logged at DEBUG.
    """
    results: Dict[str, List[dict]] = {}

    # Build analyst context payload once — used only for the "analyst" worker.
    analyst_payload = {
        "regime":            state.current_regime,
        "confidence":        state.regime_confidence,
        "war_premium_score": state.war_premium_score,
        "worker_states":     {
            w: {
                "pnl":          state.worker_pnl.get(w, 0.0),
                "sharpe":       state.worker_sharpe.get(w),
                "allocated_usd": state.worker_allocated.get(w, 0.0),
            }
            for w in WORKER_REGISTRY
            if w != "analyst"
        },
        "watchlist":     state.watchlist,
        "cycle_number":  state.cycle_count,
        "paper_trading": PAPER_TRADING,
    }

    async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_SEC) as client:
        for worker in workers:
            if allocations.get(worker, 0) < MIN_TRADE_SIZE_USD:
                continue
            url = WORKER_REGISTRY.get(worker)
            if not url:
                continue
            payload = analyst_payload if worker == "analyst" else {}
            try:
                resp = await client.post(f"{url}/signal", json=payload)
                if resp.status_code == 200:
                    sigs = resp.json()
                    if isinstance(sigs, list):
                        results[worker] = sigs
                        if worker == "analyst" and sigs:
                            state.last_analyst_signal = sigs[0]
            except Exception as exc:
                logger.debug("  %s /signal failed: %s", worker, exc)
    return results


def _run_execution_risk_checks(
    worker_signals: Dict[str, List[dict]],
    allocations: Dict[str, float],
) -> set:
    """
    Run slippage + liquidity checks for each worker's signals.
    Stores results in execution_risk.last_slippage_results / last_liquidity_results.
    Returns the set of worker names that have BLOCK-level results in live mode.
    Advisory-only symbols (CROSS_EXCHANGE_ARB) and empty symbols are skipped.
    """
    blocked_workers: set = set()

    for worker, signals in worker_signals.items():
        order_size_usd = allocations.get(worker, 0.0)
        worker_blocked = False
        for sig in signals[:2]:   # check up to 2 signals per worker
            symbol = sig.get("symbol", "")
            if not symbol or symbol in ("CROSS_EXCHANGE_ARB",):
                continue
            side = sig.get("direction", "long")
            slip = execution_risk.check_slippage(
                symbol=symbol,
                signal_price=0.0,
                side=side,
                regime=state.current_regime,
            )
            liq = execution_risk.check_liquidity(
                symbol=symbol,
                order_size_usd=order_size_usd,
            )
            if slip.get("flag") == "BLOCK" or liq.get("flag") == "BLOCK":
                worker_blocked = True

        if worker_blocked and not PAPER_TRADING:
            blocked_workers.add(worker)

    return blocked_workers


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reconcile_capital():
    if PAPER_TRADING:
        # Paper PnL is simulated — keep capital base fixed at INITIAL_CAPITAL_USD.
        state.total_capital = INITIAL_CAPITAL_USD
    else:
        total_pnl           = sum(state.worker_pnl.values())
        state.total_capital = round(INITIAL_CAPITAL_USD + total_pnl, 2)
    deployed            = sum(state.allocations.values())
    state.free_capital  = round(state.total_capital - deployed, 2)
    allocator.update_capital(state.total_capital)


def _count_open_positions() -> int:
    return sum(int(s.get("open_positions", 0)) for s in state.worker_status.values())


# ── REST API ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "uptime_sec": round(time.time() - state.started_at)}


@app.get("/status")
async def status():
    return {
        "regime":            state.current_regime,
        "regime_confidence": round(state.regime_confidence, 3),
        "total_capital":     round(state.total_capital, 2),
        "free_capital":      round(state.free_capital, 2),
        "cycle_count":       state.cycle_count,
        "last_cycle_at":     state.last_cycle_at,
        "halted":            state.halted,
        "halt_reason":       state.halt_reason,
        "risk_verdict":      state.risk_verdict,
        "worker_health":     state.worker_health,
        "allocations":       state.allocations,
        "worker_pnl":        state.worker_pnl,
        "worker_sharpe":     state.worker_sharpe,
        "paper_trading":     PAPER_TRADING,
    }


@app.get("/workers")
async def workers():
    return {
        worker: {
            "url":       url,
            "healthy":   state.worker_health.get(worker, False),
            "allocated": state.worker_allocated.get(worker, 0.0),
            "pnl":       state.worker_pnl.get(worker, 0.0),
            "sharpe":    state.worker_sharpe.get(worker, 0.0),
        }
        for worker, url in WORKER_REGISTRY.items()
    }


@app.get("/regime")
async def current_regime():
    return {"regime": state.current_regime, "confidence": round(state.regime_confidence, 3)}


@app.get("/risk")
async def risk_summary():
    return {
        "verdict":      state.risk_verdict,
        "halted":       state.halted,
        "halt_reason":  state.halt_reason,
        "total_capital": round(state.total_capital, 2),
        "risk_summary": risk_mgr.summary(state.total_capital, state.free_capital),
    }


@app.post("/halt")
async def manual_halt():
    healthy = [w for w, h in state.worker_health.items() if h]
    await _broadcast_pause(healthy)
    state.halted     = True
    state.halt_reason = "Manual halt via API"
    logger.warning("MANUAL HALT triggered via API")
    return {"halted": True, "workers_paused": healthy}


@app.post("/resume")
async def manual_resume():
    if not state.halted:
        raise HTTPException(status_code=400, detail="Hypervisor is not halted")
    risk_mgr.reset_halt()
    state.halted     = False
    state.halt_reason = ""
    healthy = [w for w, h in state.worker_health.items() if h]
    async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_SEC) as client:
        for worker in healthy:
            url = WORKER_REGISTRY.get(worker)
            if url:
                try:
                    await client.post(f"{url}/resume")
                except Exception:
                    pass
    return {"resumed": True, "workers": healthy}


@app.post("/workers/{worker}/pause")
async def pause_worker(worker: str):
    url = WORKER_REGISTRY.get(worker)
    if not url:
        raise HTTPException(status_code=404, detail=f"Unknown worker: {worker}")
    await _pause_worker(worker)
    return {"paused": worker}


@app.post("/workers/{worker}/resume")
async def resume_worker(worker: str):
    url = WORKER_REGISTRY.get(worker)
    if not url:
        raise HTTPException(status_code=404, detail=f"Unknown worker: {worker}")
    await _resume_worker(worker)
    return {"resumed": worker}


@app.get("/watchlist")
async def get_watchlist():
    return {"watchlist": state.watchlist}


@app.post("/watchlist")
async def add_to_watchlist(body: dict):
    ticker = body.get("ticker", "").upper().strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")
    if ticker not in state.watchlist:
        state.watchlist.append(ticker)
        logger.info(f"Watchlist: added {ticker}")
    return {"watchlist": state.watchlist}


@app.get("/execution-risk")
async def execution_risk_state():
    """Latest pre-execution risk check results from the most recent cycle."""
    return state.execution_risk_state


@app.get("/thesis")
async def get_thesis():
    """Latest analyst signal/thesis — read by Grafana thesis panel and Telegram bot."""
    return state.last_analyst_signal


@app.get("/edgar-alerts")
async def get_edgar_alerts():
    """Latest EDGAR significant filing alerts (updated every 30 min). Used by Grafana and analyst worker."""
    return {"alerts": state.last_edgar_alerts, "count": len(state.last_edgar_alerts)}


_ALL_REGIMES = [
    "WAR_PREMIUM", "CRISIS_ACUTE", "BEAR_RECESSION",
    "BULL_FROTHY", "REGIME_CHANGE", "SHADOW_DRIFT", "BULL_CALM",
]


@app.get("/metrics")
def hypervisor_metrics():
    """Prometheus text metrics for the hypervisor and execution risk gauges."""
    lines = [
        # ── Hypervisor internals ─────────────────────────────────────────────
        f'mara_hypervisor_cycle_count {state.cycle_count}',
        f'mara_hypervisor_capital_usd {state.total_capital:.2f}',
        f'mara_hypervisor_free_capital_usd {state.free_capital:.2f}',
        f'mara_hypervisor_halted {1 if state.halted else 0}',
        f'mara_hypervisor_workers_healthy {sum(1 for h in state.worker_health.values() if h)}',
        # ── Regime state ─────────────────────────────────────────────────────
        f'mara_regime_confidence {state.regime_confidence:.4f}',
        f'mara_war_premium_score {state.war_premium_score:.2f}',
        f'mara_cycle_duration_ms {state.cycle_duration_ms:.1f}',
    ]

    # One time series per regime label; active=1, all others=0
    for regime in _ALL_REGIMES:
        val = 1 if regime == state.current_regime else 0
        lines.append(f'mara_regime_label{{regime="{regime}"}} {val}')

    # ── Per-worker gauges ─────────────────────────────────────────────────────
    for worker, alloc_usd in state.allocations.items():
        lines.append(f'mara_worker_allocated_usd{{worker="{worker}"}} {alloc_usd:.2f}')

    for worker, pnl in state.worker_pnl.items():
        lines.append(f'mara_worker_pnl_usd{{worker="{worker}"}} {pnl:.4f}')

    for worker in WORKER_REGISTRY:
        sharpe = state.worker_sharpe.get(worker) or 0.0
        lines.append(f'mara_worker_sharpe{{worker="{worker}"}} {sharpe:.4f}')
        wstatus = state.worker_status.get(worker, {})
        lines.append(
            f'mara_worker_open_positions{{worker="{worker}"}} '
            f'{int(wstatus.get("open_positions", 0))}'
        )
        paused_int = 1 if wstatus.get("paused", False) else 0
        lines.append(f'mara_worker_paused{{worker="{worker}"}} {paused_int}')

    # ── Execution risk gauges (slippage, fill_rate, api_latency_p95) ─────────
    exec_risk_text = execution_risk.prometheus_metrics()
    if exec_risk_text:
        lines.append(exec_risk_text.rstrip("\n"))

    content = "\n".join(lines) + "\n"
    return Response(content=content, media_type="text/plain")
