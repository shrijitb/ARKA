"""
hypervisor/audit.py

Structured audit logging for all state-changing events in Arka.

Every state mutation gets a structured log entry that can be queried.
Audit events are written to both stdout (for Docker logs) and a rotating
file (data/audit.jsonl) for queryability.

Event types:
  regime_change       — regime label changed
  allocation_update   — capital allocated to workers
  signal_generated    — worker produced a signal
  order_executed      — trade placed
  order_filled        — trade filled
  risk_breach         — risk limit triggered
  worker_paused       — worker halted
  worker_resumed      — worker restarted
  domain_enter        — domain router entered a market domain
  domain_exit         — domain router exited a market domain
  config_change       — credential or config updated via setup wizard
  circuit_breaker     — external dependency circuit breaker state change
  backtest_promotion  — nightly pipeline promoted new parameters
  emergency_stop      — global stop triggered
  startup             — system startup
  shutdown            — system shutdown
  health_check        — worker health check result
  capital_reconcile   — capital reconciliation completed

Usage:
    from hypervisor.audit import audit
    
    await audit("regime_change", old=prev_regime, new=regime_label, probs=regime_probs)
    await audit("allocation_update", allocations=new_allocs, regime=regime_label)
    await audit("order_executed", worker=worker, instrument=inst, side=side, qty=qty, price=price)
"""

from __future__ import annotations

import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import structlog

# Configure structlog for audit events
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

audit_log = structlog.get_logger("arka.audit")

# Also set up file-based audit logging
_audit_file_path = Path("data/audit.jsonl")


def _setup_file_handler() -> Optional[RotatingFileHandler]:
    """Set up rotating file handler for audit logs."""
    try:
        # Ensure data directory exists
        _audit_file_path.parent.mkdir(parents=True, exist_ok=True)
        
        handler = RotatingFileHandler(
            _audit_file_path,
            maxBytes=10_000_000,  # 10MB
            backupCount=5,
        )
        handler.setFormatter(logging.Formatter('%(message)s'))
        
        audit_logger = logging.getLogger("arka.audit")
        audit_logger.addHandler(handler)
        audit_logger.setLevel(logging.INFO)
        
        return handler
    except Exception as exc:
        logging.warning(f"Could not set up audit file handler: {exc}")
        return None


# Initialize file handler on module load
_file_handler = _setup_file_handler()


# ── Event Type Constants ──────────────────────────────────────────────────────

class AuditEvent:
    """Constants for audit event types."""
    REGIME_CHANGE = "regime_change"
    ALLOCATION_UPDATE = "allocation_update"
    SIGNAL_GENERATED = "signal_generated"
    ORDER_EXECUTED = "order_executed"
    ORDER_FILLED = "order_filled"
    RISK_BREACH = "risk_breach"
    WORKER_PAUSED = "worker_paused"
    WORKER_RESUMED = "worker_resumed"
    DOMAIN_ENTER = "domain_enter"
    DOMAIN_EXIT = "domain_exit"
    CONFIG_CHANGE = "config_change"
    CIRCUIT_BREAKER = "circuit_breaker"
    BACKTEST_PROMOTION = "backtest_promotion"
    EMERGENCY_STOP = "emergency_stop"
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    HEALTH_CHECK = "health_check"
    CAPITAL_RECONCILE = "capital_reconcile"


# ── Audit Functions ───────────────────────────────────────────────────────────

async def audit(event: str, **kwargs: Any) -> None:
    """
    Write an audit entry. All entries include timestamp (automatic via structlog),
    event type, and arbitrary key-value context.
    
    Args:
        event: Event type string (see AuditEvent constants)
        **kwargs: Arbitrary context key-value pairs
    """
    # Add event type to context
    context = {"event": event, **kwargs}
    
    # Log via structlog (stdout)
    audit_log.info(event, **kwargs)
    
    # Also write to file if handler is available
    if _file_handler:
        try:
            log_entry = json.dumps({
                "timestamp": time.time(),
                **context
            })
            _file_handler.write(log_entry + "\n")
            _file_handler.flush()
        except Exception as exc:
            logging.warning(f"Failed to write audit entry to file: {exc}")


# ── Convenience Functions ─────────────────────────────────────────────────────

