"""
workers/nautilus/strategies/range_mean_revert.py

Mean-reversion strategy for ranging markets (ADX < 20).

Entry logic:
  LONG  — close at/below BB lower + RSI oversold + fractal support confirmation
  SHORT — close at/above BB upper + RSI overbought + fractal resistance confirmation
  All 4 conditions must be true simultaneously.

Exit logic:
  Long  — close >= BB middle  OR  RSI >= 55
  Short — close <= BB middle  OR  RSI <= 45

Dead-market filter:
  BB width < 0.02 (2%) → no signal (bands too tight, no mean-reversion edge)

Risk:
  Stop loss  : stop_loss_pct below/above entry
  Take profit: take_profit_ratio × stop_loss_pct (default 1.5:1 R:R)
  Position size: max 25% of allocated_usd per position

Config env vars:
  RANGE_BB_PERIOD=20
  RANGE_BB_STD=2.0
  RANGE_RSI_PERIOD=14
  RANGE_STOP_LOSS_PCT=0.015
  RANGE_TAKE_PROFIT_RATIO=1.5
"""

import logging
import numpy as np
import pandas as pd
from typing import Optional

logger = logging.getLogger(__name__)


class RangeMeanRevertStrategy:

    def __init__(
        self,
        bb_period:          int   = 20,
        bb_std:             float = 2.0,
        rsi_period:         int   = 14,
        fractal_lookback:   int   = 5,
        stop_loss_pct:      float = 0.015,
        take_profit_ratio:  float = 1.5,
    ):
        self.bb_period         = bb_period
        self.bb_std            = bb_std
        self.rsi_period        = rsi_period
        self.fractal_lookback  = fractal_lookback
        self.stop_loss_pct     = stop_loss_pct
        self.take_profit_ratio = take_profit_ratio

    # ── Bollinger Bands ────────────────────────────────────────────────────────

    def calculate_bollinger(self, df: pd.DataFrame) -> pd.DataFrame:
        middle = df["close"].rolling(self.bb_period).mean()
        std    = df["close"].rolling(self.bb_period).std(ddof=0)
        df = df.copy()
        df["bb_upper"]  = middle + self.bb_std * std
        df["bb_lower"]  = middle - self.bb_std * std
        df["bb_middle"] = middle
        df["bb_width"]  = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"].replace(0, np.nan)
        return df

    # ── RSI ───────────────────────────────────────────────────────────────────

    def calculate_rsi(self, df: pd.DataFrame) -> pd.DataFrame:
        """Wilder's RSI — same smoothing method as swing_macd._calculate_rsi."""
        closes = df["close"].to_numpy(dtype=np.float64)
        n      = len(closes)
        p      = self.rsi_period

        df = df.copy()
        if n < p + 1:
            df["rsi"] = 50.0
            return df

        deltas = np.diff(closes)
        gains  = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        avg_gain = np.zeros(n)
        avg_loss = np.zeros(n)

        avg_gain[p] = np.mean(gains[:p])
        avg_loss[p] = np.mean(losses[:p])

        for i in range(p + 1, n):
            avg_gain[i] = (avg_gain[i - 1] * (p - 1) + gains[i - 1]) / p
            avg_loss[i] = (avg_loss[i - 1] * (p - 1) + losses[i - 1]) / p

        with np.errstate(divide="ignore", invalid="ignore"):
            rs  = np.where(avg_loss > 1e-12, avg_gain / avg_loss, 100.0)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi[:p] = 50.0   # pad warmup period

        df["rsi"] = rsi
        return df

    # ── Fractal S/R levels ────────────────────────────────────────────────────

    def identify_fractal_levels(
        self, df: pd.DataFrame
    ) -> tuple:
        """
        Williams 5-bar fractal pattern over the last 20 confirmed candles.
        Returns (fractal_low, fractal_high) as (support, resistance).
        Returns (None, None) if no fractal found in the lookback window.

        Confirmed candles = df[:-2] (last 2 bars may be still-forming).
        """
        confirmed = df.iloc[:-2] if len(df) > 2 else df
        highs = confirmed["high"].to_numpy(dtype=np.float64)
        lows  = confirmed["low"].to_numpy(dtype=np.float64)

        lookback  = 20
        start_idx = max(2, len(highs) - lookback - 2)
        window_h  = highs[start_idx:]
        window_l  = lows[start_idx:]

        fractal_low  = None
        fractal_high = None

        for i in range(2, len(window_h) - 2):
            # Bearish fractal (swing high)
            if (window_h[i] > window_h[i - 1] and window_h[i] > window_h[i - 2]
                    and window_h[i] > window_h[i + 1] and window_h[i] > window_h[i + 2]):
                fractal_high = float(window_h[i])

            # Bullish fractal (swing low)
            if (window_l[i] < window_l[i - 1] and window_l[i] < window_l[i - 2]
                    and window_l[i] < window_l[i + 1] and window_l[i] < window_l[i + 2]):
                fractal_low = float(window_l[i])

        return fractal_low, fractal_high

    # ── Signal generation ─────────────────────────────────────────────────────

    def generate_signals(
        self,
        df:            pd.DataFrame,
        symbol:        str,
        allocated_usd: float,
        paper_trading: bool,
    ) -> list:
        """
        Generate BUY, SELL, or CLOSE signals for a ranging market.

        Entry conditions:
          LONG  : ADX < 20 + close <= bb_lower + RSI <= 30 + close within 0.5% of fractal_low
          SHORT : ADX < 20 + close >= bb_upper + RSI >= 70 + close within 0.5% of fractal_high

        Dead-market filter: bb_width < 0.02 → return []

        Returns list of signal dicts (empty if no conditions met).
        """
        min_bars = self.bb_period + self.rsi_period + 5
        if len(df) < min_bars:
            logger.debug(
                f"range_mean_revert: not enough bars for {symbol} "
                f"({len(df)} < {min_bars})"
            )
            return []

        df = self.calculate_bollinger(df)
        df = self.calculate_rsi(df)

        last       = df.iloc[-1]
        close      = float(last["close"])
        bb_upper   = float(last["bb_upper"])
        bb_lower   = float(last["bb_lower"])
        bb_middle  = float(last["bb_middle"])
        bb_width   = float(last["bb_width"]) if not np.isnan(last["bb_width"]) else 0.0
        rsi        = float(last["rsi"])

        adx_value = float(last["adx"]) if "adx" in df.columns else 0.0

        # Dead-market filter
        if bb_width < 0.02:
            logger.debug(
                f"range_mean_revert: {symbol} bb_width={bb_width:.4f} too tight — no edge"
            )
            return []

        fractal_low, fractal_high = self.identify_fractal_levels(df)
        size_usd = allocated_usd * 0.25

        # ── LONG (buy at support) ─────────────────────────────────────────────
        long_fractal_ok = (
            fractal_low is not None
            and abs(close - fractal_low) / max(fractal_low, 1e-12) <= 0.005
        )
        if (close <= bb_lower and rsi <= 30 and long_fractal_ok):
            sl = close * (1 - self.stop_loss_pct)
            tp = close + (close - sl) * self.take_profit_ratio
            logger.info(
                f"range_mean_revert LONG: {symbol} @ {close:.4f} | "
                f"bb_lower={bb_lower:.4f} rsi={rsi:.1f} fractal_low={fractal_low:.4f} "
                f"bb_width={bb_width:.4f} sl={sl:.4f} tp={tp:.4f}"
            )
            return [{
                "symbol":        symbol,
                "action":        "BUY",
                "strategy":      "range_mean_revert",
                "entry_price":   close,
                "stop_loss":     sl,
                "take_profit":   tp,
                "size_usd":      size_usd,
                "advisory_only": paper_trading,
                "rationale":     "BB lower + RSI oversold + fractal support",
                "adx_value":     adx_value,
                "bb_width":      bb_width,
                "rsi_value":     rsi,
            }]

        # ── SHORT (sell at resistance) ────────────────────────────────────────
        short_fractal_ok = (
            fractal_high is not None
            and abs(close - fractal_high) / max(fractal_high, 1e-12) <= 0.005
        )
        if (close >= bb_upper and rsi >= 70 and short_fractal_ok):
            sl = close * (1 + self.stop_loss_pct)
            tp = close - (sl - close) * self.take_profit_ratio
            logger.info(
                f"range_mean_revert SHORT: {symbol} @ {close:.4f} | "
                f"bb_upper={bb_upper:.4f} rsi={rsi:.1f} fractal_high={fractal_high:.4f} "
                f"bb_width={bb_width:.4f} sl={sl:.4f} tp={tp:.4f}"
            )
            return [{
                "symbol":        symbol,
                "action":        "SELL",
                "strategy":      "range_mean_revert",
                "entry_price":   close,
                "stop_loss":     sl,
                "take_profit":   tp,
                "size_usd":      size_usd,
                "advisory_only": paper_trading,
                "rationale":     "BB upper + RSI overbought + fractal resistance",
                "adx_value":     adx_value,
                "bb_width":      bb_width,
                "rsi_value":     rsi,
            }]

        # ── CLOSE (exit mean-reversion position) ──────────────────────────────
        long_exit  = close >= bb_middle or rsi >= 55
        short_exit = close <= bb_middle or rsi <= 45

        if long_exit or short_exit:
            logger.debug(
                f"range_mean_revert CLOSE signal: {symbol} close={close:.4f} "
                f"bb_mid={bb_middle:.4f} rsi={rsi:.1f}"
            )
            return [{
                "symbol":        symbol,
                "action":        "CLOSE",
                "strategy":      "range_mean_revert",
                "entry_price":   close,
                "stop_loss":     close,
                "take_profit":   close,
                "size_usd":      0.0,
                "advisory_only": paper_trading,
                "rationale":     "Mean reversion target reached (BB middle or RSI neutral)",
                "adx_value":     adx_value,
                "bb_width":      bb_width,
                "rsi_value":     rsi,
            }]

        return []
