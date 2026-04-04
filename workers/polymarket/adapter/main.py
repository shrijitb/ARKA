"""
MARA Polymarket Adapter
Wraps lorine93s/polymarket-market-maker-bot for Hypervisor integration.

What this does:
- Starts MarketMakerBot with regime-adjusted Settings
- Reads its Prometheus metrics from :9305
- Exposes MARA worker REST interface (/health /signal /pause /resume /metrics)
- Accepts regime broadcasts from Hypervisor and hot-reloads exposure limits

Place this file at: workers/polymarket/adapter/main.py
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import structlog
from fastapi import FastAPI
from fastapi.responses import Response

# --- Path setup so we can import the bot directly ---
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import Settings
from src.main import MarketMakerBot

logger = structlog.get_logger(__name__)

METRICS_URL = "http://localhost:9305/metrics"
WORKER_NAME = "polymarket"

# Regime → exposure multiplier (mirrors config/regimes.yaml)
REGIME_EXPOSURE_MULTIPLIERS: dict[str, float] = {
    "BULL_CALM":      1.0,
    "BULL_FROTHY":    0.8,   # reduce size in euphoria — spreads compress
    "BEAR_RECESSION": 0.6,
    "CRISIS_ACUTE":   0.0,   # pause — liquidity dries up, spreads blow out
    "WAR_PREMIUM":    1.5,   # election/war markets = peak prediction activity
    "REGIME_CHANGE":  1.2,   # high uncertainty = good market-making conditions
    "SHADOW_DRIFT":   0.8,
}

BASE_MAX_EXPOSURE = float(os.getenv("POLY_MAX_EXPOSURE_USD", "1000.0"))
BASE_MIN_EXPOSURE = float(os.getenv("POLY_MIN_EXPOSURE_USD", "-1000.0"))


def build_settings(regime: str) -> Settings:
    """Build Settings with regime-adjusted exposure limits."""
    multiplier = REGIME_EXPOSURE_MULTIPLIERS.get(regime, 1.0)
    return Settings(
        private_key=os.environ["POLY_PRIVATE_KEY"],
        public_address=os.environ["POLY_PUBLIC_ADDRESS"],
        market_id=os.environ["POLY_MARKET_ID"],
        max_exposure_usd=BASE_MAX_EXPOSURE * multiplier,
        min_exposure_usd=BASE_MIN_EXPOSURE * multiplier,
        max_position_size_usd=BASE_MAX_EXPOSURE * multiplier * 0.5,
        environment=os.getenv("MARA_MODE", "backtest"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


class AdapterState:
    def __init__(self):
        self.bot: MarketMakerBot | None = None
        self.bot_task: asyncio.Task | None = None
        self.current_regime: str = "BULL_CALM"
        self.paused: bool = False
        self.last_metrics: dict[str, float] = {}
        self.start_time: float = time.time()

    allocated_usd: float = 0.0

    async def start_bot(self, regime: str):
        """Start or restart the bot with regime-adjusted settings."""
        await self.stop_bot()
        if self.paused:
            return
        settings = build_settings(regime)
        self.bot = MarketMakerBot(settings)
        self.bot_task = asyncio.create_task(self.bot.run())
        logger.info("polymarket_bot_started", regime=regime,
                    max_exposure=settings.max_exposure_usd)

    async def stop_bot(self):
        if self.bot and self.bot.running:
            self.bot.running = False
            await self.bot.cleanup()
        if self.bot_task and not self.bot_task.done():
            self.bot_task.cancel()
            try:
                await self.bot_task
            except asyncio.CancelledError:
                pass
        self.bot = None
        self.bot_task = None

    def get_exposure(self) -> float:
        if self.bot:
            return self.bot.inventory_manager.inventory.net_exposure_usd
        return 0.0

    def get_skew(self) -> float:
        if self.bot:
            return self.bot.inventory_manager.inventory.get_skew()
        return 0.0

    def is_healthy(self) -> bool:
        if self.paused:
            return True   # paused is intentional, not unhealthy
        if self.bot is None:
            return False
        if self.bot_task and self.bot_task.done():
            # Task ended unexpectedly — unhealthy
            return False
        return self.bot.running


state = AdapterState()


async def metrics_poll_loop():
    """Polls the bot's Prometheus endpoint and caches parsed values."""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(METRICS_URL, timeout=3.0)
                state.last_metrics = parse_prometheus(r.text)
            except Exception:
                pass
            await asyncio.sleep(10)


def parse_prometheus(text: str) -> dict[str, float]:
    """Minimal Prometheus text format parser — extracts gauge/counter values."""
    result = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        m = re.match(r'^([a-zA-Z_:][a-zA-Z0-9_:]*(?:\{[^}]*\})?)\s+([\d.eE+\-]+)', line)
        if m:
            result[m.group(1)] = float(m.group(2))
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Only start bot if not in pure backtest mode (no wallet needed for backtest)
    mode = os.getenv("MARA_MODE", "backtest")
    if mode != "backtest":
        await state.start_bot(state.current_regime)
        asyncio.create_task(metrics_poll_loop())
    else:
        logger.info("polymarket_adapter_standby", mode=mode,
                    reason="backtest mode — bot not started, wallet not required")
    yield
    await state.stop_bot()


app = FastAPI(lifespan=lifespan)


# --- MARA Worker REST Interface ---

@app.get("/health")
def health():
    healthy = state.is_healthy()
    return {
        "status": "ok" if healthy else "degraded",
        "worker": WORKER_NAME,
        "paused": state.paused,
        "regime": state.current_regime,
        "bot_running": state.bot.running if state.bot else False,
    }


