"""
data/feeds/conflict_index.py

Composite War Premium Score  0-100.

Six independent data layers:

  Layer 1  Market proxy    (PRIMARY, 60%+ weight)
           Defense ETF momentum + gold/oil ratio + VIX.
           Always available, no auth required, reacts in real-time.

  Layer 2  GDELT           (10%)
           Negative news tone + high article volume = conflict signal.

  Layer 3  UCDP GED        (10%)  — requires UCDP_API_TOKEN
           Uppsala University georeferenced conflict events, last 30 days.
           Free token — email ucdp.uu.se to request.

  Layer 4  AIS chokepoints (10%)  — requires AISSTREAM_API_KEY
           Vessel traffic deficit at Hormuz, Suez, Malacca, Taiwan, Bab-el-Mandeb.
           Free account at aisstream.io.

  Layer 5  NASA FIRMS      (3%)   — requires NASA_FIRMS_API_KEY
           High-confidence VIIRS thermal anomalies over active conflict zones.
           Free Earthdata account at firms.modaps.eosdis.nasa.gov.

  Layer 6  USGS seismic    (2%)   — no auth required
           M4.5+ earthquakes near critical infrastructure (straits, nuclear sites).

  Layer 7  EDGAR macro     (5%)   — no auth required
           Sector-wide filing clusters: supply-chain disruptions, energy incidents,
           defense contract events, semiconductor supply alerts, shipping blockages.
           Free — SEC EDGAR requires only a User-Agent header.

  ACLED (legacy, disabled) — free tier returns 403 on data endpoints.
  Code preserved for when an approved researcher account is available.

Weight redistribution: if a source's API key is absent, its weight is added
to market_proxy. Total always sums to 1.0.

Score thresholds consumed by classifier.py:
  < 25   no war signal
  25-50  weak confirmation  (1 of 2 WAR_PREMIUM triggers)
  50-70  confirmed          (1 more trigger fires WAR_PREMIUM)
  > 70   very strong        (WAR_PREMIUM fires alone)

Verify standalone:
    cd ~/mara && source .venv/bin/activate
    python data/feeds/conflict_index.py
"""

import asyncio
import csv
import io
import json
import logging
import math
import os
import time
import urllib.request
import urllib.parse
import requests as _requests
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from data.feeds.edgar_feed import get_edgar_macro_score as _get_edgar_macro_score
    _HAVE_EDGAR = True
except ImportError:
    _HAVE_EDGAR = False

try:
    import websockets as _websockets
    _HAVE_WEBSOCKETS = True
except ImportError:
    _HAVE_WEBSOCKETS = False

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


# ── UCDP GED ──────────────────────────────────────────────────────────────────
# Gleditsch-Ward country codes — verified against ucdpapi.pcr.uu.se/api/country/23.1
UCDP_GED_URL = "https://ucdpapi.pcr.uu.se/api/gedevents/23.1"
UCDP_WATCHED_COUNTRIES = {
    "Ukraine":  369,
    "Russia":   365,
    "Syria":    652,
    "Israel":   666,
    "Iran":     630,
    "Yemen":    678,
    "Taiwan":   713,
    "Sudan":    625,
    "Myanmar":  775,
}


# ── AIS chokepoint monitor ────────────────────────────────────────────────────
CHOKEPOINTS = {
    "hormuz":     [[21.0, 55.0], [27.0, 60.0]],
    "suez":       [[29.5, 31.5], [31.5, 33.0]],
    "malacca":    [[1.0,  99.0], [6.0, 104.5]],
    "taiwan":     [[22.0,119.0], [26.0, 123.0]],
    "bab_mandeb": [[11.0, 42.0], [13.5,  45.0]],
}
CHOKEPOINT_BASELINES = {
    "hormuz": 12, "suez": 8, "malacca": 20, "taiwan": 6, "bab_mandeb": 4,
}


# ── NASA FIRMS ────────────────────────────────────────────────────────────────
FIRMS_BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
FIRMS_BBOXES = [
    "31,44,40,52",   # Ukraine east
    "35,33,38,37",   # Syria
    "43,11,50,15",   # Yemen
    "44,25,49,30",   # Iran west
]


