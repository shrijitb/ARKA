"""
MARA Data Layer — data/feeds/market_data.py

Single source of truth for all market data in the system.
All workers and the hypervisor import from here — never directly from yfinance/ccxt/etc.

Free sources only:
  - yfinance           : ETFs, equities, VIX, DXY, commodity futures
  - pandas_datareader  : BDI via stooq.com (yfinance ^BDI is delisted)
  - fredapi            : macro data (yield curve, CPI, unemployment)
  - ccxt               : crypto OHLCV via Bybit (Binance is US geo-blocked)
  - requests           : OKX funding rates (public REST, no auth)
  - conflict_index     : composite War Premium Score (ACLED + GDELT + market proxy)

Known limitations:
  - BDI proxy via BDRY ETF (^BDI delisted from yfinance)
  - OKX used for crypto (Binance/Bybit CloudFront-blocked in US)
  - ACLED optional — system works without it, score less precise
"""

from __future__ import annotations

import os
import time
import requests
import pandas as pd
import numpy as np
import yfinance as yf

from typing import Optional

# ccxt — optional, Pylance may show "import could not be resolved" if not in system Python
# This is a venv-only package. The warning is a Pylance limitation, not a runtime error.
# To suppress in VS Code: add "python.analysis.ignore": ["**/market_data.py"] to settings.json
# or install ccxt in the system Python with: pip install ccxt --break-system-packages
try:
    import ccxt  # type: ignore[import-untyped]  # noqa: F401
    _CCXT_AVAILABLE = True
except ImportError:
    ccxt = None  # type: ignore[assignment]
    _CCXT_AVAILABLE = False

# fredapi — same situation as ccxt above
try:
    from fredapi import Fred as _Fred  # type: ignore[import-untyped]  # noqa: F401
    _FRED_KEY = os.getenv("FRED_API_KEY", "")
    FRED = _Fred(api_key=_FRED_KEY) if _FRED_KEY else None
except ImportError:
    FRED = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# yfinance compat helper — fixes "Series instead of scalar" bug in yfinance 0.2.x
# New yfinance returns MultiIndex columns even for single tickers.
# Always use this instead of df["Close"].iloc[-1] directly.
# ---------------------------------------------------------------------------

def _last_close(df: pd.DataFrame) -> float:
    """Safely extract the last closing price from a yfinance DataFrame."""
    col = df["Close"]
    # MultiIndex case: df["Close"] returns a DataFrame — take first column
    if isinstance(col, pd.DataFrame):
        col = col.iloc[:, 0]
    return float(col.dropna().iloc[-1])


# ---------------------------------------------------------------------------
# Simple in-process cache — avoids hammering APIs during dev/backtest loops
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, object]] = {}
CACHE_TTL = 300  # seconds — refresh data every 5 minutes


def _cached(key: str, ttl: int, fn):
    now = time.time()
    if key in _cache:
        ts, val = _cache[key]
        if now - ts < ttl:
            return val
    val = fn()
    _cache[key] = (now, val)
    return val


# ---------------------------------------------------------------------------
# BDI — Baltic Dry Index
# ^BDI delisted from yfinance, stooq returns no data for this ticker.
# Using BDRY (Breakwave Dry Bulk Shipping ETF) as proxy — tracks near-term
# BDI futures directly, available on yfinance, sufficient for regime signals.
# ---------------------------------------------------------------------------

def get_bdi(period: str = "1y") -> pd.DataFrame:
    """
    Returns BDRY ETF as BDI proxy.
    BDRY tracks front-month dry bulk freight futures — correlates ~0.85 with BDI.
    """
    def _fetch():
        df = yf.download("BDRY", period=period, progress=False, auto_adjust=True)
        if df.empty:
            raise ValueError("BDRY returned empty — yfinance may be down")
        return df
    return _cached(f"bdi_{period}", CACHE_TTL, _fetch)


def get_bdi_slope(weeks: int = 12) -> float:
    """
    Linear regression slope of BDRY over N weeks, normalised by mean.
    Positive = shipping demand rising. Negative = demand destruction.
    """
    df  = get_bdi()
    col = df["Close"]
    if isinstance(col, pd.DataFrame):
        col = col.iloc[:, 0]
    series = col.dropna().tail(weeks * 5)
    if len(series) < 2:
        return 0.0
    x     = np.arange(len(series))
    slope = float(np.polyfit(x, series.values.flatten(), 1)[0])
    mean  = float(series.mean())
    return slope / mean if mean != 0 else 0.0


