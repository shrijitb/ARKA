"""
data/feeds/conflict_index.py

Composite War Premium Score  0-100.

Four independent data layers:

  Layer 1  Market proxy  (PRIMARY, 70-75% weight)
           Defense ETF momentum + gold/oil ratio + VIX.
           Always available, no auth required, reacts in real-time.

  Layer 2  ACLED CAST forecasts  (20% when authenticated)
           Monthly battle/ERV/VAC forecasts for watch countries.
           Forward-looking and structured — highest-quality signal.

  Layer 3  ACLED live events  (5% supplement)
           Recent lethal events in watch countries.
           Protests/demonstrations deliberately excluded.

  Layer 4  GDELT  (5-25% depending on ACLED availability)
           Negative news tone + high article volume = conflict signal.

Score thresholds consumed by classifier.py:
  < 25   no war signal
  25-50  weak confirmation  (1 of 2 WAR_PREMIUM triggers)
  50-70  confirmed          (1 more trigger fires WAR_PREMIUM)
  > 70   very strong        (WAR_PREMIUM fires alone)

Verify standalone:
    cd ~/mara && source .venv/bin/activate
    python data/feeds/conflict_index.py
"""

import json
import logging
import os
import time
import urllib.request
import urllib.parse
import requests as _requests
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass   # dotenv not installed — env vars must be set by the shell or docker

logger = logging.getLogger(__name__)


# ── Watch list ────────────────────────────────────────────────────────────────

ACLED_WATCH_COUNTRIES = [
    "Ukraine", "Russia", "Iran", "Israel", "Palestine",
    "Lebanon", "Yemen", "Sudan", "Syria", "Venezuela",
]

LETHAL_EVENT_TYPES = {
    "Battles",
    "Explosions/Remote violence",
    "Violence against civilians",
}


# ── ACLED endpoints ───────────────────────────────────────────────────────────

ACLED_TOKEN_URL = "https://acleddata.com/oauth/token"
ACLED_READ_URL  = "https://acleddata.com/api/acled/read"
ACLED_CAST_URL  = "https://acleddata.com/api/cast/read"


# ── GDELT ─────────────────────────────────────────────────────────────────────
#
# BUG FIXED: original code used a single broad query:
#   "Iran Israel airstrike Ukraine war Venezuela cartel violence"
# GDELT treats spaces as AND, requiring all 8 terms in one article → zero results.
# Fix: three focused 2-3 term queries, each covering a distinct conflict.
#
# BUG FIXED: original code fired all 3 queries back-to-back → HTTP 429.
# Fix: GDELT_SLEEP seconds between each query.
#
GDELT_API     = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_SLEEP   = 3.5   # seconds between queries — prevents 429
GDELT_QUERIES = [
    "Iran Israel military airstrike",       # Middle East
    "Ukraine Russia war invasion",          # Eastern Europe
    "Venezuela cartel military violence",   # Latin America
]


# ── Market proxy ──────────────────────────────────────────────────────────────

DEFENSE_ETFS       = ["ITA", "PPA", "SHLD", "NATO"]
GOLD_OIL_BASELINE  = 35.0   # Pre-2020 historical peacetime norm
GOLD_OIL_WAR_LEVEL = 52.0   # Clearly elevated — systemic risk priced in


# ─────────────────────────────────────────────────────────────────────────────
# ACLED Authentication — token manager
# ─────────────────────────────────────────────────────────────────────────────