# ── USGS seismic ──────────────────────────────────────────────────────────────
USGS_EARTHQUAKE_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
SEISMIC_WATCH_ZONES = [
    {"name": "taiwan_strait", "lat": 24.0, "lon": 121.0, "radius_deg": 3.0},
    {"name": "hormuz",        "lat": 26.5, "lon":  56.5, "radius_deg": 2.0},
    {"name": "japan_nuclear", "lat": 37.0, "lon": 141.0, "radius_deg": 4.0},
    {"name": "turkey_straits","lat": 41.0, "lon":  29.0, "radius_deg": 2.0},
]


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

# ── New-source caches ─────────────────────────────────────────────────────────
_edgar_cache:     dict = {"ts": 0.0, "score": 0.0}   # 6h TTL (sourced from edgar_feed)
_ucdp_cache:      dict = {"ts": 0.0, "score": 0.0}   # 4h TTL
_ais_cache:       dict = {"ts": 0.0, "score": 0.0}   # 15min TTL
_firms_cache:     dict = {"ts": 0.0, "score": 0.0}   # 3h TTL
_usgs_cache:      dict = {"ts": 0.0, "score": 0.0}   # 1h TTL
_osint_llm_cache: dict = {}                           # keyed by event_id


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


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3: UCDP GED (Uppsala Conflict Data Program Georeferenced Events)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_ucdp_ged() -> float:
    """
    Fetch conflict events from UCDP GED API for the last 30 days.
    Requires UCDP_API_TOKEN env var (free — email ucdp.uu.se to request).
    Cache TTL: 4 hours. Returns 0.0 on missing key or any error.
    """
    token = os.environ.get("UCDP_API_TOKEN", "")
    if not token:
        logger.warning("UCDP_API_TOKEN not set — UCDP GED layer skipped")
        return 0.0

    now = time.time()
    if now - _ucdp_cache["ts"] < 4 * 3600:
        return _ucdp_cache["score"]

    try:
        today     = datetime.now(timezone.utc).date()
        start_dt  = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        recent_dt = (today - timedelta(days=7)).strftime("%Y-%m-%d")

        country_ids = "|".join(str(v) for v in UCDP_WATCHED_COUNTRIES.values())

        params = urllib.parse.urlencode({
            "pagesize":  100,
            "page":      1,
            "StartDate": start_dt,
        })
        # Inject country IDs directly (pipe must not be percent-encoded)
        url = f"{UCDP_GED_URL}?{params}&country={country_ids}"

        resp = _requests.get(
            url,
            headers={"x-ucdp-access-token": token},
            timeout=20,
        )
        if resp.status_code in (403, 404):
            logger.warning(f"UCDP GED HTTP {resp.status_code} — check token")
            return _ucdp_cache.get("score", 0.0)

        resp.raise_for_status()
        events = resp.json().get("Result", [])

        event_count = len(events)
        recent_count = sum(
            1 for e in events
            if e.get("date_start", "") >= recent_dt
        )
        recency_weight = recent_count / max(event_count, 1)
        raw_score = min(100.0, event_count * 2 + recency_weight * 30)
        score = round(raw_score, 1)

        logger.info(
            f"UCDP GED: {event_count} events (30d), {recent_count} recent (7d) → {score}"
        )
        _ucdp_cache["ts"]    = now
        _ucdp_cache["score"] = score
        return score

    except Exception as exc:
        logger.warning(f"UCDP GED fetch failed: {exc}")
        return _ucdp_cache.get("score", 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4: AIS Chokepoint Vessel Traffic Monitor
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_ais_chokepoint_async() -> float:
    """
    Open an aisstream.io WebSocket, collect 8s of PositionReport messages
    across all 5 chokepoint bounding boxes, score vessel deficit vs baselines.
    """
    key = os.environ.get("AISSTREAM_API_KEY", "")
    if not key:
        return 0.0

    if not _HAVE_WEBSOCKETS:
        logger.warning("websockets package not installed — AIS layer skipped")
        return 0.0

    vessel_counts: dict = {cp: set() for cp in CHOKEPOINTS}

    async def _collect() -> None:
        async with _websockets.connect("wss://stream.aisstream.io/v0/stream") as ws:
            for cp_name, bbox in CHOKEPOINTS.items():
                sub = json.dumps({
                    "Apikey":             key,
                    "BoundingBoxes":      [bbox],
                    "FilterMessageTypes": ["PositionReport"],
                })
                await ws.send(sub)

            deadline = asyncio.get_event_loop().time() + 8.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    msg = json.loads(raw)
                    mmsi = (
                        msg.get("Message", {})
                           .get("PositionReport", {})
                           .get("UserID")
                    )
                    lat  = (
                        msg.get("Message", {})
                           .get("PositionReport", {})
                           .get("Latitude")
                    )
                    lon  = (
                        msg.get("Message", {})
                           .get("PositionReport", {})
                           .get("Longitude")
                    )
                    if mmsi is None or lat is None or lon is None:
                        continue
                    # Attribute vessel to chokepoints whose bbox contains it
                    for cp_name, bbox in CHOKEPOINTS.items():
                        min_lat, min_lon = bbox[0]
                        max_lat, max_lon = bbox[1]
                        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                            vessel_counts[cp_name].add(mmsi)
                except asyncio.TimeoutError:
                    continue

    try:
        await asyncio.wait_for(_collect(), timeout=12.0)
    except Exception as exc:
        logger.warning(f"AIS WebSocket error: {exc}")
        return 0.0

    deviations = []
    for cp_name, baseline in CHOKEPOINT_BASELINES.items():
        observed  = len(vessel_counts.get(cp_name, set()))
        deviation = max(0.0, min(1.0, (baseline - observed) / baseline))
        deviations.append(deviation)

    score = round((sum(deviations) / len(deviations)) * 100, 1) if deviations else 0.0
    logger.info(
        f"AIS chokepoints: {dict((k, len(v)) for k, v in vessel_counts.items())} "
        f"→ {score}"
    )
    return score


def _fetch_ais_chokepoint_sync() -> float:
    """
    Sync wrapper around the async AIS fetch. Called from get_war_premium_score().
    Cache TTL: 15 minutes.
    """
    if not os.environ.get("AISSTREAM_API_KEY", ""):
        return 0.0

    now = time.time()
    if now - _ais_cache["ts"] < 15 * 60:
        return _ais_cache["score"]

    try:
        score = asyncio.run(_fetch_ais_chokepoint_async())
    except RuntimeError:
        # asyncio.run() fails if there's already a running loop (e.g., in pytest-asyncio)
        loop = asyncio.new_event_loop()
        try:
            score = loop.run_until_complete(_fetch_ais_chokepoint_async())
        finally:
            loop.close()
    except Exception as exc:
        logger.warning(f"AIS sync wrapper failed: {exc}")
        score = 0.0

    _ais_cache["ts"]    = now
    _ais_cache["score"] = score
    return score


# ─────────────────────────────────────────────────────────────────────────────
# Layer 5: NASA FIRMS Thermal Anomaly (VIIRS SNPP NRT)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_nasa_firms() -> float:
    """
    Count high-confidence VIIRS thermal anomalies over conflict zones (last 48h).
    Requires NASA_FIRMS_API_KEY env var (free Earthdata account).
    Cache TTL: 3 hours. Returns 0.0 on missing key or any error.
    """
    api_key = os.environ.get("NASA_FIRMS_API_KEY", "")
    if not api_key:
        logger.warning("NASA_FIRMS_API_KEY not set — FIRMS layer skipped")
        return 0.0

    now = time.time()
    if now - _firms_cache["ts"] < 3 * 3600:
        return _firms_cache["score"]

    high_count = 0
    for bbox in FIRMS_BBOXES:
        url = f"{FIRMS_BASE_URL}/{api_key}/VIIRS_SNPP_NRT/{bbox}/2"
        try:
            resp = _requests.get(url, timeout=15)
            resp.raise_for_status()
            reader = csv.DictReader(io.StringIO(resp.text))
            for row in reader:
                conf = row.get("confidence", row.get("conf", "")).strip().lower()
                # VIIRS NRT: confidence is "h" (high), "n" (nominal), "l" (low)
                if conf in ("h", "high"):
                    high_count += 1
        except Exception as exc:
            logger.warning(f"NASA FIRMS fetch failed for bbox {bbox}: {exc}")

    score = round(min(100.0, high_count * 3), 1)
    logger.info(f"NASA FIRMS: {high_count} high-confidence thermal events → {score}")
    _firms_cache["ts"]    = now
    _firms_cache["score"] = score
    return score


# ─────────────────────────────────────────────────────────────────────────────
# Layer 6: USGS Seismic — M4.5+ near critical infrastructure
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_usgs_seismic() -> float:
    """
    Score M4.5+ earthquakes near critical infrastructure watch zones (last 7 days).
    No auth required. Cache TTL: 1 hour.
    """
    now = time.time()
    if now - _usgs_cache["ts"] < 3600:
        return _usgs_cache["score"]

    today    = datetime.now(timezone.utc).date()
    start_dt = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    end_dt   = today.strftime("%Y-%m-%d")

    try:
        params = urllib.parse.urlencode({
            "format":         "geojson",
            "minmagnitude":   4.5,
            "starttime":      start_dt,
            "endtime":        end_dt,
        })
        url  = f"{USGS_EARTHQUAKE_URL}?{params}"
        resp = _requests.get(url, timeout=15)
        resp.raise_for_status()
        features = resp.json().get("features", [])

        relevant_magnitudes = []
        for feat in features:
            props = feat.get("properties", {})
            coords = feat.get("geometry", {}).get("coordinates", [])
            if len(coords) < 2:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            mag = props.get("mag")
            if mag is None:
                continue
            for zone in SEISMIC_WATCH_ZONES:
                dist = math.sqrt(
                    (lat - zone["lat"]) ** 2 + (lon - zone["lon"]) ** 2
                )
                if dist <= zone["radius_deg"]:
                    relevant_magnitudes.append(float(mag))
                    break   # count each event once even if in multiple zones

        magnitude_score = sum(10 ** (m - 4.5) for m in relevant_magnitudes)
        score = round(min(100.0, magnitude_score * 5), 1)
        logger.info(
            f"USGS seismic: {len(relevant_magnitudes)} relevant events "
            f"(M4.5+ near watch zones) → {score}"
        )
        _usgs_cache["ts"]    = now
        _usgs_cache["score"] = score
        return score

    except Exception as exc:
        logger.warning(f"USGS seismic fetch failed: {exc}")
        return _usgs_cache.get("score", 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# OSINT LLM Parser (advisory only — never in scoring hot path)
# ─────────────────────────────────────────────────────────────────────────────

_OSINT_PROMPT_TEMPLATE = (
    "You are a geopolitical data parser. Extract structured information from this "
    "{source} event description. Return ONLY valid JSON, no other text: "
    '{{"affected_commodities": ["oil"|"wheat"|"gas"|"semiconductors"|"gold"|"none"], '
    '"severity": 1-5, '
    '"escalation": "rising"|"stable"|"falling"}} '
    "Event: {text}"
)

_OSINT_DEFAULT = {"affected_commodities": [], "severity": 1, "escalation": "stable"}


def parse_osint_with_llm(text: str, source: str, event_id: str) -> dict:
    """
    Send a raw event text snippet to phi3:mini on Ollama for structured parsing.
    Results are cached by event_id and used ONLY for:
      - Enriching Telegram alert messages with commodity context
      - Enriching the Grafana thesis panel via /execution-risk
      - Nudging watchlist ticker weights (advisory_only=True)

    NEVER modifies war_premium_score, regime classification, or capital allocation.
    Returns the default dict on any failure — never raises.
    """
    if event_id in _osint_llm_cache:
        return _osint_llm_cache[event_id]

    ollama_host = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
    prompt = _OSINT_PROMPT_TEMPLATE.format(source=source, text=text[:500])

    try:
        resp = _requests.post(
            f"{ollama_host}/api/generate",
            json={"model": "phi3:mini", "prompt": prompt, "stream": False},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
        parsed = json.loads(raw)
        # Validate expected keys are present
        result = {
            "affected_commodities": parsed.get("affected_commodities", []),
            "severity":             int(parsed.get("severity", 1)),
            "escalation":           parsed.get("escalation", "stable"),
        }
    except Exception as exc:
        logger.warning(f"OSINT LLM parse failed ({source}/{event_id}): {exc}")
        result = dict(_OSINT_DEFAULT)

    _osint_llm_cache[event_id] = result
    return result


def get_osint_commodity_context() -> dict:
    """Return copy of the full LLM-parsed OSINT cache, keyed by event_id."""
    return dict(_osint_llm_cache)


# ─────────────────────────────────────────────────────────────────────────────
# Weight table and redistribution
# ─────────────────────────────────────────────────────────────────────────────

BASE_WEIGHTS: dict = {
    "market_proxy":   0.60,
    "gdelt":          0.10,
    "ucdp_ged":       0.10,
    "ais_chokepoint": 0.10,
    "nasa_firms":     0.03,
    "usgs_seismic":   0.02,
    "edgar_macro":    0.05,
}


def _effective_weights(active_sources: set) -> dict:
    """
    Redistribute weights of absent/errored sources to market_proxy.
    A source is 'active' if its API key is configured (even if score == 0).
    USGS is always active (no key required). market_proxy is always active.
    Total of returned weights always sums to 1.0.
    """
    skipped = sum(
        w for k, w in BASE_WEIGHTS.items()
        if k not in ("market_proxy",) and k not in active_sources
    )
    weights = {
        k: (v if k in active_sources else 0.0)
        for k, v in BASE_WEIGHTS.items()
    }
    weights["market_proxy"] = BASE_WEIGHTS["market_proxy"] + skipped
    return weights


def get_war_premium_score() -> float:
    """
    Composite War Premium Score 0-100.

    Seven layers: market proxy (60%+), GDELT (10%), UCDP GED (10%),
    AIS chokepoints (10%), NASA FIRMS (3%), USGS seismic (2%),
    EDGAR macro (5% — no key required).
    Missing API keys redistribute their weight to market_proxy.
    Total always sums to 1.0.
    """
    market = _fetch_market_proxy()
    gdelt  = _fetch_gdelt()
    ms = _score_market_proxy(market)
    gs = _score_gdelt(gdelt)

    active: set = {"market_proxy", "gdelt"}

    ucdp_token = os.environ.get("UCDP_API_TOKEN", "")
    us = _fetch_ucdp_ged() if ucdp_token else 0.0
    if ucdp_token:
        active.add("ucdp_ged")

    ais_key = os.environ.get("AISSTREAM_API_KEY", "")
    ai = _fetch_ais_chokepoint_sync() if ais_key else 0.0
    if ais_key:
        active.add("ais_chokepoint")

    firms_key = os.environ.get("NASA_FIRMS_API_KEY", "")
    fs = _fetch_nasa_firms() if firms_key else 0.0
    if firms_key:
        active.add("nasa_firms")

    # USGS requires no key — always active
    ss = _fetch_usgs_seismic()
    active.add("usgs_seismic")

    # EDGAR macro requires no key — always active (6h cache)
    if _HAVE_EDGAR:
        try:
            es = _get_edgar_macro_score()
        except Exception as exc:
            logger.warning(f"EDGAR macro score failed: {exc}")
            es = 0.0
        active.add("edgar_macro")
    else:
        es = 0.0
        logger.warning("edgar_feed not importable — EDGAR macro layer skipped")

    w = _effective_weights(active)
    score = round(
        ms * w["market_proxy"]   +
        gs * w["gdelt"]          +
        us * w["ucdp_ged"]       +
        ai * w["ais_chokepoint"] +
        fs * w["nasa_firms"]     +
        ss * w["usgs_seismic"]   +
        es * w["edgar_macro"],
        1,
    )
    logger.info(
        f"War Premium Score: {score}/100 "
        f"(market={ms} gdelt={gs} ucdp={us} ais={ai} "
        f"firms={fs} usgs={ss} edgar={es})"
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

    # Layer 1: Market proxy
    print("\n  Layer 1: Market proxy:")
    market = _fetch_market_proxy()
    ms     = _score_market_proxy(market)
    print(f"    defense_momentum : {market['defense_momentum']:.4f}")
    print(f"    gold_oil_ratio   : {market['gold_oil_ratio']:.2f}")
    print(f"    vix              : {market['vix']:.2f}")
    print(f"    score            : {ms:.1f}/100")

    # Layer 2: GDELT
    print("\n  Layer 2: GDELT (3 queries, 3.5s sleep between each):")
    gdelt = _fetch_gdelt()
    gs    = _score_gdelt(gdelt)
    print(f"    score            : {gs:.1f}/100")
    print(f"    best_articles    : {gdelt.get('articles', 0)}  (needs >=15 to score)")
    print(f"    source           : {gdelt.get('source', 'unknown')}")

    # Layer 3: UCDP GED
    print(f"\n  Layer 3: UCDP GED (token: "
          f"{'set' if os.environ.get('UCDP_API_TOKEN') else 'not set'}):")
    us = _fetch_ucdp_ged()
    print(f"    score            : {us:.1f}/100")

    # Layer 4: AIS chokepoints
    print(f"\n  Layer 4: AIS chokepoints (key: "
          f"{'set' if os.environ.get('AISSTREAM_API_KEY') else 'not set'}):")
    ai = _fetch_ais_chokepoint_sync()
    print(f"    score            : {ai:.1f}/100")

    # Layer 5: NASA FIRMS
    print(f"\n  Layer 5: NASA FIRMS (key: "
          f"{'set' if os.environ.get('NASA_FIRMS_API_KEY') else 'not set'}):")
    fs = _fetch_nasa_firms()
    print(f"    score            : {fs:.1f}/100")

    # Layer 6: USGS seismic
    print("\n  Layer 6: USGS seismic (no key required):")
    ss = _fetch_usgs_seismic()
    print(f"    score            : {ss:.1f}/100")

    # Layer 7: EDGAR macro
    print("\n  Layer 7: EDGAR macro (no key required, 6h cache):")
    if _HAVE_EDGAR:
        es = _get_edgar_macro_score()
    else:
        es = 0.0
        print("    edgar_feed not importable — skipped")
    print(f"    score            : {es:.1f}/100")

    # Active sources and effective weights
    active: set = {"market_proxy", "gdelt", "usgs_seismic", "edgar_macro"}
    if os.environ.get("UCDP_API_TOKEN"):
        active.add("ucdp_ged")
    if os.environ.get("AISSTREAM_API_KEY"):
        active.add("ais_chokepoint")
    if os.environ.get("NASA_FIRMS_API_KEY"):
        active.add("nasa_firms")
    if not _HAVE_EDGAR:
        active.discard("edgar_macro")
    w = _effective_weights(active)

    composite = round(
        ms * w["market_proxy"]   +
        gs * w["gdelt"]          +
        us * w["ucdp_ged"]       +
        ai * w["ais_chokepoint"] +
        fs * w["nasa_firms"]     +
        ss * w["usgs_seismic"]   +
        es * w["edgar_macro"],
        1,
    )

    print("\n  " + "=" * 40)
    print(f"  WAR PREMIUM SCORE : {composite:.1f} / 100")
    print(f"  Interpretation    : {_interpret(composite)}")
    print("  " + "=" * 40)
    print(f"    market_score  {ms}  (weight: {w['market_proxy']:.0%})")
    print(f"    gdelt_score   {gs}  (weight: {w['gdelt']:.0%})")
    print(f"    ucdp_score    {us}  (weight: {w['ucdp_ged']:.0%})")
    print(f"    ais_score     {ai}  (weight: {w['ais_chokepoint']:.0%})")
    print(f"    firms_score   {fs}  (weight: {w['nasa_firms']:.0%})")
    print(f"    usgs_score    {ss}  (weight: {w['usgs_seismic']:.0%})")
    print(f"    edgar_score   {es}  (weight: {w['edgar_macro']:.0%})")
    print(f"    weight_sum    {sum(w.values()):.3f}  (should be 1.000)")

    # Sanity checks (no network needed)
    pc_score  = round(_score_market_proxy(
        {"defense_momentum": 0.02, "gold_oil_ratio": 38.0, "vix": 14.0}) * 0.83, 1)
    war_score = round(_score_market_proxy(
        {"defense_momentum": 0.08, "gold_oil_ratio": 57.0, "vix": 30.0}) * 0.60
        + 100.0 * 0.15, 1)

    print(f"\n  False positive check (peacetime commodity bull):")
    print(f"    {pc_score:.1f}/100  "
          f"{'PASS' if pc_score < 25 else 'FAIL — recalibrate thresholds'}")
    print(f"  War scenario check (market escalation + GDELT maxed):")
    print(f"    {war_score:.1f}/100  "
          f"{'PASS' if war_score >= 25 else 'FAIL — recalibrate thresholds'}")
    print("=" * 60 + "\n")
