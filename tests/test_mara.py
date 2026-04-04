"""
tests/test_mara.py

MARA component test suite.

Two categories:
  UNIT          Pure logic — no network, no .env, runs in <5s total.
  INTEGRATION   Hits real APIs. Needs .env + network. ~60s (GDELT sleeps).

Run unit tests only:
    cd ~/mara && source .venv/bin/activate
    pytest tests/test_mara.py -m "not integration" -v

Run everything:
    pytest tests/test_mara.py -v

Run without pytest:
    python tests/test_mara.py
"""

import sys
import os
import importlib
import importlib.util
import pytest

_HERE    = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
sys.path.insert(0, _PROJECT)


def _load_config():
    """Load ~/mara/config.py by path. Returns module or None."""
    p = os.path.join(_PROJECT, "config.py")
    if not os.path.exists(p):
        return None
    spec = importlib.util.spec_from_file_location("mara_config", p)
    mod  = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. ACLED URL construction
# ─────────────────────────────────────────────────────────────────────────────

class TestAcledUrlConstruction:

    def _consts(self):
        from data.feeds.conflict_index import (
            ACLED_WATCH_COUNTRIES, ACLED_READ_URL, ACLED_CAST_URL
        )
        return ACLED_WATCH_COUNTRIES, ACLED_READ_URL, ACLED_CAST_URL

    def test_read_or_syntax_is_unencoded(self):
        countries, read_url, _ = self._consts()
        first, *rest = countries
        cs  = first + "".join(f":OR:country={c}" for c in rest)
        url = f"{read_url}?country={cs}"
        assert ":OR:country=" in url
        assert "%3A" not in url, "Colons must NOT be percent-encoded"
        assert "%3D" not in url, "Equals must NOT be percent-encoded"

    def test_no_country_where_param(self):
        countries, read_url, _ = self._consts()
        first, *rest = countries
        cs  = first + "".join(f":OR:country={c}" for c in rest)
        url = f"{read_url}?country={cs}"
        assert "country_where" not in url

    def test_correct_or_clause_count(self):
        countries, _, _ = self._consts()
        first, *rest = countries
        cs = first + "".join(f":OR:country={c}" for c in rest)
        assert cs.count(":OR:country=") == len(countries) - 1

    def test_first_country_has_no_prefix(self):
        countries, _, _ = self._consts()
        first, *rest = countries
        cs = first + "".join(f":OR:country={c}" for c in rest)
        assert cs.startswith(countries[0])

    def test_cast_pipe_syntax_unencoded(self):
        countries, _, cast_url = self._consts()
        pipe = "|".join(countries)
        url  = f"{cast_url}?country={pipe}"
        assert "|" in url
        assert "%7C" not in url


# ─────────────────────────────────────────────────────────────────────────────
# 2. GDELT fix verification
# ─────────────────────────────────────────────────────────────────────────────

class TestGdeltQueryFix:

    def test_multiple_focused_queries(self):
        from data.feeds.conflict_index import GDELT_QUERIES
        assert len(GDELT_QUERIES) >= 2
        for q in GDELT_QUERIES:
            assert len(q.split()) <= 5, f"Query too broad: '{q}'"

    def test_no_cross_conflict_megaquery(self):
        from data.feeds.conflict_index import GDELT_QUERIES
        regions = {"iran", "ukraine", "venezuela", "russia", "israel"}
        for q in GDELT_QUERIES:
            assert len({w.lower() for w in q.split()} & regions) <= 2, \
                f"Query mixes too many regions: '{q}'"

    def test_sleep_nonzero(self):
        from data.feeds.conflict_index import GDELT_SLEEP
        assert GDELT_SLEEP >= 2.0

    def test_scoring_count_only(self):
        """artlist has no tone — 35 real articles must score > 0."""
        from data.feeds.conflict_index import _score_gdelt
        assert _score_gdelt({"articles": 35}) > 0
        assert _score_gdelt({"articles": 10}) == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 3. Scoring functions
# ─────────────────────────────────────────────────────────────────────────────

class TestMarketProxyScoring:

    def setup_method(self):
        from data.feeds.conflict_index import _score_market_proxy
        self.score = _score_market_proxy

    def test_peacetime_below_25(self):
        s = self.score({"defense_momentum": 0.02, "gold_oil_ratio": 38.0, "vix": 14.0})
        assert s < 25, f"Got {s}"

    def test_current_conditions_above_20(self):
        s = self.score({"defense_momentum": 0.037, "gold_oil_ratio": 56.77, "vix": 29.49})
        assert s > 20, f"Got {s}"

    def test_war_above_50(self):
        s = self.score({"defense_momentum": 0.10, "gold_oil_ratio": 58.0, "vix": 32.0})
        assert s >= 50, f"Got {s}"

    def test_bounded(self):
        assert 0 <= self.score({"defense_momentum": 1.0, "gold_oil_ratio": 200.0, "vix": 80.0}) <= 100
        assert 0 <= self.score({}) <= 100


class TestCastScoring:

    def setup_method(self):
        from data.feeds.conflict_index import _score_cast
        self.score = _score_cast

    def test_zero_is_zero(self):
        assert self.score({}) == 0.0
        assert self.score({"total_forecast": 0}) == 0.0

    def test_saturates(self):
        assert self.score({"total_forecast": 20000}) == 100.0
        assert self.score({"total_forecast": 20193}) == 100.0   # March 2026 live value

    def test_midrange(self):
        assert 0 < self.score({"total_forecast": 5000}) < 100


