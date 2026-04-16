"""
hypervisor/risk/expiry_guard.py

Physical Delivery Prevention System.

Futures contracts with expiry dates must be closed before delivery. This applies
to any instrument that is not a perpetual swap.

Rules:
    1. Any non-perpetual futures position must be closed before the delivery window begins
    2. Close trigger: days_to_expiry <= CLOSE_BEFORE_DAYS
    3. Warning trigger: days_to_expiry <= WARN_BEFORE_DAYS
    4. No new positions in contracts expiring within NO_ENTRY_DAYS
    5. Perpetual swaps (no expiry) are exempt

OKX expiry format for dated futures: BTC-USDT-250627
    The last 6 digits are YYMMDD. Settlement is on that date.
    Delivery for physically settled contracts begins on expiry date.

For safety: close 3 business days before expiry to avoid settlement mechanics,
reduced liquidity, and basis convergence risk.

Usage:
    from hypervisor.risk.expiry_guard import ExpiryGuard

    eg = ExpiryGuard()

    # Pre-trade check
    can_enter, reason = eg.can_enter("BTC-USDT-250627")

    # Check a position
    result = eg.check_position("BTC-USDT-250627")
    if result["action"] == "close":
        # Force close the position

    # Scan all positions
    actions = eg.scan_all_positions(positions)
"""

