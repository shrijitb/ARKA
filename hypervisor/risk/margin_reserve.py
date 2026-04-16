"""
hypervisor/risk/margin_reserve.py

Per-Position Margin Call Reserve Manager.

Every leveraged position must reserve a buffer that cannot be allocated to new trades.
This is separate from the portfolio-level 15% free capital floor (which protects the
whole portfolio). The margin reserve protects individual positions from liquidation.

Reserve formula:
    reserve_usd = position_notional * reserve_pct / leverage

Where:
    position_notional = quantity * current_price
    reserve_pct varies by strategy risk level:
        funding_arb (delta-neutral):   15% — low directional risk but basis can move
        swing_macd (directional):      25% — exposed to adverse price moves
        range_mean_revert (directional): 25% — exposed to adverse price moves
        day_scalp (directional):       20% — tighter stops reduce needed reserve
        order_flow (directional):      20% — short holding period
        factor_model (multi-asset):    25% — correlation risk during stress

The reserve is held in USDT on OKX, not allocated to any strategy.
If available_margin < reserve_usd, the strategy must:
    1. Reduce position size until reserve is satisfied
    2. If cannot reduce: close the position entirely

Integration:
    Before any trade execution, check:
        available_balance >= order_cost + reserve_for_new_position + existing_reserves

Usage:
    from hypervisor.risk.margin_reserve import MarginReserveManager

    mrm = MarginReserveManager()
    can_open, reason = mrm.can_open_position(
        strategy="swing_macd",
        notional_usd=100.0,
        leverage=3,
        available_balance_usd=200.0,
        order_cost_usd=33.33,
    )
    if can_open:
        mrm.register_position(position_id, reserve_usd)
"""

import logging
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


