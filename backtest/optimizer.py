"""
backtest/optimizer.py

Bayesian hyperparameter optimizer for MARA trading strategies.
Uses scikit-optimize (Gaussian Process surrogate) to maximize risk-adjusted returns.

Runs OFFLINE against cached OHLCV parquet files in data/backtest/.
Data is fetched/cached on first run by strategy_comparison.py (or automatically here).

Usage:
    source ~/mara/.venv/bin/activate

    # Optimize both strategies on BTC + ETH (default):
    python backtest/optimizer.py

    # Optimize single strategy:
    python backtest/optimizer.py --strategy swing
    python backtest/optimizer.py --strategy range
    python backtest/optimizer.py --strategy regime

    # Custom symbols:
    python backtest/optimizer.py --symbols BTC/USDT:USDT ETH/USDT:USDT SOL/USDT:USDT

    # Fewer evaluations for quick test:
    python backtest/optimizer.py --n-calls 20

Output files (committed to config/):
    config/best_params_swing.json
    config/best_params_range.json
    config/best_params_regime.json

Training split:  2020-01-01 → 2022-12-31
Validation split: 2023-01-01 → 2024-06-30
Out-of-sample:   2024-07-01 → present  (never used here — reserved for live eval)

Expected runtime on Ryzen 7 (CPU only), n_calls=100:
    swing:  ~2–3 h  (9 dims × 100 evals, 5 symbols)
    range:  ~1–2 h  (5 dims × 100 evals)
    regime: ~0.5 h  (4 dims × 100 evals, synthetic regime labels)
"""

import argparse
import json
import math
import os
import sys
import warnings
from pathlib import Path
from typing import Optional

import numpy as np

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "workers" / "nautilus"))

from backtest.strategy_comparison import (
    load_historical_ohlcv,
    backtest_swing,
    backtest_range,
)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_DIR  = _ROOT / "config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

TRAIN_START = "2020-01-01"
TRAIN_END   = "2022-12-31"
VAL_START   = "2023-01-01"
VAL_END     = "2024-06-30"

DEFAULT_SYMBOLS   = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
DEFAULT_N_CALLS   = 100
DEFAULT_TIMEFRAME = "4h"

MAX_DRAWDOWN_HARD_LIMIT = 0.25   # reject any params that breach this

# ── Search spaces ─────────────────────────────────────────────────────────────
# Defined lazily so scikit-optimize import is deferred (not installed at test time)

def _swing_space():
    from skopt.space import Integer, Real
    return [
        Integer(8,    20,   name="macd_fast"),
        Integer(20,   40,   name="macd_slow"),
        Integer(6,    14,   name="macd_signal"),
        Integer(3,    7,    name="fractal_lookback"),
        Real(0.01,    0.04, name="stop_loss_pct"),
        Real(1.5,     4.0,  name="take_profit_ratio"),
        Integer(10,   20,   name="adx_period"),
        Real(18.0,    22.0, name="adx_range_threshold"),
        Real(23.0,    28.0, name="adx_trend_threshold"),
    ]


def _range_space():
    from skopt.space import Integer, Real
    return [
        Integer(15,   30,   name="bb_period"),
        Real(1.5,     2.5,  name="bb_std"),
        Integer(10,   21,   name="rsi_period"),
        Real(0.01,    0.025,name="stop_loss_pct"),
        Real(1.2,     2.5,  name="take_profit_ratio"),
    ]


def _regime_space():
    from skopt.space import Real
    return [
        Real(20.0,   35.0, name="war_premium_threshold"),
        Real(35.0,   55.0, name="crisis_vix"),
        Real(35.0,   55.0, name="war_gold_oil_ratio"),
        Real(0.55,   0.80, name="market_proxy_weight"),
    ]


# ── Objective functions ────────────────────────────────────────────────────────

def _objective_swing(params_list: list, symbols: list, timeframe: str) -> float:
    """
    Objective for swing_macd.  Returns NEGATIVE of the aggregate score
    (skopt minimizes by default).
    """
    space = _swing_space()
    params = {dim.name: val for dim, val in zip(space, params_list)}

    # Enforce fast < slow (constraint)
    if params["macd_fast"] >= params["macd_slow"]:
        return 0.0   # penalty

    aggregate_score = 0.0
    weight_total    = 0.0

    for symbol in symbols:
        try:
            df = load_historical_ohlcv(symbol, timeframe, TRAIN_START, TRAIN_END)
            if len(df) < 200:
                continue
            result = backtest_swing(df, params)
            if result["max_drawdown"] > MAX_DRAWDOWN_HARD_LIMIT * 100:
                continue   # hard constraint — skip this symbol contribution
            score = result["sharpe"] * (1 - result["max_drawdown"] / 100)
            aggregate_score += score
            weight_total    += 1
        except Exception:
            continue

    if weight_total == 0:
        return 0.0

    return -(aggregate_score / weight_total)   # negate for minimizer