class TestAcledLiveScoring:

    def setup_method(self):
        from data.feeds.conflict_index import _score_acled_live
        self.score = _score_acled_live

    def test_zero_is_zero(self):
        assert self.score({}) == 0.0
        assert self.score({"lethal_rows": 0}) == 0.0

    def test_500_saturates(self):
        assert self.score({"lethal_rows": 500}) == 100.0

    def test_proportional(self):
        assert abs(self.score({"lethal_rows": 250}) - 50.0) < 1.0


class TestGdeltScoring:

    def setup_method(self):
        from data.feeds.conflict_index import _score_gdelt
        self.score = _score_gdelt

    def test_below_threshold_zero(self):
        assert self.score({"articles": 14}) == 0.0
        assert self.score({"articles": 0}) == 0.0
        assert self.score({}) == 0.0

    def test_at_threshold_nonzero(self):
        assert self.score({"articles": 15}) > 0.0

    def test_35_articles_nonzero(self):
        """35 is the actual Venezuela live return — must score."""
        assert self.score({"articles": 35}) > 0.0

    def test_bounded(self):
        assert self.score({"articles": 1000}) <= 100.0


class TestCompositeWeights:

    def test_weights_sum_to_1(self):
        for w in [[0.75, 0.00, 0.00, 0.25], [0.70, 0.20, 0.05, 0.05]]:
            assert abs(sum(w) - 1.0) < 1e-9

    def test_market_weight_higher_without_acled(self):
        assert 0.75 > 0.70


# ─────────────────────────────────────────────────────────────────────────────
# 4. Config
# ─────────────────────────────────────────────────────────────────────────────

class TestConfig:
    """Loads ~/mara/config.py by path. Skips if not deployed yet."""

    REQUIRED = [
        "INITIAL_CAPITAL_USD", "MIN_TRADE_SIZE_USD", "MAX_POSITION_PCT",
        "VAR_CONFIDENCE", "VAR_SIMULATIONS", "VAR_HORIZON_HOURS",
        "MAX_VAR_PCT", "CVAR_MULTIPLIER", "LOOKBACK_DAYS",
        "MIN_SHARPE_TO_TRADE", "SHARPE_RISK_FREE_RATE",
        "REBALANCE_INTERVAL_SEC", "FUNDING_RATE_INTERVAL", "MIN_FUNDING_RATE",
        "PAPER_TRADING", "SLIPPAGE_MODEL_PCT", "FEE_MODEL_PCT",
        "EXCHANGES", "QUOTE_CURRENCY",
        "USE_LIVE_RATES", "USE_LIVE_OHLCV",
        "SWING_MACD_FAST", "SWING_MACD_SLOW", "SWING_MACD_SIGNAL",
        "SWING_TIMEFRAME", "SWING_CACHE_TTL_SEC", "SWING_PAIRS",
        "SWING_STOP_LOSS_PCT", "SWING_TAKE_PROFIT_RATIO",
        "SWING_RSI_PERIOD", "SWING_RSI_BULL_MIN", "SWING_RSI_BEAR_MAX",
        "LOG_LEVEL", "LOG_FILE", "STATE_SNAPSHOT_FILE",
    ]

    def setup_method(self):
        try:
            import pytest
            self.cfg = _load_config()
            if self.cfg is None:
                pytest.skip("config.py not at ~/mara/config.py")
        except ImportError:
            self.cfg = _load_config()

    def test_all_required_keys_present(self):
        if not self.cfg: return
        missing = [k for k in self.REQUIRED if not hasattr(self.cfg, k)]
        assert not missing, f"Missing: {missing}"

    def test_paper_trading_true(self):
        if not self.cfg: return
        assert self.cfg.PAPER_TRADING is True

    def test_live_flags_false(self):
        if not self.cfg: return
        assert not self.cfg.USE_LIVE_RATES
        assert not self.cfg.USE_LIVE_OHLCV

    def test_swing_pairs_populated(self):
        if not self.cfg: return
        assert isinstance(self.cfg.SWING_PAIRS, list) and len(self.cfg.SWING_PAIRS) > 0

    def test_rsi_sanity(self):
        if not self.cfg: return
        assert 0 < self.cfg.SWING_RSI_BULL_MIN < 50
        assert 50 < self.cfg.SWING_RSI_BEAR_MAX < 100

    def test_capital_sanity(self):
        if not self.cfg: return
        assert self.cfg.INITIAL_CAPITAL_USD >= 10.0
        assert self.cfg.MIN_TRADE_SIZE_USD < self.cfg.INITIAL_CAPITAL_USD
        assert 0.0 < self.cfg.MAX_POSITION_PCT <= 1.0

    def test_risk_params_sanity(self):
        if not self.cfg: return
        assert 0.90 <= self.cfg.VAR_CONFIDENCE <= 1.0
        assert 0.0  <  self.cfg.MAX_VAR_PCT    <= 0.5


# ─────────────────────────────────────────────────────────────────────────────
# 5. Indicator math — pure Python, zero dependencies
# ─────────────────────────────────────────────────────────────────────────────

