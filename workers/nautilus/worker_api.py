"""
workers/nautilus/worker_api.py

NautilusTrader Worker — Systematic Strategy Execution.
FastAPI service on port 8001.

What this does:
    Wraps NautilusTrader as an Arka worker.  On startup, ArkaEngine (engine.py)
    attempts to start a TradingNode connected to OKX.  If credentials are absent
    or nautilus_trader is not installed, it silently falls back to the internal
    pure-Python paper simulator.

    Strategy selection is ADX-routed and regime-aware:
      ADX > 25  (trending)  → SwingMACDStrategy
      ADX < 20  (ranging)   → RangeMeanRevertStrategy
      20–25     (ambiguous) → no signal (CLAUDE.md invariant)
      ACTIVE_STRATEGY env var overrides the ADX gate at runtime.

    TRADING_MODE (env):
      swing  (default) — 4H bars, swing + range strategies
      day              — 1m bars, day scalp strategy
      both             — all three strategies registered

Mode selection:
    TRADING_MODE=swing|day|both   in .env
    POST /strategy {"mode": "auto"|"swing"|"range"|"day"|"funding"|"order_flow"|"factor"} at runtime

    New quant modes (force via POST /strategy):
      funding    — funding rate carry (long/short perp based on OKX funding sign)
      order_flow — order flow imbalance (bid-ask volume pressure)
      factor     — cross-sectional 3-factor model (momentum + carry + size)

OKX perpetual format: BTC-USDT-SWAP (not BTC/USDT).

REST contract (full Arka standard + /strategy):
    GET  /health     liveness
    GET  /status     pnl, sharpe, allocated_usd, open_positions, active_strategy
    GET  /metrics    Prometheus text
    POST /regime     adapt active strategy to regime
    POST /allocate   receive capital from hypervisor, resize positions
    POST /signal     return current signal(s) to hypervisor (advisory)
    POST /execute    execute a specific trade instruction
    POST /pause      stop new entries (keep open positions)
    POST /resume     resume
    POST /strategy   runtime mode override {"mode": "auto"|"swing"|"range"|"day"}
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import structlog
from fastapi import FastAPI
from fastapi.responses import Response

logger = structlog.get_logger(__name__)

WORKER_NAME = "nautilus"

# ── OKX symbol map — NautilusTrader InstrumentId format ──────────────────────
OKX_INSTRUMENTS = {
    "BTC/USDT":  "BTC-USDT-SWAP.OKX",
    "ETH/USDT":  "ETH-USDT-SWAP.OKX",
    "SOL/USDT":  "SOL-USDT-SWAP.OKX",
    "BNB/USDT":  "BNB-USDT-SWAP.OKX",
    "AVAX/USDT": "AVAX-USDT-SWAP.OKX",
}

# ── Regime → strategy bias ────────────────────────────────────────────────────
REGIME_BIAS: Dict[str, str] = {
    "RISK_ON":    "momentum_long",   # Full swing
    "RISK_OFF":   "swing_neutral",   # Both sides, smaller size
    "CRISIS":     "flat",            # No new directional entries
    "TRANSITION": "swing_neutral",   # Cautious both sides
}

# ── Strategy mode: runtime var seeded from ACTIVE_STRATEGY env var ───────────
# POST /strategy mutates this; ACTIVE_STRATEGY seeds it on container startup.
# "auto" means ADX-gated routing; "swing", "range", "day" force a strategy.
ACTIVE_STRATEGY_MODE: str = os.environ.get("ACTIVE_STRATEGY", "auto")

# Trading mode: which strategy classes to register with the TradingNode
TRADING_MODE: str = os.environ.get("TRADING_MODE", "swing")


# ─────────────────────────────────────────────────────────────────────────────
# Worker state
# ─────────────────────────────────────────────────────────────────────────────

class StrategyState:
    """All mutable state for the Nautilus worker."""

    def __init__(self):
        self.allocated_usd:    float  = 0.0
        self.paper_trading:    bool   = True
        self.current_regime:   str    = "TRANSITION"
        self.bias:             str    = "swing_neutral"
        self.paused:           bool   = False
        self.open_positions:   int    = 0
        self.realised_pnl:     float  = 0.0
        self.unrealised_pnl:   float  = 0.0
        self.trade_count:      int    = 0
        self.win_count:        int    = 0
        self.returns_log:      List[float] = []
        self.engine_ready:     bool   = False
        self.engine_error:     Optional[str] = None
        self.start_time:       float  = time.time()
        # Lightweight position book for paper simulator
        self._positions: Dict[str, Dict] = {}

    def active_strategy(self) -> str:
        """Returns current routing mode label for /status."""
        global ACTIVE_STRATEGY_MODE
        return ACTIVE_STRATEGY_MODE

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
        return (mean / std) * math.sqrt(6 * 252)  # 6 bars/day × 252 trading days

    def win_rate(self) -> float:
        return self.win_count / self.trade_count if self.trade_count > 0 else 0.0

    def uptime(self) -> float:
        return time.time() - self.start_time

    def is_healthy(self) -> bool:
        return True  # engine failure degrades to paper mode, not crash

    # ── ADX-gated strategy routing ────────────────────────────────────────────

    def _adx_routed_strategy(self, pairs: List[str]) -> str:
        """
        Compute ADX on synthetic OHLCV and classify the market regime.
        Returns "swing", "range", or "ambiguous".
        """
        global ACTIVE_STRATEGY_MODE
        if ACTIVE_STRATEGY_MODE != "auto":
            return ACTIVE_STRATEGY_MODE  # forced override

        try:
            from indicators.adx import calculate_adx, classify_trend
            from strategies.swing_macd import _synthetic_ohlcv
            ohlcv  = _synthetic_ohlcv(pairs[0] if pairs else "BTC/USDT")
            closes = [b[4] for b in ohlcv]
            highs  = [b[2] for b in ohlcv]
            lows   = [b[3] for b in ohlcv]
            adx, _, _ = calculate_adx(highs, lows, closes)
            last_adx  = next((v for v in reversed(adx) if not math.isnan(v)), float("nan"))
            return classify_trend(last_adx)
        except Exception as exc:
            logger.debug("adx_routing_failed", error=str(exc))
            return "swing"   # safe default

    # ── Paper simulator ───────────────────────────────────────────────────────

    async def run_paper_cycle(self) -> Optional[Dict[str, Any]]:
        """One paper cycle.  ADX-gates the strategy, then scans for a setup."""
        if self.paused or self.bias == "flat" or self.allocated_usd < 10.0:
            return None

        # ── Check exits for open positions before looking for new entries ────
        if self._positions:
            try:
                from strategies.swing_macd import _synthetic_ohlcv
                current_prices: Dict[str, float] = {}
                for pair in list(self._positions.keys()):
                    bars = _synthetic_ohlcv(pair)
                    if bars:
                        current_prices[pair] = float(bars[-1][4])  # last close
                if current_prices:
                    await self.check_exits(current_prices)
            except Exception as exc:
                logger.debug("exit_check_failed", error=str(exc))

        pairs   = list(OKX_INSTRUMENTS.keys())
        routed  = self._adx_routed_strategy(pairs)
        signal  = None

        if routed == "ambiguous":
            return None   # CLAUDE.md invariant: no signal in ambiguous zone

        try:
            if routed == "range":
                from strategies.range_mean_revert import evaluate_signal as rmr_eval
                signal = rmr_eval(pairs, self.bias)
            elif routed == "day" or TRADING_MODE in ("day", "both"):
                from strategies.day_scalp import evaluate_signal as day_eval
                signal = day_eval(pairs, self.bias)
            elif routed == "funding":
                from strategies.funding_arb import evaluate_signal as fa_eval
                signal = fa_eval(pairs, self.bias)
            elif routed == "order_flow":
                from strategies.order_flow import evaluate_signal as ofi_eval
                signal = ofi_eval(pairs, self.bias)
            elif routed == "factor":
                from strategies.factor_model import evaluate_signal as fm_eval
                signal = fm_eval(pairs, self.bias)
            else:
                from strategies.swing_macd import evaluate_signal
                signal = evaluate_signal(pairs, self.bias)
        except ImportError:
            signal = None

        if signal is None:
            return None

        pair, side, entry, sl, tp = signal

        if len(self._positions) >= 2:
            return None

        size_usd = self.allocated_usd * 0.4
        self._positions[pair] = {
            "side": side, "entry": entry,
            "sl": sl,     "tp": tp,
            "size_usd": size_usd,
            "opened_at": time.time(),
            "strategy": routed,
        }
        self.open_positions = len(self._positions)

        logger.info("paper_position_opened", pair=pair, side=side,
                    strategy=routed, entry=entry, size_usd=size_usd)
        return {
            "action": "opened", "pair": pair, "side": side,
            "size_usd": size_usd, "strategy": routed,
        }

    async def check_exits(self, current_prices: Dict[str, float]) -> None:
        to_close = []
        for pair, pos in self._positions.items():
            price  = current_prices.get(pair, pos["entry"])
            side   = pos["side"]
            hit_sl = (side == "long"  and price <= pos["sl"]) or \
                     (side == "short" and price >= pos["sl"])
            hit_tp = (side == "long"  and price >= pos["tp"]) or \
                     (side == "short" and price <= pos["tp"])
            if hit_sl or hit_tp:
                reason = "tp" if hit_tp else "sl"
                pnl    = pos["size_usd"] * (
                    (price - pos["entry"]) / pos["entry"] if side == "long"
                    else (pos["entry"] - price) / pos["entry"]
                )
                self.realised_pnl += pnl
                ret = pnl / pos["size_usd"]
                self.returns_log.append(ret)
                self.trade_count += 1
                if pnl > 0:
                    self.win_count += 1
                to_close.append(pair)
                logger.info("paper_position_closed", pair=pair, reason=reason,
                            pnl=round(pnl, 4))

        for pair in to_close:
            del self._positions[pair]
        self.open_positions = len(self._positions)


state  = StrategyState()
engine = None   # ArkaEngine instance, set in lifespan


# ─────────────────────────────────────────────────────────────────────────────
# App lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine

    logger.info("nautilus_worker_starting",
                trading_mode=TRADING_MODE, strategy_mode=ACTIVE_STRATEGY_MODE)

    _engine_task: Optional[asyncio.Task] = None

    try:
        from engine import ArkaEngine
        engine = ArkaEngine(mode=TRADING_MODE)
        _engine_task = asyncio.create_task(
            engine.run(allocated_usd=state.allocated_usd),
            name="arka-engine",
        )
        # Give the engine a moment to detect missing credentials / import errors
        await asyncio.sleep(0.1)
        state.engine_ready = engine.is_ready()
        state.engine_error = engine.state.error
    except ImportError as exc:
        state.engine_error = f"engine.py import failed: {exc}"
        logger.warning("engine_import_failed", error=str(exc))
    except Exception as exc:
        state.engine_error = f"engine startup error: {exc}"
        logger.warning("engine_startup_error", error=str(exc))

    logger.info("nautilus_worker_ready",
                engine_ready=state.engine_ready,
                engine_error=state.engine_error)
    yield

    # Shutdown
    if engine is not None:
        engine.stop()
    if _engine_task is not None and not _engine_task.done():
        try:
            await asyncio.wait_for(_engine_task, timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            _engine_task.cancel()

    logger.info("nautilus_worker_shutdown",
                trades=state.trade_count, pnl=round(state.realised_pnl, 4))


app = FastAPI(lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────────────
# REST Contract
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    pnl = engine.get_pnl() if (engine and engine.is_ready()) else state.realised_pnl
    return {
        "status":          "ok" if state.is_healthy() else "degraded",
        "worker":          WORKER_NAME,
        "paused":          state.paused,
        "regime":          state.current_regime,
        "engine_ready":    state.engine_ready,
        "engine_error":    state.engine_error,
        "open_positions":  state.open_positions,
        "trading_mode":    TRADING_MODE,
        "strategy_mode":   ACTIVE_STRATEGY_MODE,
    }


@app.get("/status")
def status():
    pnl           = engine.get_pnl() if (engine and engine.is_ready()) else state.realised_pnl
    open_pos      = engine.get_open_positions() if (engine and engine.is_ready()) else state.open_positions
    return {
        "worker":           WORKER_NAME,
        "regime":           state.current_regime,
        "bias":             state.bias,
        "paused":           state.paused,
        "allocated_usd":    round(state.allocated_usd, 2),
        "pnl":              round(pnl + state.unrealised_pnl, 4),
        "realised_pnl":     round(state.realised_pnl, 4),
        "unrealised_pnl":   round(state.unrealised_pnl, 4),
        "sharpe":           round(state.sharpe(), 3),
        "win_rate":         round(state.win_rate(), 3),
        "trade_count":      state.trade_count,
        "open_positions":   open_pos,
        "active_strategy":  state.active_strategy(),
        "trading_mode":     TRADING_MODE,
        "engine_ready":     state.engine_ready,
        "uptime_s":         round(state.uptime(), 1),
    }


@app.post("/allocate")
async def allocate(body: dict):
    amount = float(body.get("amount_usd", 0.0))
    paper  = bool(body.get("paper_trading", True))
    state.allocated_usd = amount
    state.paper_trading = paper

    logger.info("capital_allocated", amount_usd=amount, paper=paper,
                regime=state.current_regime)

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

    if new_regime == "CRISIS":
        state.paused = True
        logger.warning("nautilus_paused_by_regime", regime=new_regime)

    logger.info("regime_updated", old=old_regime, new=new_regime, bias=state.bias)
    return {"status": "updated", "regime": new_regime, "bias": state.bias}


@app.post("/signal")
async def signal(body: dict):
    """Return current advisory signals — called by Hypervisor for monitoring."""
    if state.paused or state.bias == "flat":
        return []

    pairs   = list(OKX_INSTRUMENTS.keys())
    routed  = state._adx_routed_strategy(pairs)

    if routed == "ambiguous":
        return []

    signals = []
    for pair, instrument in OKX_INSTRUMENTS.items():
        signals.append({
            "worker":             WORKER_NAME,
            "symbol":             instrument,
            "direction":          "long" if state.bias == "momentum_long" else "neutral",
            "confidence":         0.6,
            "suggested_size_pct": 0.4,
            "regime_tags":        [state.current_regime],
            "ttl_seconds":        3600,
            "rationale": (
                f"ADX-routed:{routed} | bias={state.bias} | "
                f"mode={TRADING_MODE} | regime={state.current_regime}"
            ),
        })
    return signals


@app.post("/execute")
async def execute(body: dict):
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


@app.post("/strategy")
def set_strategy(body: dict):
    """
    Runtime strategy mode override.
    body: {"mode": "auto" | "swing" | "range" | "day"}

    "auto" re-enables ADX-gated routing.
    Other values force the named strategy regardless of ADX.
    Source of truth is the module-level ACTIVE_STRATEGY_MODE var.
    """
    global ACTIVE_STRATEGY_MODE
    mode = body.get("mode", "auto")
    valid = {"auto", "swing", "range", "day", "funding", "order_flow", "factor"}
    if mode not in valid:
        return {"status": "error", "detail": f"mode must be one of {sorted(valid)}"}
    ACTIVE_STRATEGY_MODE = mode
    logger.info("strategy_mode_changed", mode=mode)
    return {"status": "updated", "active_strategy": mode}


@app.get("/metrics")
def metrics():
    active  = 0 if state.paused else 1
    pnl     = engine.get_pnl() if (engine and engine.is_ready()) else state.realised_pnl
    open_p  = engine.get_open_positions() if (engine and engine.is_ready()) else state.open_positions
    content = (
        f'arka_worker_active{{worker="nautilus"}} {active}\n'
        f'mara_nautilus_pnl_usd {pnl:.4f}\n'
        f'mara_nautilus_open_positions {open_p}\n'
        f'mara_nautilus_trade_count {state.trade_count}\n'
        f'mara_nautilus_sharpe {state.sharpe():.4f}\n'
        f'mara_nautilus_win_rate {state.win_rate():.4f}\n'
        f'mara_nautilus_engine_ready {1 if state.engine_ready else 0}\n'
    )
    return Response(content=content, media_type="text/plain")