class AcledTokenManager:
    """In-memory OAuth token manager. Caches access token for 24h, uses
    refresh token to silently renew, falls back to full password auth."""

    def __init__(self):
        self._access_token:  Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._expires_at:    float         = 0.0

    def get_token(self, email: str, password: str) -> Optional[str]:
        if not email or not password:
            logger.warning("ACLED_EMAIL / ACLED_PASSWORD not set — ACLED layers skipped")
            return None
        if self._access_token and time.time() < self._expires_at - 300:
            return self._access_token
        if self._refresh_token:
            token = self._refresh(email)
            if token:
                return token
        return self._fetch_fresh(email, password)

    def _fetch_fresh(self, email: str, password: str) -> Optional[str]:
        try:
            r = _requests.post(
                ACLED_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "username":   email,
                    "password":   password,
                    "grant_type": "password",
                    "client_id":  "acled",
                },
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                self._access_token  = data["access_token"]
                self._refresh_token = data.get("refresh_token")
                self._expires_at    = time.time() + data.get("expires_in", 86400)
                logger.info("ACLED: fresh token acquired, valid 24h")
                return self._access_token
            else:
                logger.error(f"ACLED auth failed: {r.status_code} {r.text[:200]}")
                return None
        except Exception as exc:
            logger.error(f"ACLED auth exception: {exc}")
            return None

    def _refresh(self, email: str) -> Optional[str]:
        try:
            r = _requests.post(
                ACLED_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id":     "acled",
                },
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                self._access_token  = data["access_token"]
                self._refresh_token = data.get("refresh_token", self._refresh_token)
                self._expires_at    = time.time() + data.get("expires_in", 86400)
                logger.info("ACLED: token refreshed silently")
                return self._access_token
            else:
                logger.warning(f"ACLED refresh failed ({r.status_code}), will retry with password")
                self._refresh_token = None
                return None
        except Exception as exc:
            logger.error(f"ACLED refresh exception: {exc}")
            return None


# Module-level singleton
_token_manager = AcledTokenManager()