# ---------------------------------------------------------------------------
# VIX — Fear gauge
# ---------------------------------------------------------------------------

def get_vix() -> float:
    """Spot VIX level. >30 = stress, >40 = crisis, <15 = complacency."""
    def _fetch():
        df = yf.download("^VIX", period="5d", progress=False, auto_adjust=True)
        if df.empty:
            raise ValueError("VIX returned empty — check yfinance")
        return _last_close(df)
    return _cached("vix", CACHE_TTL, _fetch)


# ---------------------------------------------------------------------------
# DXY — US Dollar Index
# ---------------------------------------------------------------------------

def get_dxy() -> float:
    """
    DXY proxy via UUP ETF (Invesco DB US Dollar Index Bullish Fund).
    UUP tracks the Deutsche Bank Long US Dollar Index — reliable yfinance
    data with no KeyError/empty-frame issues affecting DX=F and DX-Y.NYB.
    Strong USD (UUP > 28) = risk-off, weak USD (UUP < 26) = risk-on.
    """
    def _fetch():
        df = yf.download("UUP", period="5d", progress=False, auto_adjust=True)
        if df.empty:
            raise ValueError("UUP returned empty — yfinance may be down")
        return _last_close(df)
    return _cached("dxy", CACHE_TTL, _fetch)


# ---------------------------------------------------------------------------
# Yield Curve — 10Y minus 2Y spread (from FRED)
# ---------------------------------------------------------------------------

def get_yield_curve() -> float:
    """
    10Y-2Y Treasury spread from FRED (series T10Y2Y).
    Negative = inverted = recession signal.
    Falls back to yfinance approximation if FRED key not set.
    """
    def _fetch_fred():
        if FRED is None:
            raise RuntimeError("FRED not configured")
        series = FRED.get_series("T10Y2Y")
        return float(series.dropna().iloc[-1])

    def _fetch_yf_fallback():
        t10 = yf.download("^TNX", period="5d", progress=False, auto_adjust=True)
        t2  = yf.download("^IRX", period="5d", progress=False, auto_adjust=True)
        if t10.empty or t2.empty:
            return 0.0
        return (_last_close(t10) / 100) - (_last_close(t2) / 100)

    def _fetch():
        try:
            return _fetch_fred()
        except Exception:
            return _fetch_yf_fallback()

    return _cached("yield_curve", CACHE_TTL, _fetch)


# ---------------------------------------------------------------------------
# Commodities — spot prices via futures tickers
# ---------------------------------------------------------------------------

COMMODITY_TICKERS = {
    "gold":   "GC=F",
    "oil":    "CL=F",
    "silver": "SI=F",
    "copper": "HG=F",
    "natgas": "NG=F",
    "wheat":  "ZW=F",
    "corn":   "ZC=F",
}


def get_commodity(name: str) -> float:
    """Returns latest close price for a named commodity."""
    ticker = COMMODITY_TICKERS.get(name.lower())
    if not ticker:
        raise ValueError(f"Unknown commodity: {name}. Valid: {list(COMMODITY_TICKERS)}")
    def _fetch():
        df = yf.download(ticker, period="5d", progress=False, auto_adjust=True)
        if df.empty:
            raise ValueError(f"No data for {ticker}")
        return _last_close(df)
    return _cached(f"commodity_{name}", CACHE_TTL, _fetch)


def get_gold_oil_ratio() -> float:
    """
    Gold/Oil ratio. High (>25) = risk-off / war premium / recession hedge.
    Low (<15) = risk-on / economic expansion.
    """
    gold = get_commodity("gold")
    oil  = get_commodity("oil")
    return gold / oil if oil > 0 else 0.0


# ---------------------------------------------------------------------------
# ETFs — all target instruments
# ---------------------------------------------------------------------------

ETF_GROUPS = {
    "core":        ["VOO", "VT", "BND"],
    "commodities": ["GLDM", "BWET", "SLVR", "SILJ", "GOEX"],
    "defense":     ["NATO", "SHLD", "PPA", "ITA"],
    "resources":   ["REMX"],
}


