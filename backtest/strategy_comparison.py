"""
backtest/strategy_comparison.py

Vectorized pandas backtest for MARA strategy comparison.
Compares swing_macd vs range_mean_revert over historical OKX OHLCV data.

This is NOT a NautilusTrader backtest (F-09 — parked). It is a lightweight
vectorized simulation for rapid parameter iteration.

Usage:
    source ~/mara/.venv/bin/activate
    python backtest/strategy_comparison.py

OKX symbol format for ccxt: "BTC/USDT:USDT" (unified perp format),
NOT "BTC-USDT-SWAP" which is only used for direct OKX API calls.
"""

import os
import sys
import time
import math
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "workers" / "nautilus"))

CACHE_DIR = _ROOT / "data" / "backtest"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── Parameters ────────────────────────────────────────────────────────────────
DEFAULT_SYMBOLS   = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
DEFAULT_TIMEFRAME = "4h"
DEFAULT_START     = "2022-01-01"
DEFAULT_END       = "2024-06-30"

DEFAULT_SWING_PARAMS = {
    "macd_fast": 8, "macd_slow": 21, "macd_signal": 5,
    "stop_loss_pct": 0.02, "take_profit_ratio": 2.0,
    "rsi_period": 14, "rsi_bull_min": 40,
}
DEFAULT_RANGE_PARAMS = {
    "bb_period": 20, "bb_std": 2.0, "rsi_period": 14,
    "stop_loss_pct": 0.015, "take_profit_ratio": 1.5,
    "adx_period": 14,
}


# ── Data layer ────────────────────────────────────────────────────────────────