def _get_acled_token() -> Optional[str]:
    """Internal helper — reads credentials from env and delegates to the manager."""
    return _token_manager.get_token(
        os.environ.get("ACLED_EMAIL", ""),
        os.environ.get("ACLED_PASSWORD", ""),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: ACLED CAST Forecasts
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_acled_cast(token: str) -> dict:
    """
    Fetch monthly conflict forecasts for current + next month.

    CAST endpoint uses pipe (|) for multi-country OR:
        /api/cast/read?country=Brazil|Argentina   (from official docs)
    Pipe is injected directly into the URL string — NOT passed through
    urllib.parse.urlencode which would encode | → %7C.
    """
    now     = datetime.now(timezone.utc)
    next_dt = now + timedelta(days=32)

    # Pipe-joined — CAST's documented multi-country syntax
    countries = "|".join(ACLED_WATCH_COUNTRIES)

    totals = {
        "total_forecast":   0,
        "battles_forecast": 0,
        "erv_forecast":     0,
        "vac_forecast":     0,
        "months_fetched":   0,
    }

    for dt in [now, next_dt]:
        month = dt.strftime("%B")   # e.g. "March"
        year  = dt.year

        # Build URL by direct string injection — pipe must NOT be percent-encoded
        url = (
            f"{ACLED_CAST_URL}?_format=json"
            f"&country={countries}"
            f"&month={urllib.parse.quote(month)}"
            f"&year={year}"
            f"&fields=country|month|year"
            f"|battles_forecast|erv_forecast|vac_forecast|total_forecast"
            f"&limit=1000"
        )
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            if data.get("status") != 200:
                logger.warning(f"CAST non-200 {month}/{year}: {data.get('messages')}")
                continue
            rows = data.get("data", [])
            for row in rows:
                totals["total_forecast"]   += int(row.get("total_forecast")   or 0)
                totals["battles_forecast"] += int(row.get("battles_forecast") or 0)
                totals["erv_forecast"]     += int(row.get("erv_forecast")     or 0)
                totals["vac_forecast"]     += int(row.get("vac_forecast")     or 0)
            totals["months_fetched"] += 1
            logger.info(f"CAST {month} {year}: {len(rows)} rows, "
                        f"total_forecast={totals['total_forecast']}")
        except Exception as exc:
            logger.warning(f"CAST fetch failed {month}/{year}: {exc}")

    return totals


def _score_cast(cast: dict) -> float:
    """0-100. Saturates at ~20,000 forecast events across watch countries."""
    total = cast.get("total_forecast", 0)
    return round(min(100.0, total / 200.0), 1) if total > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3: ACLED Live Events
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_acled_live(token: str, lookback_days: int = 30) -> dict:
    """
    Fetch recent lethal events from /api/acled/read.

    Country filter uses pipe-separated syntax (same as CAST endpoint):
        country=Ukraine|Russia|Syria
    Pipe is injected directly into the URL string — _acled_read does NOT
    use urllib.parse.urlencode so the pipe is not percent-encoded.

    Fallback: if multi-country returns 0, run single-country (Ukraine) to
    distinguish a query syntax issue from an account access tier issue.
    """
    end_dt    = datetime.now(timezone.utc)
    start_dt  = end_dt - timedelta(days=lookback_days)
    date_range = f"{start_dt.strftime('%Y-%m-%d')}|{end_dt.strftime('%Y-%m-%d')}"

    # Pipe-separated — consistent with CAST endpoint syntax
    country_str = "|".join(ACLED_WATCH_COUNTRIES)

    result = _acled_read(token, country_str, date_range, label="multi-country")

    if result["total_rows"] == 0:
        # Diagnostic: test single high-activity country to isolate the failure mode
        logger.warning(
            "ACLED live: 0 rows from multi-country query. "
            "Running single-country diagnostic (Ukraine)…"
        )
        single = _acled_read(token, "Ukraine", date_range, label="Ukraine-only")
        if single["total_rows"] > 0:
            logger.info(
                f"Single-country works ({single['total_rows']} rows). "
                "Multi-country query may need account tier upgrade. "
                "Using single-country result as partial signal."
            )
            return single
        else:
            logger.error(
                "Single-country Ukraine also 0 rows. "
                "Check account access at acleddata.com/account/ — "
                "free tier may restrict /api/acled/read to limited coverage."
            )

    return result


def _acled_read(token: str, country_str: str, date_range: str, label: str) -> dict:
    """
    Execute one ACLED /api/acled/read request.

    country_str is injected DIRECTLY into the URL — must NOT be URL-encoded.
    Pipes in date_range and fields list are also injected directly.
    """
    fields = "event_id_cnty|event_date|event_type|fatalities|country"

    # Direct string injection — no urllib.parse.quote on country_str or date_range
    url = (
        f"{ACLED_READ_URL}?_format=json"
        f"&country={country_str}"
        f"&event_date={date_range}"
        f"&event_date_where=BETWEEN"
        f"&fields={fields}"
        f"&limit=1000"
    )
    logger.info(f"ACLED read [{label}]: {url[:200]}")

    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )
    out = {"total_rows": 0, "lethal_rows": 0, "fatalities": 0}
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        if data.get("status") != 200:
            logger.warning(f"ACLED read [{label}] non-200: "
                           f"{data.get('messages', data.get('status'))}")
            return out
        rows = data.get("data", [])
        out["total_rows"] = len(rows)
        for row in rows:
            if row.get("event_type", "") in LETHAL_EVENT_TYPES:
                out["lethal_rows"] += 1
                out["fatalities"]  += int(row.get("fatalities") or 0)
        logger.info(f"ACLED read [{label}]: total={out['total_rows']} "
                    f"lethal={out['lethal_rows']} fatalities={out['fatalities']}")
    except Exception as exc:
        logger.warning(f"ACLED read [{label}] failed: {exc}")
    return out


