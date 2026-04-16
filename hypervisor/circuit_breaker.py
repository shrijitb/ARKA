"""
hypervisor/circuit_breaker.py

Circuit Breaker pattern implementation for external API calls.

States:
  CLOSED   — Normal operation, requests pass through
  OPEN     — Failure threshold reached, requests fail fast
  HALF_OPEN — Cooldown expired, testing if service recovered

Usage:
    breaker = CircuitBreaker("yfinance", failure_threshold=3, cooldown_seconds=30)
    
    if breaker.can_execute():
        try:
            result = call_external_api()
            breaker.record_success()
        except Exception:
            breaker.record_failure()
            raise
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

import structlog

logger = logging.getLogger(__name__)
audit_log = structlog.get_logger("arka.audit")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


T = TypeVar('T')


class CircuitBreaker:
    """
    Circuit breaker for external API calls.
    
    Tracks consecutive failures and opens the circuit when threshold is reached.
    After cooldown, transitions to half-open and allows one test request.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        cooldown_seconds: int = 30,
        expected_exceptions: tuple = (Exception,),
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.expected_exceptions = expected_exceptions

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = asyncio.Lock()
        self._last_cached_value: Optional[Any] = None

    @property
    def state(self) -> CircuitState:
        return self._state

    def _transition(self, new_state: CircuitState) -> None:
        old_state = self._state
        self._state = new_state
        if old_state != new_state:
            logger.info(
                f"Circuit breaker '{self.name}' state change: {old_state.value} -> {new_state.value}"
            )

    def can_execute(self) -> bool:
        """Check if a request can proceed."""
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            # Check if cooldown has expired
            if (self._last_failure_time is not None and
                time.time() - self._last_failure_time >= self.cooldown_seconds):
                self._transition(CircuitState.HALF_OPEN)
                return True
            return False
        # HALF_OPEN — allow one test request
        return True

    def record_success(self) -> None:
        """Record a successful call."""
        self._failure_count = 0
        if self._state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.CLOSED)

    def record_failure(self) -> None:
        """Record a failed call."""
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN)
        elif self._failure_count >= self.failure_threshold:
            self._transition(CircuitState.OPEN)

    def set_cached_value(self, value: Any) -> None:
        """Store a fallback value to return when circuit is open."""
        self._last_cached_value = value

    def get_cached_value(self) -> Optional[Any]:
        """Retrieve the last cached fallback value."""
        return self._last_cached_value

    async def execute(
        self,
        func: Callable[..., T],
        *args,
        fallback: Optional[Any] = None,
        **kwargs,
    ) -> Optional[T]:
        """
        Execute a function with circuit breaker protection.
        
        Args:
            func: The function to execute (can be sync or async)
            *args: Positional arguments for func
            fallback: Value to return if circuit is open
            **kwargs: Keyword arguments for func
            
        Returns:
            The result of func, or fallback if circuit is open
        """
        async with self._lock:
            if not self.can_execute():
                if fallback is not None:
                    return fallback
                if self._last_cached_value is not None:
                    return self._last_cached_value
                raise RuntimeError(f"Circuit breaker '{self.name}' is OPEN")

        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            async with self._lock:
                self.record_success()
                self._last_cached_value = result
            return result
        except self.expected_exceptions as exc:
            async with self._lock:
                self.record_failure()
            logger.warning(f"Circuit breaker '{self.name}': call failed — {exc}")
            if fallback is not None:
                return fallback
            if self._last_cached_value is not None:
                return self._last_cached_value
            raise


# ── Global Breakers Registry ──────────────────────────────────────────────────

BREAKERS: dict[str, CircuitBreaker] = {
    "yfinance": CircuitBreaker("yfinance", failure_threshold=3, cooldown_seconds=60),
    "fred": CircuitBreaker("fred", failure_threshold=3, cooldown_seconds=120),
    "gdelt": CircuitBreaker("gdelt", failure_threshold=3, cooldown_seconds=60),
    "okx": CircuitBreaker("okx", failure_threshold=3, cooldown_seconds=30),
    "edgar": CircuitBreaker("edgar", failure_threshold=3, cooldown_seconds=60),
    "ollama": CircuitBreaker("ollama", failure_threshold=3, cooldown_seconds=30),
    "kalshi": CircuitBreaker("kalshi", failure_threshold=3, cooldown_seconds=30),
    "sqlite": CircuitBreaker("sqlite", failure_threshold=5, cooldown_seconds=10),
}


async def check_db_connection() -> bool:
    """Check if SQLite database is accessible."""
    from hypervisor.db.engine import engine
    from sqlalchemy import text
    
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def get_dependency_health() -> dict[str, str]:
    """
    Check health of all external dependencies.
    Returns dict of dependency_name -> state value.
    """
    health = {}
    for name, breaker in BREAKERS.items():
        if name == "sqlite":
            db_ok = await check_db_connection()
            health[name] = "closed" if db_ok else "open"
        else:
            health[name] = breaker.state.value
    return health