def load_historical_ohlcv(
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
) -> pd.DataFrame:
    """
    Fetch historical OHLCV from OKX public endpoint (no auth) via ccxt.
    Caches to data/backtest/{symbol}_{timeframe}_{start}_{end}.parquet.
    """
    safe_sym  = symbol.replace("/", "_").replace(":", "_")
    cache_key = f"{safe_sym}_{timeframe}_{start}_{end}"
    cache_path = CACHE_DIR / f"{cache_key}.parquet"

    if cache_path.exists():
        print(f"  [cache] Loading {symbol} {timeframe} from {cache_path.name}")
        return pd.read_parquet(cache_path)

    print(f"  [fetch] Downloading {symbol} {timeframe} {start}→{end} from OKX…")
    try:
        import ccxt
        exchange = ccxt.okx({"enableRateLimit": True})

        start_ms = int(datetime.strptime(start, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp() * 1000)
        end_ms   = int(datetime.strptime(end,   "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp() * 1000)

        all_rows = []
        since = start_ms
        while since < end_ms:
            bars = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not bars:
                break
            all_rows.extend(bars)
            since = bars[-1][0] + 1
            if bars[-1][0] >= end_ms:
                break
            time.sleep(exchange.rateLimit / 1000)

        if not all_rows:
            raise ValueError(f"No OHLCV data returned for {symbol}")

        df = pd.DataFrame(
            all_rows, columns=["ts", "open", "high", "low", "close", "volume"]
        )
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("ts").sort_index()
        df = df[df.index < pd.Timestamp(end, tz="UTC")]
        df = df.astype(float)

        df.to_parquet(cache_path)
        print(f"  [cache] Saved {len(df)} bars → {cache_path.name}")
        return df

    except ImportError:
        print("  [warn] ccxt not installed — generating synthetic data for demo")
        return _synthetic_ohlcv(symbol, timeframe, start, end)
    except Exception as exc:
        print(f"  [warn] Fetch failed ({exc}) — using synthetic data")
        return _synthetic_ohlcv(symbol, timeframe, start, end)


def _synthetic_ohlcv(symbol: str, timeframe: str, start: str, end: str) -> pd.DataFrame:
    """Synthetic GBM OHLCV for offline testing / demo runs."""
    n_bars = 2000
    base   = {"BTC/USDT:USDT": 40_000.0, "ETH/USDT:USDT": 2_800.0}.get(symbol, 100.0)
    rng    = np.random.RandomState(abs(hash(symbol)) % (2**31))
    drift  = 0.00005
    sigma  = 0.008
    shocks = rng.normal(drift, sigma, n_bars)
    closes = [base]
    for s in shocks:
        closes.append(closes[-1] * (1 + s))
    closes = closes[1:]

    rows = []
    for i, c in enumerate(closes):
        bar_range = c * rng.uniform(0.005, 0.02)
        high  = c + bar_range * rng.uniform(0.3, 0.7)
        low   = c - bar_range * rng.uniform(0.3, 0.7)
        open_ = closes[i - 1] if i > 0 else c
        rows.append({"open": open_, "high": high, "low": low, "close": c, "volume": rng.uniform(1e6, 5e7)})

    freq = {"4h": "4h", "1h": "1h", "1d": "1D"}.get(timeframe, "4h")
    idx = pd.date_range(start=start, periods=n_bars, freq=freq, tz="UTC")
    df  = pd.DataFrame(rows, index=idx[:len(rows)])
    return df.astype(float)


# ── Vectorized backtests ───────────────────────────────────────────────────────

def _ema_series(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def _wilder_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta  = closes.diff()
    gains  = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    avg_g  = gains.ewm(com=period - 1, adjust=False).mean()
    avg_l  = losses.ewm(com=period - 1, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def backtest_swing(df: pd.DataFrame, params: dict) -> dict:
    """
    Vectorised swing_macd backtest.
    Entry:  MACD crossover above zero + bullish/bearish fractal within 15 bars + RSI gate
    Exit:   stop-loss or take-profit (trailing stop not modelled in vectorised form)
    """
    p = {**DEFAULT_SWING_PARAMS, **params}
    df = df.copy()

    # Indicators
    ema_f = _ema_series(df["close"], p["macd_fast"])
    ema_s = _ema_series(df["close"], p["macd_slow"])
    macd  = ema_f - ema_s
    signal_line = _ema_series(macd, p["macd_signal"])
    rsi   = _wilder_rsi(df["close"], p["rsi_period"])

    # MACD crossover
    prev_below = macd.shift(1) <= signal_line.shift(1)
    now_above  = macd > signal_line
    bull_cross = prev_below & now_above & (macd > 0) & (rsi >= p["rsi_bull_min"]) & (rsi <= 70)

    prev_above = macd.shift(1) >= signal_line.shift(1)
    now_below  = macd < signal_line
    bear_cross = prev_above & now_below & (macd < 0) & (rsi >= 30) & (rsi <= 60)

    trades  = []
    in_pos  = False
    entry   = 0.0
    sl      = 0.0
    tp      = 0.0
    side    = ""
    closes  = df["close"].to_numpy()
    highs   = df["high"].to_numpy()
    lows    = df["low"].to_numpy()

    for i in range(len(df)):
        if in_pos:
            if side == "long":
                if lows[i] <= sl:
                    trades.append((side, entry, sl, "sl"))
                    in_pos = False
                elif highs[i] >= tp:
                    trades.append((side, entry, tp, "tp"))
                    in_pos = False
            else:
                if highs[i] >= sl:
                    trades.append((side, entry, sl, "sl"))
                    in_pos = False
                elif lows[i] <= tp:
                    trades.append((side, entry, tp, "tp"))
                    in_pos = False
            continue

        if bull_cross.iloc[i]:
            entry  = closes[i]
            sl     = entry * (1 - p["stop_loss_pct"])
            tp     = entry + (entry - sl) * p["take_profit_ratio"]
            side   = "long"
            in_pos = True
        elif bear_cross.iloc[i]:
            entry  = closes[i]
            sl     = entry * (1 + p["stop_loss_pct"])
            tp     = entry - (sl - entry) * p["take_profit_ratio"]
            side   = "short"
            in_pos = True

    return _compute_metrics(trades)


def backtest_range(df: pd.DataFrame, params: dict) -> dict:
    """
    Vectorised range_mean_revert backtest.
    Entry:  BB lower/upper + RSI extreme + ADX < 20 (ranging)
    Exit:   BB middle or RSI neutral (55/45)
    """
    from indicators.adx import AdxCalculator

    p   = {**DEFAULT_RANGE_PARAMS, **params}
    df  = df.copy()

    # Bollinger
    mid = df["close"].rolling(p["bb_period"]).mean()
    std = df["close"].rolling(p["bb_period"]).std(ddof=0)
    df["bb_upper"]  = mid + p["bb_std"] * std
    df["bb_lower"]  = mid - p["bb_std"] * std
    df["bb_middle"] = mid
    df["bb_width"]  = (df["bb_upper"] - df["bb_lower"]) / mid.replace(0, np.nan)

    # RSI
    df["rsi"] = _wilder_rsi(df["close"], p["rsi_period"])

    # ADX
    try:
        calc = AdxCalculator(period=p["adx_period"])
        df = calc.calculate(df)
    except Exception:
        df["adx"] = 0.0

    trades  = []
    in_pos  = False
    entry   = 0.0
    sl      = 0.0
    tp      = 0.0
    side    = ""
    closes  = df["close"].to_numpy()
    highs   = df["high"].to_numpy()
    lows    = df["low"].to_numpy()

    for i in range(p["bb_period"] + p["rsi_period"], len(df)):
        row = df.iloc[i]
        adx_val  = float(row.get("adx", 0))
        bb_width = float(row["bb_width"]) if not pd.isna(row["bb_width"]) else 0.0
        rsi      = float(row["rsi"])   if not pd.isna(row["rsi"])      else 50.0
        close    = float(row["close"])
        bb_u     = float(row["bb_upper"])
        bb_l     = float(row["bb_lower"])
        bb_m     = float(row["bb_middle"])

        # Dead-market filter
        if bb_width < 0.02:
            continue

        if in_pos:
            if side == "long":
                if lows[i] <= sl or highs[i] >= tp or close >= bb_m or rsi >= 55:
                    exit_p = sl if lows[i] <= sl else (tp if highs[i] >= tp else close)
                    trades.append((side, entry, exit_p, "exit"))
                    in_pos = False
            else:
                if highs[i] >= sl or lows[i] <= tp or close <= bb_m or rsi <= 45:
                    exit_p = sl if highs[i] >= sl else (tp if lows[i] <= tp else close)
                    trades.append((side, entry, exit_p, "exit"))
                    in_pos = False
            continue

        if adx_val <= 20.0 and close <= bb_l and rsi <= 30:
            entry  = close
            sl     = entry * (1 - p["stop_loss_pct"])
            tp     = entry + (entry - sl) * p["take_profit_ratio"]
            side   = "long"
            in_pos = True
        elif adx_val <= 20.0 and close >= bb_u and rsi >= 70:
            entry  = close
            sl     = entry * (1 + p["stop_loss_pct"])
            tp     = entry - (sl - entry) * p["take_profit_ratio"]
            side   = "short"
            in_pos = True

    return _compute_metrics(trades)


def _compute_metrics(trades: list) -> dict:
    """Compute Sharpe, max drawdown, win rate, CAGR, profit factor from a trade list."""
    if not trades:
        return {
            "sharpe": 0.0, "max_drawdown": 0.0, "win_rate": 0.0,
            "total_trades": 0, "cagr": 0.0, "profit_factor": 0.0,
        }

    returns = []
    wins, gross_profit, gross_loss = 0, 0.0, 0.0
    for side, entry, exit_p, reason in trades:
        if side == "long":
            ret = (exit_p - entry) / entry
        else:
            ret = (entry - exit_p) / entry
        returns.append(ret)
        if ret > 0:
            wins       += 1
            gross_profit += ret
        else:
            gross_loss  += abs(ret)

    rets   = np.array(returns)
    mean_r = rets.mean()
    std_r  = rets.std(ddof=1) if len(rets) > 1 else 1e-12

    # Annualise (6 H4 bars per day → ~1440 bars/year)
    bars_per_year   = 6 * 365
    trades_per_year = bars_per_year / max(len(trades), 1) * len(trades)
    sharpe = (mean_r / std_r) * math.sqrt(trades_per_year) if std_r > 1e-12 else 0.0

    # Max drawdown (equity curve)
    equity = np.cumprod(1 + rets)
    peak   = np.maximum.accumulate(equity)
    dd     = (equity - peak) / peak
    max_dd = float(abs(dd.min()))

    # CAGR: assume trades span ~2.5 years (2022-01-01 to 2024-06-30)
    total_return = float(equity[-1]) - 1.0 if len(equity) > 0 else 0.0
    years        = 2.5
    cagr         = ((1 + total_return) ** (1 / years)) - 1 if total_return > -1 else -1.0

    pf = gross_profit / max(gross_loss, 1e-12)

    return {
        "sharpe":       round(sharpe,          3),
        "max_drawdown": round(max_dd * 100,    2),   # as %
        "win_rate":     round(wins / len(trades) * 100, 1),  # as %
        "total_trades": len(trades),
        "cagr":         round(cagr * 100,      2),   # as %
        "profit_factor": round(pf,             3),
    }


# ── Comparison runner ─────────────────────────────────────────────────────────

def run_comparison(
    symbols:   list = DEFAULT_SYMBOLS,
    start:     str  = DEFAULT_START,
    end:       str  = DEFAULT_END,
    timeframe: str  = DEFAULT_TIMEFRAME,
):
    """
    Run both strategies against historical data and print a comparison table.
    """
    print(f"\n{'='*70}")
    print(f"MARA Strategy Comparison  |  {timeframe}  |  {start} → {end}")
    print(f"{'='*70}\n")

    for symbol in symbols:
        print(f"  Symbol: {symbol}")
        df = load_historical_ohlcv(symbol, timeframe, start, end)
        print(f"  Bars loaded: {len(df)}\n")

        swing_r = backtest_swing(df, DEFAULT_SWING_PARAMS)
        range_r = backtest_range(df, DEFAULT_RANGE_PARAMS)

        header = f"  {'Metric':<18} {'swing_macd':>14} {'range_mean_revert':>20}"
        sep    = "  " + "-" * (len(header) - 2)

        print(header)
        print(sep)
        metrics_order = [
            ("Sharpe",       "sharpe",       "{:.3f}"),
            ("Max Drawdown", "max_drawdown", "{:.1f}%"),
            ("Win Rate",     "win_rate",     "{:.1f}%"),
            ("Total Trades", "total_trades", "{:d}"),
            ("CAGR",         "cagr",         "{:.1f}%"),
            ("Profit Factor","profit_factor","{:.3f}"),
        ]
        for label, key, fmt in metrics_order:
            sv = swing_r.get(key, 0)
            rv = range_r.get(key, 0)
            sv_str = fmt.format(sv) if isinstance(sv, float) else str(sv)
            rv_str = fmt.format(rv) if isinstance(rv, float) else str(rv)
            print(f"  {label:<18} {sv_str:>14} {rv_str:>20}")
        print()


if __name__ == "__main__":
    run_comparison(
        symbols   = DEFAULT_SYMBOLS,
        start     = DEFAULT_START,
        end       = DEFAULT_END,
        timeframe = DEFAULT_TIMEFRAME,
    )