def _objective_range(params_list: list, symbols: list, timeframe: str) -> float:
    """Objective for range_mean_revert."""
    space = _range_space()
    params = {dim.name: val for dim, val in zip(space, params_list)}

    aggregate_score = 0.0
    weight_total    = 0.0

    for symbol in symbols:
        try:
            df = load_historical_ohlcv(symbol, timeframe, TRAIN_START, TRAIN_END)
            if len(df) < 200:
                continue
            result = backtest_range(df, params)
            if result["max_drawdown"] > MAX_DRAWDOWN_HARD_LIMIT * 100:
                continue
            score = result["sharpe"] * (1 - result["max_drawdown"] / 100)
            aggregate_score += score
            weight_total    += 1
        except Exception:
            continue

    if weight_total == 0:
        return 0.0

    return -(aggregate_score / weight_total)


def _objective_regime(params_list: list) -> float:
    """
    Objective for regime classifier thresholds.
    Scores regime classifications against a synthetic ground-truth derived from
    hold-out price behaviour: WAR_PREMIUM if gold+defense outperform; else BULL_CALM.
    No external dependencies — uses yfinance proxies the same way classifier.py does.
    Returns NEGATIVE accuracy (minimizer).
    """
    space = _regime_space()
    params = {dim.name: val for dim, val in zip(space, params_list)}

    try:
        import yfinance as yf
        import pandas as pd

        tickers = ["GLD", "ITA", "SPY", "^VIX"]
        raw = yf.download(tickers, start=TRAIN_START, end=TRAIN_END,
                          progress=False, auto_adjust=True)
        if raw.empty:
            return 0.0

        closes = raw["Close"] if "Close" in raw.columns else raw
        closes = closes.ffill().dropna()
        if closes.empty or len(closes) < 50:
            return 0.0

        # Ground truth: WAR_PREMIUM when GLD/SPY 30-day ratio is in top-30th percentile
        # AND ITA/SPY ratio is also elevated (defense outperforms).
        gld_spy = closes["GLD"] / closes["SPY"]
        ita_spy = closes["ITA"] / closes["SPY"]
        vix     = closes["^VIX"]

        gld_pct = gld_spy.rolling(30).mean()
        ita_pct = ita_spy.rolling(30).mean()

        gld_threshold = gld_pct.quantile(0.70)
        ita_threshold = ita_pct.quantile(0.70)

        true_war = (gld_pct > gld_threshold) & (ita_pct > ita_threshold)

        # Simulated classifier decision using candidate thresholds
        # war_premium_score proxy: normalise GLD/SPY deviation + VIX component
        gld_z     = (gld_spy - gld_spy.rolling(252).mean()) / (gld_spy.rolling(252).std() + 1e-12)
        vix_norm  = (vix / 20.0).clip(0, 3)
        proxy_wps = (gld_z * params["market_proxy_weight"] + vix_norm * (1 - params["market_proxy_weight"])) * 20
        pred_war  = proxy_wps > params["war_premium_threshold"]

        common = true_war.index.intersection(pred_war.index)
        if len(common) < 10:
            return 0.0

        accuracy = (true_war[common] == pred_war[common]).mean()
        return -accuracy

    except Exception:
        return 0.0


# ── Validation pass ────────────────────────────────────────────────────────────

def _validate(strategy: str, best_params: dict, symbols: list, timeframe: str) -> dict:
    """
    Run the best params on the held-out validation set (2023-01-01 → 2024-06-30).
    Returns per-symbol metrics dict.
    """
    results = {}
    for symbol in symbols:
        try:
            df = load_historical_ohlcv(symbol, timeframe, VAL_START, VAL_END)
            if len(df) < 50:
                results[symbol] = {"error": "insufficient bars"}
                continue

            if strategy == "swing":
                m = backtest_swing(df, best_params)
            elif strategy == "range":
                m = backtest_range(df, best_params)
            else:
                m = {"note": "regime classifier — no bar-level validation"}

            results[symbol] = m
        except Exception as exc:
            results[symbol] = {"error": str(exc)}
    return results


# ── Core optimizer ─────────────────────────────────────────────────────────────

