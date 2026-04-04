"""
hypervisor/risk/execution_risk.py

Pre-execution risk checks — run AFTER signal generation, BEFORE /execute.

Three checks:
  1. Slippage / Gapping     — historical 4h candle gap → implied slippage bps
  2. Liquidity Dependency   — order book depth vs order size → fill rate estimate
  3. Intermediary Latency   — per-source p95 fetch latency → staleness flag

Paper trading mode (PAPER_TRADING=True / MARA_LIVE != "true"):
  Checks log warnings but never return flag="BLOCK". BLOCK-level conditions
  are downgraded to WARN so the paper cycle continues unimpeded.

Exception: total data-fetch latency > 45 000 ms always returns flag="BLOCK"
  regardless of mode — a 60 s cycle cannot absorb a 45 s fetch without
  logic errors (data would be classified, then allocated on stale snapshot).

Usage:
    checker = ExecutionRiskChecker(paper_trading=PAPER_TRADING)

    # During the data-fetch phase of each cycle:
    checker.record_source_latency("yfinance", elapsed_ms)

    # Immediately before dispatching /execute or /allocate:
    slip = checker.check_slippage(symbol, signal_price, side, regime)
    liq  = checker.check_liquidity(symbol, order_size_usd)
    lat  = checker.check_latency()
"""

from __future__ import annotations

import math
import logging
from collections import deque
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

SLIPPAGE_WARN_BPS  = 150   # 1.5% implied slippage → WARN
SLIPPAGE_BLOCK_BPS = 300   # 3.0% implied slippage → BLOCK (live only)

LIQUIDITY_WARN_FILL  = 0.5   # < 50% fill rate → WARN
LIQUIDITY_BLOCK_FILL = 0.2   # < 20% fill rate → BLOCK (live only)

LATENCY_WARN_MS  =  8_000   # 8 s p95 per source → data may be stale → WARN
LATENCY_BLOCK_MS = 20_000   # 20 s p95 per source → BLOCK (live only)
TOTAL_BLOCK_MS   = 45_000   # 45 s total across all sources → BLOCK (always)

# Regime volatility multipliers applied to the raw candle-gap estimate
REGIME_VOL_MULTIPLIERS: Dict[str, float] = {
    "WAR_PREMIUM":    2.0,
    "CRISIS_ACUTE":   2.5,
    "BEAR_RECESSION": 1.5,
    # All other regimes default to 1.0
}

_LATENCY_WINDOW = 10   # rolling readings kept per source


# ── Main class ────────────────────────────────────────────────────────────────

