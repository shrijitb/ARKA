"""
hypervisor/regime/feature_pipeline.py

Feature extraction for the 4-state HMM regime classifier.
Produces a 6-element feature vector, z-score normalized using a 252-day rolling window.

Features:
  0. vix_level              — ^VIX spot level
  1. vix_term_structure     — VIX3M / VIX - 1.0 (positive=contango/calm, negative=crisis)
  2. yield_spread_2y10y     — 10Y − 2Y Treasury spread (negative=inverted=recession)
  3. hy_credit_spread       — ICE BofA HY OAS in basis points (BAMLH0A0HYM2)
  4. nfci                   — Chicago Fed National Financial Conditions Index
  5. equity_momentum_60d    — log(SPY_t / SPY_{t-60d})
"""

from __future__ import annotations

import logging
import os
import pickle
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_STATS_PATH = Path(__file__).parent / "model_state" / "feature_stats.pkl"

# Approximate long-run averages used as fallbacks when a source is unavailable.
_FALLBACKS: dict = {
    "vix_level":           20.0,
    "vix_term_structure":  0.10,   # mild contango
    "yield_spread_2y10y":  0.50,
    "hy_credit_spread":    350.0,  # ~long-run average HY OAS (bps)
    "nfci":                0.0,    # neutral
    "equity_momentum_60d": 0.0,
}

N_FEATURES = 6


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_last_close(
    df: pd.DataFrame,
    min_price: float = 0.0,
    max_price: float = 1_000_000.0,
    ticker: str = "",
) -> Optional[float]:
    """
    Extract last close from a yfinance DataFrame (handles MultiIndex).

    Raises ValueError if the returned price is outside [min_price, max_price],
    which catches yfinance returning 0.0, NaN-as-zero, or implausibly large
    values caused by unadjusted splits or data errors.
    """
    if df is None or df.empty:
        return None
    col = df["Close"]
    if isinstance(col, pd.DataFrame):
        col = col.iloc[:, 0]
    col = col.dropna()
    if col.empty:
        return None
    value = float(col.iloc[-1])
    if not (min_price < value <= max_price):
        raise ValueError(
            f"Implausible close price for {ticker!r}: {value} "
            f"(expected {min_price} < price <= {max_price}). "
            "yfinance may have returned corrupt or unadjusted data."
        )
    return value


def _fetch_fred(series_ids: list, start: str) -> pd.DataFrame:
    """
    Fetch FRED series and return as a daily forward-filled DataFrame.
    Returns empty DataFrame if fredapi is unavailable or key not set.
    """
    try:
        from fredapi import Fred
        key = os.getenv("FRED_API_KEY", "")
        if not key:
            return pd.DataFrame()
        fred = Fred(api_key=key)
        frames = {}
        for sid in series_ids:
            try:
                frames[sid] = fred.get_series(sid, observation_start=start)
            except Exception as e:
                logger.debug(f"FRED {sid}: {e}")
        if not frames:
            return pd.DataFrame()
        df = pd.DataFrame(frames)
        # Resample to daily and forward-fill weekly/monthly series (e.g. NFCI)
        df = df.resample("D").last().ffill()
        return df
    except ImportError:
        return pd.DataFrame()
    except Exception as e:
        logger.warning(f"FRED batch fetch failed: {e}")
        return pd.DataFrame()


def _fetch_yield_history(period: str = "3y") -> pd.Series:
    """
    Historical daily 10Y-2Y yield spread.
    Tries FRED (GS10 - GS2) first, falls back to yfinance (^TNX - ^IRX).
    """
    try:
        from fredapi import Fred
        key = os.getenv("FRED_API_KEY", "")
        if key:
            fred = Fred(api_key=key)
            gs10 = fred.get_series("GS10")
            gs2  = fred.get_series("GS2")
            spread = (gs10 - gs2).resample("D").last().ffill()
            return spread
    except Exception as e:
        logger.debug(f"FRED yield history: {e}")

    try:
        t10 = yf.download("^TNX", period=period, progress=False, auto_adjust=True)
        t2  = yf.download("^IRX", period=period, progress=False, auto_adjust=True)
        if not t10.empty and not t2.empty:
            c10 = t10["Close"]
            c2  = t2["Close"]
            if isinstance(c10, pd.DataFrame): c10 = c10.iloc[:, 0]
            if isinstance(c2, pd.DataFrame):  c2  = c2.iloc[:, 0]
            return (c10 / 100.0 - c2 / 100.0).dropna()
    except Exception as e:
        logger.debug(f"yfinance yield fallback: {e}")

    return pd.Series(dtype=float)


