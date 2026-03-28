"""
hypervisor/risk/manager.py

Portfolio-Level Risk Manager.

Sits above all workers.  The regime classifier says WHAT market we are in.
The risk manager enforces HOW MUCH damage is acceptable at the portfolio level.

Checks run each Hypervisor cycle:
  1. Portfolio drawdown from peak (halt all workers if > MAX_DRAWDOWN_PCT)
  2. Per-worker allocation cap (no single worker > MAX_SINGLE_WORKER_PCT)
  3. Open position count (too many open legs = correlated exposure)
  4. Free capital floor (always keep MIN_FREE_PCT in cash as emergency buffer)

Risk decisions are binary:
  - PASS  → normal operation, proceed
  - HALT  → stop all new entries, optionally liquidate
  - TRIM  → reduce the offending worker's allocation only

Usage:
    from hypervisor.risk.manager import RiskManager

    rm = RiskManager(initial_capital=200.0)
    verdict = rm.assess(portfolio_state_dict, allocations_dict)
    if not verdict.safe:
        logger.warning(verdict.reason)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

logger = logging.getLogger(__name__)

# ── Risk Limits ───────────────────────────────────────────────────────────────
MAX_DRAWDOWN_PCT        = 0.20   # 20% portfolio drawdown from peak → halt everything
MAX_SINGLE_WORKER_PCT   = 0.50   # No single worker > 50% of total capital
MAX_OPEN_POSITIONS      = 6      # Across all workers — more = correlated crash risk
MIN_FREE_PCT            = 0.15   # Always keep ≥ 15% in cash (emergency buffer)
PNL_FLOOR_USD           = -40.0  # Absolute floor: stop if total realised PnL < -$40
WORKER_MAX_DRAWDOWN_PCT = 0.30   # Per-worker: halt that worker if drawdown > 30%


@dataclass
class RiskVerdict:
    """Result of a single risk assessment cycle."""
    safe:            bool
    reason:          str  = "OK"
    action:          str  = "none"   # "none" | "halt_all" | "trim_worker" | "halt_worker"
    affected_worker: Optional[str] = None

    def __bool__(self):
        return self.safe


@dataclass
class WorkerRiskState:
    """Tracks peak capital and drawdown for one worker."""
    worker:         str
    peak_capital:   float = 0.0
    current_pnl:    float = 0.0
    entry_capital:  float = 0.0    # Capital at time of first allocation
    last_updated:   float = field(default_factory=time.time)

    def drawdown_pct(self) -> float:
        """Current drawdown from peak as a positive fraction."""
        if self.peak_capital <= 0:
            return 0.0
        return max(0.0, (self.peak_capital - (self.entry_capital + self.current_pnl))
                   / self.peak_capital)


class RiskManager:
    """
    Portfolio-level risk gatekeeper.

    State tracked across cycles:
        _peak_capital    — all-time high of total portfolio value
        _worker_states   — per-worker peak/pnl tracking
        _halt_timestamp  — when the last portfolio halt was triggered (for cooldown)
    """

    def __init__(self, initial_capital: float):
        self._initial_capital  = initial_capital
        self._peak_capital:    float                    = initial_capital
        self._worker_states:   Dict[str, WorkerRiskState] = {}
        self._halt_timestamp:  Optional[float]          = None
        self._halt_cooldown_sec = 3600  # Don't re-enter for at least 1 hour after halt

        logger.info(
            f"RiskManager initialized | "
            f"Max portfolio drawdown: {MAX_DRAWDOWN_PCT*100:.0f}% | "
            f"Worker cap: {MAX_SINGLE_WORKER_PCT*100:.0f}% | "
            f"Max positions: {MAX_OPEN_POSITIONS}"
        )

    # ── Main Assessment ───────────────────────────────────────────────────────

    def assess(
        self,
        total_capital:   float,
        free_capital:    float,
        open_positions:  int,
        worker_pnl:      Optional[Dict[str, float]] = None,
        worker_allocated: Optional[Dict[str, float]] = None,
    ) -> RiskVerdict:
        """
        Run all risk checks.  Returns the FIRST failing check (most critical first).

        Args:
            total_capital:    Current total portfolio value (initial + all P&L).
            free_capital:     Capital not currently deployed.
            open_positions:   Count of all open position legs across all workers.
            worker_pnl:       {worker_name: realised_pnl_usd}
            worker_allocated: {worker_name: allocated_usd}
        """
        worker_pnl      = worker_pnl      or {}
        worker_allocated = worker_allocated or {}

        # ── Update peak ───────────────────────────────────────────────────────
        if total_capital > self._peak_capital:
            self._peak_capital = total_capital

        # ── Check 1: Portfolio halt cooldown ──────────────────────────────────
        if self._halt_timestamp is not None:
            elapsed = time.time() - self._halt_timestamp
            if elapsed < self._halt_cooldown_sec:
                remaining = self._halt_cooldown_sec - elapsed
                return RiskVerdict(
                    safe   = False,
                    reason = f"Halt cooldown active — {remaining/60:.0f}m remaining before re-entry",
                    action = "halt_all",
                )

        # ── Check 2: Portfolio drawdown from peak ─────────────────────────────
        portfolio_drawdown = self._portfolio_drawdown(total_capital)
        if portfolio_drawdown > MAX_DRAWDOWN_PCT:
            self._halt_timestamp = time.time()
            return RiskVerdict(
                safe   = False,
                reason = (
                    f"Portfolio drawdown {portfolio_drawdown*100:.1f}% "
                    f"exceeds limit {MAX_DRAWDOWN_PCT*100:.0f}%. "
                    f"Peak: ${self._peak_capital:.2f} | Now: ${total_capital:.2f}"
                ),
                action = "halt_all",
            )

        # ── Check 3: Absolute P&L floor ───────────────────────────────────────
        realised_pnl = total_capital - self._initial_capital
        if realised_pnl < PNL_FLOOR_USD:
            self._halt_timestamp = time.time()
            return RiskVerdict(
                safe   = False,
                reason = f"Total P&L ${realised_pnl:.2f} below floor ${PNL_FLOOR_USD:.2f}",
                action = "halt_all",
            )

        # ── Check 4: Open position count ──────────────────────────────────────
        if open_positions > MAX_OPEN_POSITIONS:
            return RiskVerdict(
                safe   = False,
                reason = f"Open positions {open_positions} exceeds limit {MAX_OPEN_POSITIONS}",
                action = "halt_all",
            )

        # ── Check 5: Free capital floor ───────────────────────────────────────
        free_pct = free_capital / total_capital if total_capital > 0 else 1.0
        if free_pct < MIN_FREE_PCT:
            return RiskVerdict(
                safe   = False,
                reason = (
                    f"Free capital {free_pct*100:.1f}% "
                    f"below minimum {MIN_FREE_PCT*100:.0f}% buffer"
                ),
                action = "halt_all",
            )

        # ── Check 6: Per-worker allocation cap ───────────────────────────────
        for worker, allocated in worker_allocated.items():
            if total_capital <= 0:
                continue
            alloc_pct = allocated / total_capital
            if alloc_pct > MAX_SINGLE_WORKER_PCT:
                return RiskVerdict(
                    safe            = False,
                    reason          = (
                        f"Worker {worker} holds {alloc_pct*100:.1f}% of capital "
                        f"(limit {MAX_SINGLE_WORKER_PCT*100:.0f}%)"
                    ),
                    action          = "trim_worker",
                    affected_worker = worker,
                )

        # ── Check 7: Per-worker drawdown ──────────────────────────────────────
        for worker, pnl in worker_pnl.items():
            state = self._get_worker_state(worker)
            state.current_pnl  = pnl
            # Do NOT update peak_capital here from /status data.
            # peak_capital is authoritative only when set by record_worker_allocation().
            # Updating it here from worker_allocated (/status responses) causes
            # out-of-sync state: peak rises but entry_capital doesn't, producing
            # false drawdown readings on workers with no active positions.
            state.last_updated = time.time()

            # Skip drawdown gate until capital has been formally allocated.
            # entry_capital=0 means record_worker_allocation() hasn't been called yet
            # (first cycle cold-start). Without this guard every fresh worker shows
            # 100% drawdown because peak_capital defaults to entry_capital=0.
            if state.entry_capital <= 0:
                continue
            dd = state.drawdown_pct()
            if dd > WORKER_MAX_DRAWDOWN_PCT:
                return RiskVerdict(
                    safe            = False,
                    reason          = (
                        f"Worker {worker} drawdown {dd*100:.1f}% "
                        f"exceeds per-worker limit {WORKER_MAX_DRAWDOWN_PCT*100:.0f}%"
                    ),
                    action          = "halt_worker",
                    affected_worker = worker,
                )

        return RiskVerdict(safe=True)

    # ── Reporting ─────────────────────────────────────────────────────────────

    def summary(self, total_capital: float, free_capital: float) -> str:
        dd = self._portfolio_drawdown(total_capital)
        pnl = total_capital - self._initial_capital
        lines = [
            f"RiskManager | Capital: ${total_capital:.2f} "
            f"(peak ${self._peak_capital:.2f}) | "
            f"Drawdown: {dd*100:.1f}% | P&L: ${pnl:+.2f} | "
            f"Free: ${free_capital:.2f} ({free_capital/total_capital*100:.0f}%)"
        ]
        for worker, state in self._worker_states.items():
            lines.append(
                f"  {worker:<16}: drawdown {state.drawdown_pct()*100:.1f}%  "
                f"pnl ${state.current_pnl:+.2f}"
            )
        return "\n".join(lines)

    def reset_halt(self) -> None:
        """Manually clear the halt cooldown (use after reviewing situation)."""
        logger.warning("RiskManager: halt cooldown manually cleared")
        self._halt_timestamp = None

    def record_worker_allocation(self, worker: str, amount_usd: float) -> None:
        """Call when capital is allocated or re-allocated to a worker.

        Recalibrates the drawdown baseline to the current position value so that
        a hypervisor re-allocation (e.g. more workers joining mid-run) does not
        appear as a drawdown.  Actual PnL losses will still fire the drawdown gate
        because current_pnl only moves via worker /status reports, not here.
        """
        state = self._get_worker_state(worker)
        state.entry_capital = amount_usd
        # Reset peak to current position value.  Drawdown accumulates from here.
        state.peak_capital = amount_usd + state.current_pnl
        logger.info(f"RiskManager: {worker} entry capital set to ${amount_usd:.2f}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _portfolio_drawdown(self, current_capital: float) -> float:
        """Drawdown from all-time peak as a positive fraction."""
        if self._peak_capital <= 0:
            return 0.0
        return max(0.0, (self._peak_capital - current_capital) / self._peak_capital)

    def _get_worker_state(self, worker: str) -> WorkerRiskState:
        if worker not in self._worker_states:
            self._worker_states[worker] = WorkerRiskState(worker=worker)
        return self._worker_states[worker]
