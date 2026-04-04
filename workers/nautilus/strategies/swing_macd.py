"""
workers/swing_trend.py

Delta-Directional Swing Trading Worker.

Strategy: MACD + Williams Fractals dual-confirmation
─────────────────────────────────────────────────────
Williams Fractals define high-probability swing structure (support / resistance).
MACD confirms that momentum is aligned with the fractal signal before entering.
RSI acts as a third gate — filters entries in overbought / oversold extremes.

Using only 3 signals deliberately (per the strategy spec — "avoid 2+ signals").

Entry rules — LONG
  1. Confirmed bullish fractal within the last 15 bars (structural support nearby)
  2. MACD bullish crossover within the last 3 bars (momentum trigger)
  3. MACD line > 0 (macro uptrend filter — don't buy into a downtrend)
  4. RSI in 40–70 (momentum building but not overextended)

Entry rules — SHORT
  1. Confirmed bearish fractal within the last 15 bars (structural resistance)
  2. MACD bearish crossover within the last 3 bars
  3. MACD line < 0
  4. RSI in 30–60

Exit rules (first condition hit wins)
  - Hard stop loss   : 2 % beyond the fractal level that triggered entry
  - Take profit      : 2× the stop distance (1:2 R:R)
  - MACD reversal    : MACD crosses against the position after ≥ 2 hold cycles
  - Trailing stop    : once price hits 1:1 R:R, stop moves to breakeven

Timeframe: H4 (4-hour candles) — best signal-to-noise for 1–3 day swings.
MACD settings: (8, 21, 5) — tuned for short-term crypto swings per spec.

Paper trading uses deterministic synthetic OHLCV so indicators are stable
across cycles within the same 4-hour window.  Set USE_LIVE_OHLCV = True in
config.py to pull real Binance candles via ccxt.
"""

import time
import logging
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Any, Tuple

import config
from workers.base_worker import BaseWorker
from core.portfolio_state import PortfolioState, Position, PositionSide, WorkerType

try:
    from indicators.adx import AdxCalculator as _AdxCalculator
    _adx_calc = _AdxCalculator(period=14)
except ImportError:
    _adx_calc = None

logger = logging.getLogger(__name__)