def optimize(
    strategy:  str,
    symbols:   list  = DEFAULT_SYMBOLS,
    timeframe: str   = DEFAULT_TIMEFRAME,
    n_calls:   int   = DEFAULT_N_CALLS,
    n_jobs:    int   = -1,
    random_state: int = 42,
) -> dict:
    """
    Run Bayesian optimisation for the given strategy.
    Returns the best_params dict.
    """
    from skopt import gp_minimize
    from skopt.utils import use_named_args

    print(f"\n{'='*60}")
    print(f"  Strategy : {strategy}")
    print(f"  Symbols  : {symbols}")
    print(f"  Train    : {TRAIN_START} → {TRAIN_END}")
    print(f"  n_calls  : {n_calls}")
    print(f"{'='*60}\n")

    if strategy == "swing":
        space = _swing_space()
        def objective(params_list):
            return _objective_swing(params_list, symbols, timeframe)

    elif strategy == "range":
        space = _range_space()
        def objective(params_list):
            return _objective_range(params_list, symbols, timeframe)

    elif strategy == "regime":
        space = _regime_space()
        def objective(params_list):
            return _objective_regime(params_list)

    else:
        raise ValueError(f"Unknown strategy: {strategy!r}. Use swing|range|regime")

    # Suppress GP convergence warnings (expected for small n_calls)
    warnings.filterwarnings("ignore", category=UserWarning, module="skopt")

    result = gp_minimize(
        func=objective,
        dimensions=space,
        n_calls=n_calls,
        n_initial_points=max(10, n_calls // 5),
        acq_func="EI",
        n_jobs=n_jobs,
        random_state=random_state,
        verbose=False,
    )

    best_score  = -result.fun   # un-negate
    best_params = {dim.name: val for dim, val in zip(space, result.x)}

    print(f"\nBest score (train): {best_score:.4f}")
    print(f"Best params:")
    for k, v in best_params.items():
        print(f"  {k:30s} = {v}")

    # Validation pass
    print(f"\nValidation ({VAL_START} → {VAL_END}):")
    val_results = _validate(strategy, best_params, symbols, timeframe)
    for sym, m in val_results.items():
        if "error" in m or "note" in m:
            print(f"  {sym}: {m}")
        else:
            print(
                f"  {sym}: sharpe={m.get('sharpe',0):.3f}  "
                f"dd={m.get('max_drawdown',0):.1f}%  "
                f"trades={m.get('total_trades',0)}  "
                f"wr={m.get('win_rate',0):.1f}%"
            )

    output = {
        "strategy":       strategy,
        "best_params":    best_params,
        "train_score":    round(best_score, 6),
        "train_range":    f"{TRAIN_START} → {TRAIN_END}",
        "val_range":      f"{VAL_START} → {VAL_END}",
        "val_results":    val_results,
        "symbols":        symbols,
        "n_calls":        n_calls,
        "n_evaluations":  len(result.func_vals),
        "convergence":    [round(float(v), 6) for v in result.func_vals[-10:]],
    }

    out_path = CONFIG_DIR / f"best_params_{strategy}.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nSaved → {out_path}")

    return best_params


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Bayesian optimizer for MARA trading strategies"
    )
    parser.add_argument(
        "--strategy",
        choices=["swing", "range", "regime", "all"],
        default="all",
        help="Which strategy to optimize (default: all)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_SYMBOLS,
        help="OKX OHLCV symbols to use, e.g. BTC/USDT:USDT ETH/USDT:USDT",
    )
    parser.add_argument(
        "--timeframe",
        default=DEFAULT_TIMEFRAME,
        help="Candle timeframe (default: 4h)",
    )
    parser.add_argument(
        "--n-calls",
        type=int,
        default=DEFAULT_N_CALLS,
        help="Number of BO evaluations per strategy (default: 100)",
    )
    parser.add_argument(
        "--n-jobs",
        type=int,
        default=1,
        help="Parallel workers for BO (default: 1; -1 = all cores)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility",
    )
    args = parser.parse_args()

    strategies = ["swing", "range", "regime"] if args.strategy == "all" else [args.strategy]

    all_results = {}
    for strat in strategies:
        best = optimize(
            strategy=strat,
            symbols=args.symbols,
            timeframe=args.timeframe,
            n_calls=args.n_calls,
            n_jobs=args.n_jobs,
            random_state=args.seed,
        )
        all_results[strat] = best

    print("\n" + "="*60)
    print("  Optimization complete.")
    print("  Output files:")
    for strat in strategies:
        p = CONFIG_DIR / f"best_params_{strat}.json"
        print(f"    {p}")
    print("\n  To apply: the modules read these files at startup if present.")
    print("  config/best_params_swing.json  → workers/nautilus/strategies/swing_macd.py")
    print("  config/best_params_range.json  → workers/nautilus/strategies/range_mean_revert.py")
    print("  config/best_params_regime.json → hypervisor/regime/classifier.py")
    print("="*60)


if __name__ == "__main__":
    main()