class MarginReserveManager:
    """
    Enforce per-position liquid reserves for margin call protection.

    Tracks active reserves per position and enforces pre-trade checks
    to ensure sufficient buffer exists for margin calls.
    """

    # Reserve percentages by strategy (higher = more risk)
    STRATEGY_RESERVE_PCT = {
        "funding_arb":        0.15,  # Delta-neutral, low directional risk
        "swing_macd":         0.25,  # Directional exposure
        "range_mean_revert":  0.25,  # Directional exposure
        "day_scalp":          0.20,  # Tighter stops reduce needed reserve
        "order_flow":         0.20,  # Short holding period
        "factor_model":       0.25,  # Correlation risk during stress
    }

    DEFAULT_RESERVE_PCT = 0.25  # Conservative default for unknown strategies

    def __init__(self):
        # position_id -> reserve_usd tracking
        self.active_reserves: Dict[str, float] = {}

    def compute_reserve(
        self,
        strategy: str,
        notional_usd: float,
        leverage: int,
    ) -> float:
        """
        Compute required margin reserve for a position.

        Args:
            strategy: Strategy name (determines reserve percentage)
            notional_usd: Position notional value in USD
            leverage: Leverage multiplier (1 = no leverage)

        Returns:
            reserve_usd: Amount that must remain liquid
        """
        pct = self.STRATEGY_RESERVE_PCT.get(strategy, self.DEFAULT_RESERVE_PCT)
        return notional_usd * pct / max(leverage, 1)

    def can_open_position(
        self,
        strategy: str,
        notional_usd: float,
        leverage: int,
        available_balance_usd: float,
        order_cost_usd: float,
    ) -> Tuple[bool, str]:
        """
        Pre-trade check: is there enough balance for the trade PLUS the reserve?

        Args:
            strategy: Strategy name
            notional_usd: Position notional value
            leverage: Leverage multiplier
            available_balance_usd: Current available balance
            order_cost_usd: Cost to open the position (margin required)

        Returns:
            (allowed, reason) tuple
        """
        new_reserve = self.compute_reserve(strategy, notional_usd, leverage)
        existing_reserves = sum(self.active_reserves.values())
        total_needed = order_cost_usd + new_reserve + existing_reserves

        if available_balance_usd < total_needed:
            shortfall = total_needed - available_balance_usd
            return False, (
                f"Insufficient balance for margin reserve. "
                f"Need ${total_needed:.2f} (trade ${order_cost_usd:.2f} + "
                f"new reserve ${new_reserve:.2f} + "
                f"existing reserves ${existing_reserves:.2f}), "
                f"have ${available_balance_usd:.2f}. "
                f"Shortfall: ${shortfall:.2f}."
            )

        return True, "OK"

    def register_position(self, position_id: str, reserve_usd: float) -> None:
        """
        Register a new position's reserve requirement.

        Args:
            position_id: Unique identifier for the position
            reserve_usd: Reserve amount to hold
        """
        old = self.active_reserves.get(position_id, 0.0)
        self.active_reserves[position_id] = reserve_usd
        if reserve_usd > 0:
            logger.debug(
                f"MarginReserve: registered {position_id} → ${reserve_usd:.2f} "
                f"(was ${old:.2f})"
            )

    def release_position(self, position_id: str) -> None:
        """
        Release a position's reserve (when position is closed).

        Args:
            position_id: Unique identifier for the position
        """
        released = self.active_reserves.pop(position_id, None)
        if released is not None:
            logger.debug(f"MarginReserve: released {position_id} → ${released:.2f}")

    def get_total_reserves(self) -> float:
        """Return the total amount held in reserve across all positions."""
        return sum(self.active_reserves.values())

    def get_position_reserve(self, position_id: str) -> float:
        """Return the reserve for a specific position."""
        return self.active_reserves.get(position_id, 0.0)

    def check_existing_positions(
        self,
        positions: List[dict],
        available_balance: float,
    ) -> List[dict]:
        """
        Check all open positions. If balance has dropped below the sum of
        all reserves, return positions that must be reduced/closed (largest first).

        Args:
            positions: List of position dicts with 'position_id' key
            available_balance: Current available balance

        Returns:
            List of positions to reduce, sorted by reserve size descending
        """
        total_reserves = sum(self.active_reserves.values())
        if available_balance >= total_reserves:
            return []

        shortfall = total_reserves - available_balance
        reduce_list = []

        # Sort by reserve size descending, reduce largest positions first
        sorted_positions = sorted(
            [(pid, res) for pid, res in self.active_reserves.items()],
            key=lambda x: x[1],
            reverse=True,
        )

        accumulated = 0.0
        for pid, res in sorted_positions:
            reduce_list.append({"position_id": pid, "reserve_usd": res})
            accumulated += res
            if accumulated >= shortfall:
                break

        logger.warning(
            f"MarginReserve: balance ${available_balance:.2f} below total reserves "
            f"${total_reserves:.2f}. Shortfall: ${shortfall:.2f}. "
            f"Positions to reduce: {len(reduce_list)}"
        )
        return reduce_list

    def update_position_reserve(
        self,
        position_id: str,
        strategy: str,
        notional_usd: float,
        leverage: int,
    ) -> float:
        """
        Recalculate and update reserve for an existing position.

        Useful when position size changes or price moves significantly.

        Args:
            position_id: Unique identifier for the position
            strategy: Strategy name
            notional_usd: Updated position notional value
            leverage: Leverage multiplier

        Returns:
            New reserve amount
        """
        new_reserve = self.compute_reserve(strategy, notional_usd, leverage)
        self.register_position(position_id, new_reserve)
        return new_reserve

    def summary(self) -> str:
        """Return a human-readable summary of all active reserves."""
        if not self.active_reserves:
            return "MarginReserve: no active reserves"

        total = sum(self.active_reserves.values())
        lines = [f"MarginReserve: {len(self.active_reserves)} positions, ${total:.2f} total"]
        for pid, res in sorted(self.active_reserves.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  {pid}: ${res:.2f}")
        return "\n".join(lines)