@app.get("/status")
def status():
    inv = state.bot.inventory_manager.inventory if state.bot else None
    return {
        "worker": WORKER_NAME,
        "regime": state.current_regime,
        "paused": state.paused,
        "net_exposure_usd": inv.net_exposure_usd if inv else 0.0,
        "yes_position": inv.yes_position if inv else 0.0,
        "no_position": inv.no_position if inv else 0.0,
        "skew": state.get_skew(),
        "pnl": state.last_metrics.get("pm_mm_profit_usd", 0.0),
        "orders_placed": state.last_metrics.get("pm_mm_orders_placed_total", 0.0),
        "fill_rate": state.last_metrics.get("pm_mm_orders_filled_total", 0.0),
    }


@app.post("/signal")
async def signal(body: dict):
    """
    Returns a Signal for the Hypervisor.
    Polymarket market-making is regime-gated, not directional —
    so we report activity level and PnL as the signal.
    """
    regime = body.get("regime", state.current_regime)
    multiplier = REGIME_EXPOSURE_MULTIPLIERS.get(regime, 1.0)

    if multiplier == 0.0 or state.paused:
        return []

    return [{
        "worker": WORKER_NAME,
        "symbol": "POLYMARKET_MM",
        "direction": "market_make",
        "confidence": min(0.5 + multiplier * 0.2, 0.95),
        "suggested_size_pct": 0.05 * multiplier,
        "regime_tags": [regime],
        "ttl_seconds": 300,
        "rationale": (
            f"Market making on Polymarket CLOB. "
            f"Regime={regime}, exposure_multiplier={multiplier}, "
            f"net_exposure={state.get_exposure():.2f} USD, skew={state.get_skew():.3f}"
        ),
    }]


@app.post("/regime")
async def update_regime(body: dict):
    """
    Called by Hypervisor when regime changes.
    Hot-restarts the bot with new exposure limits.
    """
    new_regime = body.get("regime")
    if not new_regime or new_regime == state.current_regime:
        return {"status": "no_change"}

    old_regime = state.current_regime
    state.current_regime = new_regime
    multiplier = REGIME_EXPOSURE_MULTIPLIERS.get(new_regime, 1.0)

    logger.info("regime_change", old=old_regime, new=new_regime, multiplier=multiplier)

    mode = os.getenv("MARA_MODE", "backtest")
    if mode != "backtest":
        if multiplier == 0.0:
            # CRISIS_ACUTE — stop trading entirely
            await state.stop_bot()
            logger.info("polymarket_bot_paused_by_regime", regime=new_regime)
        else:
            # Restart with new exposure settings
            await state.start_bot(new_regime)

    return {
        "status": "updated",
        "regime": new_regime,
        "new_max_exposure_usd": BASE_MAX_EXPOSURE * multiplier,
    }


@app.post("/allocate")
async def allocate(body: dict):
    """Receive capital allocation from Hypervisor."""
    amount = float(body.get("amount_usd", 0.0))
    state.allocated_usd = amount
    logger.info("polymarket_allocated", amount_usd=amount,
                paper=body.get("paper_trading", True))
    return {"status": "ok", "worker": WORKER_NAME, "allocated_usd": amount}


@app.post("/pause")
async def pause():
    state.paused = True
    await state.stop_bot()
    return {"status": "paused"}


@app.post("/resume")
async def resume():
    state.paused = False
    mode = os.getenv("MARA_MODE", "backtest")
    if mode != "backtest":
        await state.start_bot(state.current_regime)
    return {"status": "resumed"}


@app.get("/metrics")
def metrics():
    active     = 0 if (state.paused or not state.is_healthy()) else 1
    paused_int = 1 if state.paused else 0
    exposure   = state.get_exposure()
    pnl        = state.last_metrics.get("pm_mm_profit_usd", 0.0)
    alloc      = getattr(state, "allocated_usd", 0.0)
    fill_rate  = state.last_metrics.get("pm_mm_orders_filled_total", 0.0)

    lines = [
        # ── Standard labeled gauges ──────────────────────────────────────────
        f'mara_worker_pnl_usd{{worker="polymarket"}} {pnl:.4f}',
        f'mara_worker_allocated_usd{{worker="polymarket"}} {alloc:.2f}',
        f'mara_worker_sharpe{{worker="polymarket"}} 0.0',
        # Polymarket positions = exposure / avg_price — tracked as 1 if active
        f'mara_worker_open_positions{{worker="polymarket"}} {1 if exposure != 0.0 else 0}',
        f'mara_worker_paused{{worker="polymarket"}} {paused_int}',
        # ── Legacy ───────────────────────────────────────────────────────────
        f'mara_worker_active{{worker="polymarket"}} {active}',
        f'mara_polymarket_exposure_usd {exposure:.4f}',
        f'mara_polymarket_pnl_usd {pnl:.4f}',
        f'mara_polymarket_skew {state.get_skew():.4f}',
        # ── Execution quality ────────────────────────────────────────────────
        f'mara_observed_slippage_bps{{worker="polymarket",symbol="POLYMARKET_MM",paper_mode="true"}} 0',
        f'mara_fill_rate_observed{{worker="polymarket",symbol="POLYMARKET_MM",paper_mode="true"}} {fill_rate:.4f}',
    ]
    content = "\n".join(lines) + "\n"
    return Response(content=content, media_type="text/plain")