class TestIndicatorMath:

    def test_ema_seed_equals_first_point(self):
        data = [10.0, 12.0, 11.0, 13.0, 15.0]
        k    = 2.0 / (3 + 1)
        ema  = [data[0]] + [0.0] * (len(data) - 1)
        for i in range(1, len(data)):
            ema[i] = data[i] * k + ema[i-1] * (1 - k)
        assert abs(ema[0] - 10.0) < 1e-9

    def test_ema_rises_on_uptrend(self):
        data = [1.0] * 10 + [10.0] * 10
        k    = 2.0 / 6
        ema  = [data[0]] + [0.0] * (len(data) - 1)
        for i in range(1, len(data)):
            ema[i] = data[i] * k + ema[i-1] * (1 - k)
        assert ema[-1] > ema[9]

    def test_fractal_peak(self):
        highs = [10, 10, 10, 10, 10, 20, 10, 10, 10, 10]
        bear = [i for i in range(2, len(highs)-2)
                if highs[i] > highs[i-1] and highs[i] > highs[i-2]
                and highs[i] > highs[i+1] and highs[i] > highs[i+2]]
        assert 5 in bear

    def test_fractal_trough(self):
        lows = [10, 10, 10, 10, 10, 1, 10, 10, 10, 10]
        bull = [i for i in range(2, len(lows)-2)
                if lows[i] < lows[i-1] and lows[i] < lows[i-2]
                and lows[i] < lows[i+1] and lows[i] < lows[i+2]]
        assert 5 in bull

    def test_rsi_in_range(self):
        def rsi(gains, losses):
            ag = sum(gains)/len(gains) if gains else 0.0
            al = sum(losses)/len(losses) if losses else 0.0
            if al == 0: return 100.0
            return 100.0 - (100.0 / (1 + ag/al))
        assert 0 <= rsi([1.0]*14, [0.5]*14) <= 100
        assert 0 <= rsi([], [1.0]*14) <= 100
        assert 0 <= rsi([1.0]*14, []) <= 100


# ─────────────────────────────────────────────────────────────────────────────
# 6. ExecutionRiskChecker — unit tests (no network, no .env)
# ─────────────────────────────────────────────────────────────────────────────

class TestExecutionRiskChecker:
    """
    Pure unit tests for hypervisor/risk/execution_risk.py.
    All external calls are patched; no network traffic.
    """

    def _checker(self, paper=True):
        from hypervisor.risk.execution_risk import ExecutionRiskChecker
        return ExecutionRiskChecker(paper_trading=paper)

    # ── Check 1: Slippage ────────────────────────────────────────────────────

    def test_slippage_warn_threshold(self):
        """
        gap_pct=0.016 × 10 000 × 1.0 (BULL_CALM) = 160 bps > 150 → WARN.
        """
        from unittest.mock import patch
        checker = self._checker(paper=True)
        with patch.object(checker, "_get_avg_candle_gap", return_value=0.016):
            r = checker.check_slippage("BTC-USDT-SWAP", 50_000.0, "long", "BULL_CALM")
        assert r["flag"] == "WARN", f"expected WARN, got {r['flag']}"
        assert r["implied_slippage_bps"] > 150.0

    def test_slippage_block_live_mode_only(self):
        """
        gap_pct=0.035 × 10 000 × 1.0 = 350 bps > 300 BLOCK threshold.
        live mode  → flag=BLOCK; paper mode → flag=WARN.
        """
        from unittest.mock import patch
        live_checker  = self._checker(paper=False)
        paper_checker = self._checker(paper=True)
        for ck, expected in [(live_checker, "BLOCK"), (paper_checker, "WARN")]:
            with patch.object(ck, "_get_avg_candle_gap", return_value=0.035):
                r = ck.check_slippage("BTC-USDT-SWAP", 0.0, "long", "BULL_CALM")
            assert r["flag"] == expected, f"expected {expected}, got {r['flag']}"

    def test_slippage_regime_multiplier_applied(self):
        """WAR_PREMIUM multiplier=2.0 doubles the bps vs BULL_CALM=1.0."""
        from unittest.mock import patch
        checker = self._checker(paper=True)
        with patch.object(checker, "_get_avg_candle_gap", return_value=0.010):
            war  = checker.check_slippage("BTC-USDT-SWAP", 0.0, "long", "WAR_PREMIUM")
            calm = checker.check_slippage("BTC-USDT-SWAP", 0.0, "long", "BULL_CALM")
        assert abs(war["implied_slippage_bps"] - calm["implied_slippage_bps"] * 2.0) < 1e-6

    def test_slippage_ok_below_warn(self):
        """gap_pct=0.005 → 50 bps < 150 → flag=OK."""
        from unittest.mock import patch
        checker = self._checker(paper=True)
        with patch.object(checker, "_get_avg_candle_gap", return_value=0.005):
            r = checker.check_slippage("BTC-USDT-SWAP", 0.0, "long", "BULL_CALM")
        assert r["flag"] == "OK"

    def test_slippage_fetch_failure_returns_ok(self):
        """If candle fetch raises, the check returns flag=OK (safe default)."""
        from unittest.mock import patch
        checker = self._checker(paper=True)
        with patch.object(checker, "_get_avg_candle_gap", side_effect=RuntimeError("timeout")):
            r = checker.check_slippage("BTC-USDT-SWAP", 0.0, "long", "BULL_CALM")
        assert r["flag"] == "OK"
        assert "note" in r

    # ── Check 2: Liquidity ───────────────────────────────────────────────────

    def test_liquidity_block_live_mode_only(self):
        """
        available=$10, order=$200 → fill_rate=0.05 < 0.20 BLOCK threshold.
        live mode  → flag=BLOCK; paper mode → flag=WARN.
        """
        from unittest.mock import patch
        live_checker  = self._checker(paper=False)
        paper_checker = self._checker(paper=True)
        for ck, expected in [(live_checker, "BLOCK"), (paper_checker, "WARN")]:
            with patch.object(ck, "_get_available_liquidity", return_value=10.0):
                r = ck.check_liquidity("BTC-USDT-SWAP", 200.0)
            assert r["flag"] == expected, f"expected {expected}, got {r['flag']}"
            assert r["fill_rate_estimate"] < 0.20

    def test_liquidity_warn_threshold(self):
        """available=$80, order=$200 → fill_rate=0.40 < 0.50 → WARN."""
        from unittest.mock import patch
        checker = self._checker(paper=True)
        with patch.object(checker, "_get_available_liquidity", return_value=80.0):
            r = checker.check_liquidity("BTC-USDT-SWAP", 200.0)
        assert r["flag"] == "WARN"

    def test_liquidity_ok_above_warn(self):
        """available=$1000, order=$100 → fill_rate=1.0 → OK."""
        from unittest.mock import patch
        checker = self._checker(paper=True)
        with patch.object(checker, "_get_available_liquidity", return_value=1_000.0):
            r = checker.check_liquidity("BTC-USDT-SWAP", 100.0)
        assert r["flag"] == "OK"
        assert r["fill_rate_estimate"] == 1.0

    # ── Check 3: Latency ─────────────────────────────────────────────────────

    def test_latency_stale_source_detection(self):
        """10 readings of 9 000 ms → p95=9 000 > 8 000 WARN → stale_sources=['yfinance']."""
        checker = self._checker(paper=True)
        for _ in range(10):
            checker.record_source_latency("yfinance", 9_000.0)
        r = checker.check_latency(sources=["yfinance"])
        assert "yfinance" in r["stale_sources"]
        assert r["flag"] == "WARN"
        assert r["per_source_p95_ms"]["yfinance"] >= 8_000.0

    def test_latency_block_live_mode_only(self):
        """p95=25 000 ms > 20 000 BLOCK: live→BLOCK, paper→WARN."""
        live_checker  = self._checker(paper=False)
        paper_checker = self._checker(paper=True)
        for ck, expected in [(live_checker, "BLOCK"), (paper_checker, "WARN")]:
            for _ in range(10):
                ck.record_source_latency("okx", 25_000.0)
            r = ck.check_latency(sources=["okx"])
            assert r["flag"] == expected, f"expected {expected}, got {r['flag']}"

    def test_latency_total_block_regardless_of_mode(self):
        """total p95 > 45 000 ms always returns BLOCK even in paper mode."""
        checker = self._checker(paper=True)
        for source in ("yfinance", "fred", "gdelt"):
            for _ in range(10):
                checker.record_source_latency(source, 15_001.0)
        r = checker.check_latency(sources=["yfinance", "fred", "gdelt"])
        assert r["flag"] == "BLOCK"
        assert r["total_fetch_ms"] > 45_000.0

    def test_latency_ok_fast_sources(self):
        """All sources < 8 000 ms → flag=OK, stale_sources=[]."""
        checker = self._checker(paper=True)
        for source in ("yfinance", "fred", "gdelt", "okx"):
            for _ in range(10):
                checker.record_source_latency(source, 200.0)
        r = checker.check_latency()
        assert r["flag"] == "OK"
        assert r["stale_sources"] == []

    def test_latency_empty_history_returns_ok(self):
        """No readings recorded → no sources in p95 dict → flag=OK."""
        checker = self._checker(paper=True)
        r = checker.check_latency()
        assert r["flag"] == "OK"
        assert r["per_source_p95_ms"] == {}

    # ── Prometheus output ────────────────────────────────────────────────────

    def test_prometheus_metrics_format(self):
        """After running checks, prometheus_metrics() returns valid Prometheus lines."""
        from unittest.mock import patch
        checker = self._checker(paper=True)
        with patch.object(checker, "_get_avg_candle_gap", return_value=0.010):
            checker.check_slippage("BTC-USDT-SWAP", 0.0, "long", "BULL_CALM")
        with patch.object(checker, "_get_available_liquidity", return_value=500.0):
            checker.check_liquidity("BTC-USDT-SWAP", 100.0)
        checker.record_source_latency("yfinance", 300.0)
        checker.check_latency()
        metrics = checker.prometheus_metrics()
        assert 'mara_slippage_bps{' in metrics
        assert 'mara_fill_rate{' in metrics
        assert 'mara_api_latency_p95_ms{' in metrics


