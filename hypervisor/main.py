"""
hypervisor/main.py

Arka Hypervisor — The Regime-Aware Orchestrator.

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
import json
import logging
import os
import re
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import httpx
import requests as _requests
from fastapi import FastAPI, HTTPException
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _APSCHEDULER_AVAILABLE = True
except ImportError:
    _APSCHEDULER_AVAILABLE = False

from hypervisor.allocator.capital import HMM_STATE_LABELS, RegimeAllocator
import numpy as np
from hypervisor.audit import (
    audit,
    audit_allocation_update,
    audit_capital_reconcile,
    audit_circuit_breaker,
    audit_emergency_stop,
    audit_health_check,
    audit_regime_change,
    audit_shutdown,
    audit_startup,
    audit_worker_paused,
    audit_worker_resumed,
    AuditEvent,
    audit_log,
)
from hypervisor.auth import APIKeyMiddleware, get_or_create_api_key
from hypervisor.circuit_breaker import BREAKERS, get_dependency_health
from hypervisor.db.engine import async_session, init_db
from sqlalchemy import text

# Audit file path for health check endpoint
_audit_file_path = Path("data/audit.jsonl")

# Database path for health check endpoint
_DB_PATH = Path(__file__).parent.parent / "data" / "arka.db"
from hypervisor.db.repository import ArkaRepository
from hypervisor.errors import RegimeClassificationError
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
CYCLE_INTERVAL_SEC  = int(os.environ.get("CYCLE_INTERVAL_SEC", 60))
WORKER_TIMEOUT_SEC  = int(os.environ.get("WORKER_TIMEOUT_SEC", 10))
PHASE3_ENABLED      = os.environ.get("PHASE3_ENABLED", "false").lower() == "true"
MIN_TRADE_SIZE_USD  = float(os.environ.get("MIN_TRADE_SIZE_USD", 10.0))
PAPER_TRADING       = os.environ.get("ARKA_LIVE", "false").lower() != "true"

# ── Worker Registry ───────────────────────────────────────────────────────────
# Keys MUST match capital.py ALLOCATION_PROFILES keys exactly.
# docker-compose service names resolve via Docker DNS (e.g. worker-nautilus).
# Override with env vars for local dev or Pi deploy.
WORKER_REGISTRY: Dict[str, str] = {
    "nautilus":             os.environ.get("NAUTILUS_URL",             "http://worker-nautilus:8001"),
    "prediction_markets":   os.environ.get("PREDICTION_MARKETS_URL",  "http://worker-prediction-markets:8002"),
    "analyst":              os.environ.get("ANALYST_URL",              "http://worker-analyst:8003"),
    "core_dividends":       os.environ.get("CORE_DIVIDENDS_URL",       "http://worker-core-dividends:8006"),
}

# Regimes that force all directional workers to pause new entries
DEFENSIVE_REGIMES = {"CRISIS"}


# ── Pydantic Request Models ───────────────────────────────────────────────────

class PauseRequest(BaseModel):
    worker: str = Field(..., pattern="^[a-z_]+$")

class ResumeRequest(BaseModel):
    worker: str = Field(..., pattern="^[a-z_]+$")

class AllocateRequest(BaseModel):
    amount_usd: float = Field(..., gt=0, le=100000)
    paper_trading: bool = True

class CredentialUpdate(BaseModel):
    key: str = Field(..., min_length=1, max_length=100)
    value: str = Field(..., min_length=0, max_length=500)

class WatchlistRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=20, pattern=r'^[A-Z0-9.=\-/]{1,20}$')

# ── Valid worker names (for input validation) ─────────────────────────────────
VALID_WORKERS = frozenset({"nautilus", "prediction_markets", "analyst", "core_dividends"})


# ── Global State ──────────────────────────────────────────────────────────────
class HypervisorState:
    def __init__(self):
        self._lock = asyncio.Lock()

        self.total_capital:     float            = INITIAL_CAPITAL_USD
        self.free_capital:      float            = INITIAL_CAPITAL_USD
        self.current_regime:    str              = "TRANSITION"
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
        self.allocations:            Dict[str, float] = {}
        self.regime_probabilities:   Dict[str, float] = {}
        self.circuit_breaker_active: bool             = False
        self.risk_verdict:           str              = "OK"
        self.watchlist:              List[str]         = []

    # ── Locked write helpers ───────────────────────────────────────────────────

    async def update_worker_pnl(self, worker: str, pnl: float) -> None:
        async with self._lock:
            self.worker_pnl[worker] = pnl

    async def update_worker_sharpe(self, worker: str, sharpe: Optional[float]) -> None:
        async with self._lock:
            self.worker_sharpe[worker] = sharpe

    async def update_worker_health(self, worker: str, healthy: bool) -> None:
        async with self._lock:
            self.worker_health[worker] = healthy

    async def update_allocations(self, new_allocs: Dict[str, float]) -> None:
        async with self._lock:
            self.allocations = new_allocs.copy()

    async def update_regime(
        self,
        regime: str,
        confidence: float,
        probs: Dict[str, float],
        circuit_breaker: bool,
    ) -> None:
        async with self._lock:
            self.current_regime         = regime
            self.regime_confidence      = confidence
            self.regime_probabilities   = probs
            self.circuit_breaker_active = circuit_breaker

    async def get_snapshot(self) -> dict:
        """Atomic read of all mutable state."""
        async with self._lock:
            return {
                "worker_health":        self.worker_health.copy(),
                "worker_pnl":           self.worker_pnl.copy(),
                "worker_sharpe":        self.worker_sharpe.copy(),
                "allocations":          self.allocations.copy(),
                "regime":               self.current_regime,
                "regime_confidence":    self.regime_confidence,
                "regime_probs":         self.regime_probabilities.copy(),
                "circuit_breaker":      self.circuit_breaker_active,
                "total_capital":        self.total_capital,
                "free_capital":         self.free_capital,
                "halted":               self.halted,
                "halt_reason":          self.halt_reason,
                "risk_verdict":         self.risk_verdict,
            }

    async def reconcile_capital(self) -> None:
        """Atomic capital reconciliation — must be called under lock or as sole writer."""
        async with self._lock:
            if PAPER_TRADING:
                self.total_capital = INITIAL_CAPITAL_USD
            else:
                total_pnl = sum(self.worker_pnl.values())
                self.total_capital = round(INITIAL_CAPITAL_USD + total_pnl, 2)
            deployed = sum(self.allocations.values())
            self.free_capital = round(self.total_capital - deployed, 2)
            allocator.update_capital(self.total_capital)


state      = HypervisorState()
classifier = RegimeClassifier()
allocator  = RegimeAllocator(total_capital=INITIAL_CAPITAL_USD)
risk_mgr   = RiskManager(initial_capital=INITIAL_CAPITAL_USD)
repo       = ArkaRepository(async_session)


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
    sends a Telegram alert.  When PHASE3_ENABLED=true, broker redemption
    stubs are activated (OKX USDT → fiat, broker transfer).
    """
    target     = INITIAL_CAPITAL_USD * PROFIT_TARGET_MULTIPLIER
    surplus    = round(state.total_capital - target, 2)
    total      = round(state.total_capital, 2)
    regime     = state.current_regime
    cycle      = state.cycle_count

    if surplus > 0:
        _phase3_note = "\n\n_Redemption not yet wired — paper mode only._" if not PHASE3_ENABLED else ""
        msg = (
            f"📈 *Arka Quarterly Profit Sweep*\n"
            f"Total capital: ${total:.2f}\n"
            f"Target floor:  ${target:.2f} (initial × 1.10)\n"
            f"Surplus:       *${surplus:.2f}*\n"
            f"Regime: `{regime}` | Cycles: {cycle}"
            f"{_phase3_note}"
        )
        logger.info(f"Quarterly sweep: surplus=${surplus:.2f} above ${target:.2f} floor")
    else:
        shortfall = round(target - state.total_capital, 2)
        msg = (
            f"📊 *Arka Quarterly Sweep — No Surplus*\n"
            f"Total capital: ${total:.2f}\n"
            f"Target floor:  ${target:.2f}\n"
            f"Shortfall:     ${shortfall:.2f}\n"
            f"Regime: `{regime}` | Cycles: {cycle}"
        )
        logger.info(f"Quarterly sweep: no surplus — ${shortfall:.2f} below target")

    _tg_send(msg)
    audit_log.info(
        AuditEvent.ALLOCATION_UPDATE,
        sub_event="profit_sweep",
        total_capital=total,
        surplus=max(surplus, 0),
        regime=regime,
        cycle=cycle,
    )


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate config before anything else — moved from import time (BUG-04)
    validate_config()

    logger.info("=" * 60)
    logger.info("  ARKA HYPERVISOR STARTING")
    logger.info(f"  Capital  : ${INITIAL_CAPITAL_USD:.2f}")
    logger.info(f"  Cycle    : {CYCLE_INTERVAL_SEC}s")
    logger.info(f"  Mode     : {'PAPER' if PAPER_TRADING else 'LIVE — REAL MONEY'}")
    logger.info(f"  Workers  : {list(WORKER_REGISTRY.keys())}")
    logger.info("=" * 60)

    # Initialise SQLite database (creates tables from schema.sql if not present)
    try:
        await init_db()
        logger.info("Database initialised.")
    except Exception as exc:
        logger.error("Database init failed — persistence disabled: %s", exc)

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
        scheduler.add_job(
            classifier.retrain,
            CronTrigger(day=1, hour=3, minute=0),
            id="hmm_monthly_retrain",
            replace_existing=True,
        )
        scheduler.start()
        logger.info("Quarterly profit sweep scheduler started (Jan/Apr/Jul/Oct 7th @ 09:00)")
        logger.info("HMM monthly retrain scheduler started (1st of each month @ 03:00)")
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
    logger.info("Arka Hypervisor shut down cleanly.")