def get_etf(ticker: str, period: str = "3mo") -> pd.DataFrame:
    """Returns OHLCV DataFrame for any ETF or equity ticker."""
    def _fetch():
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        if df.empty:
            raise ValueError(f"No ETF data for {ticker}")
        return df
    return _cached(f"etf_{ticker}_{period}", CACHE_TTL, _fetch)


def get_etf_price(ticker: str) -> float:
    """Latest close price for a single ETF."""
    df = get_etf(ticker, period="5d")
    return _last_close(df)


def get_defense_momentum(window: int = 20) -> float:
    """
    Average 20-day price momentum across defense ETFs.
    Positive + rising = WAR_PREMIUM regime signal.
    """
    scores = []
    for t in ETF_GROUPS["defense"]:
        try:
            df  = get_etf(t, period="3mo")
            col = df["Close"]
            if isinstance(col, pd.DataFrame):
                col = col.iloc[:, 0]
            col = col.dropna()
            if len(col) >= window:
                mom = (float(col.iloc[-1]) - float(col.iloc[-window])) / float(col.iloc[-window])
                scores.append(mom)
        except Exception:
            pass
    return float(np.mean(scores)) if scores else 0.0


# ---------------------------------------------------------------------------
# Crypto — OHLCV via ccxt / Kraken (not geo-blocked in the US)
# ---------------------------------------------------------------------------

def get_crypto_ohlcv(
    symbol: str = "BTC/USDT",
    exchange: str = "kraken",
    timeframe: str = "1d",
    limit: int = 90,
) -> pd.DataFrame:
    """
    Returns OHLCV DataFrame via Kraken public endpoint.
    Kraken uses BTC/USDT or XBT/USD — we normalise symbol automatically.
    No API keys needed.
    """
    def _fetch():
        if not _CCXT_AVAILABLE:
            raise ImportError("ccxt not installed — run: pip install ccxt --break-system-packages")
        ex = getattr(ccxt, exchange)({"enableRateLimit": True})
        # Kraken uses XBT not BTC — remap silently
        kraken_symbol = symbol.replace("BTC", "XBT") if exchange == "kraken" else symbol
        try:
            raw = ex.fetch_ohlcv(kraken_symbol, timeframe=timeframe, limit=limit)
        except Exception:
            # Fallback to original symbol if remap fails
            raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df
    return _cached(f"crypto_{exchange}_{symbol}_{timeframe}", CACHE_TTL, _fetch)


# ---------------------------------------------------------------------------
# Crypto — Perpetual Funding Rates via OKX public REST
# OKX is accessible from US IPs. Binance/Bybit both CloudFront-blocked.
# ---------------------------------------------------------------------------

def get_crypto_funding_rate(symbol: str = "BTC-USDT-SWAP") -> float:
    """
    Latest perpetual funding rate from OKX V5 public API.
    Symbol format for OKX: BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP.
    No authentication required.

    Positive (>0.0003) = euphoria / overleveraged longs = BULL_FROTHY.
    Negative (<-0.0001) = fear / forced shorts = BEAR_RECESSION.
    """
    def _fetch():
        url = f"https://www.okx.com/api/v5/public/funding-rate?instId={symbol}"
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return 0.0
        return float(data[0]["fundingRate"])
    return _cached(f"funding_{symbol}", CACHE_TTL, _fetch)


def get_all_funding_rates() -> dict[str, float]:
    """Returns funding rates for all tracked crypto perps via OKX."""
    symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
    result  = {}
    for s in symbols:
        try:
            result[s] = get_crypto_funding_rate(s)
        except Exception:
            result[s] = 0.0
    return result


# ---------------------------------------------------------------------------
# GDELT simple tension score — kept for standalone use and testing
# For the regime classifier, use get_war_premium_score() from conflict_index
# ---------------------------------------------------------------------------

def get_gdelt_tension_score(query: str = "war conflict military") -> float:
    """
    Simple GDELT article count — kept for backward compat and testing.
    The macro snapshot now uses get_war_premium_score() instead.
    """
    def _fetch():
        url = (
            "https://api.gdeltproject.org/api/v2/doc/doc"
            f"?query={query.replace(' ', '%20')}"
            "&mode=artlist&maxrecords=25&format=json"
        )
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return 0.0
        articles = r.json().get("articles", [])
        return float(len(articles))
    return _cached(f"gdelt_{query[:20]}", 600, _fetch)