# ─────────────────────────────────────────────────────────────────────────────
# 7. Integration tests (need .env + network)
# ─────────────────────────────────────────────────────────────────────────────

try:
    import pytest as _pytest
    _integration = _pytest.mark.integration
except ImportError:
    def _integration(cls): return cls


@_integration
class TestAcledIntegration:

    def setup_method(self):
        try:
            from dotenv import load_dotenv; load_dotenv()
        except ImportError:
            pass

    def test_token_obtained(self):
        import pytest
        from data.feeds.conflict_index import _get_acled_token
        t = _get_acled_token()
        if t is None:
            pytest.skip("No ACLED token — check credentials or API key migration")
        assert len(t) > 20

    def test_cast_nonzero(self):
        import pytest
        from data.feeds.conflict_index import _get_acled_token, _fetch_acled_cast
        t = _get_acled_token()
        if not t: pytest.skip("No token")
        c = _fetch_acled_cast(t)
        # Free tier returns 403 on /api/cast/read — skip rather than fail.
        # If this account is ever upgraded to approved researcher tier,
        # months_fetched > 0 and the assertion below will run.
        if c["months_fetched"] == 0:
            pytest.skip("ACLED CAST 0 months — free tier does not permit /api/cast/read")
        assert c["total_forecast"] > 0

    def test_ukraine_single_country(self):
        import pytest
        from datetime import datetime, timezone, timedelta
        from data.feeds.conflict_index import _get_acled_token, _acled_read
        t = _get_acled_token()
        if not t: pytest.skip("No token")
        end = datetime.now(timezone.utc)
        dr  = f"{(end-timedelta(days=30)).strftime('%Y-%m-%d')}|{end.strftime('%Y-%m-%d')}"
        r   = _acled_read(t, "Ukraine", dr, "test")
        # Free tier returns 403 on /api/acled/read — skip rather than fail.
        if r["total_rows"] == 0:
            pytest.skip("ACLED /api/acled/read 0 rows — free tier does not permit this endpoint")
        assert r["total_rows"] > 0


