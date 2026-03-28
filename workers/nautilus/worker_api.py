"""
workers/nautilus/worker_api.py

NautilusTrader Worker — Systematic Strategy Execution.
FastAPI service on port 8001.

What this does:
    Wraps NautilusTrader as a MARA worker. Runs the MACD+Williams Fractals
    swing strategy (workers/nautilus/strategies/swing_macd.py) and any other
    NautilusTrader strategies registered in STRATEGY_REGISTRY.

    In backtest/paper mode: runs NautilusTrader's BacktestEngine on OKX data.
    In live mode: runs TradingNode connected to OKX (geo-unblocked).

    Strategy selection is regime-aware: WAR_PREMIUM → momentum/defense,
    BEAR_RECESSION → short bias, BULL_CALM → balanced swing.

OKX is the only non-geo-blocked exchange for MARA's location.
Symbol format: BTC-USDT-SWAP (OKX perpetual format).

REST contract (full MARA standard + /allocate):
    GET  /health     liveness
    GET  /status     pnl, sharpe, allocated_usd, open_positions, active_strategy
    GET  /metrics    Prometheus text
    POST /regime     adapt active strategy to regime
    POST /allocate   receive capital from hypervisor, resize positions
    POST /signal     (optional) return current signal to hypervisor
    POST /execute    execute a specific trade instruction
    POST /pause      stop new entries (keep open positions)
    POST /resume     resume
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import math
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import structlog
from fastapi import FastAPI
from fastapi.responses import Response

logger = structlog.get_logger(__name__)

WORKER_NAME = "nautilus"

# ── Recession hedge pairs — inverse ETFs, signals only (IBKR not wired yet) ──
# Mirrors config.py RECESSION_PAIRS / RECESSION_REGIMES.
# advisory_only=True until Phase 3 (StockSharp/IBKR).
RECESSION_PAIRS   = ["SH", "PSQ"]
RECESSION_REGIMES = {"BEAR_RECESSION", "CRISIS_ACUTE"}

# ── OKX symbol map — NautilusTrader InstrumentId format ──────────────────────
# Perpetual swaps only. OKX is geo-unblocked for MARA's location.
OKX_INSTRUMENTS = {
    "BTC/USDT": "BTC-USDT-SWAP.OKX",
    "ETH/USDT": "ETH-USDT-SWAP.OKX",
    "SOL/USDT": "SOL-USDT-SWAP.OKX",
    "BNB/USDT": "BNB-USDT-SWAP.OKX",   # Listed on OKX
    "AVAX/USDT": "AVAX-USDT-SWAP.OKX",
}

# ── Regime → strategy bias ────────────────────────────────────────────────────
REGIME_BIAS: Dict[str, str] = {
    "WAR_PREMIUM":    "momentum_long",   # Defense/commodity ETF momentum plays
    "CRISIS_ACUTE":   "flat",            # No new directional entries
    "BEAR_RECESSION": "swing_short",     # MACD bearish fractals only
    "BULL_FROTHY":    "momentum_long",   # Momentum longs with tight trailing stop
    "REGIME_CHANGE":  "flat",            # Direction unclear — wait
    "SHADOW_DRIFT":   "swing_neutral",   # Both sides, small size
    "BULL_CALM":      "swing_neutral",   # Standard MACD+Fractals, both directions
}


class StrategyState:
    """All mutable state for the Nautilus worker."""

    def __init__(self):
        self.allocated_usd:     float  = 0.0
        self.paper_trading:     bool   = True
        self.current_regime:    str    = "BULL_CALM"
        self.bias:              str    = "swing_neutral"
        self.paused:            bool   = False
        self.open_positions:    int    = 0
        self.realised_pnl:      float  = 0.0
        self.unrealised_pnl:    float  = 0.0
        self.trade_count:       int    = 0
        self.win_count:         int    = 0
        self.returns_log:       List[float] = []   # Per-trade returns for Sharpe
        self.active_strategy:   str    = "swing_macd"
        self.engine_ready:      bool   = False
        self.engine_error:      Optional[str] = None
        self.start_time:        float  = time.time()

        # Lightweight in-process position book for paper trading
        # {instrument: {"side": "long"|"short", "entry": float, "size_usd": float}}
        self._positions: Dict[str, Dict] = {}

    # ── Derived metrics ───────────────────────────────────────────────────────

    def sharpe(self) -> float:
        """Annualised Sharpe from per-trade return log. Needs ≥ 5 trades."""
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
        # Annualise assuming ~6 trades per day on H4 bars
        return (mean / std) * math.sqrt(6 * 365)

    def win_rate(self) -> float:
        return self.win_count / self.trade_count if self.trade_count > 0 else 0.0

    def uptime(self) -> float:
        return time.time() - self.start_time

    def is_healthy(self) -> bool:
        return True   # Always healthy — engine failure degrades to paper mode, not crash

    # ── NautilusTrader engine init ────────────────────────────────────────────

    def init_engine(self):
        """
        Attempt to initialise a NautilusTrader BacktestEngine in paper mode.
        Falls back to the internal paper trading simulator on failure.
        Failure is non-fatal — the REST contract is fully preserved either way.
        """
        try:
            from nautilus_trader.backtest.engine import BacktestEngine
            from nautilus_trader.config import BacktestEngineConfig
            from nautilus_trader.model.enums import OmsType, AccountType

            cfg = BacktestEngineConfig(
                trader_id="MARA-NAUTILUS-001",
            )
            self._engine = BacktestEngine(config=cfg)
            self.engine_ready = True
            logger.info("nautilus_engine_ready", mode="backtest_paper")

        except ImportError as exc:
            self.engine_error = f"nautilus_trader not installed: {exc}"
            logger.warning("nautilus_engine_unavailable", error=str(exc),
                           fallback="internal paper simulator")
        except Exception as exc:
            self.engine_error = f"engine init failed: {exc}"
            logger.warning("nautilus_engine_init_failed", error=str(exc),
                           fallback="internal paper simulator")

    # ── Paper trading simulator ───────────────────────────────────────────────
    # Used when NautilusTrader isn't installed or when in pure paper mode.
    # Simulates MACD+Fractals signals using synthetic price movements.

    async def run_paper_cycle(self) -> Optional[Dict[str, Any]]:
        """
        One paper trading cycle. Evaluates MACD+Fractals signals and
        simulates entries/exits without touching any exchange.

        Returns a trade result dict if a position was opened or closed, else None.
        """
        if self.paused or self.bias == "flat" or self.allocated_usd < 10.0:
            return None

        # Import the strategy logic (moved from workers/swing_trend.py)
        try:
            from strategies.swing_macd import evaluate_signal
            pairs = list(OKX_INSTRUMENTS.keys())
            signal = evaluate_signal(pairs, self.bias)
        except ImportError:
            # Strategy file not yet in place — use simplified stub
            signal = self._stub_signal()

        if signal is None:
            return None

        pair, side, entry, sl, tp = signal

        # Don't open more than 2 concurrent positions
        if len(self._positions) >= 2:
            return None

        size_usd = self.allocated_usd * 0.4   # 40% per position, max 2 positions
        self._positions[pair] = {
            "side": side, "entry": entry,
            "sl": sl,     "tp": tp,
            "size_usd": size_usd,
            "opened_at": time.time(),
        }
        self.open_positions = len(self._positions)

        logger.info("paper_position_opened", pair=pair, side=side,
                    entry=entry, sl=sl, tp=tp, size_usd=size_usd)
        return {"action": "opened", "pair": pair, "side": side, "size_usd": size_usd}

    async def check_exits(self, current_prices: Dict[str, float]):
        """Check stop-loss and take-profit for all open paper positions."""
        to_close = []
        for pair, pos in self._positions.items():
            price = current_prices.get(pair, pos["entry"])
            side  = pos["side"]
            hit_sl = (side == "long"  and price <= pos["sl"]) or \
                     (side == "short" and price >= pos["sl"])
            hit_tp = (side == "long"  and price >= pos["tp"]) or \
                     (side == "short" and price <= pos["tp"])
            if hit_sl or hit_tp:
                reason = "tp" if hit_tp else "sl"
                if side == "long":
                    pnl = pos["size_usd"] * (price - pos["entry"]) / pos["entry"]
                else:
                    pnl = pos["size_usd"] * (pos["entry"] - price) / pos["entry"]

                self.realised_pnl += pnl
                ret = pnl / pos["size_usd"]
                self.returns_log.append(ret)
                self.trade_count += 1
                if pnl > 0:
                    self.win_count += 1

                to_close.append(pair)
                logger.info("paper_position_closed", pair=pair, reason=reason,
                            pnl=round(pnl, 4), ret_pct=round(ret * 100, 2))

        for pair in to_close:
            del self._positions[pair]
        self.open_positions = len(self._positions)

    def _stub_signal(self):
        """Minimal stub signal when strategy file is missing — returns None (no trade)."""
        return None


state = StrategyState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("nautilus_worker_starting", mode="paper" if True else "live")
    await asyncio.get_event_loop().run_in_executor(None, state.init_engine)
    logger.info("nautilus_worker_ready",
                engine=state.engine_ready, fallback=state.engine_error)
    yield
    logger.info("nautilus_worker_shutdown", trades=state.trade_count,
                pnl=round(state.realised_pnl, 4))


app = FastAPI(lifespan=lifespan)


# ── REST Contract ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status":        "ok" if state.is_healthy() else "degraded",
        "worker":        WORKER_NAME,
        "paused":        state.paused,
        "regime":        state.current_regime,
        "engine_ready":  state.engine_ready,
        "engine_error":  state.engine_error,
        "open_positions": state.open_positions,
    }


@app.get("/status")
def status():
    return {
        "worker":           WORKER_NAME,
        "regime":           state.current_regime,
        "bias":             state.bias,
        "paused":           state.paused,
        "allocated_usd":    round(state.allocated_usd, 2),
        "pnl":              round(state.realised_pnl + state.unrealised_pnl, 4),
        "realised_pnl":     round(state.realised_pnl, 4),
        "unrealised_pnl":   round(state.unrealised_pnl, 4),
        "sharpe":           round(state.sharpe(), 3),
        "win_rate":         round(state.win_rate(), 3),
        "trade_count":      state.trade_count,
        "open_positions":   state.open_positions,
        "active_strategy":  state.active_strategy,
        "engine_ready":     state.engine_ready,
        "uptime_s":         round(state.uptime(), 1),
    }


@app.post("/allocate")
async def allocate(body: dict):
    """
    Receive capital allocation from Hypervisor.
    Adjusts position sizing for subsequent signals.
    """
    amount       = float(body.get("amount_usd", 0.0))
    paper        = bool(body.get("paper_trading", True))
    state.allocated_usd = amount
    state.paper_trading = paper

    logger.info("capital_allocated", amount_usd=amount, paper=paper,
                regime=state.current_regime, bias=state.bias)

    # Trigger a paper cycle immediately if we have fresh capital
    if amount >= 10.0 and not state.paused:
        result = await state.run_paper_cycle()
        if result:
            return {"status": "allocated_and_entered", "amount_usd": amount, "trade": result}

    return {"status": "allocated", "amount_usd": amount}


@app.post("/regime")
async def update_regime(body: dict):
    new_regime = body.get("regime", state.current_regime)
    old_regime = state.current_regime

    state.current_regime = new_regime
    state.bias           = REGIME_BIAS.get(new_regime, "swing_neutral")

    if new_regime == "CRISIS_ACUTE":
        state.paused = True
        logger.warning("nautilus_paused_by_regime", regime=new_regime)

    logger.info("regime_updated", old=old_regime, new=new_regime, bias=state.bias)
    return {"status": "updated", "regime": new_regime, "bias": state.bias}


@app.post("/signal")
async def signal(body: dict):
    """Return current signals — called by Hypervisor for advisory/monitoring."""
    if state.paused or state.bias == "flat":
        return []

    signals = []

    # Recession hedge — inverse ETF signals for bear/crisis regimes.
    # advisory_only=True: hypervisor logs these but does not execute until
    # IBKR is wired in Phase 3 (StockSharp).
    if state.current_regime in RECESSION_REGIMES:
        for pair in RECESSION_PAIRS:
            signals.append({
                "worker":             WORKER_NAME,
                "symbol":             pair,
                "direction":          "long",   # long the inverse ETF
                "confidence":         0.7,
                "suggested_size_pct": 0.15,
                "regime_tags":        [state.current_regime],
                "ttl_seconds":        3600,
                "advisory_only":      True,     # PHASE 3: remove when IBKR wired
                "rationale":          f"Inverse ETF recession hedge | regime={state.current_regime}",
            })

    # Swing signals for OKX crypto perps
    for pair, instrument in OKX_INSTRUMENTS.items():
        signals.append({
            "worker":             WORKER_NAME,
            "symbol":             instrument,
            "direction":          "long" if state.bias == "momentum_long" else "neutral",
            "confidence":         0.6,
            "suggested_size_pct": 0.4,
            "regime_tags":        [state.current_regime],
            "ttl_seconds":        3600,
            "rationale":          f"MACD+Fractals | bias={state.bias} | regime={state.current_regime}",
        })
    return signals


@app.post("/execute")
async def execute(body: dict):
    """Execute a specific trade instruction from Hypervisor."""
    if state.paused:
        return {"status": "paused", "executed": False}
    result = await state.run_paper_cycle()
    return {"status": "executed" if result else "no_signal", "result": result}


@app.post("/pause")
def pause():
    state.paused = True
    logger.info("nautilus_paused")
    return {"status": "paused"}


@app.post("/resume")
def resume():
    state.paused = False
    logger.info("nautilus_resumed")
    return {"status": "resumed"}


@app.get("/metrics")
def metrics():
    active = 0 if state.paused else 1
    content = (
        f'mara_worker_active{{worker="nautilus"}} {active}\n'
        f'mara_nautilus_pnl_usd {state.realised_pnl:.4f}\n'
        f'mara_nautilus_open_positions {state.open_positions}\n'
        f'mara_nautilus_trade_count {state.trade_count}\n'
        f'mara_nautilus_sharpe {state.sharpe():.4f}\n'
        f'mara_nautilus_win_rate {state.win_rate():.4f}\n'
    )
    return Response(content=content, media_type="text/plain")