class ExecutionRiskChecker:
    """
    Pre-execution risk gatekeeper. Instantiate once at hypervisor startup.

    Thread-safety: all mutating operations happen inside the asyncio event
    loop (via asyncio.to_thread wrappers in the caller) so no locking is
    needed.
    """

    def __init__(self, paper_trading: bool = True):
        self._paper_trading = paper_trading
        # Rolling deque of latency readings (ms) per data source
        self._latency_history: Dict[str, deque] = {
            "yfinance": deque(maxlen=_LATENCY_WINDOW),
            "fred":     deque(maxlen=_LATENCY_WINDOW),
            "gdelt":    deque(maxlen=_LATENCY_WINDOW),
            "okx":      deque(maxlen=_LATENCY_WINDOW),
        }
        # Last computed results — exposed at /execution-risk and in /metrics
        self.last_slippage_results:  Dict[str, dict] = {}
        self.last_liquidity_results: Dict[str, dict] = {}
        self.last_latency_result:    dict             = {}

    # ── Record latency ────────────────────────────────────────────────────────

    def record_source_latency(self, source: str, latency_ms: float) -> None:
        """
        Record one fetch-latency reading for the named source.
        Call this from the hypervisor cycle during the data-prefetch phase.
        """
        if source not in self._latency_history:
            self._latency_history[source] = deque(maxlen=_LATENCY_WINDOW)
        self._latency_history[source].append(latency_ms)
        logger.debug("exec_risk latency recorded source=%s ms=%.0f", source, latency_ms)

    # ── Check 1: Slippage / Gapping ───────────────────────────────────────────

    def check_slippage(
        self,
        symbol:       str,
        signal_price: float,
        side:         str,
        regime:       str,
    ) -> dict:
        """
        Estimate implied slippage in basis points from historical 4 h candle gaps.

        realized_gap_pct  = mean((high - low) / open) over last 20 candles
        implied_slippage  = realized_gap_pct × 10 000 × regime_vol_multiplier

        Returns
        -------
        {
            "symbol":               str,
            "implied_slippage_bps": float,
            "flag":                 "OK" | "WARN" | "BLOCK",
            "regime_multiplier":    float,
            "regime":               str,
        }

        BLOCK is returned only when paper_trading=False; otherwise capped at WARN.
        On data-fetch failure the check returns flag="OK" to avoid spurious blocks.
        """
        vol_multiplier = REGIME_VOL_MULTIPLIERS.get(regime, 1.0)

        try:
            gap_pct = self._get_avg_candle_gap(symbol)
        except Exception as exc:
            logger.warning(
                "check_slippage: candle fetch failed symbol=%s err=%s", symbol, exc
            )
            result = {
                "symbol":               symbol,
                "implied_slippage_bps": 0.0,
                "flag":                 "OK",
                "regime_multiplier":    vol_multiplier,
                "regime":               regime,
                "note":                 f"candle fetch failed: {exc}",
            }
            self.last_slippage_results[symbol] = result
            return result

        implied_bps = gap_pct * 10_000.0 * vol_multiplier

        if implied_bps > SLIPPAGE_BLOCK_BPS:
            flag = "BLOCK" if not self._paper_trading else "WARN"
            logger.warning(
                "check_slippage %s symbol=%s implied_bps=%.1f > %d "
                "regime=%s multiplier=%.1f%s",
                "BLOCK" if not self._paper_trading else "[PAPER] BLOCK-level",
                symbol, implied_bps, SLIPPAGE_BLOCK_BPS, regime, vol_multiplier,
                " — paper mode, not blocking" if self._paper_trading else "",
            )
        elif implied_bps > SLIPPAGE_WARN_BPS:
            flag = "WARN"
            logger.warning(
                "check_slippage WARN symbol=%s implied_bps=%.1f > %d "
                "regime=%s multiplier=%.1f",
                symbol, implied_bps, SLIPPAGE_WARN_BPS, regime, vol_multiplier,
            )
        else:
            flag = "OK"

        result = {
            "symbol":               symbol,
            "implied_slippage_bps": round(implied_bps, 2),
            "flag":                 flag,
            "regime_multiplier":    vol_multiplier,
            "regime":               regime,
        }
        self.last_slippage_results[symbol] = result
        return result

    # ── Check 2: Liquidity Dependency ─────────────────────────────────────────

    def check_liquidity(self, symbol: str, order_size_usd: float) -> dict:
        """
        Estimate fill rate from live order book (OKX perps) or yfinance volume (ETFs).

        OKX symbols (e.g. BTC-USDT-SWAP):
            GET /api/v5/market/books?instId={symbol}&sz=5
            estimated_available_liquidity_usd = sum(price × size for top-5 bids)

        Equity ETF symbols (e.g. SCHD, SH):
            estimated_available_liquidity_usd = volume × last_price / 390
            (per-minute proxy assuming 390 trading minutes per day)

        fill_rate_estimate = min(1.0, available_usd / order_size_usd)

        Returns
        -------
        {
            "symbol":             str,
            "fill_rate_estimate": float,
            "available_usd":      float,
            "order_size_usd":     float,
            "flag":               "OK" | "WARN" | "BLOCK",
        }

        BLOCK returned only when paper_trading=False; otherwise capped at WARN.
        On data-fetch failure the check returns flag="OK".
        """
        try:
            available_usd = self._get_available_liquidity(symbol)
        except Exception as exc:
            logger.warning(
                "check_liquidity: fetch failed symbol=%s err=%s", symbol, exc
            )
            result = {
                "symbol":             symbol,
                "fill_rate_estimate": 1.0,
                "available_usd":      0.0,
                "order_size_usd":     order_size_usd,
                "flag":               "OK",
                "note":               f"liquidity fetch failed: {exc}",
            }
            self.last_liquidity_results[symbol] = result
            return result

        fill_rate = (
            min(1.0, available_usd / order_size_usd)
            if order_size_usd > 0 else 1.0
        )

        if fill_rate < LIQUIDITY_BLOCK_FILL:
            flag = "BLOCK" if not self._paper_trading else "WARN"
            logger.warning(
                "check_liquidity %s symbol=%s fill_rate=%.3f < %.2f "
                "available=$%.0f order=$%.0f%s",
                "BLOCK" if not self._paper_trading else "[PAPER] BLOCK-level",
                symbol, fill_rate, LIQUIDITY_BLOCK_FILL,
                available_usd, order_size_usd,
                " — paper mode, not blocking" if self._paper_trading else "",
            )
        elif fill_rate < LIQUIDITY_WARN_FILL:
            flag = "WARN"
            logger.warning(
                "check_liquidity WARN symbol=%s fill_rate=%.3f < %.2f "
                "available=$%.0f order=$%.0f",
                symbol, fill_rate, LIQUIDITY_WARN_FILL,
                available_usd, order_size_usd,
            )
        else:
            flag = "OK"

        result = {
            "symbol":             symbol,
            "fill_rate_estimate": round(fill_rate, 4),
            "available_usd":      round(available_usd, 2),
            "order_size_usd":     order_size_usd,
            "flag":               flag,
        }
        self.last_liquidity_results[symbol] = result
        return result

    # ── Check 3: Intermediary Latency ─────────────────────────────────────────

    def check_latency(self, sources: Optional[List[str]] = None) -> dict:
        """
        Compute p95 latency per source from the rolling 10-reading history.

        Per-source BLOCK (> 20 s): live mode only.
        Total fetch BLOCK (> 45 s): enforced regardless of paper_trading.

        Returns
        -------
        {
            "per_source_p95_ms": {source: float, ...},
            "total_fetch_ms":    float,
            "flag":              "OK" | "WARN" | "BLOCK",
            "stale_sources":     [source, ...],
        }
        """
        if sources is None:
            sources = list(self._latency_history.keys())

        per_source_p95: Dict[str, float] = {}
        stale_sources: List[str] = []
        total_fetch_ms = 0.0
        worst_flag = "OK"

        for source in sources:
            readings = list(self._latency_history.get(source, []))
            if not readings:
                continue
            p95 = self._p95(readings)
            per_source_p95[source] = round(p95, 1)
            total_fetch_ms += p95

            if p95 > LATENCY_BLOCK_MS:
                stale_sources.append(source)
                flag = "BLOCK" if not self._paper_trading else "WARN"
                if flag == "BLOCK":
                    worst_flag = "BLOCK"
                elif worst_flag == "OK":
                    worst_flag = "WARN"
                logger.warning(
                    "check_latency %s source=%s p95=%.0fms > %dms",
                    flag, source, p95, LATENCY_BLOCK_MS,
                )
            elif p95 > LATENCY_WARN_MS:
                stale_sources.append(source)
                if worst_flag == "OK":
                    worst_flag = "WARN"
                logger.warning(
                    "check_latency WARN source=%s p95=%.0fms > %dms",
                    source, p95, LATENCY_WARN_MS,
                )

        # Total fetch BLOCK applies regardless of mode
        if total_fetch_ms > TOTAL_BLOCK_MS:
            worst_flag = "BLOCK"
            logger.warning(
                "check_latency BLOCK total=%.0fms > %dms "
                "(60s cycle cannot absorb this fetch latency)",
                total_fetch_ms, TOTAL_BLOCK_MS,
            )

        result = {
            "per_source_p95_ms": per_source_p95,
            "total_fetch_ms":    round(total_fetch_ms, 1),
            "flag":              worst_flag,
            "stale_sources":     stale_sources,
        }
        self.last_latency_result = result
        return result

    # ── Prometheus metrics ────────────────────────────────────────────────────

    def prometheus_metrics(self) -> str:
        """
        Return Prometheus text lines for all three execution risk gauges.

        Gauges emitted:
            mara_slippage_bps{symbol, regime}
            mara_fill_rate{symbol}
            mara_latency_p95_ms{source}
        """
        lines: List[str] = []

        for symbol, r in self.last_slippage_results.items():
            safe_sym = _prom_label(symbol)
            regime   = _prom_label(r.get("regime", "unknown"))
            lines.append(
                f'mara_slippage_bps{{symbol="{safe_sym}",regime="{regime}"}} '
                f'{r.get("implied_slippage_bps", 0.0):.2f}'
            )

        for symbol, r in self.last_liquidity_results.items():
            safe_sym = _prom_label(symbol)
            lines.append(
                f'mara_fill_rate{{symbol="{safe_sym}"}} '
                f'{r.get("fill_rate_estimate", 1.0):.4f}'
            )

        for source, p95 in self.last_latency_result.get("per_source_p95_ms", {}).items():
            lines.append(
                f'mara_api_latency_p95_ms{{source="{source}"}} {p95:.1f}'
            )

        return "\n".join(lines) + ("\n" if lines else "")

    # ── Private: data helpers ─────────────────────────────────────────────────

    def _get_avg_candle_gap(self, symbol: str) -> float:
        """
        Return the mean (high - low) / open over the last 20 candles.

        OKX perps (contain a hyphen, e.g. BTC-USDT-SWAP):
            Converted to ccxt format (BTC/USDT) and fetched via get_crypto_ohlcv
            which uses Kraken public endpoint (no auth, not geo-blocked).

        Equity ETFs (e.g. SH, SCHD, PSQ):
            yfinance .history(period="30d", interval="1d") — no 4 h bars on
            yfinance; daily OHLCV is sufficient for the gap estimate at this
            capital scale.
        """
        if _is_okx_symbol(symbol):
            from data.feeds.market_data import get_crypto_ohlcv
            ccxt_sym = _okx_to_ccxt(symbol)
            df = get_crypto_ohlcv(symbol=ccxt_sym, timeframe="4h", limit=20)
            safe_open = df["open"].astype(float).where(df["open"].astype(float) > 0)
            gaps = (df["high"].astype(float) - df["low"].astype(float)) / safe_open
            return float(gaps.dropna().mean())
        else:
            import yfinance as yf
            hist = yf.Ticker(symbol).history(period="30d", interval="1d")
            if hist.empty:
                raise ValueError(f"No yfinance OHLCV for {symbol}")
            safe_open = hist["Open"].where(hist["Open"] > 0)
            gaps = (hist["High"] - hist["Low"]) / safe_open
            return float(gaps.dropna().tail(20).mean())

    def _get_available_liquidity(self, symbol: str) -> float:
        """
        Estimate available USD liquidity.

        OKX perps: sum top-5 bid levels (price × size) from public order book.
        ETFs: three_month_average_volume × last_price / 390 (per-minute proxy).
        """
        if _is_okx_symbol(symbol):
            url  = (
                "https://www.okx.com/api/v5/market/books"
                f"?instId={symbol}&sz=5"
            )
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json().get("data", [{}])[0]
            bids = data.get("bids", [])
            total = 0.0
            for level in bids[:5]:
                total += float(level[0]) * float(level[1])
            return total
        else:
            import yfinance as yf
            fi    = yf.Ticker(symbol).fast_info
            vol   = getattr(fi, "three_month_average_volume", None) or 0
            price = getattr(fi, "last_price", None) or 0.0
            return float(vol) * float(price) / 390.0

    @staticmethod
    def _p95(values: list) -> float:
        """95th-percentile of a list of floats."""
        if not values:
            return 0.0
        s   = sorted(values)
        idx = min(len(s) - 1, max(0, math.ceil(len(s) * 0.95) - 1))
        return float(s[idx])


# ── Module-level helpers ──────────────────────────────────────────────────────

def _is_okx_symbol(symbol: str) -> bool:
    """True for OKX perp symbols like BTC-USDT-SWAP (identified by hyphen)."""
    return "-" in symbol


def _okx_to_ccxt(symbol: str) -> str:
    """BTC-USDT-SWAP → BTC/USDT for ccxt / Kraken."""
    parts = symbol.split("-")
    return f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else symbol


def _prom_label(value: str) -> str:
    """Sanitise a string for safe use as a Prometheus label value."""
    return value.replace('"', "").replace("\\", "")