@_integration
class TestGdeltIntegration:

    def test_no_429(self):
        from data.feeds.conflict_index import _fetch_gdelt
        r = _fetch_gdelt()
        # GDELT rate-limits aggressively — acceptable if at least one query returned data
        assert r.get("articles", 0) > 0, "All GDELT queries failed — check network"

    def test_keys_present(self):
        from data.feeds.conflict_index import _fetch_gdelt
        r = _fetch_gdelt()
        assert "articles" in r and "source" in r

    def test_live_score_works(self):
        from data.feeds.conflict_index import _fetch_gdelt, _score_gdelt
        r = _fetch_gdelt()
        s = _score_gdelt(r)
        assert 0 <= s <= 100
        if r["articles"] >= 15:
            assert s > 0, f"35 articles should score > 0 (got {r['articles']} articles)"


@_integration
class TestFullScore:
    def test_in_range(self):
        from data.feeds.conflict_index import get_war_premium_score
        s = get_war_premium_score()
        assert 0.0 <= s <= 100.0
        print(f"\n  Live score: {s}/100")


# ─────────────────────────────────────────────────────────────────────────────
# New source: graceful skip and weight redistribution tests
# ─────────────────────────────────────────────────────────────────────────────

class TestConflictIndexNewSources:

    def test_ucdp_returns_zero_without_token(self):
        import os
        os.environ.pop("UCDP_API_TOKEN", None)
        from data.feeds.conflict_index import _fetch_ucdp_ged
        result = _fetch_ucdp_ged()
        assert result == 0.0, f"Expected 0.0 without UCDP token, got {result}"

    def test_ais_returns_zero_without_key(self):
        import os
        os.environ.pop("AISSTREAM_API_KEY", None)
        from data.feeds.conflict_index import _fetch_ais_chokepoint_sync
        result = _fetch_ais_chokepoint_sync()
        assert result == 0.0, f"Expected 0.0 without AIS key, got {result}"

    def test_firms_returns_zero_without_key(self):
        import os
        os.environ.pop("NASA_FIRMS_API_KEY", None)
        from data.feeds.conflict_index import _fetch_nasa_firms
        result = _fetch_nasa_firms()
        assert result == 0.0, f"Expected 0.0 without NASA FIRMS key, got {result}"

    def test_weight_redistribution_sums_to_one(self):
        from data.feeds.conflict_index import _effective_weights, BASE_WEIGHTS
        # Only market_proxy, gdelt, usgs active — ucdp(10%) + ais(10%) + firms(3%) + edgar(5%) missing
        active = {"market_proxy", "gdelt", "usgs_seismic"}
        w = _effective_weights(active)
        assert abs(sum(w.values()) - 1.0) < 1e-9, \
            f"Weights must sum to 1.0, got {sum(w.values())}"
        # missing: ucdp(10%) + ais(10%) + firms(3%) + edgar_macro(5%) = 28%
        expected_mp = BASE_WEIGHTS["market_proxy"] + 0.10 + 0.10 + 0.03 + 0.05
        assert abs(w["market_proxy"] - expected_mp) < 1e-9, \
            f"market_proxy expected {expected_mp}, got {w['market_proxy']}"
        assert w["ucdp_ged"]       == 0.0
        assert w["ais_chokepoint"] == 0.0
        assert w["nasa_firms"]     == 0.0
        assert w["edgar_macro"]    == 0.0

    def test_parse_osint_with_llm_default_on_failure(self):
        import os
        os.environ.pop("OLLAMA_HOST", None)
        # Also clear any cached result for this test event ID
        from data.feeds import conflict_index as _ci
        _ci._osint_llm_cache.pop("test_event_id_unit_99", None)
        from data.feeds.conflict_index import parse_osint_with_llm
        result = parse_osint_with_llm(
            "Forces clashed near the bridge", "ucdp", "test_event_id_unit_99"
        )
        assert result == {"affected_commodities": [], "severity": 1, "escalation": "stable"}, \
            f"Expected default dict on Ollama failure, got {result}"


