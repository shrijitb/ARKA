"""
hypervisor/allocator/capital.py

Regime-Gated Capital Allocator.

Maps the current market regime → worker allocation weights, adjusted for:
  - Worker health (unhealthy workers get 0 regardless of regime profile)
  - Worker performance (Sharpe below MIN_SHARPE cuts allocation in half)
  - Cash buffer (enforced minimum reserve, scaled up in crisis regimes)

Worker keys match docker-compose service names exactly:
    arbitrader      — Java cross-exchange price arb (delta-neutral, port 8004)
    nautilus        — NautilusTrader strategies incl. MACD/Fractals swing (port 8001)
    polymarket      — CLOB market-making (port 8002)
    autohedge       — AI advisory pipeline, Director+Quant+Risk (port 8003)
    core_dividends  — Passive dividend sleeve: SCHD + VYM buy-and-hold (port 8006)

NOTE: swing_trend is NOT a separate worker. Swing strategies run inside
NautilusTrader (workers/nautilus/) and are allocated via the 'nautilus' key.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)

REGIME_PROFILES: Dict[str, Dict[str, float]] = {

    "WAR_PREMIUM": {
        # Arbitrader: funding rates spike in geopolitical stress — best risk/reward
        # Nautilus: swing strategies ride defense/commodity momentum; backtest confirms
        # Polymarket: war/election prediction markets are maximally active
        # AutoHedge: advisory paused — high uncertainty degrades model output quality
        # core_dividends: 20% passive sleeve — SCHD/VYM hold through volatility
        "arbitrader":     0.36,
        "nautilus":       0.20,
        "polymarket":     0.24,
        "autohedge":      0.00,
        "core_dividends": 0.20,
    },

    "CRISIS_ACUTE": {
        # Cash is king. Cut everything. VaR too high for directional bets.
        # Arbitrader survives — delta-neutral, earns while flat.
        # 50%+ stays in cash (MAX_DEPLOY_PCT = 0.50 in this regime).
        # core_dividends: 0% — even passive positions are liquidated in acute crisis.
        "arbitrader":     0.40,
        "nautilus":       0.10,
        "polymarket":     0.20,
        "autohedge":      0.00,
        "core_dividends": 0.00,
    },

    "BEAR_RECESSION": {
        # Nautilus swing strategies expected to SHORT (MACD reads direction itself).
        # Arbitrader trimmed — price spreads narrow as volume dries up.
        # Polymarket: recession/rate-cut prediction markets active.
        # core_dividends: 20% — dividend ETFs are defensive in slow recessions.
        "arbitrader":     0.20,
        "nautilus":       0.36,
        "polymarket":     0.16,
        "autohedge":      0.08,
        "core_dividends": 0.20,
    },

    "BULL_FROTHY": {
        # Arbitrader: leveraged longs paying high funding → maximum spread
        # Nautilus: LONG setups abundant in bull — maximum swing allocation
        # Polymarket: election/macro markets quiet in calm bull
        # core_dividends: 20% passive — dividend capture in risk-on environment.
        "arbitrader":     0.28,
        "nautilus":       0.36,
        "polymarket":     0.08,
        "autohedge":      0.08,
        "core_dividends": 0.20,
    },

    "REGIME_CHANGE": {
        # Transition: direction unknown. Favour delta-neutral + systematic.
        # Reduce nautilus swing allocation until new regime stabilises.
        "arbitrader":     0.32,
        "nautilus":       0.24,
        "polymarket":     0.16,
        "autohedge":      0.08,
        "core_dividends": 0.20,
    },

    "SHADOW_DRIFT": {
        # Hidden pressure — BDI moving but VIX calm. Moderate caution.
        "arbitrader":     0.32,
        "nautilus":       0.28,
        "polymarket":     0.12,
        "autohedge":      0.08,
        "core_dividends": 0.20,
    },

    "BULL_CALM": {
        # Default / peacetime. Balanced. Nautilus gets most directional capital.
        "arbitrader":     0.24,
        "nautilus":       0.36,
        "polymarket":     0.08,
        "autohedge":      0.12,
        "core_dividends": 0.20,
    },
}

# Maximum % of total capital to deploy per regime.
REGIME_MAX_DEPLOY: Dict[str, float] = {
    "WAR_PREMIUM":    0.70,
    "CRISIS_ACUTE":   0.50,
    "BEAR_RECESSION": 0.75,
    "BULL_FROTHY":    0.80,
    "REGIME_CHANGE":  0.70,
    "SHADOW_DRIFT":   0.75,
    "BULL_CALM":      0.80,
}

MIN_SHARPE_FULL_ALLOC = 1.0
MIN_SHARPE_FLOOR      = 0.5


@dataclass
class AllocationResult:
    regime:          str
    total_capital:   float
    max_deployable:  float
    allocations:     Dict[str, float] = field(default_factory=dict)
    skipped_workers: Dict[str, str]   = field(default_factory=dict)
    cash_reserve:    float = 0.0

    def summary(self) -> str:
        lines = [
            f"Regime: {self.regime}  |  Total: ${self.total_capital:.2f}  "
            f"|  Max deploy: ${self.max_deployable:.2f}  |  Cash: ${self.cash_reserve:.2f}",
        ]
        for w, amt in self.allocations.items():
            lines.append(f"  {w:<16} → ${amt:.2f}")
        for w, reason in self.skipped_workers.items():
            lines.append(f"  {w:<16} → SKIPPED ({reason})")
        return "\n".join(lines)


class RegimeAllocator:
    def __init__(self, total_capital: float):
        self.total_capital = total_capital

    def compute(
        self,
        regime:          str,
        worker_health:   Optional[Dict[str, bool]]  = None,
        worker_sharpe:   Optional[Dict[str, float]] = None,
        registered_only: Optional[list]             = None,
    ) -> AllocationResult:
        worker_health = worker_health or {}
        worker_sharpe = worker_sharpe or {}

        profile    = REGIME_PROFILES.get(regime, REGIME_PROFILES["BULL_CALM"])
        max_deploy = self.total_capital * REGIME_MAX_DEPLOY.get(regime, 0.80)

        result = AllocationResult(
            regime        = regime,
            total_capital = self.total_capital,
            max_deployable = max_deploy,
        )

        eligible: Dict[str, float] = {}

        for worker, base_weight in profile.items():
            if base_weight == 0.0:
                result.skipped_workers[worker] = "regime_profile_zero"
                continue

            if registered_only is not None and worker not in registered_only:
                result.skipped_workers[worker] = "not_registered"
                continue

            healthy = worker_health.get(worker, True)
            if not healthy:
                result.skipped_workers[worker] = "unhealthy"
                logger.warning(f"Allocator: skipping {worker} — health check failed")
                continue

            sharpe = worker_sharpe.get(worker)
            if sharpe is not None:
                if sharpe < MIN_SHARPE_FLOOR:
                    result.skipped_workers[worker] = f"sharpe_too_low ({sharpe:.2f})"
                    continue
                if sharpe < MIN_SHARPE_FULL_ALLOC:
                    eligible[worker] = base_weight * 0.5
                    logger.info(f"Allocator: {worker} Sharpe {sharpe:.2f} — halved weight")
                    continue

            eligible[worker] = base_weight

        if not eligible:
            logger.warning(f"Allocator: no eligible workers for {regime}. Staying cash.")
            result.cash_reserve = self.total_capital
            return result

        # Normalise against the SUM OF ALL NON-ZERO PROFILE WEIGHTS, not just the
        # eligible subset.  This means a single healthy worker only receives its
        # intended profile share, even if all other workers are still starting up.
        # Without this, a single eligible worker absorbs all of max_deploy and
        # exceeds the risk manager's per-worker cap (50%).
        profile_nonzero_sum = sum(w for w in profile.values() if w > 0.0)
        result.allocations = {
            w: round(max_deploy * (wt / profile_nonzero_sum), 2)
            for w, wt in eligible.items()
        }
        result.cash_reserve = round(
            self.total_capital - sum(result.allocations.values()), 2
        )

        logger.info(
            f"Allocator [{regime}]: "
            + ", ".join(f"{w}=${v:.2f}" for w, v in result.allocations.items())
            + f" | cash=${result.cash_reserve:.2f}"
        )
        return result

    def update_capital(self, new_total: float) -> None:
        self.total_capital = new_total