import logging
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ExpiryGuard:
    """
    Prevent holding expiring contracts through to physical delivery.

    Monitors all positions for expiry dates and triggers warnings/closures
    based on configurable thresholds.
    """

    CLOSE_BEFORE_DAYS = 3   # Force close this many days before expiry
    WARN_BEFORE_DAYS = 7    # Start warning in dashboard/Telegram
    NO_ENTRY_DAYS = 5       # Block new entries this close to expiry

    def parse_expiry(self, instrument: str) -> Optional[date]:
        """
        Extract expiry date from OKX instrument name.
        Returns None for perpetual swaps (no expiry).

        Examples:
            BTC-USDT-SWAP      → None (perpetual, no expiry)
            BTC-USDT-250627    → 2025-06-27
            ETH-USDT-260320    → 2026-03-20

        Args:
            instrument: OKX instrument name (e.g., "BTC-USDT-250627")

        Returns:
            Expiry date or None for perpetual swaps
        """
        parts = instrument.split("-")
        if len(parts) < 3:
            return None

        last = parts[-1]
        if last == "SWAP":
            return None

        if len(last) == 6 and last.isdigit():
            try:
                yy, mm, dd = int(last[:2]), int(last[2:4]), int(last[4:6])
                # Handle 2-digit year: assume 20xx for all (valid until 2100)
                return date(2000 + yy, mm, dd)
            except (ValueError, IndexError):
                logger.warning(f"ExpiryGuard: invalid date in instrument {instrument}")
                return None

        return None

    def check_position(
        self,
        instrument: str,
        today: Optional[date] = None,
    ) -> Dict:
        """
        Check a single position against expiry rules.

        Args:
            instrument: OKX instrument name
            today: Date to use for calculation (defaults to today)

        Returns:
            Dict with keys:
                - instrument: str
                - expiry: str (ISO format) or None
                - days_to_expiry: int or None
                - action: "none" | "warn" | "close" | "block_entry"
                - reason: str
        """
        if today is None:
            today = date.today()

        expiry = self.parse_expiry(instrument)

        if expiry is None:
            return {
                "instrument": instrument,
                "expiry": None,
                "days_to_expiry": None,
                "action": "none",
                "reason": "Perpetual swap, no expiry.",
            }

        days_left = (expiry - today).days

        if days_left <= 0:
            return {
                "instrument": instrument,
                "expiry": expiry.isoformat(),
                "days_to_expiry": days_left,
                "action": "close",
                "reason": f"EXPIRED (expiry: {expiry.isoformat()}). Close immediately.",
            }

        if days_left <= self.CLOSE_BEFORE_DAYS:
            return {
                "instrument": instrument,
                "expiry": expiry.isoformat(),
                "days_to_expiry": days_left,
                "action": "close",
                "reason": (
                    f"{days_left} days to expiry. "
                    f"Closing to prevent delivery settlement "
                    f"(threshold: {self.CLOSE_BEFORE_DAYS} days)."
                ),
            }

        if days_left <= self.WARN_BEFORE_DAYS:
            return {
                "instrument": instrument,
                "expiry": expiry.isoformat(),
                "days_to_expiry": days_left,
                "action": "warn",
                "reason": (
                    f"{days_left} days to expiry. "
                    f"Will auto-close at {self.CLOSE_BEFORE_DAYS} days remaining."
                ),
            }

        return {
            "instrument": instrument,
            "expiry": expiry.isoformat(),
            "days_to_expiry": days_left,
            "action": "none",
            "reason": f"{days_left} days to expiry. No action needed.",
        }

    def can_enter(self, instrument: str) -> Tuple[bool, str]:
        """
        Pre-trade check: block new entries near expiry.

        Args:
            instrument: OKX instrument name to check

        Returns:
            (allowed, reason) tuple
        """
        expiry = self.parse_expiry(instrument)

        if expiry is None:
            return True, "Perpetual swap, no expiry restriction."

        days_left = (expiry - date.today()).days

        if days_left <= self.NO_ENTRY_DAYS:
            return False, (
                f"Blocked: {instrument} expires in {days_left} days. "
                f"No new entries within {self.NO_ENTRY_DAYS} days of expiry. "
                f"Use the perpetual swap instead."
            )

        return True, "OK"

    def scan_all_positions(self, positions: List[Dict]) -> List[Dict]:
        """
        Scan all open positions, return any needing action.

        Args:
            positions: List of position dicts, each with 'instrument' key

        Returns:
            List of action dicts for positions needing attention
        """
        actions = []
        for pos in positions:
            instrument = pos.get("instrument", "")
            if not instrument:
                continue

            result = self.check_position(instrument)
            if result["action"] in ("close", "warn"):
                # Include additional position info if available
                action_item = result.copy()
                action_item["position_id"] = pos.get("position_id", instrument)
                action_item["quantity"] = pos.get("quantity")
                actions.append(action_item)

        return actions

    def get_upcoming_expiries(
        self,
        instruments: List[str],
        lookahead_days: int = 30,
    ) -> List[Dict]:
        """
        Get all instruments expiring within a lookahead window.

        Useful for planning and risk assessment.

        Args:
            instruments: List of instrument names to check
            lookahead_days: How many days ahead to look

        Returns:
            List of dicts sorted by days_to_expiry ascending
        """
        today = date.today()
        expiries = []

        for instrument in instruments:
            expiry = self.parse_expiry(instrument)
            if expiry is None:
                continue

            days_left = (expiry - today).days
            if 0 < days_left <= lookahead_days:
                expiries.append({
                    "instrument": instrument,
                    "expiry": expiry.isoformat(),
                    "days_to_expiry": days_left,
                })

        return sorted(expiries, key=lambda x: x["days_to_expiry"])

    def is_perpetual(self, instrument: str) -> bool:
        """
        Check if an instrument is a perpetual swap (no expiry).

        Args:
            instrument: OKX instrument name

        Returns:
            True if perpetual, False if dated or unknown
        """
        return self.parse_expiry(instrument) is None

    def summary(self, positions: List[Dict]) -> str:
        """
        Return a human-readable summary of expiry status for all positions.

        Args:
            positions: List of position dicts with 'instrument' key

        Returns:
            Formatted summary string
        """
        if not positions:
            return "ExpiryGuard: no positions to check"

        actions = self.scan_all_positions(positions)

        # Categorize
        closes = [a for a in actions if a["action"] == "close"]
        warns = [a for a in actions if a["action"] == "warn"]
        perpetuals = [
            p for p in positions
            if self.is_perpetual(p.get("instrument", ""))
        ]

        lines = [f"ExpiryGuard: {len(positions)} positions"]

        if closes:
            lines.append(f"  🔴 URGENT - Close immediately ({len(closes)}):")
            for c in closes:
                lines.append(
                    f"    {c['instrument']}: {c['reason']}"
                )

        if warns:
            lines.append(f"  🟡 Warning - Approaching expiry ({len(warns)}):")
            for w in warns:
                lines.append(
                    f"    {w['instrument']}: {w['reason']}"
                )

        if perpetuals:
            lines.append(f"  🟢 Perpetual swaps (no expiry risk): {len(perpetuals)}")

        if not closes and not warns and not perpetuals:
            lines.append("  All positions are dated futures with safe expiry dates.")

        return "\n".join(lines)