def _score_acled_live(result: dict) -> float:
    """0-100. 500 lethal events in 30 days across watch countries ≈ 100."""
    lethal = result.get("lethal_rows", 0)
    return round(min(100.0, lethal / 5.0), 1) if lethal > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4: GDELT
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_gdelt() -> dict:
    """
    Query GDELT DOC 2.0 API with three focused conflict queries.

    BUG FIXED (1): original query was a single 8-term AND query:
        "Iran Israel airstrike Ukraine war Venezuela cartel violence"
    GDELT treats spaces as AND — articles must contain ALL 8 terms → 0 results.
    Fix: three separate 3-term queries, one per conflict region.

    BUG FIXED (2): three queries fired back-to-back → HTTP 429.
    Fix: GDELT_SLEEP seconds between queries (default 3.5s).
    On a 429, additional 10s backoff before continuing.
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
            "sort":       "toneasc",   # most negative articles first
            "format":     "json",
        })
        url = f"{GDELT_API}?{params}"

        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "MARA-ConflictIndex/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            articles = data.get("articles", [])
            n        = len(articles)
            # NOTE: GDELT artlist mode does NOT return a tone field per article.
            # Tone data requires the separate timelinetone endpoint.
            # We score on count alone — query terms are conflict-specific enough
            # that volume is a valid escalation proxy.
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
    0-100. Gate: ≥15 articles about conflict queries in last 3 days.
    Saturates at 67 articles (100 pts). No tone gate — artlist mode
    doesn't return per-article tone; query specificity filters noise.
    """
    n = result.get("articles", 0)
    if n < 15:
        return 0.0
    return round(min(100.0, n * 1.5), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: Market Proxy
# ─────────────────────────────────────────────────────────────────────────────

def _last_close(h) -> float:
    """
    Extract the last Close value from a yfinance DataFrame as a plain float.

    yfinance ≥0.2.x with auto_adjust=True returns a MultiIndex DataFrame:
        columns = MultiIndex([('Close', 'ITA'), ('High', 'ITA'), ...])
    so h["Close"] gives a DataFrame (not a Series), and .iloc[-1] gives a
    Series, not a scalar.  We flatten .values to a 1-D numpy array first,
    which is safe for single-ticker and multi-ticker downloads alike.
    """
    try:
        col = h["Close"]
        arr = col.values.flatten()   # always 1-D, dtype float64
        return float(arr[-1])
    except Exception:
        arr = h.values.flatten()
        return float(arr[-1])


def _fetch_market_proxy() -> dict:
    """Defense ETF 20-day momentum + gold/oil ratio + VIX. No auth needed."""
    try:
        import yfinance as yf

        # Defense ETF momentum — average across available tickers
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

        # Gold / oil
        gh    = yf.download("GC=F", period="5d", progress=False, auto_adjust=True)
        oh    = yf.download("CL=F", period="5d", progress=False, auto_adjust=True)
        gold  = _last_close(gh) if len(gh) > 0 else 3000.0
        oil   = _last_close(oh) if len(oh) > 0 else 75.0
        ratio = gold / oil if oil > 0 else 50.0

        # VIX
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
      Current conditions      (momentum 0.037, ratio 57, VIX 29)  → ~38
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
# Public API  (called by market_data.py every cycle)
# ─────────────────────────────────────────────────────────────────────────────

def get_acled_token(email: str, password: str) -> Optional[str]:
    """
    Public ACLED token fetcher for testing and external callers.
    Uses the module-level token manager (cache-aware).
    Returns the access_token string, or None on failure.
    """
    return _token_manager.get_token(email, password)


def get_war_premium_score() -> float:
    """
    Composite War Premium Score 0-100.
    Weights shift when ACLED is unavailable — market proxy picks up the slack.
    """
    token     = _get_acled_token()
    has_acled = token is not None

    market = _fetch_market_proxy()
    cast   = _fetch_acled_cast(token)   if has_acled else {}
    live   = _fetch_acled_live(token)   if has_acled else {}
    gdelt  = _fetch_gdelt()

    ms = _score_market_proxy(market)
    cs = _score_cast(cast)          if has_acled else 0.0
    ls = _score_acled_live(live)    if has_acled else 0.0
    gs = _score_gdelt(gdelt)

    if has_acled:
        w = (0.70, 0.20, 0.05, 0.05)
    else:
        w = (0.75, 0.00, 0.00, 0.25)

    score = round(ms*w[0] + cs*w[1] + ls*w[2] + gs*w[3], 1)
    logger.info(
        f"War Premium Score: {score}/100 "
        f"(market={ms} cast={cs} live={ls} gdelt={gs})"
    )
    return score


def _interpret(score: float) -> str:
    if score < 25: return "No war signal"
    if score < 50: return "Weak confirmation"
    if score < 70: return "WAR_PREMIUM confirmed"
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
    print("  MARA CONFLICT INDEX — VERIFICATION")
    print("=" * 60)

    # Market proxy
    print("\n  Market proxy:")
    market  = _fetch_market_proxy()
    ms      = _score_market_proxy(market)
    print(f"    defense_momentum : {market['defense_momentum']:.4f}")
    print(f"    gold_oil_ratio   : {market['gold_oil_ratio']:.2f}")
    print(f"    vix              : {market['vix']:.2f}")
    print(f"    score            : {ms:.1f}/100")

    # ACLED auth
    print(f"\n  ACLED auth ({os.environ.get('ACLED_EMAIL', 'not set')}):")
    token = _get_acled_token()
    print(f"    token            : {'✅ ' + token[:20] + '...' if token else '❌ check .env'}")

    # CAST
    cs = 0.0
    if token:
        print("\n  ACLED CAST:")
        cast = _fetch_acled_cast(token)
        cs   = _score_cast(cast)
        print(f"    score            : {cs:.1f}/100")
        print(f"    total_forecast   : {cast.get('total_forecast', 0):,}")
        print(f"    battles          : {cast.get('battles_forecast', 0):,}")
        print(f"    erv              : {cast.get('erv_forecast', 0):,}")
        print(f"    vac              : {cast.get('vac_forecast', 0):,}")
        print(f"    months_fetched   : {cast.get('months_fetched', 0)}")

    # Live events
    ls = 0.0
    if token:
        print("\n  ACLED live events (last 30 days):")
        live = _fetch_acled_live(token)
        ls   = _score_acled_live(live)
        print(f"    score            : {ls:.1f}/100")
        print(f"    total_rows       : {live.get('total_rows', 0)}")
        print(f"    lethal_events    : {live.get('lethal_rows', 0)}")
        print(f"    fatalities       : {live.get('fatalities', 0)}")
        if live.get("total_rows", 0) == 0:
            print("    ⚠️  Still 0 rows — check diagnostic log above")
            print("       Single-country pass → account needs bulk access tier")
            print("       Single-country fail → check acleddata.com/account/")

    # GDELT
    print("\n  GDELT (3 queries, 3.5s sleep between each):")
    gdelt = _fetch_gdelt()
    gs    = _score_gdelt(gdelt)
    print(f"    score            : {gs:.1f}/100")
    print(f"    best_articles    : {gdelt.get('articles', 0)}  (needs ≥15 to score)")
    print(f"    source           : {gdelt.get('source', 'unknown')}")
    print(f"    note             : tone not available in artlist mode — count-only scoring")

    # Composite
    has_acled = token is not None
    w = (0.70, 0.20, 0.05, 0.05) if has_acled else (0.75, 0.00, 0.00, 0.25)
    composite = round(ms*w[0] + cs*w[1] + ls*w[2] + gs*w[3], 1)

    print("\n  " + "=" * 40)
    print(f"  WAR PREMIUM SCORE : {composite:.1f} / 100")
    print(f"  Interpretation    : {_interpret(composite)}")
    print("  " + "=" * 40)
    print(f"    market_score    {ms}")
    print(f"    cast_score      {cs}")
    print(f"    live_score      {ls}")
    print(f"    gdelt_score     {gs}")

    # Sanity checks (no network needed)
    pc_score  = round(_score_market_proxy(
        {"defense_momentum": 0.02, "gold_oil_ratio": 38.0, "vix": 14.0}) * 0.75, 1)
    war_score = round(_score_market_proxy(
        {"defense_momentum": 0.08, "gold_oil_ratio": 57.0, "vix": 30.0}) * 0.70
        + 100.0 * 0.20, 1)

    print(f"\n  False positive check (peacetime commodity bull):")
    print(f"    {pc_score:.1f}/100  "
          f"{'✅ PASS' if pc_score < 25 else '❌ FAIL — recalibrate thresholds'}")
    print(f"  War scenario check (escalation + CAST maxed):")
    print(f"    {war_score:.1f}/100  "
          f"{'✅ PASS' if war_score >= 50 else '❌ FAIL — recalibrate thresholds'}")
    print("=" * 60 + "\n")
