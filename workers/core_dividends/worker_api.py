"""
workers/core_dividends/worker_api.py

Passive Dividend Sleeve Worker — buy-and-hold SCHD + VYM.

Strategy:
  Receives capital allocation from the hypervisor and holds it passively in
  SCHD (Schwab US Dividend Equity ETF) and VYM (Vanguard High Dividend Yield ETF).
  50/50 split between the two ETFs. No timing, no stop-losses.

  In paper mode: tracks virtual positions at last known price, reports
  mark-to-market PnL via yfinance prices.

  In live mode (Phase 3): requires IBKR/StockSharp to place orders.
  Until then, advisory_only=True on all signals.

REST contract (standard MARA worker):
  GET  /health    liveness
  GET  /status    pnl, sharpe, allocated_usd, open_positions
  GET  /metrics   Prometheus text
  POST /regime    (passive — no regime-specific behaviour)
  POST /allocate  receive capital, resize paper positions
  POST /signal    return current hold signals
  POST /execute   advisory only until IBKR wired
  POST /pause
  POST /resume
"""

from __future__ import annotations

import logging
import math
import os
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import structlog
from fastapi import FastAPI
from fastapi.responses import Response

logger = structlog.get_logger(__name__)

WORKER_NAME    = "core_dividends"
DIVIDEND_PAIRS = ["SCHD", "VYM"]
WEIGHT_EACH    = 0.5            # 50/50 split

PORT = int(os.environ.get("PORT", 8006))


# ── Price fetcher ──────────────────────────────────────────────────────────────

def _fetch_price(ticker: str) -> float:
    """Get last close price via yfinance. Returns 0.0 on failure."""
    try:
        import yfinance as yf
        df = yf.download(ticker, period="5d", progress=False, auto_adjust=True)
        if df.empty:
            return 0.0
        col = df["Close"]
        if hasattr(col, "iloc"):
            if col.ndim > 1:
                col = col.iloc[:, 0]
            return float(col.dropna().iloc[-1])
        return float(col)
    except Exception:
        return 0.0


# ── State ──────────────────────────────────────────────────────────────────────

class DividendState:
    def __init__(self):
        self.allocated_usd:   float = 0.0
        self.paper_trading:   bool  = True
        self.current_regime:  str   = "BULL_CALM"
        self.paused:          bool  = False
        self.start_time:      float = time.time()

        # Paper position book: {ticker: {"shares": float, "entry_price": float}}
        self._positions: Dict[str, Dict] = {}

    def uptime(self) -> float:
        return time.time() - self.start_time

    def open_positions(self) -> int:
        return len(self._positions)

    def mark_to_market_pnl(self) -> float:
        """Fetch current prices and compute unrealised PnL on paper positions."""
        total = 0.0
        for ticker, pos in self._positions.items():
            current = _fetch_price(ticker)
            if current > 0 and pos["entry_price"] > 0:
                total += pos["shares"] * (current - pos["entry_price"])
        return round(total, 4)

    def sharpe(self) -> float:
        """Dividend sleeves have near-zero return volatility — not enough trades."""
        return 0.0

    def enter_positions(self, amount_usd: float):
        """
        (Re)size paper positions to match the new allocation.
        Splits amount 50/50 across SCHD and VYM.
        """
        self._positions = {}
        for ticker in DIVIDEND_PAIRS:
            price = _fetch_price(ticker)
            if price <= 0:
                logger.warning(f"core_dividends: could not fetch price for {ticker}, skipping")
                continue
            alloc_usd = amount_usd * WEIGHT_EACH
            shares    = alloc_usd / price
            self._positions[ticker] = {
                "shares":      round(shares, 6),
                "entry_price": round(price, 4),
                "alloc_usd":   round(alloc_usd, 2),
            }
            logger.info(f"core_dividends: paper position {ticker} "
                        f"{shares:.4f} shares @ ${price:.2f}")


state = DividendState()


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("core_dividends_worker_starting", pairs=DIVIDEND_PAIRS, port=PORT)
    yield
    logger.info("core_dividends_worker_shutdown",
                positions=len(state._positions), pnl=state.mark_to_market_pnl())


app = FastAPI(title="MARA Core Dividends Worker", lifespan=lifespan)


# ── REST Contract ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status":         "ok",
        "worker":         WORKER_NAME,
        "paused":         state.paused,
        "regime":         state.current_regime,
        "open_positions": state.open_positions(),
    }


@app.get("/status")
def status():
    return {
        "worker":         WORKER_NAME,
        "regime":         state.current_regime,
        "paused":         state.paused,
        "allocated_usd":  round(state.allocated_usd, 2),
        "pnl":            state.mark_to_market_pnl(),
        "sharpe":         state.sharpe(),
        "open_positions": state.open_positions(),
        "positions":      state._positions,
        "uptime_s":       round(state.uptime(), 1),
    }


@app.post("/allocate")
async def allocate(body: dict):
    amount = float(body.get("amount_usd", 0.0))
    paper  = bool(body.get("paper_trading", True))
    state.allocated_usd = amount
    state.paper_trading = paper

    if amount >= 10.0 and not state.paused:
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, state.enter_positions, amount)
        return {"status": "allocated_and_positioned", "amount_usd": amount,
                "positions": list(state._positions.keys())}

    return {"status": "allocated", "amount_usd": amount}


@app.post("/regime")
async def update_regime(body: dict):
    state.current_regime = body.get("regime", state.current_regime)
    # Passive strategy — no regime-specific adjustments
    return {"status": "updated", "regime": state.current_regime}


@app.post("/signal")
async def signal(body: dict):
    """Return hold signals for SCHD and VYM. advisory_only until IBKR wired."""
    if state.paused:
        return []
    return [
        {
            "worker":             WORKER_NAME,
            "symbol":             ticker,
            "direction":          "long",
            "confidence":         0.9,
            "suggested_size_pct": WEIGHT_EACH,
            "regime_tags":        [state.current_regime],
            "ttl_seconds":        86400,
            "advisory_only":      True,   # PHASE 3: remove when IBKR wired
            "rationale":          "Passive dividend buy-and-hold | SCHD+VYM 50/50",
        }
        for ticker in DIVIDEND_PAIRS
    ]


@app.post("/execute")
async def execute(body: dict):
    """Advisory only — no live execution until IBKR/StockSharp wired (Phase 3)."""
    return {"status": "advisory_only", "executed": False,
            "note": "PHASE 3: live execution via IBKR not yet wired"}


@app.post("/pause")
def pause():
    state.paused = True
    logger.info("core_dividends_paused")
    return {"status": "paused"}


@app.post("/resume")
def resume():
    state.paused = False
    logger.info("core_dividends_resumed")
    return {"status": "resumed"}


@app.get("/metrics")
def metrics():
    active  = 0 if state.paused else 1
    pnl     = state.mark_to_market_pnl()
    content = (
        f'mara_worker_active{{worker="core_dividends"}} {active}\n'
        f'mara_core_dividends_pnl_usd {pnl:.4f}\n'
        f'mara_core_dividends_open_positions {state.open_positions()}\n'
        f'mara_core_dividends_allocated_usd {state.allocated_usd:.2f}\n'
    )
    return Response(content=content, media_type="text/plain")
