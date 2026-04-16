"""
hypervisor/db/repository.py

All database write/read operations for the hypervisor.
Every public method opens its own session so callers don't manage sessions.

Usage:
    from hypervisor.db.engine import async_session
    from hypervisor.db.repository import ArkaRepository
    repo = ArkaRepository(async_session)

    await repo.log_regime("RISK_ON", snapshot, False)
    await repo.snapshot_portfolio(state)
    await repo.log_signal("nautilus", "BTC-USDT-SWAP", "BUY", "momentum", 0.82)
    await repo.log_order(signal_id, "nautilus", "BTC-USDT-SWAP", "buy", 0.01, 65000)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hypervisor.db.models import Order, PortfolioState, RegimeLog, Signal

logger = logging.getLogger(__name__)


class ArkaRepository:
    """All database I/O. Each method is an independent async transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    # ── Regime logging ────────────────────────────────────────────────────────

    async def log_regime(
        self,
        regime: str,
        macro_snapshot: dict,
        circuit_breaker: bool,
    ) -> None:
        """
        Persist a regime classification event.
        macro_snapshot keys: vix, yield_curve, dxy, bdi_slope_12w
        (all optional — missing keys are stored as NULL).
        """
        try:
            async with self.session_factory() as session:
                session.add(
                    RegimeLog(
                        timestamp=time.time(),
                        regime=regime,
                        bdi_value=macro_snapshot.get("bdi_slope_12w"),
                        vix_value=macro_snapshot.get("vix"),
                        yield_curve=macro_snapshot.get("yield_curve"),
                        dxy=macro_snapshot.get("dxy"),
                        notes="circuit_breaker=True" if circuit_breaker else None,
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.warning("log_regime failed: %s", exc)

    # ── Signal logging ────────────────────────────────────────────────────────

    async def log_signal(
        self,
        worker: str,
        symbol: str,
        direction: str,
        rationale: str,
        confidence: Optional[float] = None,
        regime_tags: Optional[list[str]] = None,
    ) -> Optional[int]:
        """
        Persist a trading signal. Returns the new row id, or None on error.
        direction: BUY | SELL | HOLD
        """
        try:
            async with self.session_factory() as session:
                sig = Signal(
                    timestamp=time.time(),
                    worker=worker,
                    symbol=symbol,
                    direction=direction.upper(),
                    confidence=confidence,
                    rationale=rationale,
                    regime_tags=json.dumps(regime_tags) if regime_tags else None,
                )
                session.add(sig)
                await session.commit()
                await session.refresh(sig)
                return sig.id
        except Exception as exc:
            logger.warning("log_signal failed: %s", exc)
            return None

    # ── Order logging ─────────────────────────────────────────────────────────

    async def log_order(
        self,
        worker: str,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        signal_id: Optional[int] = None,
        mode: str = "paper",
    ) -> None:
        """Persist an order (paper or live)."""
        try:
            async with self.session_factory() as session:
                session.add(
                    Order(
                        timestamp=time.time(),
                        signal_id=signal_id,
                        symbol=symbol,
                        side=side.lower(),
                        quantity=quantity,
                        price=price,
                        status="pending",
                        worker=worker,
                        mode=mode,
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.warning("log_order failed: %s", exc)

    # ── Portfolio snapshots ───────────────────────────────────────────────────

    async def snapshot_portfolio(
        self,
        total_value: float,
        cash_pct: float,
        drawdown_pct: float,
        regime: str,
        allocations: dict,
    ) -> None:
        """Write a periodic portfolio state snapshot."""
        try:
            async with self.session_factory() as session:
                session.add(
                    PortfolioState(
                        timestamp=time.time(),
                        total_value=round(total_value, 4),
                        cash_pct=round(cash_pct, 4),
                        drawdown_pct=round(drawdown_pct, 4),
                        regime=regime,
                        allocations=json.dumps(allocations),
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.warning("snapshot_portfolio failed: %s", exc)

    # ── History queries ───────────────────────────────────────────────────────

    async def get_portfolio_history(self, hours: int = 24) -> list[PortfolioState]:
        """Return portfolio snapshots from the last N hours, oldest first."""
        cutoff = time.time() - hours * 3600
        try:
            async with self.session_factory() as session:
                result = await session.execute(
                    select(PortfolioState)
                    .where(PortfolioState.timestamp >= cutoff)
                    .order_by(PortfolioState.timestamp)
                )
                return list(result.scalars().all())
        except Exception as exc:
            logger.warning("get_portfolio_history failed: %s", exc)
            return []

    async def get_recent_regime_log(self, limit: int = 48) -> list[RegimeLog]:
        """Return the most recent N regime classification events."""
        try:
            async with self.session_factory() as session:
                result = await session.execute(
                    select(RegimeLog)
                    .order_by(RegimeLog.timestamp.desc())
                    .limit(limit)
                )
                return list(result.scalars().all())
        except Exception as exc:
            logger.warning("get_recent_regime_log failed: %s", exc)
            return []