class SwingTrendWorker(BaseWorker):
    """
    MACD + Williams Fractals swing-trend worker.

    State owned by this worker (Hypervisor does not touch these directly):
        _current_pair      — ticker currently in play, or None
        _current_side      — LONG / SHORT
        _deployed_capital  — USD committed to this trade
        _entry_price       — price at which the position was opened
        _stop_loss         — hard stop level (moves to breakeven once 1:1 is reached)
        _take_profit       — profit target level
        _entry_fractal     — fractal level that triggered entry (reference)
        _hold_cycles       — how many execute() calls this position has seen
        _ohlcv_cache       — {pair: {"ohlcv": [...], "fetched_at": float}}
    """

    def __init__(self):
        super().__init__(name="SwingTrendWorker")

        # MACD parameters (short-term swing setting per spec)
        self._macd_fast:   int = config.SWING_MACD_FAST
        self._macd_slow:   int = config.SWING_MACD_SLOW
        self._macd_signal: int = config.SWING_MACD_SIGNAL

        # Active position state
        self._current_pair:     Optional[str]          = None
        self._current_side:     Optional[PositionSide] = None
        self._deployed_capital: float                  = 0.0
        self._entry_price:      Optional[float]        = None
        self._stop_loss:        Optional[float]        = None
        self._take_profit:      Optional[float]        = None
        self._entry_fractal:    Optional[float]        = None
        self._hold_cycles:      int                    = 0

        # OHLCV cache — refreshed every SWING_CACHE_TTL seconds
        self._ohlcv_cache:      Dict[str, dict]        = {}
        self._cache_ttl:        float                  = config.SWING_CACHE_TTL_SEC

        # ADX state — set by _evaluate_setup, read by _enter_position
        self._last_adx_value:   float                  = 0.0
        self._last_adx_state:   str                    = "unknown"

    # ─────────────────────────────────────────────────────────────────────────
    # BaseWorker Interface
    # ─────────────────────────────────────────────────────────────────────────

    def execute(
        self,
        capital:         float,
        portfolio_state: PortfolioState,
        paper_trading:   bool,
    ) -> Optional[Dict[str, Any]]:
        """
        Main cycle. Two modes:
          A) Position open  → manage it (stop / take-profit / MACD reversal)
          B) No position    → scan all configured pairs for a fresh setup
        """
        # ── Case A: manage existing position ──────────────────────────────────
        if self._current_pair is not None:
            return self._manage_position(portfolio_state, paper_trading)

        # ── Case B: scan for a new entry ──────────────────────────────────────
        # Guard: no new directional positions during emergency or low capital
        if portfolio_state.emergency_mode:
            logger.warning("SwingTrend: Hypervisor in emergency mode — suppressing new entries")
            return None

        if capital < config.MIN_TRADE_SIZE_USD:
            logger.info("SwingTrend: allocated capital too small — skipping scan")
            return None

        setup = self._scan_for_setup(paper_trading)
        if setup is None:
            logger.info("SwingTrend: no qualifying setup found this cycle")
            return None

        pair, side, entry_price, stop_loss, take_profit, fractal_level = setup
        return self._enter_position(
            pair, side, entry_price, stop_loss, take_profit,
            fractal_level, capital, portfolio_state, paper_trading,
        )

    def close_position(
        self,
        position_key: str,
        portfolio_state: PortfolioState,
        paper_trading: bool,
    ) -> Optional[float]:
        """
        Emergency close called by the Hypervisor (risk-gate breach or shutdown).
        Closes the position at current market price, records P&L.
        """
        pos = portfolio_state.positions.get(position_key)
        if pos is None:
            return None

        current_price = self._get_current_price(pos.ticker, paper_trading)
        pnl = portfolio_state.close_position(position_key, current_price)

        if pnl is not None:
            portfolio_state.deallocate_from_worker(WorkerType.SWING_TREND, pos.size_usd, pnl)
            return_pct = pnl / pos.size_usd if pos.size_usd > 0 else 0.0
            self.record_return(return_pct)
            portfolio_state.record_hourly_return(return_pct)

        # Clear internal state only if this key belongs to our current pair
        if self._current_pair and self._current_pair in position_key:
            self._reset_state()

        return pnl

    def get_market_data(self) -> Dict[str, Any]:
        """
        Return latest close/high/low for all configured swing pairs.
        Called by Hypervisor for monitoring; does not trigger a trade.
        """
        snapshot = {}
        for pair in config.SWING_PAIRS:
            ohlcv = self._get_ohlcv(pair)
            if ohlcv and len(ohlcv) > 0:
                snapshot[pair] = {
                    "close": ohlcv[-1][4],
                    "high":  ohlcv[-1][2],
                    "low":   ohlcv[-1][3],
                    "bars":  len(ohlcv),
                }
        return snapshot

    # ─────────────────────────────────────────────────────────────────────────
    # Setup Scanning
    # ─────────────────────────────────────────────────────────────────────────

    def _scan_for_setup(self, paper_trading: bool) -> Optional[Tuple]:
        """
        Iterate over all SWING_PAIRS.  Evaluate each for entry conditions.
        Returns the strongest qualifying setup as a tuple, or None.
        """
        best_setup    = None
        best_strength = -1.0

        for pair in config.SWING_PAIRS:
            ohlcv = self._get_ohlcv(pair)
            if ohlcv is None or len(ohlcv) < self._macd_slow + 15:
                logger.debug(f"SwingTrend: not enough bars for {pair} ({len(ohlcv) if ohlcv else 0})")
                continue

            result = self._evaluate_setup(pair, ohlcv)
            if result is None:
                continue

            pair_out, side, entry, sl, tp, fractal, strength = result
            if strength > best_strength:
                best_strength = strength
                best_setup    = (pair_out, side, entry, sl, tp, fractal)

        return best_setup

    def _evaluate_setup(self, pair: str, ohlcv: List) -> Optional[Tuple]:
        """
        Full indicator suite for one pair.
        Returns (pair, side, entry_price, stop_loss, take_profit, fractal_level, strength)
        or None if conditions are not met.
        """
        closes = np.array([c[4] for c in ohlcv], dtype=np.float64)
        highs  = np.array([c[2] for c in ohlcv], dtype=np.float64)
        lows   = np.array([c[3] for c in ohlcv], dtype=np.float64)

        # ── ADX gate — suppress signals in ranging/ambiguous markets ─────────
        adx_value = 0.0
        adx_state = "unknown"
        if _adx_calc is not None and len(ohlcv) >= 30:
            try:
                opens = np.array([c[1] for c in ohlcv], dtype=np.float64)
                vols  = np.array([c[5] for c in ohlcv], dtype=np.float64)
                df_adx = pd.DataFrame({
                    "open": opens, "high": highs, "low": lows,
                    "close": closes, "volume": vols,
                })
                df_adx = _adx_calc.calculate(df_adx)
                adx_value = float(df_adx["adx"].iloc[-1])
                adx_state = _adx_calc.classify(adx_value)
            except Exception as exc:
                logger.debug(f"SwingTrend: ADX calculation failed for {pair}: {exc}")
                adx_state = "unknown"

            if adx_state in ("ranging", "ambiguous"):
                logger.debug(
                    f"SwingTrend: ADX gate suppressed signal for {pair} "
                    f"(adx={adx_value:.1f}, state={adx_state})"
                )
                return None

        self._last_adx_value = adx_value
        self._last_adx_state = adx_state

        # ── Indicators ────────────────────────────────────────────────────────
        macd_line, signal_line, histogram = self._calculate_macd(
            closes, self._macd_fast, self._macd_slow, self._macd_signal
        )
        rsi = self._calculate_rsi(closes, config.SWING_RSI_PERIOD)
        current_rsi = float(rsi[-1]) if rsi is not None else 50.0

        # Fractals need 2 confirmed bars to the right — exclude the last 2 bars
        confirmed_highs = highs[:-2]
        confirmed_lows  = lows[:-2]
        bull_fractals, bear_fractals = self._find_fractals(confirmed_highs, confirmed_lows)

        current_price = float(closes[-1])
        current_macd  = float(macd_line[-1])

        # ── Long Setup ────────────────────────────────────────────────────────
        if self._check_long_conditions(
            macd_line, signal_line, current_rsi, bull_fractals, confirmed_lows
        ):
            fractal_idx   = bull_fractals[-1]
            fractal_level = float(confirmed_lows[fractal_idx])
            stop_loss     = fractal_level * (1 - config.SWING_STOP_LOSS_PCT)
            risk_per_unit = current_price - stop_loss
            if risk_per_unit <= 0:
                return None
            take_profit = current_price + risk_per_unit * config.SWING_TAKE_PROFIT_RATIO

            # Strength = MACD as % of price (dimensionless, comparable across assets)
            # + RSI position within target zone
            macd_pct   = abs(current_macd) / (current_price + 1e-12)  # 0→~0.01 range
            rsi_score  = (current_rsi - 30) / 40  # 0 → 1 from RSI 30 to 70
            strength   = macd_pct * 100 + max(0.0, rsi_score)

            logger.info(
                f"SwingTrend LONG signal: {pair} @ {current_price:.4f} | "
                f"SL: {stop_loss:.4f} | TP: {take_profit:.4f} | "
                f"MACD: {current_macd:+.4f} | RSI: {current_rsi:.1f} | "
                f"Fractal support: {fractal_level:.4f}"
            )
            return (pair, PositionSide.LONG, current_price,
                    stop_loss, take_profit, fractal_level, strength)

        # ── Short Setup ───────────────────────────────────────────────────────
        if self._check_short_conditions(
            macd_line, signal_line, current_rsi, bear_fractals, confirmed_highs
        ):
            fractal_idx   = bear_fractals[-1]
            fractal_level = float(confirmed_highs[fractal_idx])
            stop_loss     = fractal_level * (1 + config.SWING_STOP_LOSS_PCT)
            risk_per_unit = stop_loss - current_price
            if risk_per_unit <= 0:
                return None
            take_profit = current_price - risk_per_unit * config.SWING_TAKE_PROFIT_RATIO

            macd_pct   = abs(current_macd) / (current_price + 1e-12)
            rsi_score  = (60 - current_rsi) / 30
            strength   = macd_pct * 100 + max(0.0, rsi_score)

            logger.info(
                f"SwingTrend SHORT signal: {pair} @ {current_price:.4f} | "
                f"SL: {stop_loss:.4f} | TP: {take_profit:.4f} | "
                f"MACD: {current_macd:+.4f} | RSI: {current_rsi:.1f} | "
                f"Fractal resistance: {fractal_level:.4f}"
            )
            return (pair, PositionSide.SHORT, current_price,
                    stop_loss, take_profit, fractal_level, strength)

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Entry Condition Checkers
    # ─────────────────────────────────────────────────────────────────────────

    def _check_long_conditions(
        self,
        macd_line:    np.ndarray,
        signal_line:  np.ndarray,
        rsi:          float,
        bull_fractals: List[int],
        confirmed_lows: np.ndarray,
    ) -> bool:
        """
        All four conditions must be true simultaneously.

        1. Recent bullish fractal   — structural support is close
        2. MACD bullish crossover   — momentum is turning up RIGHT NOW
        3. MACD line > 0            — macro direction is bullish (zero-line filter)
        4. RSI in 40–70             — not overbought, momentum is building
        """
        # Condition 1: fractal within the last 15 bars
        if not bull_fractals:
            return False
        if len(confirmed_lows) - bull_fractals[-1] > 15:
            return False

        # Condition 2: crossover in the last 3 bars
        # macd crossed from below to above signal line
        crossed_bullish = False
        for i in range(1, min(4, len(macd_line))):
            prev_below = macd_line[-(i+1)] <= signal_line[-(i+1)]
            now_above  = macd_line[-i]     >  signal_line[-i]
            if prev_below and now_above:
                crossed_bullish = True
                break
        if not crossed_bullish:
            return False

        # Condition 3: MACD above zero (trend filter)
        if macd_line[-1] <= 0:
            return False

        # Condition 4: RSI zone
        if not (config.SWING_RSI_BULL_MIN <= rsi <= 70):
            return False

        return True

    def _check_short_conditions(
        self,
        macd_line:    np.ndarray,
        signal_line:  np.ndarray,
        rsi:          float,
        bear_fractals: List[int],
        confirmed_highs: np.ndarray,
    ) -> bool:
        """
        Mirror of _check_long_conditions for short entries.
        """
        if not bear_fractals:
            return False
        if len(confirmed_highs) - bear_fractals[-1] > 15:
            return False

        crossed_bearish = False
        for i in range(1, min(4, len(macd_line))):
            prev_above = macd_line[-(i+1)] >= signal_line[-(i+1)]
            now_below  = macd_line[-i]     <  signal_line[-i]
            if prev_above and now_below:
                crossed_bearish = True
                break
        if not crossed_bearish:
            return False

        if macd_line[-1] >= 0:
            return False

        if not (30 <= rsi <= config.SWING_RSI_BEAR_MAX):
            return False

        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Position Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def _enter_position(
        self,
        pair:            str,
        side:            PositionSide,
        entry_price:     float,
        stop_loss:       float,
        take_profit:     float,
        fractal_level:   float,
        capital:         float,
        portfolio_state: PortfolioState,
        paper_trading:   bool,
    ) -> Dict[str, Any]:
        """Open a directional swing position and record all entry metadata."""
        exchange_label = "okx" if getattr(config, "USE_LIVE_OHLCV", False) else "paper"
        pos = Position(
            ticker      = pair,
            side        = side,
            size_usd    = capital,
            entry_price = entry_price,
            exchange    = exchange_label,
            worker      = WorkerType.SWING_TREND,
        )
        portfolio_state.open_position(pos)

        self._current_pair     = pair
        self._current_side     = side
        self._deployed_capital = capital
        self._entry_price      = entry_price
        self._stop_loss        = stop_loss
        self._take_profit      = take_profit
        self._entry_fractal    = fractal_level
        self._hold_cycles      = 0

        risk_usd   = capital * abs(entry_price - stop_loss)   / entry_price
        reward_usd = capital * abs(take_profit - entry_price) / entry_price
        rr_ratio   = reward_usd / risk_usd if risk_usd > 0 else 0.0

        logger.info(
            f"SwingTrend OPENED {side.value.upper()} {pair} | "
            f"Entry: {entry_price:.4f} | SL: {stop_loss:.4f} | TP: {take_profit:.4f} | "
            f"Risk: ${risk_usd:.2f} → Reward: ${reward_usd:.2f} (R:R {rr_ratio:.1f})"
        )
        return {
            "action":      "opened",
            "pair":        pair,
            "side":        side.value,
            "entry_price": entry_price,
            "stop_loss":   stop_loss,
            "take_profit": take_profit,
            "risk_usd":    round(risk_usd, 2),
            "reward_usd":  round(reward_usd, 2),
            "rr_ratio":    round(rr_ratio, 2),
            "adx_value":   round(self._last_adx_value, 2),
            "adx_state":   self._last_adx_state,
        }

    def _manage_position(
        self,
        portfolio_state: PortfolioState,
        paper_trading:   bool,
    ) -> Dict[str, Any]:
        """
        Called every cycle while a position is open.
        Priority order for exits: stop-loss → take-profit → MACD reversal.
        After 1:1 R:R is reached, stop is trailed to breakeven.
        """
        self._hold_cycles += 1
        current_price = self._get_current_price(self._current_pair, paper_trading)
        entry         = self._entry_price or current_price

        if self._current_side == PositionSide.LONG:
            unrealised_pct = (current_price - entry) / entry * 100
        else:
            unrealised_pct = (entry - current_price) / entry * 100

        exit_reason: Optional[str] = None

        # ── Hard stop loss ────────────────────────────────────────────────────
        if self._stop_loss is not None:
            if self._current_side == PositionSide.LONG  and current_price <= self._stop_loss:
                exit_reason = "stop_loss"
            elif self._current_side == PositionSide.SHORT and current_price >= self._stop_loss:
                exit_reason = "stop_loss"

        # ── Take profit ───────────────────────────────────────────────────────
        if exit_reason is None and self._take_profit is not None:
            if self._current_side == PositionSide.LONG  and current_price >= self._take_profit:
                exit_reason = "take_profit"
            elif self._current_side == PositionSide.SHORT and current_price <= self._take_profit:
                exit_reason = "take_profit"

        # ── MACD reversal check (skip first 2 cycles to avoid noise) ─────────
        if exit_reason is None and self._hold_cycles >= 2:
            ohlcv = self._get_ohlcv(self._current_pair)
            if ohlcv and len(ohlcv) >= self._macd_slow + 5:
                closes = np.array([c[4] for c in ohlcv], dtype=np.float64)
                macd, sig, _ = self._calculate_macd(
                    closes, self._macd_fast, self._macd_slow, self._macd_signal
                )
                if (self._current_side == PositionSide.LONG
                        and macd[-2] >= sig[-2] and macd[-1] < sig[-1]):
                    exit_reason = "macd_reversal"
                elif (self._current_side == PositionSide.SHORT
                        and macd[-2] <= sig[-2] and macd[-1] > sig[-1]):
                    exit_reason = "macd_reversal"

        # ── Exit if triggered ─────────────────────────────────────────────────
        if exit_reason:
            return self._exit_position(portfolio_state, paper_trading, exit_reason, current_price)

        # ── Trail stop to breakeven once 1:1 R:R is reached ──────────────────
        if self._stop_loss is not None and self._entry_price is not None:
            if self._current_side == PositionSide.LONG:
                initial_risk = self._entry_price - self._stop_loss
                if initial_risk > 0 and current_price >= self._entry_price + initial_risk:
                    new_stop = self._entry_price  # Move stop to breakeven
                    if new_stop > self._stop_loss:
                        old_stop = self._stop_loss
                        self._stop_loss = new_stop
                        logger.info(
                            f"SwingTrend [{self._current_pair}] LONG trailing stop "
                            f"moved to breakeven: {old_stop:.4f} → {new_stop:.4f}"
                        )
            elif self._current_side == PositionSide.SHORT:
                initial_risk = self._stop_loss - self._entry_price
                if initial_risk > 0 and current_price <= self._entry_price - initial_risk:
                    new_stop = self._entry_price  # Move stop to breakeven
                    if new_stop < self._stop_loss:  # Only tighten (lower) for shorts
                        old_stop = self._stop_loss
                        self._stop_loss = new_stop
                        logger.info(
                            f"SwingTrend [{self._current_pair}] SHORT trailing stop "
                            f"moved to breakeven: {old_stop:.4f} → {new_stop:.4f}"
                        )

        logger.info(
            f"SwingTrend HOLD {self._current_side.value.upper()} "
            f"{self._current_pair} | price: {current_price:.4f} | "
            f"P&L: {unrealised_pct:+.2f}% | cycle {self._hold_cycles} | "
            f"SL: {self._stop_loss:.4f} | TP: {self._take_profit:.4f}"
        )
        return {
            "action":        "hold",
            "pair":          self._current_pair,
            "side":          self._current_side.value,
            "price":         current_price,
            "unrealised_pct": round(unrealised_pct, 2),
            "hold_cycles":   self._hold_cycles,
            "stop_loss":     self._stop_loss,
            "take_profit":   self._take_profit,
        }

    def _exit_position(
        self,
        portfolio_state: PortfolioState,
        paper_trading:   bool,
        reason:          str,
        exit_price:      float,
    ) -> Dict[str, Any]:
        """Close all open legs for the current pair and record P&L."""
        keys_to_close = [
            k for k, p in portfolio_state.positions.items()
            if self._current_pair and self._current_pair in k
               and p.worker == WorkerType.SWING_TREND
        ]

        total_pnl       = 0.0
        total_size_usd  = 0.0
        for key in keys_to_close:
            pos = portfolio_state.positions.get(key)
            if pos:
                pnl = portfolio_state.close_position(key, exit_price)
                if pnl is not None:
                    total_pnl      += pnl
                    total_size_usd += pos.size_usd
                    portfolio_state.deallocate_from_worker(
                        WorkerType.SWING_TREND, pos.size_usd, pnl
                    )

        return_pct = total_pnl / self._deployed_capital if self._deployed_capital > 0 else 0.0
        self.record_return(return_pct)
        portfolio_state.record_hourly_return(return_pct)

        logger.info(
            f"SwingTrend CLOSED [{reason}] {self._current_pair} @ {exit_price:.4f} | "
            f"Net P&L: ${total_pnl:+.4f} ({return_pct*100:+.2f}%) | "
            f"Held {self._hold_cycles} cycles"
        )

        result = {
            "action":      "closed",
            "reason":      reason,
            "pair":        self._current_pair,
            "side":        self._current_side.value if self._current_side else None,
            "exit_price":  exit_price,
            "net_pnl":     round(total_pnl, 4),
            "return_pct":  round(return_pct * 100, 2),
            "hold_cycles": self._hold_cycles,
        }
        self._reset_state()
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Technical Indicators
    # ─────────────────────────────────────────────────────────────────────────

    def _calculate_macd(
        self,
        closes: np.ndarray,
        fast:   int,
        slow:   int,
        signal: int,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        MACD line = EMA(fast) − EMA(slow)
        Signal    = EMA(signal) of MACD line
        Histogram = MACD − Signal
        """
        ema_fast    = self._ema(closes, fast)
        ema_slow    = self._ema(closes, slow)
        macd_line   = ema_fast - ema_slow
        signal_line = self._ema(macd_line, signal)
        histogram   = macd_line - signal_line
        return macd_line, signal_line, histogram

    def _ema(self, data: np.ndarray, period: int) -> np.ndarray:
        """Standard EMA with multiplier k = 2 / (period + 1)."""
        k      = 2.0 / (period + 1)
        result = np.empty_like(data)
        result[0] = data[0]
        for i in range(1, len(data)):
            result[i] = data[i] * k + result[i - 1] * (1.0 - k)
        return result

    def _calculate_rsi(
        self, closes: np.ndarray, period: int = 14
    ) -> Optional[np.ndarray]:
        """
        Wilder's RSI using Smoothed Moving Average for avg gain / avg loss.
        Returns None if there are insufficient bars.
        """
        if len(closes) < period + 1:
            return None

        deltas = np.diff(closes)
        gains  = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        avg_gain = np.zeros(len(closes))
        avg_loss = np.zeros(len(closes))

        avg_gain[period] = np.mean(gains[:period])
        avg_loss[period] = np.mean(losses[:period])

        for i in range(period + 1, len(closes)):
            avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i - 1]) / period
            avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i - 1]) / period

        with np.errstate(divide="ignore", invalid="ignore"):
            rs  = np.where(avg_loss > 1e-12, avg_gain / avg_loss, 100.0)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi[:period] = 50.0  # Pad warmup period with neutral value
        return rsi

    def _find_fractals(
        self,
        highs: np.ndarray,
        lows:  np.ndarray,
    ) -> Tuple[List[int], List[int]]:
        """
        Williams Fractals — 5-bar reversal pattern.

        A bullish fractal at index i means:
            lows[i] < lows[i-2], lows[i-1], lows[i+1], lows[i+2]
            (i.e. it is the lowest of the 5-bar window centred on i)

        A bearish fractal at index i means:
            highs[i] > highs[i-2], highs[i-1], highs[i+1], highs[i+2]

        The caller must pass confirmed_highs / confirmed_lows (i.e. ohlcv[:-2])
        so that we are not calling fractals on still-forming bars.
        """
        bull_fractals: List[int] = []
        bear_fractals: List[int] = []

        for i in range(2, len(highs) - 2):
            # Bearish fractal — swing high
            if (highs[i] > highs[i - 1] and highs[i] > highs[i - 2]
                    and highs[i] > highs[i + 1] and highs[i] > highs[i + 2]):
                bear_fractals.append(i)

            # Bullish fractal — swing low
            if (lows[i] < lows[i - 1] and lows[i] < lows[i - 2]
                    and lows[i] < lows[i + 1] and lows[i] < lows[i + 2]):
                bull_fractals.append(i)

        return bull_fractals, bear_fractals

    # ─────────────────────────────────────────────────────────────────────────
    # OHLCV Data Layer
    # ─────────────────────────────────────────────────────────────────────────

    def _get_ohlcv(self, pair: str, n_bars: int = 200) -> Optional[List]:
        """
        Return OHLCV for one pair, using an in-memory cache.
        Cache is refreshed at SWING_CACHE_TTL_SEC intervals (default 4 h).
        """
        now   = time.time()
        cache = self._ohlcv_cache.get(pair)
        if cache and (now - cache["fetched_at"]) < self._cache_ttl:
            return cache["ohlcv"]

        ohlcv = (
            self._fetch_live_ohlcv(pair, config.SWING_TIMEFRAME, n_bars)
            if getattr(config, "USE_LIVE_OHLCV", False)
            else self._simulate_ohlcv(pair, n_bars)
        )

        if ohlcv:
            self._ohlcv_cache[pair] = {"ohlcv": ohlcv, "fetched_at": now}

        return ohlcv

    def _fetch_live_ohlcv(self, pair: str, timeframe: str, limit: int) -> Optional[List]:
        """
        Fetch real candles from OKX via ccxt.

        OKX is used instead of Binance — Binance returns HTTP 451 for US IPs.
        OKX spot symbol format matches our internal format ("BTC/USDT") directly.
        Requires: pip install ccxt
        """
        try:
            import ccxt
            exchange = ccxt.okx()
            ohlcv    = exchange.fetch_ohlcv(pair, timeframe, limit=limit)
            logger.info(f"SwingTrend: fetched {len(ohlcv)} {timeframe} bars for {pair} via OKX")
            return ohlcv
        except ImportError:
            logger.warning("ccxt not installed — falling back to simulated OHLCV")
        except Exception as exc:
            logger.warning(f"SwingTrend: live OHLCV fetch failed for {pair}: {exc} — simulating")
        return self._simulate_ohlcv(pair, limit)

    def _simulate_ohlcv(self, pair: str, n_bars: int = 200) -> List:
        """
        Synthetic H4 OHLCV using Geometric Brownian Motion.

        Design choices:
          - Sigma 0.8 % per 4h bar is a realistic crypto volatility floor
          - A mild random drift ∈ {-0.01 %, 0 %, +0.02 %} creates trending
            periods so MACD signals can form naturally
          - Bar OHLC is constructed from the close + a randomly scaled range,
            keeping the resulting candles visually plausible
          - The random state is seeded from the pair name so results are
            repeatable within the current cache TTL window
        """
        base_prices = {
            "BTC/USDT":  65_000.0,
            "ETH/USDT":  3_500.0,
            "SOL/USDT":  150.0,
            "BNB/USDT":  580.0,
            "ARB/USDT":  1.20,
            "AVAX/USDT": 35.0,
            "DOGE/USDT": 0.15,
            "LINK/USDT": 15.0,
        }
        base = base_prices.get(pair, 100.0)

        # Local seeded RNG — deterministic per pair, stable for this cache window
        rng = np.random.RandomState(
            (hash(pair) + int(time.time() // self._cache_ttl)) % (2 ** 31)
        )
        drift = rng.choice([-0.0001, 0.0, 0.0001, 0.0002])
        sigma = 0.008

        shocks = rng.normal(drift, sigma, n_bars)
        closes = [base]
        for s in shocks:
            closes.append(closes[-1] * (1 + s))
        closes = closes[1:]

        ohlcv = []
        ts_start = int(time.time()) - n_bars * 14_400  # 4 h = 14 400 s
        for i, close in enumerate(closes):
            bar_range = close * rng.uniform(0.005, 0.02)
            high      = close + bar_range * rng.uniform(0.3, 0.7)
            low       = close - bar_range * rng.uniform(0.3, 0.7)
            open_     = closes[i - 1] if i > 0 else close
            volume    = rng.uniform(1e6, 5e7)
            ohlcv.append([ts_start + i * 14_400, open_, high, low, close, volume])

        return ohlcv

    # ─────────────────────────────────────────────────────────────────────────
    # Utilities
    # ─────────────────────────────────────────────────────────────────────────

    def _get_current_price(self, pair: str, paper_trading: bool) -> float:
        """Return the latest close price for a pair from the OHLCV cache."""
        ohlcv = self._get_ohlcv(pair)
        if ohlcv:
            return float(ohlcv[-1][4])
        return self._entry_price or 1.0

    def _reset_state(self):
        """Clear all position-tracking state after an exit."""
        self._current_pair     = None
        self._current_side     = None
        self._deployed_capital = 0.0
        self._entry_price      = None
        self._stop_loss        = None
        self._take_profit      = None
        self._entry_fractal    = None
        self._hold_cycles      = 0
