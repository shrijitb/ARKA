"""
hypervisor/db/models.py

ORM models that mirror data/db/schema.sql exactly.
schema.sql is the source of truth — do not add columns here that
are not in schema.sql (or vice versa).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, Integer, REAL as Real, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RegimeLog(Base):
    __tablename__ = "regime_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[float] = mapped_column(Real, nullable=False)
    regime: Mapped[str] = mapped_column(Text, nullable=False)
    bdi_value: Mapped[Optional[float]] = mapped_column(Real, nullable=True)
    vix_value: Mapped[Optional[float]] = mapped_column(Real, nullable=True)
    yield_curve: Mapped[Optional[float]] = mapped_column(Real, nullable=True)
    dxy: Mapped[Optional[float]] = mapped_column(Real, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[float] = mapped_column(Real, nullable=False)
    worker: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)   # BUY / SELL / HOLD
    confidence: Mapped[Optional[float]] = mapped_column(Real, nullable=True)
    suggested_size_pct: Mapped[Optional[float]] = mapped_column(Real, nullable=True)
    regime_tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # JSON list
    ttl_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    acted_on: Mapped[int] = mapped_column(Integer, default=0)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[float] = mapped_column(Real, nullable=False)
    signal_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("signals.id"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)         # buy / sell
    quantity: Mapped[Optional[float]] = mapped_column(Real, nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Real, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # pending/filled/...
    worker: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mode: Mapped[Optional[str]] = mapped_column(Text, nullable=True)    # paper / live
    pnl: Mapped[Optional[float]] = mapped_column(Real, nullable=True)


class PortfolioState(Base):
    __tablename__ = "portfolio_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[float] = mapped_column(Real, nullable=False)
    total_value: Mapped[Optional[float]] = mapped_column(Real, nullable=True)
    cash_pct: Mapped[Optional[float]] = mapped_column(Real, nullable=True)
    drawdown_pct: Mapped[Optional[float]] = mapped_column(Real, nullable=True)
    regime: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    allocations: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON string
