"""
data/feeds/conflict_index.py

Composite War Premium Score  0-100.

Two independent tiers:

  Tier 1  Market proxy  (50% weight) — PRIMARY
          Defense ETF momentum + gold/oil ratio + VIX.
          Always available, no auth required, reacts in real-time.

  Tier 2  OSINT layer   (50% weight)
          Six sources with dynamic weight redistribution.
          When a source is unavailable its weight is absorbed proportionally.

          Source          Base weight  Auth
          ─────────────   ──────────   ─────────────────────────
          GDELT           25%          None
          SEC EDGAR       25%          None (User-Agent only)
          UCDP            15%          None (free REST)
          AIS Maritime    15%          AIS_API_KEY (free)
          NASA FIRMS      10%          NASA_FIRMS_API_KEY (free)
          USGS Seismic    10%          None

Score thresholds consumed by classifier.py:
  < 25   no war signal
  25-50  weak confirmation
  50-70  confirmed
  > 70   very strong

Also exposes:
  get_osint_snapshot() → OSINTPipelineResult  (structured events for analyst)
  get_domain_decisions(osint_result)          → list[DomainDecision]

Verify standalone:
    cd ~/mara && source .venv/bin/activate
    python data/feeds/conflict_index.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.parse
import urllib.request
import json
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


# ── GDELT constants (kept here for backward-compatible test imports) ───────────
GDELT_API   = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_SLEEP = 3.5
GDELT_QUERIES = [
    "Iran Israel military airstrike",
    "Ukraine Russia war invasion",
    "Venezuela cartel military violence",
]

# ── Market proxy constants ─────────────────────────────────────────────────────
DEFENSE_ETFS       = ["ITA", "PPA", "SHLD", "NATO"]
GOLD_OIL_BASELINE  = 35.0
GOLD_OIL_WAR_LEVEL = 52.0

# ── OSINT source base weights (must sum to 1.0) ───────────────────────────────
# April 2026: added aviation (0.12) and company_intel (0.08).
# Existing weights scaled down proportionally to absorb the two new sources.
_OSINT_BASE_WEIGHTS: dict[str, float] = {
    "gdelt":         0.20,   # geopolitical news articles
    "edgar":         0.20,   # SEC 8-K filings + insider trading
    "ucdp":          0.12,   # Uppsala academic conflict events
    "maritime":      0.12,   # AIS vessel density at chokepoints
    "firms":         0.08,   # NASA thermal anomalies (infrastructure fires)
    "usgs":          0.08,   # USGS earthquake events
    "aviation":      0.12,   # OpenSky military/VIP aircraft activity ← new
    "company_intel": 0.08,   # SearXNG + ScrapeGraphAI deep research ← new
}

# Tier weights: market proxy vs OSINT layer
_MARKET_PROXY_WEIGHT = 0.50
_OSINT_LAYER_WEIGHT  = 0.50


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1: Market Proxy (unchanged from previous implementation)
# ─────────────────────────────────────────────────────────────────────────────

def _last_close(h) -> float:
    """
    Extract the last Close value from a yfinance DataFrame as a plain float.
    Handles MultiIndex columns from yfinance ≥0.2.x.
    """
    try:
        col = h["Close"]
        arr = col.values.flatten()
        return float(arr[-1])
    except Exception:
        arr = h.values.flatten()
        return float(arr[-1])


def _fetch_market_proxy() -> dict:
    """Defense ETF 20-day momentum + gold/oil ratio + VIX. No auth needed."""
    try:
        import yfinance as yf

        momentum = 0.0
        fetched  = 0
        for ticker in DEFENSE_ETFS:
            try:
                h = yf.download(ticker, period="30d", interval="1d",
                                progress=False, auto_adjust=True)
                if len(h) >= 20:
                    close = h["Close"].values.flatten()
                    momentum += float(close[-1]) / float(close[-20]) - 1
                    fetched  += 1
            except Exception:
                pass
        if fetched > 0:
            momentum /= fetched

        gh    = yf.download("GC=F", period="5d", progress=False, auto_adjust=True)
        oh    = yf.download("CL=F", period="5d", progress=False, auto_adjust=True)
        gold  = _last_close(gh) if len(gh) > 0 else 3000.0
        oil   = _last_close(oh) if len(oh) > 0 else 75.0
        ratio = gold / oil if oil > 0 else 50.0

        vh  = yf.download("^VIX", period="5d", progress=False, auto_adjust=True)
        vix = _last_close(vh) if len(vh) > 0 else 20.0

        return {
            "defense_momentum": round(momentum, 4),
            "gold_oil_ratio":   round(ratio, 2),
            "vix":              round(vix, 2),
        }
    except Exception as exc:
        logger.warning(f"Market proxy fetch failed: {exc} — using safe defaults")
        return {"defense_momentum": 0.02, "gold_oil_ratio": 40.0, "vix": 20.0}


def _score_market_proxy(market: dict) -> float:
    """
    0-100.
    Calibration against known data points:
      Peacetime commodity bull (momentum 0.02, ratio 38, VIX 14)  →  ~2
      Current conditions      (momentum 0.037, ratio 56.77, VIX 29)  → ~38
      Active war scenario     (momentum 0.10,  ratio 58, VIX 32)  → ~55+
    """
    m = market.get("defense_momentum", 0.0)
    r = market.get("gold_oil_ratio",   35.0)
    v = market.get("vix",              15.0)

    m_score = min(50.0, max(0.0, (m - 0.015) / 0.085) * 50)
    r_score = min(30.0, max(0.0, (r - GOLD_OIL_BASELINE) /
                            (GOLD_OIL_WAR_LEVEL - GOLD_OIL_BASELINE)) * 30)
    v_score = min(20.0, max(0.0, (v - 15.0) / 25.0) * 20)
    return round(m_score + r_score + v_score, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Tier 2: OSINT Layer with Dynamic Weight Redistribution
# ─────────────────────────────────────────────────────────────────────────────

def _redistribute_weights(available_sources: set[str]) -> dict[str, float]:
    """
    Recompute source weights given which sources are available.

    Unavailable sources have their weight absorbed proportionally by
    available sources, preserving relative weight ratios.
    Total always sums to 1.0.

    >>> w = _redistribute_weights({"gdelt", "ucdp"})
    >>> abs(sum(w.values()) - 1.0) < 1e-9
    True
    """
    avail = {k: v for k, v in _OSINT_BASE_WEIGHTS.items() if k in available_sources}
    if not avail:
        return {}
    total = sum(avail.values())
    return {k: v / total for k, v in avail.items()}


def _fetch_gdelt_legacy() -> dict:
    """
    Backward-compatible GDELT fetch using urllib.
    Returns {"articles": int, "source": str}.
    """
    best = {"articles": 0, "avg_tone": 0.0, "source": "gdelt_no_data"}

    for i, query in enumerate(GDELT_QUERIES):
        if i > 0:
            time.sleep(GDELT_SLEEP)

        params = urllib.parse.urlencode({
            "query":      query,
            "mode":       "artlist",
            "maxrecords": "75",
            "timespan":   "3d",
            "sort":       "toneasc",
            "format":     "json",
        })
        url = f"{GDELT_API}?{params}"

        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Arka-ConflictIndex/2.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            articles = data.get("articles", [])
            n = len(articles)
            logger.info(f"GDELT [{i+1}] '{query}': {n} articles")
            if n > best["articles"]:
                best = {"articles": n, "source": "gdelt_ok"}

        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                logger.warning(f"GDELT [{i+1}] HTTP 429 — sleeping extra 10s")
                time.sleep(10.0)
                best["source"] = "gdelt_429"
            else:
                logger.warning(f"GDELT [{i+1}] HTTP {exc.code}")
                best["source"] = f"gdelt_http_{exc.code}"
        except Exception as exc:
            logger.warning(f"GDELT [{i+1}] failed: {exc}")
            if best["source"] == "gdelt_no_data":
                best["source"] = "gdelt_error"

    return best


def _score_gdelt(result: dict) -> float:
    """
    0-100. Gate: ≥15 articles. Saturates at 67 articles (100 pts).
    Preserved for backward compatibility with test imports.
    """
    n = result.get("articles", 0)
    if n < 15:
        return 0.0
    return round(min(100.0, n * 1.5), 1)


def _fetch_gdelt():
    """Alias for backward-compatible test imports."""
    return _fetch_gdelt_legacy()


def _fetch_osint_layer() -> tuple[dict[str, float], float]:
    """
    Fetch all 6 OSINT sources, compute per-source scores, and return
    the dynamically-weighted composite OSINT score.

    Returns:
        (source_scores, osint_composite_score)
        source_scores: {source_name: score_0_100}
        osint_composite_score: 0-100
    """
    source_scores: dict[str, float] = {}

    # ── GDELT ────────────────────────────────────────────────────────────────
    try:
        gdelt_result = _fetch_gdelt_legacy()
        source_scores["gdelt"] = _score_gdelt(gdelt_result)
    except Exception as exc:
        logger.warning(f"GDELT fetch failed: {exc}")

    # ── UCDP ─────────────────────────────────────────────────────────────────
    try:
        from data.feeds.ucdp_client import fetch_ucdp_events, score_ucdp_events
        events = fetch_ucdp_events(days_back=30)
        source_scores["ucdp"] = score_ucdp_events(events)
    except Exception as exc:
        logger.debug(f"UCDP unavailable: {exc}")

    # ── SEC EDGAR ─────────────────────────────────────────────────────────────
    try:
        from data.feeds.edgar_client import EdgarIntelClient, score_edgar_signals
        client = EdgarIntelClient()
        loop   = asyncio.new_event_loop()
        try:
            filings  = loop.run_until_complete(client.check_recent_8k_filings(hours_back=48))
            insiders = loop.run_until_complete(client.check_insider_trading(days_back=7))
        finally:
            loop.close()
        source_scores["edgar"] = score_edgar_signals(filings, insiders)
    except Exception as exc:
        logger.debug(f"EDGAR unavailable: {exc}")

    # ── AIS Maritime ──────────────────────────────────────────────────────────
    try:
        from data.feeds.maritime_client import fetch_vessel_activity, score_maritime
        AIS_KEY = os.environ.get("AIS_API_KEY", "")
        if AIS_KEY:
            loop = asyncio.new_event_loop()
            try:
                vessels = loop.run_until_complete(fetch_vessel_activity(timeout_sec=10.0))
            finally:
                loop.close()
            source_scores["maritime"] = score_maritime(vessels)
    except Exception as exc:
        logger.debug(f"AIS maritime unavailable: {exc}")

    # ── NASA FIRMS ────────────────────────────────────────────────────────────
    try:
        from data.feeds.environment_client import (
            fetch_thermal_anomalies, fetch_earthquakes, score_environment
        )
        FIRMS_KEY = os.environ.get("NASA_FIRMS_API_KEY", "")
        if FIRMS_KEY:
            firms = fetch_thermal_anomalies(days_back=1)
            # Always record FIRMS score (even 0.0) — keeps it in the weight pool
            source_scores["firms"] = score_environment(firms, [])
        # USGS needs no key — always available; record even when 0 events
        quakes = fetch_earthquakes(days_back=3)
        source_scores["usgs"] = score_environment([], quakes)
    except Exception as exc:
        logger.debug(f"Environment feeds unavailable: {exc}")

    # ── OpenSky Aviation ─────────────────────────────────────────────────────
    try:
        from data.feeds.aviation_client import fetch_aircraft_states, score_aviation
        aviation_states = fetch_aircraft_states()
        source_scores["aviation"] = score_aviation(aviation_states)
    except Exception as exc:
        logger.debug(f"Aviation feed unavailable: {exc}")

    # ── Company Intelligence (SearXNG + ScrapeGraphAI) ────────────────────────
    try:
        from data.feeds.company_researcher import research_event, score_company_intel
        # Derive a representative event context from the highest-scoring OSINT
        # source seen so far (GDELT articles are the richest text source)
        _gdelt_score = source_scores.get("gdelt", 0.0)
        if _gdelt_score > 20:
            # Only run expensive research pipeline when GDELT shows conflict signal
            from data.feeds.gdelt_client import fetch_gdelt_articles
            articles   = fetch_gdelt_articles(max_articles=3)
            event_text = " ".join(
                a.get("title", "") for a in articles[:3] if a.get("title")
            )
            if event_text:
                profiles = research_event(event_text, max_companies=3)
                source_scores["company_intel"] = score_company_intel(profiles)
    except Exception as exc:
        logger.debug(f"Company intel feed unavailable: {exc}")

    # ── Dynamic weight redistribution ─────────────────────────────────────────
    available      = set(source_scores.keys())
    redistributed  = _redistribute_weights(available)
    if not redistributed:
        return source_scores, 0.0

    composite = sum(
        source_scores.get(src, 0.0) * weight
        for src, weight in redistributed.items()
    )
    logger.info(
        f"OSINT layer: {composite:.1f}/100 "
        f"sources={sorted(available)} "
        f"weights={{{', '.join(f'{k}:{v:.2f}' for k, v in redistributed.items())}}}"
    )
    return source_scores, round(composite, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_war_premium_score() -> float:
    """
    Composite War Premium Score 0-100.
    Tier 1: market proxy 50%.
    Tier 2: OSINT layer 50% (8 sources, dynamic redistribution).
        gdelt, edgar, ucdp, maritime, firms, usgs, aviation, company_intel
    """
    market = _fetch_market_proxy()
    ms     = _score_market_proxy(market)

    _, osint_score = _fetch_osint_layer()

    score = round(ms * _MARKET_PROXY_WEIGHT + osint_score * _OSINT_LAYER_WEIGHT, 1)
    logger.info(
        f"War Premium Score: {score}/100 "
        f"(market={ms} osint={osint_score})"
    )
    return score


def get_osint_snapshot() -> "OSINTPipelineResult":
    """
    Run all 8 OSINT sources through the extraction pipeline.
    Returns OSINTPipelineResult with structured events + per-source scores.

    Sources: gdelt, ucdp, edgar, maritime, firms, usgs, aviation, company_intel
    Used by the analyst worker for thesis generation.
    """
    try:
        from data.feeds.gdelt_client import fetch_gdelt_articles
        from data.feeds.ucdp_client import fetch_ucdp_events
        from data.feeds.edgar_client import EdgarIntelClient
        from data.feeds.maritime_client import fetch_vessel_activity, detect_traffic_anomalies
        from data.feeds.environment_client import fetch_thermal_anomalies, fetch_earthquakes
        from data.feeds.aviation_client import fetch_aircraft_states, detect_aviation_anomalies
        from data.feeds.company_researcher import research_event
        from data.feeds.osint_processor import run_pipeline

        # Fetch raw data
        gdelt_articles = fetch_gdelt_articles()
        ucdp_events    = fetch_ucdp_events(days_back=30)

        edgar_client   = EdgarIntelClient()
        loop           = asyncio.new_event_loop()
        try:
            edgar_filings = loop.run_until_complete(
                edgar_client.check_recent_8k_filings(hours_back=48))
            insider_sigs  = loop.run_until_complete(
                edgar_client.check_insider_trading(days_back=7))
            vessels = []
            if os.environ.get("AIS_API_KEY"):
                vessels = loop.run_until_complete(
                    fetch_vessel_activity(timeout_sec=10.0))
        finally:
            loop.close()

        maritime_anomalies  = detect_traffic_anomalies(vessels)
        firms_records       = []
        if os.environ.get("NASA_FIRMS_API_KEY"):
            firms_records   = fetch_thermal_anomalies(days_back=1)
        quake_features      = fetch_earthquakes(days_back=3)

        # Aviation — always attempt (no API key required)
        aviation_states     = fetch_aircraft_states()
        aviation_anomalies  = detect_aviation_anomalies(aviation_states)

        # Company intelligence — only when GDELT shows a conflict signal
        company_profiles = []
        gdelt_titles     = [a.get("title", "") for a in gdelt_articles[:3]
                            if a.get("title")]
        if gdelt_titles:
            event_text       = " ".join(gdelt_titles)
            company_profiles = research_event(event_text, max_companies=3)

        return run_pipeline(
            gdelt_articles     = gdelt_articles,
            ucdp_events        = ucdp_events,
            edgar_filings      = edgar_filings,
            insider_signals    = insider_sigs,
            maritime_anomalies = maritime_anomalies,
            firms_records      = firms_records,
            quake_features     = quake_features,
            aviation_anomalies = aviation_anomalies,
            company_profiles   = company_profiles,
        )

    except Exception as exc:
        logger.error(f"OSINT snapshot failed: {exc}")
        from data.feeds.osint_processor import OSINTPipelineResult
        return OSINTPipelineResult()


def get_domain_decisions(
    osint_result,
    domain_performance: Optional[dict] = None,
    regime_probs: Optional[dict] = None,
) -> list:
    """
    Run domain router on an OSINTPipelineResult.
    Returns list[DomainDecision].
    """
    try:
        from data.feeds.domain_router import DomainRouter
        router = DomainRouter()
        return router.evaluate(
            osint_events        = osint_result.events,
            edgar_signals       = [],   # insider signals already in osint_result.events
            domain_performance  = domain_performance or {},
            regime_probs        = regime_probs or {"RISK_ON": 0.5, "TRANSITION": 0.5},
        )
    except Exception as exc:
        logger.error(f"Domain router failed: {exc}")
        return []


def _interpret(score: float) -> str:
    if score < 25: return "No war signal"
    if score < 50: return "Weak confirmation"
    if score < 70: return "Conflict signal confirmed"
    return "Strong escalation"


# ─────────────────────────────────────────────────────────────────────────────
# Standalone verification runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level  = logging.INFO,
        format = "%(asctime)s | %(levelname)-8s | %(message)s",
    )

    print("\n" + "=" * 60)
    print("  ARKA CONFLICT INDEX — VERIFICATION")
    print("=" * 60)

    print("\n  Market proxy:")
    market  = _fetch_market_proxy()
    ms      = _score_market_proxy(market)
    print(f"    defense_momentum : {market['defense_momentum']:.4f}")
    print(f"    gold_oil_ratio   : {market['gold_oil_ratio']:.2f}")
    print(f"    vix              : {market['vix']:.2f}")
    print(f"    score            : {ms:.1f}/100")

    print("\n  OSINT layer (all 6 sources):")
    source_scores, osint_score = _fetch_osint_layer()
    for src, score in source_scores.items():
        print(f"    {src:<12}: {score:.1f}/100")
    print(f"    composite  : {osint_score:.1f}/100")

    composite = round(ms * _MARKET_PROXY_WEIGHT + osint_score * _OSINT_LAYER_WEIGHT, 1)

    print("\n  " + "=" * 40)
    print(f"  WAR PREMIUM SCORE : {composite:.1f} / 100")
    print(f"  Interpretation    : {_interpret(composite)}")
    print("  " + "=" * 40)

    pc_score  = round(_score_market_proxy(
        {"defense_momentum": 0.02, "gold_oil_ratio": 38.0, "vix": 14.0}) * _MARKET_PROXY_WEIGHT, 1)
    war_score = round(_score_market_proxy(
        {"defense_momentum": 0.08, "gold_oil_ratio": 57.0, "vix": 30.0}) * _MARKET_PROXY_WEIGHT, 1)

    print(f"\n  False positive check (peacetime commodity bull):")
    print(f"    {pc_score:.1f}/100  "
          f"{'PASS' if pc_score < 25 else 'FAIL — recalibrate thresholds'}")
    print(f"  War scenario check (escalation):")
    print(f"    {war_score:.1f}/100  "
          f"{'PASS' if war_score >= 15 else 'FAIL — recalibrate thresholds'}")
    print("=" * 60 + "\n")