async def audit_regime_change(
    old_regime: str,
    new_regime: str,
    probabilities: dict[str, float],
    circuit_breaker_active: bool = False,
) -> None:
    """Audit a regime change event."""
    await audit(
        AuditEvent.REGIME_CHANGE,
        old=old_regime,
        new=new_regime,
        probabilities=probabilities,
        circuit_breaker=circuit_breaker_active,
    )


async def audit_allocation_update(
    allocations: dict[str, float],
    regime: str,
    total_capital: float,
) -> None:
    """Audit a capital allocation update."""
    await audit(
        AuditEvent.ALLOCATION_UPDATE,
        allocations=allocations,
        regime=regime,
        total_capital=total_capital,
    )


async def audit_signal(
    worker: str,
    instrument: str,
    action: str,
    rationale: str,
    confidence: Optional[float] = None,
    signal_id: Optional[int] = None,
) -> None:
    """Audit a trading signal generation."""
    await audit(
        AuditEvent.SIGNAL_GENERATED,
        worker=worker,
        instrument=instrument,
        action=action,
        rationale=rationale,
        confidence=confidence,
        signal_id=signal_id,
    )


async def audit_order(
    worker: str,
    instrument: str,
    side: str,
    quantity: float,
    price: float,
    signal_id: Optional[int] = None,
    mode: str = "paper",
) -> None:
    """Audit an order execution."""
    await audit(
        AuditEvent.ORDER_EXECUTED,
        worker=worker,
        instrument=instrument,
        side=side,
        quantity=quantity,
        price=price,
        signal_id=signal_id,
        mode=mode,
    )


async def audit_risk_breach(
    limit_name: str,
    current_value: float,
    threshold: float,
    action: str,
    worker: Optional[str] = None,
) -> None:
    """Audit a risk limit breach."""
    await audit(
        AuditEvent.RISK_BREACH,
        limit=limit_name,
        value=current_value,
        threshold=threshold,
        action=action,
        worker=worker,
    )


async def audit_worker_paused(worker: str, reason: str) -> None:
    """Audit a worker pause event."""
    await audit(AuditEvent.WORKER_PAUSED, worker=worker, reason=reason)


async def audit_worker_resumed(worker: str) -> None:
    """Audit a worker resume event."""
    await audit(AuditEvent.WORKER_RESUMED, worker=worker)


async def audit_circuit_breaker(
    dependency: str,
    old_state: str,
    new_state: str,
    failure_count: Optional[int] = None,
) -> None:
    """Audit a circuit breaker state change."""
    await audit(
        AuditEvent.CIRCUIT_BREAKER,
        dependency=dependency,
        old_state=old_state,
        new_state=new_state,
        failure_count=failure_count,
    )


async def audit_config_change(
    key: str,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
) -> None:
    """Audit a configuration change."""
    await audit(
        AuditEvent.CONFIG_CHANGE,
        key=key,
        old_value=old_value,
        new_value=new_value,
    )


async def audit_emergency_stop(reason: str, triggered_by: str = "manual") -> None:
    """Audit an emergency stop event."""
    await audit(
        AuditEvent.EMERGENCY_STOP,
        reason=reason,
        triggered_by=triggered_by,
    )


async def audit_health_check(
    worker: str,
    healthy: bool,
    response_time_ms: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    """Audit a worker health check result."""
    await audit(
        AuditEvent.HEALTH_CHECK,
        worker=worker,
        healthy=healthy,
        response_time_ms=response_time_ms,
        error=error,
    )


async def audit_startup(
    capital: float,
    mode: str,
    workers: list[str],
    cycle_interval: int,
) -> None:
    """Audit system startup."""
    await audit(
        AuditEvent.STARTUP,
        capital=capital,
        mode=mode,
        workers=workers,
        cycle_interval=cycle_interval,
    )


async def audit_shutdown(clean: bool = True, reason: Optional[str] = None) -> None:
    """Audit system shutdown."""
    await audit(
        AuditEvent.SHUTDOWN,
        clean=clean,
        reason=reason,
    )


async def audit_capital_reconcile(
    total_capital: float,
    deployed: float,
    free_capital: float,
    pnl: float,
) -> None:
    """Audit capital reconciliation."""
    await audit(
        AuditEvent.CAPITAL_RECONCILE,
        total_capital=total_capital,
        deployed=deployed,
        free_capital=free_capital,
        pnl=pnl,
    )