app = FastAPI(title="Arka Hypervisor", version="2.0.0", lifespan=lifespan)

# Auth middleware — must be added before CORS so it runs first on inbound requests.
# CORS is still needed so the dashboard (different port in dev) can make requests.
_api_key = get_or_create_api_key()
app.add_middleware(APIKeyMiddleware, api_key=_api_key)

# CORS origins — default allows only the dashboard (port 3000) and Vite dev server (5173).
# Override at deploy time via CORS_ALLOWED_ORIGINS (comma-separated list).
_cors_origins_env = os.environ.get("CORS_ALLOWED_ORIGINS", "")
_cors_allowed_origins: List[str] = (
    [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    if _cors_origins_env
    else ["http://localhost:3000", "http://localhost:5173"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


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
    snap = await state.get_snapshot()
    healthy = [w for w, h in snap["worker_health"].items() if h]
    logger.info(f"  Healthy workers: {healthy}")

    # Step 2: Pull status
    await _pull_worker_status(healthy)
    await state.reconcile_capital()

    # Step 3: Classify regime
    regime_probs_array = None   # numpy (4,) — set when HMM classification succeeds
    try:
        result = await asyncio.to_thread(classifier.classify_sync)
        await state.update_regime(
            result.regime.value,
            result.confidence,
            result.probabilities,
            result.circuit_breaker_active,
        )
        # Persist regime event
        await repo.log_regime(
            result.regime.value,
            {},  # macro snapshot not available here; captured in feature_pipeline
            result.circuit_breaker_active,
        )
        # Build numpy array from probabilities dict (order must match HMM_STATE_LABELS)
        regime_probs_array = np.array(
            [result.probabilities.get(lbl, 0.0) for lbl in HMM_STATE_LABELS],
            dtype=float,
        )
        logger.info(
            f"  Regime: {state.current_regime} ({state.regime_confidence:.0%})"
            f"  CB={state.circuit_breaker_active}"
        )
        await audit_regime_change(
            old_regime=snap["regime"],
            new_regime=result.regime.value,
            probabilities=result.probabilities,
            circuit_breaker_active=result.circuit_breaker_active,
        )
    except RegimeClassificationError as exc:
        logger.error(f"Regime classification failed: {exc} — holding {state.current_regime}")
    except Exception as exc:
        logger.error(f"Regime classification failed: {exc} — holding {state.current_regime}")

    # Re-read snapshot after regime update
    snap = await state.get_snapshot()

    # Step 4: Risk check
    verdict = risk_mgr.assess(
        total_capital    = snap["total_capital"],
        free_capital     = snap["free_capital"],
        open_positions   = _count_open_positions(),
        worker_pnl       = snap["worker_pnl"],
        worker_allocated = snap["allocations"],
    )
    async with state._lock:
        state.risk_verdict = verdict.reason

    if not verdict:
        logger.warning(f"  Risk gate FAIL: {verdict.reason}")
        async with state._lock:
            state.halted      = True
            state.halt_reason = verdict.reason
        if verdict.action == "halt_all":
            await _broadcast_pause(healthy)
            return
        if verdict.action in ("halt_worker", "trim_worker") and verdict.affected_worker:
            await _pause_worker(verdict.affected_worker)
            healthy = [w for w in healthy if w != verdict.affected_worker]
    else:
        async with state._lock:
            if state.halted:
                logger.info("  Risk gate: CLEAR — resuming")
                state.halted      = False
                state.halt_reason = ""

    # Step 5: Allocate (use HMM probability vector when available)
    alloc = allocator.compute(
        regime          = snap["regime"],
        worker_health   = snap["worker_health"],
        worker_sharpe   = snap["worker_sharpe"],
        registered_only = healthy,
        probabilities   = regime_probs_array,
    )
    await state.update_allocations(alloc.allocations)
    logger.info(f"  {alloc.summary()}")
    await audit_allocation_update(
        allocations=alloc.allocations,
        regime=snap["regime"],
        total_capital=snap["total_capital"],
    )

    # Step 6: Broadcast regime
    await _broadcast_regime(healthy, snap["regime"], snap["regime_confidence"])

    # Step 7: Send allocations
    await _send_allocations(alloc.allocations)

    # Step 8: Persist portfolio snapshot
    final_snap = await state.get_snapshot()
    deployed = sum(final_snap["allocations"].values())
    total = final_snap["total_capital"]
    await repo.snapshot_portfolio(
        total_value=total,
        cash_pct=(total - deployed) / total if total > 0 else 1.0,
        drawdown_pct=risk_mgr.get_drawdown_pct(total),
        regime=final_snap["regime"],
        allocations=final_snap["allocations"],
    )

    # Step 9: Resume workers that have allocation
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
            await state.update_worker_health(worker, False)
            logger.warning(f"  {worker}: health check failed ({type(result).__name__}: {result})")
        else:
            ok = result.status_code == 200
            await state.update_worker_health(worker, ok)
            if not ok:
                logger.warning(f"  {worker}: health returned HTTP {result.status_code}")


async def _pull_worker_status(workers: List[str]):
    """Poll all healthy workers /status concurrently (BUG-06 fix)."""

    async def _fetch_one(client: httpx.AsyncClient, worker: str) -> None:
        url = WORKER_REGISTRY.get(worker)
        if not url:
            return
        try:
            resp = await client.get(f"{url}/status")
            if resp.status_code == 200:
                data = resp.json()
                async with state._lock:
                    state.worker_status[worker] = data
                pnl = float(data.get("pnl", 0.0))
                await state.update_worker_pnl(worker, pnl)
                # Use None when sharpe is 0.0 (no trade history yet).
                # capital.py treats None as "no data" and skips the Sharpe gate,
                # allowing fresh workers to receive allocations on first cycle.
                _sharpe = float(data.get("sharpe", 0.0))
                await state.update_worker_sharpe(worker, _sharpe if _sharpe != 0.0 else None)
                async with state._lock:
                    state.worker_allocated[worker] = float(data.get("allocated_usd", 0.0))
        except Exception as exc:
            logger.warning(f"  {worker} /status failed: {exc}")

    async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_SEC) as client:
        await asyncio.gather(*[_fetch_one(client, w) for w in workers])


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
            await audit_worker_paused(worker, reason="risk_limit")
        except Exception as exc:
            logger.warning(f"  {worker} /pause failed: {exc}")


async def _resume_worker(worker: str):
    url = WORKER_REGISTRY.get(worker)
    if url:
        try:
            async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_SEC) as client:
                await client.post(f"{url}/resume")
            await audit_worker_resumed(worker)
        except Exception:
            pass


# ── Helpers ───────────────────────────────────────────────────────────────────


def _count_open_positions() -> int:
    return sum(int(s.get("open_positions", 0)) for s in state.worker_status.values())


# ── REST API ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "uptime_sec": round(time.time() - state.started_at)}


@app.get("/status")
async def status():
    snap = await state.get_snapshot()
    return {
        "regime":            snap["regime"],
        "regime_confidence": round(snap["regime_confidence"], 3),
        "total_capital":     round(snap["total_capital"], 2),
        "free_capital":      round(snap["free_capital"], 2),
        "cycle_count":       state.cycle_count,
        "last_cycle_at":     state.last_cycle_at,
        "halted":            snap["halted"],
        "halt_reason":       snap["halt_reason"],
        "risk_verdict":      snap["risk_verdict"],
        "worker_health":     snap["worker_health"],
        "allocations":       snap["allocations"],
        "worker_pnl":        snap["worker_pnl"],
        "worker_sharpe":     snap["worker_sharpe"],
        "paper_trading":     PAPER_TRADING,
    }


@app.get("/workers")
async def workers():
    snap = await state.get_snapshot()
    return {
        worker: {
            "url":       url,
            "healthy":   snap["worker_health"].get(worker, False),
            "allocated": snap["allocations"].get(worker, 0.0),
            "pnl":       snap["worker_pnl"].get(worker, 0.0),
            "sharpe":    snap["worker_sharpe"].get(worker) or 0.0,
        }
        for worker, url in WORKER_REGISTRY.items()
    }


@app.get("/regime")
async def current_regime():
    snap = await state.get_snapshot()
    return {
        "regime":                 snap["regime"],
        "confidence":             round(snap["regime_confidence"], 3),
        "probabilities":          snap["regime_probs"],
        "circuit_breaker_active": snap["circuit_breaker"],
    }


@app.get("/risk")
async def risk_summary():
    snap = await state.get_snapshot()
    return {
        "verdict":       snap["risk_verdict"],
        "halted":        snap["halted"],
        "halt_reason":   snap["halt_reason"],
        "total_capital": round(snap["total_capital"], 2),
        "risk_summary":  risk_mgr.summary(snap["total_capital"], snap["free_capital"]),
    }


@app.post("/halt")
async def manual_halt():
    snap = await state.get_snapshot()
    healthy = [w for w, h in snap["worker_health"].items() if h]
    await _broadcast_pause(healthy)
    async with state._lock:
        state.halted      = True
        state.halt_reason = "Manual halt via API"
    logger.warning("MANUAL HALT triggered via API")
    return {"halted": True, "workers_paused": healthy}


@app.post("/resume")
async def manual_resume():
    async with state._lock:
        if not state.halted:
            raise HTTPException(status_code=400, detail="Hypervisor is not halted")
        risk_mgr.reset_halt()
        state.halted      = False
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
async def pause_worker(
    worker: str = PathParam(..., pattern=r"^[a-z_]+$"),
):
    if worker not in VALID_WORKERS:
        raise HTTPException(status_code=404, detail=f"Unknown worker: {worker}")
    url = WORKER_REGISTRY.get(worker)
    if not url:
        raise HTTPException(status_code=404, detail=f"Worker not registered: {worker}")
    await _pause_worker(worker)
    return {"paused": worker}


@app.post("/workers/{worker}/resume")
async def resume_worker(
    worker: str = PathParam(..., pattern=r"^[a-z_]+$"),
):
    if worker not in VALID_WORKERS:
        raise HTTPException(status_code=404, detail=f"Unknown worker: {worker}")
    url = WORKER_REGISTRY.get(worker)
    if not url:
        raise HTTPException(status_code=404, detail=f"Worker not registered: {worker}")
    await _resume_worker(worker)
    return {"resumed": worker}


@app.get("/watchlist")
async def get_watchlist():
    async with state._lock:
        return {"watchlist": list(state.watchlist)}


@app.post("/watchlist")
async def add_to_watchlist(body: WatchlistRequest):
    ticker = body.ticker.upper().strip()
    if not re.match(r'^[A-Z0-9.=\-/]{1,20}$', ticker):
        raise HTTPException(status_code=400, detail="ticker must be 1-20 chars of [A-Z0-9.=/-]")
    async with state._lock:
        if ticker not in state.watchlist:
            state.watchlist.append(ticker)
            logger.info(f"Watchlist: added {ticker}")
        watchlist = list(state.watchlist)
    return {"watchlist": watchlist}


# ── Setup / Hardware endpoints ─────────────────────────────────────────────────

_HARDWARE_PROFILE_PATH = Path("/app/.hardware_profile.json")

# Keys the wizard is allowed to write into .env
_ALLOWED_CREDENTIAL_KEYS = {
    "OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE",
    "FRED_API_KEY",
    "KALSHI_EMAIL", "KALSHI_PASSWORD",
    "POLY_PRIVATE_KEY", "POLY_PROXY_WALLET",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID",
    "NTFY_TOPIC",
    "SETUP_COMPLETE",
}

_CONTAINER_RESTART_MAP: Dict[str, str] = {
    "OKX_API_KEY":               "worker-nautilus",
    "OKX_API_SECRET":            "worker-nautilus",
    "OKX_API_PASSPHRASE":        "worker-nautilus",
    "KALSHI_EMAIL":              "worker-prediction-markets",
    "KALSHI_PASSWORD":           "worker-prediction-markets",
    "POLY_PRIVATE_KEY":          "worker-prediction-markets",
    "TELEGRAM_BOT_TOKEN":        "worker-telegram-bot",
    "TELEGRAM_ALLOWED_USER_ID":  "worker-telegram-bot",
}


async def _check_ollama_health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            r = await client.get(f"{os.environ.get('OLLAMA_HOST', 'http://ollama:11434')}/api/version")
            return r.status_code == 200
    except Exception:
        return False


def _restart_container(service: str) -> None:
    """Restart a sibling container via the Docker HTTP API.

    In production the hypervisor talks to a scoped docker-socket-proxy
    (DOCKER_PROXY_URL defaults to http://docker-proxy:2375) rather than
    mounting /var/run/docker.sock directly.  This limits the blast radius to
    only POST /containers/{id}/restart — no build, no exec, no image access.
    """
    project = os.environ.get("COMPOSE_PROJECT_NAME", "arka")
    container = f"{project}-{service.replace('worker-', '')}"
    proxy_url = os.environ.get("DOCKER_PROXY_URL", "http://docker-proxy:2375")
    try:
        resp = _requests.post(
            f"{proxy_url}/containers/{container}/restart",
            params={"t": 10},
            timeout=20,
        )
        if resp.status_code in (204, 200):
            logger.info(f"Restarted container: {container}")
        else:
            logger.warning(
                f"Could not restart {container}: HTTP {resp.status_code} {resp.text[:200]}"
            )
    except Exception as exc:
        logger.warning(f"Could not restart {container}: {exc}")


@app.get("/system/hardware")
async def system_hardware():
    """Returns detected hardware profile written by install.sh."""
    if _HARDWARE_PROFILE_PATH.exists():
        return json.loads(_HARDWARE_PROFILE_PATH.read_text())
    return {"error": "Hardware profile not detected — re-run install.sh"}


@app.get("/health/dependencies")
async def dependency_health():
    """Check every external dependency and return status."""
    health = await get_dependency_health()
    return {
        "yfinance": health.get("yfinance", "unknown"),
        "fred": health.get("fred", "unknown"),
        "gdelt": health.get("gdelt", "unknown"),
        "okx": health.get("okx", "unknown"),
        "edgar": health.get("edgar", "unknown"),
        "ollama": health.get("ollama", "unknown"),
        "kalshi": health.get("kalshi", "unknown"),
        "sqlite": health.get("sqlite", "unknown"),
    }


@app.get("/health/circuit_breakers")
async def circuit_breaker_status():
    """Detailed circuit breaker status for all dependencies."""
    status = {}
    for name, breaker in BREAKERS.items():
        status[name] = {
            "state": breaker.state.value,
            "failure_count": breaker._failure_count,
            "last_failure_time": breaker._last_failure_time,
            "cooldown_seconds": breaker.cooldown_seconds,
        }
    return status


@app.get("/health/audit")
async def audit_health():
    """Check if audit logging is working."""
    try:
        await audit("health_check", component="audit", status="ok")
        return {"status": "ok", "audit_file": str(_audit_file_path)}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@app.get("/health/persistence")
async def persistence_health():
    """Check if database persistence is working."""
    try:
        # Test database connection with a read-only query (BUG-03 fix).
        # Never write TEST rows to the production database from a health check.
        async with async_session() as session:
            result = await session.execute(text("SELECT 1"))
            reachable = result.scalar() == 1

        history = await repo.get_recent_regime_log(limit=1)

        return {
            "status": "ok",
            "database_path": str(_DB_PATH),
            "db_reachable":    reachable,
            "regime_log_rows": len(history),
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@app.get("/health/locks")
async def locks_health():
    """Check if state locking is working."""
    try:
        # Test concurrent access to state then immediately clean up (BUG-02 fix).
        # "test_worker" must not persist in worker_pnl after this health check.
        _HEALTH_TEST_WORKER = "__health_lock_test__"

        async def test_update():
            await state.update_worker_pnl(_HEALTH_TEST_WORKER, 100.0)
            snap = await state.get_snapshot()
            return snap["worker_pnl"].get(_HEALTH_TEST_WORKER)

        results = await asyncio.gather(*[test_update() for _ in range(5)])

        # Remove the ephemeral test entry from live state
        async with state._lock:
            state.worker_pnl.pop(_HEALTH_TEST_WORKER, None)

        return {
            "status": "ok",
            "concurrent_updates": all(r == 100.0 for r in results),
            "lock_functionality": True,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@app.get("/setup/status")
async def setup_status():
    """
    Returns which setup steps are complete. Safe to poll from the dashboard —
    this endpoint is exempt from API key authentication.

    Includes api_key so the dashboard can store it in localStorage on first load
    and authenticate all subsequent requests.
    """
    env = os.environ
    ollama_ok = await _check_ollama_health()
    return {
        "hardware_detected":             _HARDWARE_PROFILE_PATH.exists(),
        "okx_configured":                bool(env.get("OKX_API_KEY")),
        "telegram_configured":           bool(env.get("TELEGRAM_BOT_TOKEN")),
        "fred_configured":               bool(env.get("FRED_API_KEY")),
        "kalshi_configured":             bool(env.get("KALSHI_EMAIL")),
        "prediction_markets_configured": bool(env.get("POLY_PRIVATE_KEY") or env.get("KALSHI_EMAIL")),
        "ntfy_configured":               bool(env.get("NTFY_TOPIC")),
        "ollama_ready":                  ollama_ok,
        "setup_complete":                env.get("SETUP_COMPLETE", "false") == "true",
        # api_key is only included before setup is complete so the dashboard
        # can persist it in localStorage on first load. Once setup_complete=true
        # the key must not be re-exposed via this unauthenticated endpoint.
        **({"api_key": _api_key} if env.get("SETUP_COMPLETE", "false") != "true" else {}),
    }


@app.post("/setup/credentials")
async def save_credentials(body: dict):
    """
    Dashboard wizard POSTs credentials here.
    Writes allowed keys to .env, updates live env, restarts affected containers.
    Never returns credential values.
    """
    env_path = Path("/app/.env")
    if not env_path.exists():
        raise HTTPException(status_code=500, detail=".env not found — re-run install.sh")

    env_content = env_path.read_text()
    updated_keys: List[str] = []
    containers_to_restart: set = set()

    for key, value in body.items():
        if key not in _ALLOWED_CREDENTIAL_KEYS:
            logger.warning(f"setup/credentials: rejected unknown key {key!r}")
            continue
        value_str = str(value).strip()
        # Replace existing key=... line or append if not present
        pattern = rf'^{re.escape(key)}=.*$'
        new_line = f'{key}={value_str}'
        if re.search(pattern, env_content, flags=re.MULTILINE):
            env_content = re.sub(pattern, new_line, env_content, flags=re.MULTILINE)
        else:
            env_content += f'\n{new_line}'
        # Update live env so /setup/status reflects immediately
        os.environ[key] = value_str
        updated_keys.append(key)
        if key in _CONTAINER_RESTART_MAP:
            containers_to_restart.add(_CONTAINER_RESTART_MAP[key])

    # Atomic write: write to a temp file then rename so a crash mid-write never
    # leaves a partial/corrupt .env (rename is atomic on POSIX filesystems).
    tmp_path = env_path.with_suffix(".tmp")
    tmp_path.write_text(env_content)
    os.replace(tmp_path, env_path)
    logger.info(f"setup/credentials: wrote {updated_keys}")

    loop = asyncio.get_running_loop()
    for container in containers_to_restart:
        fut = loop.run_in_executor(None, _restart_container, container)
        # Suppress "Future exception was never retrieved" if _restart_container
        # raises — container restarts are best-effort, failures already logged
        # inside _restart_container itself.
        fut.add_done_callback(lambda f: f.exception())

    return {"status": "saved", "keys_updated": updated_keys}


# ── Dashboard State Endpoint (FEAT-01) ────────────────────────────────────────

@app.get("/dashboard/state")
async def dashboard_state():
    """
    Full data snapshot consumed by the React dashboard every 10 seconds.
    Returns all top-level keys the dashboard expects:
      regime, conflict_score, circuit_breaker, risk, portfolio, workers,
      domain_signals, timeline, thesis, backtest, system
    All fields are derived from live HypervisorState — no external calls.
    """
    import platform
    snap = await state.get_snapshot()

    # ── System metrics ─────────────────────────────────────────────────────
    try:
        import psutil
        cpu_pct  = psutil.cpu_percent(interval=None)
        ram      = psutil.virtual_memory()
        ram_pct  = ram.percent
        disk     = psutil.disk_usage("/")
        disk_pct = disk.percent
        # CPU temperature — Pi 5 / Linux only; returns None on x86 dev
        temp_c: Optional[float] = None
        temps = getattr(psutil, "sensors_temperatures", lambda: {})()
        if temps:
            for key in ("cpu_thermal", "coretemp", "k10temp"):
                if key in temps and temps[key]:
                    temp_c = round(temps[key][0].current, 1)
                    break
        ollama_online = await _check_ollama_health()
        system_info: Dict[str, Any] = {
            "cpu_pct":      round(cpu_pct, 1),
            "ram_pct":      round(ram_pct, 1),
            "disk_pct":     round(disk_pct, 1),
            "temp_c":       temp_c,
            "ollama_online": ollama_online,
        }
    except Exception:
        system_info = {
            "cpu_pct": 0.0, "ram_pct": 0.0, "disk_pct": 0.0,
            "temp_c": None, "ollama_online": False,
        }

    # ── Portfolio ──────────────────────────────────────────────────────────
    total_pnl = sum(snap["worker_pnl"].values())
    total_val = round(snap["total_capital"] + total_pnl, 2) if not PAPER_TRADING else round(snap["total_capital"], 2)
    deployed  = round(sum(snap["allocations"].values()), 2)
    gain_pct  = round((total_val - INITIAL_CAPITAL_USD) / INITIAL_CAPITAL_USD, 4) if INITIAL_CAPITAL_USD else 0.0
    portfolio: Dict[str, Any] = {
        "total_value":    total_val,
        "deployed_usd":   deployed,
        "cash_usd":       round(snap["free_capital"], 2),
        "total_gain_pct": gain_pct,
        "positions":      [],  # populated by individual worker /status calls in the cycle
    }

    # ── Risk ───────────────────────────────────────────────────────────────
    open_positions = sum(
        s.get("open_positions", 0)
        for s in state.worker_status.values()
        if isinstance(s, dict)
    )
    drawdown_pct = risk_mgr._portfolio_drawdown(snap["total_capital"])
    # Composite risk score 0-100: 70% drawdown exposure, 30% position utilisation
    risk_score = min(
        100,
        round(
            (drawdown_pct / 0.20) * 70
            + (open_positions / 6) * 30
        ),
    )
    risk_info: Dict[str, Any] = {
        "score":          risk_score,
        "drawdown_pct":   round(drawdown_pct, 4),
        "open_positions": open_positions,
        "var_pct":        0.0,   # populated when live VaR tracking is added
    }

    # ── Workers ────────────────────────────────────────────────────────────
    workers_info: Dict[str, Any] = {}
    for worker in WORKER_REGISTRY:
        ws = state.worker_status.get(worker) or {}
        workers_info[worker] = {
            "status":         "ok" if snap["worker_health"].get(worker) else "unreachable",
            "paused":         ws.get("paused", False),
            "pnl":            round(snap["worker_pnl"].get(worker, 0.0), 2),
            "sharpe":         round(snap["worker_sharpe"].get(worker) or 0.0, 3),
            "allocated_usd":  round(snap["allocations"].get(worker, 0.0), 2),
            "open_positions": ws.get("open_positions", 0),
        }

    # ── Regime ─────────────────────────────────────────────────────────────
    regime_info: Dict[str, Any] = {
        "label":         snap["regime"],
        "confidence":    round(snap["regime_confidence"], 3),
        "probabilities": snap["regime_probs"],
    }

    # ── Thesis (simplified — full thesis built by analyst worker) ──────────
    thesis: Dict[str, Any] = {
        "outlook":       "cautious" if snap["halted"] else "neutral",
        "drivers":       [],
        "signal_score":  0.0,
        "confidence":    round(snap["regime_confidence"], 3),
        "risk_approved": not snap["halted"],
    }

    return {
        "regime":         regime_info,
        "conflict_score": 0,        # populated by conflict_index cycle step when available
        "circuit_breaker": snap["circuit_breaker"],
        "risk":           risk_info,
        "portfolio":      portfolio,
        "workers":        workers_info,
        "domain_signals": [],       # populated by domain_router cycle step when available
        "timeline":       [],       # populated from audit log when available
        "thesis":         thesis,
        "backtest":       {"strategies": []},
        "system":         system_info,
    }


# ── Startup Configuration Validation ──────────────────────────────────────────

def validate_config():
    """Validate all required and optional env vars. Fail fast on startup."""
    errors = []

    # Required
    capital = os.environ.get("INITIAL_CAPITAL_USD")
    if not capital:
        errors.append("INITIAL_CAPITAL_USD is required")
    else:
        try:
            val = float(capital)
            if val <= 0:
                errors.append("INITIAL_CAPITAL_USD must be positive")
        except ValueError:
            errors.append("INITIAL_CAPITAL_USD must be a number")

    cycle = os.environ.get("CYCLE_INTERVAL_SEC", "60")
    try:
        c = int(cycle)
        if c < 10 or c > 3600:
            errors.append("CYCLE_INTERVAL_SEC must be 10-3600")
    except ValueError:
        errors.append("CYCLE_INTERVAL_SEC must be an integer")

    mode = os.environ.get("TRADING_MODE", "swing")
    if mode not in ("swing", "day", "both"):
        errors.append(f"TRADING_MODE must be swing/day/both, got '{mode}'")

    # Warn on missing optional keys
    optional = ["FRED_API_KEY", "NASA_FIRMS_API_KEY", "UCDP_API_TOKEN",
                 "AISSTREAM_API_KEY", "TELEGRAM_BOT_TOKEN"]
    for key in optional:
        if not os.environ.get(key):
            logger.info("Optional env var not set: %s", key)

    if errors:
        for e in errors:
            logger.error("Config validation failed: %s", e)
        raise SystemExit(f"Configuration invalid: {'; '.join(errors)}")


# validate_config() is called inside lifespan() — not at import time (BUG-04)
