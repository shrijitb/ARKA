"""
workers/nautilus/indicators/adx.py

Pure Python ADX (Average Directional Index) calculator.
Uses Wilder's smoothing — no TA-Lib dependency.
Requires only pandas and numpy (already in requirements.txt).

Wilder's smoothing differs from standard EMA:
  smoothed[0] = sum of first `period` raw values
  smoothed[i] = smoothed[i-1] - (smoothed[i-1] / period) + current[i]

ADX interpretation:
  >= 25  → trending market   (use swing_macd)
  <= 20  → ranging market    (use range_mean_revert)
  21–24  → ambiguous         (no signal — wait for confirmation)
"""

import numpy as np
import pandas as pd


class AdxCalculator:
    """
    Calculates ADX, +DI, and -DI from OHLCV data.

    Usage:
        calc = AdxCalculator(period=14)
        df_out = calc.calculate(df)          # adds adx, plus_di, minus_di, tr columns
        state  = calc.classify(df_out["adx"].iloc[-1])   # "trending"|"ranging"|"ambiguous"
    """

    def __init__(self, period: int = 14):
        self.period = period

    # ── Core calculation ───────────────────────────────────────────────────────

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Input:  DataFrame with columns [open, high, low, close, volume], ascending index.
        Output: Same DataFrame extended with [adx, plus_di, minus_di, tr] columns.
        Requires at least 2 * period + 1 rows for valid ADX values.
        """
        df = df.copy()
        n = len(df)

        high  = df["high"].to_numpy(dtype=np.float64)
        low   = df["low"].to_numpy(dtype=np.float64)
        close = df["close"].to_numpy(dtype=np.float64)

        # ── True Range ────────────────────────────────────────────────────────
        tr    = np.zeros(n)
        tr[0] = high[0] - low[0]
        for i in range(1, n):
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i]  - close[i - 1]),
            )

        # ── Directional Movement ──────────────────────────────────────────────
        plus_dm  = np.zeros(n)
        minus_dm = np.zeros(n)
        for i in range(1, n):
            up   = high[i] - high[i - 1]
            down = low[i - 1] - low[i]
            if up > down and up > 0:
                plus_dm[i] = up
            if down > up and down > 0:
                minus_dm[i] = down

        # ── Wilder's Smoothing ────────────────────────────────────────────────
        p = self.period

        def wilder_smooth(raw: np.ndarray) -> np.ndarray:
            smoothed = np.zeros(n)
            if n <= p:
                return smoothed
            smoothed[p] = raw[1 : p + 1].sum()   # first smoothed = sum of first period values
            for i in range(p + 1, n):
                smoothed[i] = smoothed[i - 1] - (smoothed[i - 1] / p) + raw[i]
            return smoothed

        s_tr      = wilder_smooth(tr)
        s_plus    = wilder_smooth(plus_dm)
        s_minus   = wilder_smooth(minus_dm)

        # ── DI lines ─────────────────────────────────────────────────────────
        plus_di  = np.zeros(n)
        minus_di = np.zeros(n)
        with np.errstate(divide="ignore", invalid="ignore"):
            mask = s_tr > 1e-12
            plus_di[mask]  = 100.0 * s_plus[mask]  / s_tr[mask]
            minus_di[mask] = 100.0 * s_minus[mask] / s_tr[mask]

        # ── DX and ADX ────────────────────────────────────────────────────────
        dx = np.zeros(n)
        with np.errstate(divide="ignore", invalid="ignore"):
            denom = plus_di + minus_di
            mask  = denom > 1e-12
            dx[mask] = 100.0 * np.abs(plus_di[mask] - minus_di[mask]) / denom[mask]

        # ADX = Wilder smooth of DX (starting from index 2*p)
        adx = np.zeros(n)
        start = 2 * p
        if n > start:
            adx[start] = dx[p : start + 1].mean()   # seed ADX with simple mean of first DX values
            for i in range(start + 1, n):
                adx[i] = (adx[i - 1] * (p - 1) + dx[i]) / p

        df["tr"]       = tr
        df["plus_di"]  = plus_di
        df["minus_di"] = minus_di
        df["adx"]      = adx
        return df

    # ── Classification ─────────────────────────────────────────────────────────

    def classify(self, adx_value: float) -> str:
        """
        Returns market regime classification based on ADX value.

        Returns:
          "trending"  — ADX >= 25; use swing_macd
          "ranging"   — ADX <= 20; use range_mean_revert
          "ambiguous" — 20 < ADX < 25; no signal, wait
        """
        if adx_value >= 25.0:
            return "trending"
        if adx_value <= 20.0:
            return "ranging"
        return "ambiguous"