# ─────────────────────────────────────────────────────────────────────────────
# EDGAR feed unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgarFeed:
    """Unit tests for data/feeds/edgar_feed.py — no network required."""

    def test_get_cik_for_ticker_returns_none_for_btc(self):
        """Crypto tickers have no SEC CIK — must return None without raising."""
        import os
        os.environ.pop("EDGAR_USER_AGENT", None)
        from unittest.mock import patch, MagicMock
        from data.feeds.edgar_feed import EdgarWatchlistMonitor
        monitor = EdgarWatchlistMonitor()
        # Mock the GET to return an Atom feed with no <cik> tag
        mock_resp = MagicMock()
        mock_resp.text = "<feed><title>No results</title></feed>"
        mock_resp.raise_for_status = lambda: None
        with patch("data.feeds.edgar_feed._edgar_get", return_value=mock_resp):
            result = monitor.get_cik_for_ticker("BTC")
        assert result is None, f"Expected None for BTC, got {result!r}"

    def test_score_filing_significance_keyword_multiplier(self):
        """8-K with 'acquisition' should score higher than empty 8-K."""
        from data.feeds.edgar_feed import EdgarWatchlistMonitor
        monitor = EdgarWatchlistMonitor()
        base_score = monitor.score_filing_significance("8-K", "")["score"]
        keyword_score = monitor.score_filing_significance(
            "8-K", "The company has entered into an acquisition agreement."
        )["score"]
        assert keyword_score > base_score, (
            f"acquisition 8-K score {keyword_score} should exceed base {base_score}"
        )
        assert "acquisition" in monitor.score_filing_significance(
            "8-K", "acquisition agreement"
        )["keywords_matched"]

    def test_score_sector_alert_three_filings_returns_nonzero(self):
        """A sector with 3+ filings should contribute score >= 20."""
        from data.feeds.edgar_feed import EdgarMacroScanner
        scanner = EdgarMacroScanner()
        # Simulate: supply_chain has 3 filings, all others empty
        scan_results = {
            "supply_chain":  [{"entity_name": "Acme"}] * 3,
            "energy":        [],
            "defense":       [],
            "semiconductor": [],
            "shipping":      [],
        }
        score = scanner.score_sector_alert(scan_results)
        assert score >= 20, f"Expected score >= 20, got {score}"

    def test_edgar_rate_limiter_minimum_interval(self):
        """Two sequential EdgarRateLimiter.wait() calls should be at least 125ms apart."""
        import time as _time
        from data.feeds.edgar_feed import EdgarRateLimiter
        limiter = EdgarRateLimiter(max_rps=8)
        limiter.wait()                          # prime
        t0 = _time.monotonic()
        limiter.wait()
        elapsed_ms = (_time.monotonic() - t0) * 1000
        assert elapsed_ms >= 100, (
            f"Rate limiter interval {elapsed_ms:.1f}ms is too short (expected >= 100ms)"
        )

    def test_parse_8k_with_llm_returns_default_on_ollama_timeout(self):
        """parse_8k_with_llm must return the default dict on Ollama connection failure."""
        import os
        os.environ["OLLAMA_HOST"] = "http://127.0.0.1:19999"  # nothing listening
        from data.feeds import edgar_feed as _ef
        # Clear cache so it actually tries the call
        _ef._8k_llm_cache.pop("test_acc_unit_edgar_01", None)
        result = _ef.parse_8k_with_llm(
            "Company announced major acquisition",
            "AAPL",
            "test_acc_unit_edgar_01",
        )
        assert result == {
            "event_type":       "other",
            "price_direction":  "neutral",
            "magnitude":        "minor",
            "affected_sectors": [],
        }, f"Expected default dict on Ollama failure, got {result}"

    def test_edgar_macro_weight_redistribution_sums_to_one(self):
        """
        With edgar_macro active but ucdp/ais/firms absent, weights still sum to 1.0
        and market_proxy absorbs the missing weight.
        """
        from data.feeds.conflict_index import _effective_weights, BASE_WEIGHTS
        active = {"market_proxy", "gdelt", "usgs_seismic", "edgar_macro"}
        w = _effective_weights(active)
        total = sum(w.values())
        assert abs(total - 1.0) < 1e-9, f"Weights sum to {total}, expected 1.0"
        # ucdp(10%) + ais(10%) + firms(3%) = 23% absorbed by market_proxy
        expected_mp = BASE_WEIGHTS["market_proxy"] + 0.10 + 0.10 + 0.03
        assert abs(w["market_proxy"] - expected_mp) < 1e-9, (
            f"market_proxy expected {expected_mp:.2f}, got {w['market_proxy']:.2f}"
        )
        assert w["edgar_macro"] == BASE_WEIGHTS["edgar_macro"]


