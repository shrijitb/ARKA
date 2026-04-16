"""
workers/nautilus/strategies/funding_arb.py

Funding Rate Carry Strategy — dual implementation.

Mechanism (delta-neutral carry):
  Positive funding (longs pay shorts):
    SHORT perpetual to receive funding payments every 8h.
    Delta risk hedged by LONG spot (not tracked in paper sim — carry leg only).

  Negative funding (shorts pay longs):
    LONG perpetual to receive funding payments from short-squeezed bears.

Annualized yield formula:
  annual_yield = mean_rate * 3 * 365  (3 settlements per day on OKX)

Example: 0.01% per 8h → 0.03%/day → 10.95% annualized
Bull-market extremes: 0.05–0.1% per 8h → 54–109% annualized

Entry conditions:
  1. abs(funding_rate) > ENTRY_THRESHOLD (0.01% = 0.0001)
  2. Regime bias allows the direction (flat → skip; momentum_long → long only)

Paper mode:
  Uses _synthetic_funding_rate() — deterministic within an 8-hour window.
  Live mode (when data/feeds/funding_rates.py is importable):
  Uses get_all_current_rates() from OKX public REST.

Signal format (matches all other Nautilus strategies):
  (pair, side, entry_price, stop_loss, take_profit)

SL: 1.5% from entry  — carry strategies use tight stops
TP: 0.75% from entry — target one 8h carry payment worth of move

OKX perp format: BTC-USDT-SWAP  (not BTC/USDT)
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Docker-isolation guard ────────────────────────────────────────────────────
# data/feeds/ is only accessible when running with the project root on sys.path
# (tests, hypervisor). Inside the nautilus Docker container the build context is
# ./workers/nautilus — data/feeds/ is not copied in. Fall back to synthetic data.
try:
    from data.feeds.funding_rates import get_all_current_rates as _get_live_rates
    _FEEDS_AVAILABLE = True
except ImportError:
    _FEEDS_AVAILABLE = False

# ── OKX symbol map ────────────────────────────────────────────────────────────
_PAIR_TO_OKX: dict[str, str] = {
    "BTC/USDT":  "BTC-USDT-SWAP",
    "ETH/USDT":  "ETH-USDT-SWAP",
    "SOL/USDT":  "SOL-USDT-SWAP",
    "BNB/USDT":  "BNB-USDT-SWAP",
    "AVAX/USDT": "AVAX-USDT-SWAP",
}

# ── Strategy parameters ───────────────────────────────────────────────────────
ENTRY_THRESHOLD = 0.0001   # 0.01% per 8h — minimum carry to enter
EXIT_THRESHOLD  = 0.00002  # 0.002% — near zero, close position
SL_PCT          = 0.015    # 1.5% stop loss (tight — carry, not directional)
TP_PCT          = 0.0075   # 0.75% take profit (half an 8h settlement proxy)
RESERVE_PCT     = 0.15     # 15% reserve for delta-neutral arb


# ─────────────────────────────────────────────────────────────────────────────
# Capital Allocation with Reserve
# ─────────────────────────────────────────────────────────────────────────────

def compute_arb_allocation(
    allocated_capital: float,
    leverage: int = 3,
    reserve_pct: float = RESERVE_PCT,
) -> dict:
    """
    Split allocated capital into tradeable and reserve portions.

    For a delta-neutral arb with leverage:
        total_allocated = $100
        reserve = $100 * 0.15 / 3 = $5.00
        tradeable = $100 - $5.00 = $95.00
        per_leg = $95.00 / 2 = $47.50
        spot_leg_notional = $47.50
        perp_leg_notional = $47.50

    The reserve stays liquid in USDT to cover:
        - Margin calls if basis widens unexpectedly
        - Funding rate sign flips requiring position adjustment
        - Temporary mark-to-market losses on one leg

    Args:
        allocated_capital: Total capital allocated to this arb position
        leverage: Leverage multiplier for the perp leg
        reserve_pct: Reserve percentage (default 15% for funding_arb)

    Returns:
        Dict with keys:
            - reserve_usd: Amount to hold in reserve
            - tradeable_usd: Amount available for trading
            - spot_leg_usd: Notional for spot leg
            - perp_leg_usd: Notional for perp leg
    """
    reserve = allocated_capital * reserve_pct / max(leverage, 1)
    tradeable = allocated_capital - reserve
    per_leg = tradeable / 2.0

    return {
        "reserve_usd": round(reserve, 2),
        "tradeable_usd": round(tradeable, 2),
        "spot_leg_usd": round(per_leg, 2),
        "perp_leg_usd": round(per_leg, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic fallback — paper mode
# ─────────────────────────────────────────────────────────────────────────────

def _synthetic_funding_rate(symbol: str) -> float:
    """
    Deterministic funding rate in ±0.00015 range (±0.015% per 8h).
    Seed changes every 8 hours so the rate is stable within a settlement window.

    Typical real-world range: ±0.01–0.1% per 8h.
    """
    window = int(time.time() // 28800)  # 8-hour windows
    seed   = int(hashlib.md5(f"fa{symbol}{window}".encode()).hexdigest()[:8], 16)
    # LCG one step to decorrelate from the seed directly
    state  = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
    # Map [0, 0xFFFFFFFF] → [-0.00015, +0.00015]
    return (state / 0xFFFFFFFF - 0.5) * 0.0003


# ─────────────────────────────────────────────────────────────────────────────
# Internal rate resolver
# ─────────────────────────────────────────────────────────────────────────────

def _get_funding_rate(okx_symbol: str) -> float:
    """
    Returns the current (or annualized-mean) funding rate for an OKX symbol.
    Live if data/feeds/funding_rates.py is importable, else synthetic.

    NOTE: get_all_current_rates() returns annualized yield (rate * 3 * 365).
    For comparison to ENTRY_THRESHOLD (per-period, not annualized) we divide
    the annualized yield back to per-8h:  annual / (3 * 365).
    """
    if _FEEDS_AVAILABLE:
        try:
            rates = _get_live_rates([okx_symbol])
            annual = rates.get(okx_symbol, 0.0)
            return annual / (3 * 365)  # de-annualize to per-8h
        except Exception:
            pass
    return _synthetic_funding_rate(okx_symbol)


# ─────────────────────────────────────────────────────────────────────────────
# Public paper-mode signal function
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_signal(
    pairs: List[str],
    bias:  str = "swing_neutral",
) -> Optional[Tuple[str, str, float, float, float]]:
    """
    Funding rate carry strategy scanner.
    Paper-mode fallback when NautilusTrader engine is not running.

    Returns (pair, side, entry_price, stop_loss, take_profit) or None.

    ``bias`` gates direction:
      "flat"           — no new entries
      "momentum_long"  — long carry (negative funding) only
      "swing_neutral"  — both directions
    """
    if bias == "flat":
        return None

    # Import here (intra-container, always available — same strategies/ dir)
    from strategies.swing_macd import _synthetic_ohlcv  # type: ignore[import]

    for pair in pairs:
        okx_symbol = _PAIR_TO_OKX.get(pair)
        if not okx_symbol:
            continue

        rate = _get_funding_rate(okx_symbol)

        if abs(rate) <= ENTRY_THRESHOLD:
            continue  # funding too low to justify carry trade

        # Get current price from synthetic OHLCV
        ohlcv = _synthetic_ohlcv(pair)
        price = ohlcv[-1][4]  # last close

        if rate > 0:
            # Longs paying → SHORT the perp to collect carry
            if bias == "momentum_long":
                continue  # bias forbids short entries
            sl = price * (1 + SL_PCT)
            tp = price * (1 - TP_PCT)
            logger.debug("funding_arb_short", pair=pair, rate=rate, price=price, sl=sl, tp=tp)
            return (pair, "short", price, sl, tp)

        else:
            # Shorts paying → LONG the perp to collect carry
            sl = price * (1 - SL_PCT)
            tp = price * (1 + TP_PCT)
            logger.debug("funding_arb_long", pair=pair, rate=rate, price=price, sl=sl, tp=tp)
            return (pair, "long", price, sl, tp)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# NautilusTrader Strategy — lazy factory
# ─────────────────────────────────────────────────────────────────────────────

def build_funding_arb_strategy(
    instrument_id:    str   = "BTC-USDT-SWAP.OKX",
    bar_spec:         str   = "8-HOUR-LAST-EXTERNAL",
    entry_threshold:  float = ENTRY_THRESHOLD,
    exit_threshold:   float = EXIT_THRESHOLD,
    stop_loss_pct:    float = SL_PCT,
    take_profit_pct:  float = TP_PCT,
    order_qty_str:    str   = "0.001",
):
    """
    Build and return a FundingArbStrategy instance for NautilusTrader.

    All nautilus_trader imports are inside this function so that importing
    this module never crashes when nautilus_trader is not installed.
    Raises ImportError if nautilus_trader is absent (caller catches it).

    The strategy polls funding data on each 8H bar event (the natural settlement
    window on OKX). It subscribes to 8H bars for bar-event-driven polling rather
    than a custom data type, keeping the engine config simple.
    """
    from nautilus_trader.config import StrategyConfig
    from nautilus_trader.trading.strategy import Strategy
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.objects import Quantity, Price

    bar_type_str = f"{instrument_id}-{bar_spec}"

    class FundingArbConfig(StrategyConfig, frozen=True):
        instrument_id:   str   = instrument_id
        bar_type_str:    str   = bar_type_str
        entry_threshold: float = entry_threshold
        exit_threshold:  float = exit_threshold
        stop_loss_pct:   float = stop_loss_pct
        take_profit_pct: float = take_profit_pct
        order_qty_str:   str   = order_qty_str

    class FundingArbStrategy(Strategy):
        """
        Funding rate carry strategy for NautilusTrader.

        Checks funding rate on each 8H bar.  If abs(rate) > entry_threshold,
        opens a carry position in the direction that receives the payment.
        Closes when rate normalizes toward exit_threshold or SL/TP fires.
        """

        def __init__(self, config: FundingArbConfig):
            super().__init__(config)
            self._iid         = InstrumentId.from_str(config.instrument_id)
            self._btype       = BarType.from_str(config.bar_type_str)
            self._entry_thr   = config.entry_threshold
            self._exit_thr    = config.exit_threshold
            self._sl_pct      = config.stop_loss_pct
            self._tp_pct      = config.take_profit_pct
            self._qty         = Quantity.from_str(config.order_qty_str)
            # OKX symbol (without .OKX suffix) for REST queries
            self._okx_sym = config.instrument_id.replace(".OKX", "")

        def on_start(self) -> None:
            self.subscribe_bars(self._btype)

        def on_bar(self, bar: Bar) -> None:
            rate = _get_funding_rate(self._okx_sym)
            has_pos = bool(self.cache.positions(instrument_id=self._iid))

            if has_pos:
                # Close if rate has normalized
                if abs(rate) <= self._exit_thr:
                    self.close_all_positions(self._iid)
                return

            if abs(rate) <= self._entry_thr:
                return

            price = float(bar.close)
            if rate > 0:
                # SHORT to receive carry
                sl = Price.from_str(f"{price * (1 + self._sl_pct):.2f}")
                tp = Price.from_str(f"{price * (1 - self._tp_pct):.2f}")
                try:
                    order_list = self.order_factory.bracket_market(
                        instrument_id=self._iid,
                        order_side=OrderSide.SELL,
                        quantity=self._qty,
                        sl_trigger_price=sl,
                        tp_price=tp,
                    )
                    self.submit_order_list(order_list)
                except Exception as exc:
                    self.log.warning(f"funding_arb bracket_market failed: {exc}")
            else:
                # LONG to receive carry
                sl = Price.from_str(f"{price * (1 - self._sl_pct):.2f}")
                tp = Price.from_str(f"{price * (1 + self._tp_pct):.2f}")
                try:
                    order_list = self.order_factory.bracket_market(
                        instrument_id=self._iid,
                        order_side=OrderSide.BUY,
                        quantity=self._qty,
                        sl_trigger_price=sl,
                        tp_price=tp,
                    )
                    self.submit_order_list(order_list)
                except Exception as exc:
                    self.log.warning(f"funding_arb bracket_market failed: {exc}")

        def on_stop(self) -> None:
            self.cancel_all_orders(self._iid)
            self.close_all_positions(self._iid)

        def on_dispose(self) -> None:
            pass

    config   = FundingArbConfig()
    strategy = FundingArbStrategy(config)
    return strategy
