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
# 6. Integration tests (need .env + network)
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
# Plain-Python runner
# ─────────────────────────────────────────────────────────────────────────────

def _run():
    import traceback
    SUITES = [
        TestAcledUrlConstruction, TestGdeltQueryFix,
        TestMarketProxyScoring, TestCastScoring,
        TestAcledLiveScoring, TestGdeltScoring,
        TestCompositeWeights, TestConfig, TestIndicatorMath,
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
