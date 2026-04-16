"""
data/feeds/circuit_breaker.py

Circuit breaker pattern for all external API calls.

State machine:
  CLOSED   → normal operation; failures increment counter
  OPEN     → call rejected immediately; returns fallback
  HALF_OPEN → one probe allowed after cooldown; success resets to CLOSED

Usage:
    result = await BREAKERS["yfinance"].call(
        _fetch_ticker, "^VIX",
        fallback=lambda: cached_values.get("vix")
    )

Module-level BREAKERS dict is the single set of breaker instances shared
across all callers that import from this module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """
    Thread-safe (asyncio) circuit breaker for external API calls.

    Args:
        name:              Human-readable label for logs.
        failure_threshold: Consecutive failures before opening the circuit.
        cooldown_seconds:  Seconds to wait in OPEN state before probing again.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        cooldown_seconds: int = 300,
    ) -> None:
        self.name = name
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown_seconds
        self.last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    # ── State transitions ─────────────────────────────────────────────────────

    def can_execute(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time >= self.cooldown:
                self.state = CircuitState.HALF_OPEN
                logger.info("CircuitBreaker[%s]: OPEN → HALF_OPEN (probing)", self.name)
                return True
            return False
        # HALF_OPEN: allow exactly one attempt
        return True

    def record_success(self) -> None:
        if self.state != CircuitState.CLOSED:
            logger.info("CircuitBreaker[%s]: %s → CLOSED", self.name, self.state)
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            if self.state != CircuitState.OPEN:
                logger.warning(
                    "CircuitBreaker[%s]: → OPEN after %d failures",
                    self.name,
                    self.failure_count,
                )
            self.state = CircuitState.OPEN

    # ── Primary entry point ───────────────────────────────────────────────────

    async def call(
        self,
        func: Callable,
        *args: Any,
        fallback: Any = None,
        **kwargs: Any,
    ) -> Any:
        """
        Invoke func(*args, **kwargs) with circuit-breaker protection.

        If the circuit is OPEN and the cooldown has not elapsed, returns
        fallback immediately (calling it if it is callable).

        If func raises, records the failure and returns fallback.
        """
        async with self._lock:
            can_run = self.can_execute()

        if not can_run:
            logger.debug("CircuitBreaker[%s]: OPEN — returning fallback", self.name)
            return fallback() if callable(fallback) else fallback

        try:
            # Support both sync and async callables
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = await asyncio.to_thread(func, *args, **kwargs)

            async with self._lock:
                self.record_success()
            return result

        except Exception as exc:
            async with self._lock:
                self.record_failure()
            logger.warning("CircuitBreaker[%s]: failure — %s", self.name, exc)
            if fallback is not None:
                return fallback() if callable(fallback) else fallback
            raise

    # ── Introspection ─────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "cooldown_seconds": self.cooldown,
            "seconds_until_probe": max(
                0.0,
                self.cooldown - (time.time() - self.last_failure_time),
            ) if self.state == CircuitState.OPEN else 0.0,
        }


# ── Module-level breaker instances ────────────────────────────────────────────
# One breaker per external dependency.  Import and use these directly:
#   from data.feeds.circuit_breaker import BREAKERS
#   result = await BREAKERS["yfinance"].call(my_fn, arg)

BREAKERS: dict[str, CircuitBreaker] = {
    "yfinance": CircuitBreaker("yfinance", failure_threshold=3, cooldown_seconds=300),
    "fred":     CircuitBreaker("fred",     failure_threshold=3, cooldown_seconds=600),
    "gdelt":    CircuitBreaker("gdelt",    failure_threshold=5, cooldown_seconds=120),
    "okx":      CircuitBreaker("okx",      failure_threshold=2, cooldown_seconds=60),
    "edgar":    CircuitBreaker("edgar",    failure_threshold=3, cooldown_seconds=300),
    "acled":    CircuitBreaker("acled",    failure_threshold=2, cooldown_seconds=900),
    "kraken":   CircuitBreaker("kraken",   failure_threshold=3, cooldown_seconds=180),
}