# ---------------------------------------------------------------------------
# Macro snapshot — single call that returns everything the Regime Classifier needs
# ---------------------------------------------------------------------------

def get_macro_snapshot() -> dict:
    """
    Returns all regime classification inputs in one dict.
    Hypervisor calls this every heartbeat.
    Each value is fetched with caching — actual HTTP calls only happen when cache expires.
    war_premium_score replaces the old gdelt_war_score — it is a composite 0-100
    score from conflict_index.py combining ACLED + GDELT + market proxy.
    """
    # Lazy import to avoid circular dependency at module load time
    from data.feeds.conflict_index import get_war_premium_score

    snapshot = {
        "bdi_slope_12w":        None,
        "vix":                  None,
        "yield_curve":          None,
        "dxy":                  None,
        "gold_oil_ratio":       None,
        "defense_momentum_20d": None,
        "btc_funding_rate":     None,
        "war_premium_score":    None,
        "errors":               [],
    }

    fetchers = {
        "bdi_slope_12w":        get_bdi_slope,
        "vix":                  get_vix,
        "yield_curve":          get_yield_curve,
        "dxy":                  get_dxy,
        "gold_oil_ratio":       get_gold_oil_ratio,
        "defense_momentum_20d": get_defense_momentum,
        "btc_funding_rate":     lambda: get_crypto_funding_rate("BTC-USDT-SWAP"),
        # war_premium_score fetches its own market data internally
        "war_premium_score":    get_war_premium_score,
    }

    for key, fn in fetchers.items():
        try:
            snapshot[key] = fn()
        except Exception as e:
            snapshot["errors"].append(f"{key}: {str(e)}")

    return snapshot


# ---------------------------------------------------------------------------
# VERIFICATION — run this file directly to test all data sources
# Usage: python data/feeds/market_data.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "="*60)
    print("MARA DATA LAYER — VERIFICATION")
    print("="*60)

    tests = {
        "BDI (last close)": lambda: f"{_last_close(get_bdi()):.2f}",
        "BDI 12w slope":    lambda: f"{get_bdi_slope():.5f}",
        "VIX":              lambda: f"{get_vix():.2f}",
        "DXY":              lambda: f"{get_dxy():.2f}",
        "Yield curve":      lambda: f"{get_yield_curve():.4f}",
        "Gold price":       lambda: f"${get_commodity('gold'):.2f}",
        "Oil price":        lambda: f"${get_commodity('oil'):.2f}",
        "Gold/Oil ratio":   lambda: f"{get_gold_oil_ratio():.2f}",
        "Defense momentum": lambda: f"{get_defense_momentum():.4f}",
        "BTC funding rate": lambda: f"{get_crypto_funding_rate('BTC-USDT-SWAP'):.6f}",
        "BTC/USDT (OHLCV)": lambda: f"{len(get_crypto_ohlcv())} rows",
        "VOO price":        lambda: f"${get_etf_price('VOO'):.2f}",
        "GLDM price":       lambda: f"${get_etf_price('GLDM'):.2f}",
        "NATO ETF price":   lambda: f"${get_etf_price('NATO'):.2f}",
        "GDELT war score":  lambda: f"{get_gdelt_tension_score():.0f} articles (legacy — see conflict_index.py)",
    }

    passed = 0
    failed = 0

    for name, fn in tests.items():
        try:
            result = fn()
            print(f"  ✅  {name:<25} {result}")
            passed += 1
        except Exception as e:
            print(f"  ❌  {name:<25} FAILED: {e}")
            failed += 1

    print("-"*60)
    print(f"\nMacro snapshot test:")
    snap = get_macro_snapshot()
    for k, v in snap.items():
        if k == "errors":
            continue
        status = "✅" if v is not None else "❌"
        print(f"  {status}  {k:<25} {v}")
    if snap["errors"]:
        print(f"\n  Errors: {snap['errors']}")

    print("="*60)
    print(f"Result: {passed} passed, {failed} failed")
    if failed == 0:
        print("DATA LAYER READY ✅")
    else:
        print("Fix failures above before proceeding to Regime Classifier.")
    print("="*60 + "\n")