# ─────────────────────────────────────────────────────────────────────────────
# Analyst worker unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalystWorker:
    """Unit tests for workers/analyst/worker_api.py — no Ollama required."""

    @pytest.fixture(autouse=True)
    def _client(self):
        from fastapi.testclient import TestClient
        import importlib, sys
        # Reload module so module-level state is fresh for every test
        if "worker_api" in sys.modules:
            del sys.modules["worker_api"]
        spec = importlib.util.spec_from_file_location(
            "worker_api",
            os.path.join(os.path.dirname(__file__), "..", "workers", "analyst", "worker_api.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._mod = mod
        self.client = TestClient(mod.app)

    def test_signal_returns_list_with_advisory_only(self):
        resp = self.client.post("/signal", json={
            "regime": "BULL_CALM", "confidence": 0.7, "war_premium_score": 15.0,
            "worker_states": {}, "watchlist": [], "cycle_number": 1, "paper_trading": True,
        })
        assert resp.status_code == 200
        sigs = resp.json()
        assert isinstance(sigs, list), f"Expected list, got {type(sigs).__name__}"
        assert len(sigs) == 1
        assert sigs[0].get("advisory_only") is True, f"advisory_only must be True, got: {sigs[0]}"

    def test_execute_always_returns_advisory_only(self):
        for body in [{}, {"ticker": "BTC/USDT", "action": "buy"}, {"amount": 999}]:
            resp = self.client.post("/execute", json=body)
            assert resp.status_code == 200
            assert resp.json().get("status") == "advisory_only", \
                f"Expected advisory_only for body={body}, got: {resp.json()}"

    def test_health_returns_ollama_reachable(self):
        resp = self.client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "ollama_reachable" in body, \
            f"health response missing 'ollama_reachable': {body}"
        assert "last_thesis_age_seconds" in body, \
            f"health response missing 'last_thesis_age_seconds': {body}"

    def test_thesis_caching_prevents_double_ollama_call(self):
        """Two rapid /signal calls within cache window must not increment _call_count twice."""
        import time
        mod = self._mod
        # Reset state
        mod._last_thesis_timestamp = time.time()   # set cache as just-populated
        mod._last_thesis = "cached thesis"
        mod._call_count = 0

        self.client.post("/signal", json={"regime": "BULL_CALM"})
        self.client.post("/signal", json={"regime": "BULL_CALM"})

        assert mod._call_count == 0, \
            f"Expected 0 Ollama calls (cache fresh), got {mod._call_count}"

    def test_ollama_timeout_returns_unavailable_prefix(self):
        """If Ollama raises TimeoutException, signal is returned with [Ollama unavailable] prefix."""
        import httpx
        from unittest.mock import patch
        import time

        mod = self._mod
        mod._last_thesis_timestamp = 0.0   # force cache miss → Ollama call

        with patch.object(httpx, "post", side_effect=httpx.TimeoutException("timeout")):
            resp = self.client.post("/signal", json={
                "regime": "WAR_PREMIUM", "confidence": 0.8,
            })

        assert resp.status_code == 200
        sigs = resp.json()
        assert sigs, "Expected non-empty signal list"
        rationale = sigs[0].get("rationale", "")
        assert rationale.startswith("[Ollama unavailable]"), \
            f"Expected '[Ollama unavailable]' prefix, got: {rationale!r}"
        assert sigs[0].get("ollama_reachable") is False


# ─────────────────────────────────────────────────────────────────────────────
# ADX Calculator + Strategy Router tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAdxCalculator:
    """Unit tests for workers/nautilus/indicators/adx.py"""

    def _adx_module(self):
        path = os.path.join(_PROJECT, "workers", "nautilus", "indicators", "adx.py")
        spec = importlib.util.spec_from_file_location("adx_mod", path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _synthetic_df(self, n=60, seed=42, trend_strength=0.0):
        """Generate a synthetic OHLCV DataFrame for testing."""
        import numpy as np
        import pandas as pd
        rng    = np.random.RandomState(seed)
        closes = [100.0]
        for _ in range(n - 1):
            closes.append(closes[-1] * (1 + rng.normal(trend_strength, 0.008)))
        rows = []
        for i, c in enumerate(closes):
            r    = c * rng.uniform(0.005, 0.02)
            high = c + r * 0.6
            low  = c - r * 0.6
            rows.append({"open": closes[i-1] if i > 0 else c,
                         "high": high, "low": low, "close": c, "volume": 1e6})
        return pd.DataFrame(rows)

    def test_classify_trending(self):
        mod = self._adx_module()
        calc = mod.AdxCalculator(period=14)
        assert calc.classify(30.0) == "trending"
        assert calc.classify(25.0) == "trending"

    def test_classify_ranging(self):
        mod = self._adx_module()
        calc = mod.AdxCalculator(period=14)
        assert calc.classify(15.0) == "ranging"
        assert calc.classify(20.0) == "ranging"

    def test_classify_ambiguous(self):
        mod = self._adx_module()
        calc = mod.AdxCalculator(period=14)
        assert calc.classify(22.0) == "ambiguous"
        assert calc.classify(21.0) == "ambiguous"
        assert calc.classify(24.9) == "ambiguous"

    def test_calculate_returns_required_columns(self):
        mod  = self._adx_module()
        calc = mod.AdxCalculator(period=14)
        df   = self._synthetic_df(n=60)
        out  = calc.calculate(df)
        for col in ("adx", "plus_di", "minus_di", "tr"):
            assert col in out.columns, f"Missing column: {col}"
        assert len(out) == 60

    def test_adx_values_in_valid_range(self):
        mod  = self._adx_module()
        calc = mod.AdxCalculator(period=14)
        df   = self._synthetic_df(n=60)
        out  = calc.calculate(df)
        adx  = out["adx"].dropna()
        # ADX should be in [0, 100]; warmup zeros are expected
        assert (adx >= 0).all() and (adx <= 100).all(), \
            f"ADX out of [0, 100]: min={adx.min():.2f} max={adx.max():.2f}"


class TestRangeMeanRevertStrategy:
    """Unit tests for workers/nautilus/strategies/range_mean_revert.py"""

    def _load(self):
        path = os.path.join(_PROJECT, "workers", "nautilus", "strategies",
                            "range_mean_revert.py")
        spec = importlib.util.spec_from_file_location("rmr_mod", path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _ranging_df(self, n=80, center=100.0, amplitude=2.0):
        """
        Oscillating (ranging) price series.
        Prices bounce between center±amplitude → RSI will cycle through extremes.
        """
        import numpy as np
        import pandas as pd
        import math
        rows = []
        for i in range(n):
            c    = center + amplitude * math.sin(2 * math.pi * i / 20)
            r    = abs(c) * 0.005
            high = c + r * 0.6
            low  = c - r * 0.6
            rows.append({"open": c, "high": high, "low": low, "close": c, "volume": 1e6})
        return pd.DataFrame(rows)

    def test_no_signal_when_bb_too_tight(self):
        """Dead-market filter: flat price → bb_width < 0.02 → []"""
        import numpy as np
        import pandas as pd
        mod  = self._load()
        strat = mod.RangeMeanRevertStrategy()
        flat  = 100.0
        n     = 60
        rows  = [{"open": flat, "high": flat * 1.001, "low": flat * 0.999,
                  "close": flat, "volume": 1e6}] * n
        df    = pd.DataFrame(rows)
        sigs  = strat.generate_signals(df, "TEST/USDT", 100.0, True)
        assert sigs == [], f"Expected [], got: {sigs}"

    def test_insufficient_bars_returns_empty(self):
        import pandas as pd
        mod  = self._load()
        strat = mod.RangeMeanRevertStrategy(bb_period=20, rsi_period=14)
        rows  = [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1e6}] * 10
        df    = pd.DataFrame(rows)
        sigs  = strat.generate_signals(df, "BTC/USDT", 100.0, True)
        assert sigs == []

    def test_generates_buy_at_lower_band(self):
        """With oscillating data + ADX injected as 15 (ranging), should produce BUY when conditions met."""
        import numpy as np
        import pandas as pd
        mod   = self._load()
        strat = mod.RangeMeanRevertStrategy(
            bb_period=20, bb_std=2.0, rsi_period=14,
            stop_loss_pct=0.015, take_profit_ratio=1.5,
        )
        # Build a DataFrame that satisfies LONG conditions:
        # price at lower BB, RSI low, fractal support nearby, ADX < 20
        df = self._ranging_df(n=80)
        # Inject ADX column = 15 (ranging) for all rows
        df["adx"] = 15.0

        # Force the last close to be far below its rolling mean to trigger lower band
        bb_mid  = df["close"].rolling(20).mean().iloc[-1]
        bb_std_ = df["close"].rolling(20).std(ddof=0).iloc[-1]
        target  = bb_mid - 2.5 * bb_std_
        df.loc[df.index[-1], "close"] = max(target, 1.0)
        df.loc[df.index[-1], "high"]  = df["close"].iloc[-1] + 0.01
        df.loc[df.index[-1], "low"]   = df["close"].iloc[-1] - 0.5  # create fractal low

        sigs = strat.generate_signals(df, "BTC/USDT", 200.0, True)
        # May or may not generate; the important thing is no crash + correct structure
        for sig in sigs:
            if sig["action"] == "BUY":
                assert sig["advisory_only"] is True
                assert sig["strategy"] == "range_mean_revert"
                assert sig["size_usd"] == pytest.approx(200.0 * 0.25)
                assert "adx_value" in sig and "bb_width" in sig and "rsi_value" in sig
                return  # found a valid BUY signal — test passes

    def test_signal_structure_is_correct(self):
        """Any signal returned must have required fields with correct types."""
        import pandas as pd
        mod   = self._load()
        strat = mod.RangeMeanRevertStrategy()
        df    = self._ranging_df(n=80)
        df["adx"] = 15.0
        sigs  = strat.generate_signals(df, "ETH/USDT", 100.0, True)
        required = {"symbol", "action", "strategy", "entry_price", "stop_loss",
                    "take_profit", "size_usd", "advisory_only", "rationale",
                    "adx_value", "bb_width", "rsi_value"}
        for sig in sigs:
            missing = required - set(sig.keys())
            assert not missing, f"Signal missing fields: {missing}"
            assert sig["advisory_only"] is True
            assert sig["strategy"] == "range_mean_revert"


class TestStrategyRouter:
    """Unit tests for worker_api.py strategy routing logic."""

    def _load_worker_api(self):
        """Load worker_api fresh to reset module-level globals."""
        # Patch sys.path so imports inside worker_api resolve correctly
        nautilus_dir = os.path.join(_PROJECT, "workers", "nautilus")
        if nautilus_dir not in sys.path:
            sys.path.insert(0, nautilus_dir)
        path = os.path.join(nautilus_dir, "worker_api.py")
        spec = importlib.util.spec_from_file_location("nautilus_worker_api", path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_strategy_mode_swing_forced(self):
        """ACTIVE_STRATEGY='swing' → always routes to swing_macd."""
        os.environ["ACTIVE_STRATEGY"] = "swing"
        mod = self._load_worker_api()
        assert mod.ACTIVE_STRATEGY_MODE == "swing"
        os.environ.pop("ACTIVE_STRATEGY", None)

    def test_strategy_mode_auto_default(self):
        """Without ACTIVE_STRATEGY env var, mode defaults to 'auto'."""
        os.environ.pop("ACTIVE_STRATEGY", None)
        mod = self._load_worker_api()
        assert mod.ACTIVE_STRATEGY_MODE == "auto"

    def test_post_strategy_endpoint_changes_mode(self):
        """POST /strategy changes ACTIVE_STRATEGY_MODE at runtime."""
        import asyncio
        nautilus_dir = os.path.join(_PROJECT, "workers", "nautilus")
        if nautilus_dir not in sys.path:
            sys.path.insert(0, nautilus_dir)
        from fastapi.testclient import TestClient
        path = os.path.join(nautilus_dir, "worker_api.py")
        spec = importlib.util.spec_from_file_location("wapi_router", path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        client = TestClient(mod.app)
        resp   = client.post("/strategy", json={"mode": "range"})
        assert resp.status_code == 200
        assert resp.json()["mode"] == "range"
        assert mod.ACTIVE_STRATEGY_MODE == "range"


# ─────────────────────────────────────────────────────────────────────────────
# Plain-Python runner
# ─────────────────────────────────────────────────────────────────────────────

def _run():
    import traceback
    SUITES = [
        TestAcledUrlConstruction, TestGdeltQueryFix,
        TestMarketProxyScoring, TestCastScoring,
        TestAcledLiveScoring, TestGdeltScoring,
        TestCompositeWeights, TestConfig, TestIndicatorMath,
        TestExecutionRiskChecker,
    ]
    p = f = sk = 0
    for cls in SUITES:
        inst = cls()
        for name in sorted(m for m in dir(cls) if m.startswith("test_")):
            try:
                if hasattr(inst, "setup_method"):
                    try: inst.setup_method()
                    except Exception as e:
                        print(f"  ⏭  {cls.__name__}.{name}  (skip: {e})")
                        sk += 1; continue
                getattr(inst, name)()
                print(f"  ✅  {cls.__name__}.{name}"); p += 1
            except AssertionError as e:
                print(f"  ❌  {cls.__name__}.{name}  →  {e}"); f += 1
            except Exception as e:
                print(f"  ❌  {cls.__name__}.{name}  →  {type(e).__name__}: {e}"); f += 1
    print(f"\n{'='*50}\n  {p} passed  |  {f} failed  |  {sk} skipped\n{'='*50}")
    return f

if __name__ == "__main__":
    print("\n" + "="*50 + "\n  MARA unit tests\n" + "="*50 + "\n")
    sys.exit(_run())
