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

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _APSCHEDULER_AVAILABLE = True
except ImportError:
    _APSCHEDULER_AVAILABLE = False

from hypervisor.allocator.capital import RegimeAllocator
from hypervisor.regime.classifier import RegimeClassifier
from hypervisor.risk.manager import RiskManager

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
    "autohedge":      os.environ.get("AUTOHEDGE_URL",       "http://worker-autohedge:8003"),
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


state      = HypervisorState()
classifier = RegimeClassifier()
allocator  = RegimeAllocator(total_capital=INITIAL_CAPITAL_USD)
risk_mgr   = RiskManager(initial_capital=INITIAL_CAPITAL_USD)


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

    task = asyncio.create_task(orchestration_loop())

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
    try:
        await task
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

    # Step 7: Send allocations
    await _send_allocations(alloc.allocations)

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