# ── FeaturePipeline ───────────────────────────────────────────────────────────

class FeaturePipeline:
    """
    Extract and normalize regime classification features.

    Usage:
        pipeline = FeaturePipeline()
        if not pipeline.load_stats():          # try load persisted stats
            raw_hist = pipeline.bootstrap()    # fetch 3y history, sets rolling stats
            z_hist   = pipeline.normalize(raw_hist)  # for HMM training
        z_vec = pipeline.extract_current()     # (1, 6) for inference
    """

    def __init__(self, lookback_days: int = 252):
        self.lookback = lookback_days
        self.rolling_mean: np.ndarray = np.array(_FALLBACKS_VEC)
        self.rolling_std:  np.ndarray = np.ones(N_FEATURES)
        self._raw_history: list = []   # list of (6,) raw vectors
        self._last_raw:    dict = dict(_FALLBACKS)

    # ── Public API ────────────────────────────────────────────────────────────

    def bootstrap(self, years: int = 3) -> np.ndarray:
        """
        Fetch historical market data and build a (T, 6) raw feature matrix.
        Updates rolling stats and persists them. T ≥ 504 required for HMM training.

        Returns raw (un-normalized) feature matrix.
        """
        period = f"{years}y"
        logger.info(f"FeaturePipeline.bootstrap: fetching {period} of market data...")

        # ── Equity tickers ────────────────────────────────────────────────────
        equity = yf.download(
            ["^VIX", "^VIX3M", "SPY"],
            period=period, progress=False, auto_adjust=True,
        )
        if equity.empty or "Close" not in equity:
            raise RuntimeError("Failed to fetch equity history — yfinance unavailable")

        close = equity["Close"]
        if isinstance(close, pd.Series):
            close = close.to_frame()

        # Ensure expected tickers present; fill missing VIX3M with VIX * 1.1
        if "^VIX" not in close.columns:
            raise RuntimeError("^VIX missing from yfinance response")
        if "^VIX3M" not in close.columns:
            close["^VIX3M"] = close["^VIX"] * 1.1

        # ── Yield spread ──────────────────────────────────────────────────────
        yield_series = _fetch_yield_history(period=period)

        # ── FRED: HY OAS + NFCI ───────────────────────────────────────────────
        start_str = (
            pd.Timestamp.now() - pd.DateOffset(years=years)
        ).strftime("%Y-%m-%d")
        fred_df = _fetch_fred(["BAMLH0A0HYM2", "NFCI"], start=start_str)

        # ── Align on VIX trading days ─────────────────────────────────────────
        combined = close[["^VIX", "^VIX3M", "SPY"]].copy()
        combined.columns = ["vix", "vix3m", "spy"]

        # Normalize combined index to tz-naive so FRED (tz-naive) can be reindexed in.
        if combined.index.tz is not None:
            combined.index = combined.index.tz_localize(None)

        if not yield_series.empty:
            ys_idx = pd.DatetimeIndex(yield_series.index)
            if ys_idx.tz is not None:
                ys_idx = ys_idx.tz_localize(None)
            yield_series.index = ys_idx
            combined["yield_spread"] = yield_series.reindex(combined.index).ffill()
        else:
            combined["yield_spread"] = _FALLBACKS["yield_spread_2y10y"]

        if not fred_df.empty and "BAMLH0A0HYM2" in fred_df.columns:
            fi = pd.DatetimeIndex(fred_df.index)
            if fi.tz is not None:
                fi = fi.tz_localize(None)
            fred_df.index = fi
            combined["hy_oas"] = fred_df["BAMLH0A0HYM2"].reindex(combined.index).ffill()
        else:
            combined["hy_oas"] = _FALLBACKS["hy_credit_spread"]

        if not fred_df.empty and "NFCI" in fred_df.columns:
            combined["nfci"] = fred_df["NFCI"].reindex(combined.index).ffill()
        else:
            combined["nfci"] = _FALLBACKS["nfci"]

        combined = combined.ffill().dropna(subset=["vix", "spy"])

        # ── Build feature rows ─────────────────────────────────────────────────
        vix_arr    = combined["vix"].values.astype(float)
        vix3m_arr  = combined["vix3m"].values.astype(float)
        spy_arr    = combined["spy"].values.astype(float)
        yield_arr  = combined["yield_spread"].values.astype(float)
        hy_arr     = combined["hy_oas"].values.astype(float)
        nfci_arr   = combined["nfci"].values.astype(float)

        rows = []
        for i in range(len(combined)):
            vix    = vix_arr[i]
            vix3m  = vix3m_arr[i] if np.isfinite(vix3m_arr[i]) else vix * 1.1
            term   = (vix3m / vix - 1.0) if vix > 0 else _FALLBACKS["vix_term_structure"]
            ys     = yield_arr[i] if np.isfinite(yield_arr[i]) else _FALLBACKS["yield_spread_2y10y"]
            hy     = hy_arr[i]    if np.isfinite(hy_arr[i])    else _FALLBACKS["hy_credit_spread"]
            nfci   = nfci_arr[i]  if np.isfinite(nfci_arr[i])  else _FALLBACKS["nfci"]
            spy_60 = spy_arr[i - 60] if i >= 60 else spy_arr[0]
            mom    = (np.log(spy_arr[i] / spy_60)
                      if spy_60 > 0 and spy_arr[i] > 0 else 0.0)
            rows.append(np.array([vix, term, ys, hy, nfci, mom], dtype=float))

        features = np.array(rows)   # (T, 6)
        logger.info(f"FeaturePipeline.bootstrap: {len(features)} rows")

        self._raw_history = list(features)
        self.update_rolling_stats(features)
        self._save_stats()
        return features

    def extract_current(self) -> np.ndarray:
        """
        Fetch current market data and return a (1, 6) z-scored feature vector.
        Appends the raw point to the rolling history and refreshes stats.
        """
        raw = self._fetch_current_raw()
        row = np.array([
            raw["vix_level"],
            raw["vix_term_structure"],
            raw["yield_spread_2y10y"],
            raw["hy_credit_spread"],
            raw["nfci"],
            raw["equity_momentum_60d"],
        ], dtype=float)

        self._raw_history.append(row)
        if len(self._raw_history) > self.lookback * 2:
            self._raw_history = self._raw_history[-self.lookback:]
        self.update_rolling_stats(np.array(self._raw_history))

        z = self.normalize(row.reshape(1, -1))
        return z   # (1, 6)

    def normalize(self, raw: np.ndarray) -> np.ndarray:
        """Z-score normalize a raw (T, 6) or (1, 6) array using current rolling stats."""
        return (raw - self.rolling_mean) / (self.rolling_std + 1e-8)

    def update_rolling_stats(self, history: np.ndarray) -> None:
        """Update rolling mean/std from (N, 6) history array."""
        window = history[-self.lookback:]
        self.rolling_mean = np.mean(window, axis=0)
        self.rolling_std  = np.std(window, axis=0)

    def get_raw_features(self) -> dict:
        """Return the most-recently-fetched raw feature values (for circuit breakers)."""
        return dict(self._last_raw)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_stats(self) -> None:
        _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_STATS_PATH, "wb") as f:
            pickle.dump({
                "rolling_mean":     self.rolling_mean,
                "rolling_std":      self.rolling_std,
                "raw_history_tail": self._raw_history[-self.lookback:],
            }, f)

    def load_stats(self) -> bool:
        """Load persisted rolling stats. Returns True if successful."""
        if not _STATS_PATH.exists():
            return False
        try:
            with open(_STATS_PATH, "rb") as f:
                d = pickle.load(f)
            self.rolling_mean = d["rolling_mean"]
            self.rolling_std  = d["rolling_std"]
            self._raw_history = list(d.get("raw_history_tail", []))
            return True
        except Exception as e:
            logger.warning(f"Failed to load feature stats: {e}")
            return False

    # ── Current-data fetch ────────────────────────────────────────────────────

    def _fetch_current_raw(self) -> dict:
        """Fetch latest values for all 6 features, using fallbacks on failure."""
        raw = dict(_FALLBACKS)

        # Feature 0: VIX level
        try:
            df = yf.download("^VIX", period="5d", progress=False, auto_adjust=True)
            v = _safe_last_close(df, min_price=0.0, max_price=200.0, ticker="^VIX")
            if v is not None:
                raw["vix_level"] = v
        except Exception as e:
            logger.warning(f"VIX fetch rejected or failed: {e}")

        # Feature 1: VIX term structure
        try:
            df3m = yf.download("^VIX3M", period="5d", progress=False, auto_adjust=True)
            v3m  = _safe_last_close(df3m, min_price=0.0, max_price=200.0, ticker="^VIX3M")
            if v3m is not None and raw["vix_level"] > 0:
                raw["vix_term_structure"] = v3m / raw["vix_level"] - 1.0
        except Exception as e:
            logger.warning(f"VIX3M fetch rejected or failed: {e}")

        # Feature 2: yield spread (reuse existing market_data helper)
        try:
            import sys
            from pathlib import Path as _Path
            sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
            from data.feeds.market_data import get_yield_curve
            raw["yield_spread_2y10y"] = get_yield_curve()
        except Exception as e:
            logger.debug(f"Yield curve: {e}")

        # Features 3 & 4: HY OAS + NFCI from FRED
        try:
            from fredapi import Fred
            key = os.getenv("FRED_API_KEY", "")
            if key:
                fred = Fred(api_key=key)
                hy = fred.get_series("BAMLH0A0HYM2")
                raw["hy_credit_spread"] = float(hy.dropna().iloc[-1])
                nfci = fred.get_series("NFCI")
                raw["nfci"] = float(nfci.dropna().iloc[-1])
        except Exception as e:
            logger.debug(f"FRED current: {e}")

        # Feature 5: 60-day SPY log return
        try:
            df_spy = yf.download("SPY", period="90d", progress=False, auto_adjust=True)
            if df_spy is not None and not df_spy.empty:
                col = df_spy["Close"]
                if isinstance(col, pd.DataFrame):
                    col = col.iloc[:, 0]
                col = col.dropna()
                if len(col) >= 60:
                    spy_now = float(col.iloc[-1])
                    spy_60d = float(col.iloc[-60])
                    if spy_now <= 0 or spy_60d <= 0:
                        raise ValueError(
                            f"Implausible SPY close prices: now={spy_now}, 60d={spy_60d}. "
                            "yfinance may have returned corrupt or unadjusted data."
                        )
                    raw["equity_momentum_60d"] = float(np.log(spy_now / spy_60d))
        except Exception as e:
            logger.warning(f"SPY momentum rejected or failed: {e}")

        self._last_raw = raw
        return raw


# Convenience vector form of fallbacks (for rolling_mean initialization)
_FALLBACKS_VEC = np.array([
    _FALLBACKS["vix_level"],
    _FALLBACKS["vix_term_structure"],
    _FALLBACKS["yield_spread_2y10y"],
    _FALLBACKS["hy_credit_spread"],
    _FALLBACKS["nfci"],
    _FALLBACKS["equity_momentum_60d"],
], dtype=float)
