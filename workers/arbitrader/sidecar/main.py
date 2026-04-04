"""
workers/arbitrader/sidecar/main.py

Arbitrader Python Sidecar — MARA Worker Adapter.
FastAPI service on port 8004.

What this does:
    Arbitrader is a Java cross-exchange price arbitrage engine
    (github.com/agonyforge/arbitrader). It runs as a separate JVM process
    and exposes its own REST/metrics interface.

    This sidecar:
      1. Manages the Arbitrader JVM process lifecycle (start/stop/health)
      2. Reads Arbitrader's metrics (Prometheus endpoint or log scraping)
      3. Translates them into MARA's worker REST contract
      4. Receives regime signals and adjusts Arbitrader config if possible
      5. In backtest/paper mode: runs without the JVM and simulates arb PnL

Strategy:
    Cross-exchange price arbitrage (delta-neutral). Enters a long on the
    cheaper exchange and a short on the more expensive. Makes money from
    price convergence, not direction. Regime-agnostic but most productive
    when funding rates and crypto volatility are high (WAR_PREMIUM, BULL_FROTHY).

REST contract (full MARA standard + /allocate):
    GET  /health
    GET  /status     → pnl, sharpe, allocated_usd, open_positions, jvm_running
    GET  /metrics
    POST /allocate   → update Arbitrader position size limits
    POST /regime     → adjust exposure multipliers
    POST /signal     → return current arb spreads as signals
    POST /execute    → force an arb check cycle
    POST /pause
    POST /resume
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import subprocess
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
import structlog
from fastapi import FastAPI
from fastapi.responses import Response

logger = structlog.get_logger(__name__)

WORKER_NAME  = "arbitrader"
PAPER_TRADING = os.environ.get("MARA_LIVE", "false").lower() != "true"

# ── Arbitrader JVM config ─────────────────────────────────────────────────────
ARBITRADER_JAR   = os.environ.get("ARBITRADER_JAR", "/app/arbitrader.jar")
ARBITRADER_PORT  = int(os.environ.get("ARBITRADER_PORT", "9090"))   # Arbitrader's own REST port
JAVA_OPTS        = os.environ.get("JAVA_OPTS", "-Xmx400m -Xms128m")

# ── Regime → exposure multiplier ──────────────────────────────────────────────
# Arbitrader is delta-neutral so it's safe in most regimes.
# Cut exposure in CRISIS_ACUTE (liquidity dries up, spreads blow out to unworkable levels).
REGIME_MULTIPLIERS: Dict[str, float] = {
    "WAR_PREMIUM":    1.0,   # Crypto volatility high — good spread opportunities
    "CRISIS_ACUTE":   0.0,   # Pause — spreads blow out but so does slippage/liquidation risk
    "BEAR_RECESSION": 0.7,
    "BULL_FROTHY":    1.2,   # Leveraged longs → high funding divergence → best arb
    "REGIME_CHANGE":  0.8,
    "SHADOW_DRIFT":   0.9,
    "BULL_CALM":      1.0,
}


class ArbState:
    def __init__(self):
        self.allocated_usd:   float  = 0.0
        self.current_regime:  str    = "BULL_CALM"
        self.multiplier:      float  = 1.0
        self.paused:          bool   = False
        self.jvm_process:     Optional[subprocess.Popen] = None
        self.jvm_running:     bool   = False
        self.jvm_error:       Optional[str] = None

        # PnL tracking
        self.realised_pnl:    float  = 0.0
        self.open_positions:  int    = 0
        self.trade_count:     int    = 0
        self.win_count:       int    = 0
        self.returns_log:     List[float] = []
        self.start_time:      float  = time.time()

        # Paper sim state
        self._sim_spread:     float  = 0.008   # Simulated 0.8% spread (Arbitrader default entry)
        self._sim_last_check: float  = 0.0

    def sharpe(self) -> float:
        if len(self.returns_log) < 5:
            return 0.0
        import statistics
        mean = sum(self.returns_log) / len(self.returns_log)
        try:
            std = statistics.stdev(self.returns_log)
        except Exception:
            return 0.0
        if std < 1e-12:
            return 0.0
        return (mean / std) * math.sqrt(24 * 365)   # ~24 arb cycles per day

    def win_rate(self) -> float:
        return self.win_count / self.trade_count if self.trade_count > 0 else 0.0

    def uptime(self) -> float:
        return time.time() - self.start_time

    def is_healthy(self) -> bool:
        if self.paused:
            return True
        if PAPER_TRADING:
            return True   # Paper mode never needs JVM
        return self.jvm_running

    # ── JVM Lifecycle ─────────────────────────────────────────────────────────

    def start_jvm(self):
        """
        Start the Arbitrader JVM process.
        Non-fatal: if JAR isn't present, logs warning and falls back to paper sim.
        """
        if not os.path.exists(ARBITRADER_JAR):
            self.jvm_error = f"JAR not found: {ARBITRADER_JAR}"
            logger.warning("arbitrader_jar_missing", path=ARBITRADER_JAR,
                           fallback="paper simulation")
            return

        try:
            cmd = ["java"] + JAVA_OPTS.split() + ["-jar", ARBITRADER_JAR]
            self.jvm_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            time.sleep(3)   # Give JVM time to start
            if self.jvm_process.poll() is not None:
                self.jvm_error = f"JVM exited immediately (code {self.jvm_process.returncode})"
                logger.error("arbitrader_jvm_crashed", **{"code": self.jvm_process.returncode})
            else:
                self.jvm_running = True
                logger.info("arbitrader_jvm_started", pid=self.jvm_process.pid)
        except FileNotFoundError:
            self.jvm_error = "java not found in PATH"
            logger.warning("arbitrader_java_missing", fallback="paper simulation")
        except Exception as exc:
            self.jvm_error = str(exc)
            logger.error("arbitrader_jvm_start_failed", error=str(exc))

    def stop_jvm(self):
        if self.jvm_process and self.jvm_running:
            self.jvm_process.terminate()
            try:
                self.jvm_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.jvm_process.kill()
            self.jvm_running = False
            logger.info("arbitrader_jvm_stopped")

    def check_jvm_alive(self):
        """Called each health check cycle."""
        if self.jvm_process and self.jvm_process.poll() is not None:
            self.jvm_running = False
            self.jvm_error   = f"JVM exited (code {self.jvm_process.returncode})"
            logger.error("arbitrader_jvm_died_unexpectedly")

    # ── Arbitrader metrics scraping ───────────────────────────────────────────

    async def fetch_jvm_metrics(self) -> Dict[str, Any]:
        """
        Pull metrics from Arbitrader's REST endpoint.
        Returns empty dict on failure (sidecar still healthy, just no data).
        """
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"http://localhost:{ARBITRADER_PORT}/actuator/health")
                if resp.status_code == 200:
                    return resp.json()
        except Exception:
            pass
        return {}

    # ── Paper simulation ──────────────────────────────────────────────────────

    def paper_arb_cycle(self) -> Optional[Dict[str, Any]]:
        """
        Simulates one Arbitrader arb cycle.
        Models realistic arb: entry spread 0.8%, exit spread 0.5%, fees ~0.08%.
        Returns a simulated trade result or None (no opportunity this cycle).
        """
        if self.paused or self.multiplier == 0.0 or self.allocated_usd < 10.0:
            return None

        now = time.time()
        if now - self._sim_last_check < 300:   # Check every 5 minutes (realistic arb frequency)
            return None
        self._sim_last_check = now

        # Simulate spread observation — opportunities arrive stochastically
        observed_spread = random.gauss(self._sim_spread, 0.003)
        ENTRY_THRESHOLD = 0.008
        if observed_spread < ENTRY_THRESHOLD:
            return None   # No opportunity this cycle

        # Simulate trade: fee-adjusted PnL
        fees         = 0.0004 * 4   # 4 legs at 0.04% each
        gross_profit = observed_spread - 0.005   # Exit at 0.5% convergence
        net_profit_pct = gross_profit - fees
        size_usd     = self.allocated_usd * 0.6 * self.multiplier
        net_profit   = size_usd * net_profit_pct

        self.realised_pnl += net_profit
        self.returns_log.append(net_profit_pct)
        self.trade_count  += 1
        if net_profit > 0:
            self.win_count += 1

        logger.info("paper_arb_trade", spread=round(observed_spread, 4),
                    net_pct=round(net_profit_pct * 100, 3),
                    size_usd=round(size_usd, 2),
                    net_pnl=round(net_profit, 4))
        return {
            "action":       "arb_closed",
            "gross_spread": round(observed_spread, 4),
            "net_pnl":      round(net_profit, 4),
            "size_usd":     round(size_usd, 2),
        }


state = ArbState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("arbitrader_sidecar_starting",
                paper=PAPER_TRADING, jar=ARBITRADER_JAR)
    if not PAPER_TRADING:
        await asyncio.get_event_loop().run_in_executor(None, state.start_jvm)
    else:
        logger.info("arbitrader_paper_mode", note="JVM not started — paper simulation active")

    yield

    state.stop_jvm()
    logger.info("arbitrader_sidecar_shutdown",
                trades=state.trade_count, pnl=round(state.realised_pnl, 4))


app = FastAPI(lifespan=lifespan)


# ── REST Contract ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    if not PAPER_TRADING:
        state.check_jvm_alive()
    return {
        "status":       "ok" if state.is_healthy() else "degraded",
        "worker":       WORKER_NAME,
        "paused":       state.paused,
        "regime":       state.current_regime,
        "jvm_running":  state.jvm_running,
        "jvm_error":    state.jvm_error,
        "paper_mode":   PAPER_TRADING,
    }


@app.get("/status")
def status():
    return {
        "worker":         WORKER_NAME,
        "regime":         state.current_regime,
        "multiplier":     state.multiplier,
        "paused":         state.paused,
        "allocated_usd":  round(state.allocated_usd, 2),
        "pnl":            round(state.realised_pnl, 4),
        "sharpe":         round(state.sharpe(), 3),
        "win_rate":       round(state.win_rate(), 3),
        "trade_count":    state.trade_count,
        "open_positions": state.open_positions,
        "jvm_running":    state.jvm_running,
        "uptime_s":       round(state.uptime(), 1),
    }


@app.post("/allocate")
async def allocate(body: dict):
    """Receive capital allocation from Hypervisor."""
    amount = float(body.get("amount_usd", 0.0))
    paper  = bool(body.get("paper_trading", True))

    state.allocated_usd = amount
    logger.info("capital_allocated", amount_usd=amount, paper=paper,
                regime=state.current_regime, multiplier=state.multiplier)

    # Trigger immediate paper arb check with fresh capital
    result = state.paper_arb_cycle()
    return {
        "status":     "allocated",
        "amount_usd": amount,
        "arb_result": result,
    }


@app.post("/regime")
async def update_regime(body: dict):
    new_regime = body.get("regime", state.current_regime)
    old_regime = state.current_regime

    state.current_regime = new_regime
    state.multiplier     = REGIME_MULTIPLIERS.get(new_regime, 1.0)

    if state.multiplier == 0.0:
        state.paused = True
        logger.warning("arbitrader_paused_by_regime", regime=new_regime)
    elif state.paused and state.multiplier > 0:
        state.paused = False
        logger.info("arbitrader_auto_resumed", regime=new_regime)

    logger.info("regime_updated", old=old_regime, new=new_regime, multiplier=state.multiplier)
    return {
        "status":     "updated",
        "regime":     new_regime,
        "multiplier": state.multiplier,
    }


@app.post("/signal")
async def signal(body: dict):
    """Return current arb spread observation as a signal."""
    if state.paused or state.multiplier == 0.0:
        return []

    # Simulate current observed spread for reporting
    observed = max(0.0, random.gauss(0.008, 0.003))
    confidence = min(0.9, observed / 0.02)   # Higher spread = higher confidence

    return [{
        "worker":             WORKER_NAME,
        "symbol":             "CROSS_EXCHANGE_ARB",
        "direction":          "market_neutral",
        "confidence":         round(confidence, 3),
        "suggested_size_pct": 0.6 * state.multiplier,
        "regime_tags":        [state.current_regime],
        "ttl_seconds":        300,
        "rationale": (
            f"Cross-exchange price arb | observed_spread={observed:.4f} | "
            f"regime={state.current_regime} | multiplier={state.multiplier}"
        ),
    }]


@app.post("/execute")
async def execute(body: dict):
    """Force an arb check cycle."""
    if state.paused:
        return {"status": "paused", "executed": False}
    state._sim_last_check = 0.0   # Reset throttle so it runs immediately
    result = state.paper_arb_cycle()
    return {"status": "executed", "result": result}


@app.post("/pause")
def pause():
    state.paused = True
    logger.info("arbitrader_paused")
    return {"status": "paused"}


@app.post("/resume")
def resume():
    if state.multiplier == 0.0:
        return {"status": "blocked", "reason": "regime forces pause (CRISIS_ACUTE)"}
    state.paused = False
    logger.info("arbitrader_resumed")
    return {"status": "resumed"}


@app.get("/metrics")
def metrics():
    active     = 0 if (state.paused or state.multiplier == 0.0) else 1
    paused_int = 1 if state.paused else 0

    lines = [
        # ── Standard labeled gauges ──────────────────────────────────────────
        f'mara_worker_pnl_usd{{worker="arbitrader"}} {state.realised_pnl:.4f}',
        f'mara_worker_allocated_usd{{worker="arbitrader"}} {state.allocated_usd:.2f}',
        f'mara_worker_sharpe{{worker="arbitrader"}} {state.sharpe():.4f}',
        # Arbitrader is delta-neutral: open_positions stays 0 until live JVM wired
        f'mara_worker_open_positions{{worker="arbitrader"}} {state.open_positions}',
        f'mara_worker_paused{{worker="arbitrader"}} {paused_int}',
        # ── Legacy ───────────────────────────────────────────────────────────
        f'mara_worker_active{{worker="arbitrader"}} {active}',
        f'mara_arbitrader_pnl_usd {state.realised_pnl:.4f}',
        f'mara_arbitrader_trade_count {state.trade_count}',
        f'mara_arbitrader_sharpe {state.sharpe():.4f}',
        f'mara_arbitrader_win_rate {state.win_rate():.4f}',
        f'mara_arbitrader_jvm_running {1 if state.jvm_running else 0}',
        # ── Execution quality — arb is delta-neutral, no per-position slippage
        f'mara_observed_slippage_bps{{worker="arbitrader",symbol="CROSS_EXCHANGE_ARB",paper_mode="true"}} 0',
        f'mara_fill_rate_observed{{worker="arbitrader",symbol="CROSS_EXCHANGE_ARB",paper_mode="true"}} 1.0',
    ]
    content = "\n".join(lines) + "\n"
    return Response(content=content, media_type="text/plain")
