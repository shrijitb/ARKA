"""
data/feeds/edgar_feed.py

SEC EDGAR filing monitor for MARA.

Two classes:

  EdgarWatchlistMonitor
    Per-ticker significant filing detector. Fetches CIK numbers, recent filings,
    filing text excerpts, and scores their significance. Used by the hypervisor
    edgar_watchlist_scan background task to fire Telegram alerts.

  EdgarMacroScanner
    Full-text EDGAR search for sector-wide macro risks. Detects clusters of
    filings (e.g., 3+ supply-chain 8-Ks in 3 days) before they show up in prices.
    Score feeds into conflict_index.py as edgar_macro (5% weight).

Rate limit: EDGAR enforces 10 requests/second. EdgarRateLimiter keeps us at 8/s.

No API key required. SEC only asks for a User-Agent header identifying the caller.
Set EDGAR_USER_AGENT env var (e.g. "MARA-Trading-System contact@example.com").

Standalone smoke test:
    cd ~/mara && source .venv/bin/activate
    python data/feeds/edgar_feed.py
"""

import csv
import io
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests as _requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

EDGAR_BASE_URL   = "https://data.sec.gov"
EDGAR_FULL_TEXT  = "https://efts.sec.gov"
EDGAR_BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

MONITORED_FORM_TYPES = [
    "8-K",     # material events (earnings, M&A, leadership changes, contract wins)
    "SC 13D",  # activist investor >5% stake
    "SC 13G",  # passive investor >5% stake
    "4",       # insider trading
    "S-1",     # IPO registration
    "424B4",   # prospectus (secondary offering — dilutive)
]

# Base significance scores by form type
FORM_BASE_SCORES = {
    "8-K":    60,
    "SC 13D": 80,
    "4":      30,
    "424B4":  50,
    "S-1":    40,
    "SC 13G": 20,
}

# Keyword multipliers for filing text analysis
SIGNIFICANCE_KEYWORDS = {
    "acquisition":    1.5,
    "merger":         1.5,
    "bankruptcy":     2.0,
    "default":        2.0,
    "restatement":    1.8,
    "SEC investigation": 1.7,
    "subpoena":       1.7,
    "buyback":        1.3,
    "repurchase":     1.3,
}
# "dividend" + "increase" together → 1.2x
DIVIDEND_INCREASE_MULTIPLIER = 1.2

# Full-text search keyword sets for sector macro scanning
SECTOR_SCANS = {
    "supply_chain":  ["supply chain disruption", "force majeure", "material shortage"],
    "energy":        ["pipeline", "refinery", "LNG terminal", "oil spill"],
    "defense":       ["DoD contract", "defense contract", "ITAR", "export control"],
    "semiconductor": ["fab", "foundry", "chip shortage", "TSMC", "Samsung"],
    "shipping":      ["Suez Canal", "Panama Canal", "strait", "vessel seized"],
}

# Module-level caches
_cik_cache: dict[str, Optional[str]] = {}   # ticker → CIK (None = not found)
_filing_text_cache: dict[str, str]   = {}   # accession_number → text excerpt
_sector_scan_cache: dict = {"ts": 0.0, "results": {}}  # 6h TTL
_edgar_macro_score_cache: dict = {"ts": 0.0, "score": 0.0}  # 6h TTL


# ── Rate limiter ───────────────────────────────────────────────────────────────

class EdgarRateLimiter:
    """
    Simple token-bucket rate limiter.
    Stays at max_rps=8 (safely under SEC's 10/s limit).
    Call .wait() before every requests.get() to an SEC endpoint.
    """

    def __init__(self, max_rps: int = 8):
        self._min_interval = 1.0 / max_rps
        self._last_call    = 0.0

    def wait(self):
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()


_rate_limiter = EdgarRateLimiter(max_rps=8)

_EDGAR_USER_AGENT = os.environ.get(
    "EDGAR_USER_AGENT",
    "MARA-Trading-System contact@example.com",
)


def _edgar_get(url: str, timeout: int = 15) -> Optional[_requests.Response]:
    """Rate-limited GET to any SEC endpoint. Returns Response or None on error."""
    _rate_limiter.wait()
    try:
        resp = _requests.get(
            url,
            headers={"User-Agent": _EDGAR_USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp
    except Exception as exc:
        logger.warning(f"EDGAR GET failed [{url[:80]}]: {exc}")
        return None


# ── EdgarWatchlistMonitor ──────────────────────────────────────────────────────

class EdgarWatchlistMonitor:
    """
    Per-ticker SEC filing detector.

    Usage:
        monitor = EdgarWatchlistMonitor()
        cik = monitor.get_cik_for_ticker("AAPL")
        if cik:
            filings = monitor.get_recent_filings(cik, ["8-K"], days_back=7)
            for f in filings:
                excerpt = monitor.get_filing_text_excerpt(f["accession_number"], cik)
                sig = monitor.score_filing_significance(f["form_type"], excerpt)
                if sig["score"] >= 50:
                    ...
    """

    def get_cik_for_ticker(self, ticker: str) -> Optional[str]:
        """
        Look up the SEC CIK for a stock ticker via the EDGAR browse endpoint.
        Returns zero-padded 10-digit CIK string, or None if not found.
        Crypto tickers (BTC, ETH, etc.) will not be found — returns None gracefully.
        Results are cached module-wide (CIKs are permanent).
        """
        ticker_upper = ticker.upper().strip()

        if ticker_upper in _cik_cache:
            return _cik_cache[ticker_upper]

        params = urllib.parse.urlencode({
            "action":      "getcompany",
            "company":     "",
            "CIK":         ticker_upper,
            "type":        "",
            "dateb":       "",
            "owner":       "include",
            "count":       "10",
            "search_text": "",
            "output":      "atom",
        })
        url  = f"{EDGAR_BROWSE_URL}?{params}"
        resp = _edgar_get(url)
        if resp is None:
            _cik_cache[ticker_upper] = None
            return None

        # Extract CIK from Atom feed: <cik>0000320193</cik>
        match = re.search(r"<cik>(\d+)</cik>", resp.text)
        if not match:
            logger.debug(f"EDGAR: no CIK found for ticker {ticker_upper}")
            _cik_cache[ticker_upper] = None
            return None

        cik = match.group(1).lstrip("0") or "0"
        _cik_cache[ticker_upper] = cik
        logger.debug(f"EDGAR: {ticker_upper} → CIK {cik}")
        return cik

    def get_recent_filings(
        self,
        cik: str,
        filing_types: list,
        days_back: int = 7,
    ) -> list:
        """
        Return recent filings for a CIK from the submissions JSON endpoint.
        Filters to filing_types filed within the last days_back calendar days.
        Returns list of dicts: {form_type, filing_date, accession_number, primary_document}.
        """
        cik_padded = cik.zfill(10)
        url  = f"{EDGAR_BASE_URL}/submissions/CIK{cik_padded}.json"
        resp = _edgar_get(url)
        if resp is None:
            return []

        try:
            data = resp.json()
        except Exception as exc:
            logger.warning(f"EDGAR submissions JSON parse failed for CIK {cik}: {exc}")
            return []

        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days_back)).isoformat()

        recent = data.get("filings", {}).get("recent", {})
        forms       = recent.get("form", [])
        dates       = recent.get("filingDate", [])
        accessions  = recent.get("accessionNumber", [])
        primary_doc = recent.get("primaryDocument", [])

        results = []
        for i, (form, date, acc, doc) in enumerate(zip(forms, dates, accessions, primary_doc)):
            if form in filing_types and date >= cutoff:
                results.append({
                    "form_type":        form,
                    "filing_date":      date,
                    "accession_number": acc,
                    "primary_document": doc,
                })

        return results

    def get_filing_text_excerpt(
        self,
        accession_number: str,
        cik: str,
        max_chars: int = 2000,
    ) -> str:
        """
        Fetch the primary document for a filing and return the first max_chars of clean text.
        Results are cached by accession_number (filings are immutable once filed).
        Returns empty string on any error.
        """
        if accession_number in _filing_text_cache:
            return _filing_text_cache[accession_number]

        acc_nodash = accession_number.replace("-", "")
        index_url  = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/"
        )

        resp = _edgar_get(index_url)
        if resp is None:
            return ""

        # Find primary .htm or .txt document link in the index
        # The index page lists files as href links
        links = re.findall(r'href="([^"]+\.(?:htm|txt))"', resp.text, re.IGNORECASE)
        if not links:
            return ""

        # Pick the first .htm link; fallback to .txt
        htm_links = [l for l in links if l.lower().endswith(".htm")]
        chosen    = htm_links[0] if htm_links else links[0]

        # Build absolute URL if relative
        if chosen.startswith("/"):
            doc_url = f"https://www.sec.gov{chosen}"
        elif chosen.startswith("http"):
            doc_url = chosen
        else:
            doc_url = f"{index_url}{chosen}"

        resp2 = _edgar_get(doc_url)
        if resp2 is None:
            return ""

        # Strip HTML tags
        clean = re.sub(r"<[^>]+>", " ", resp2.text)
        # Collapse whitespace
        clean = re.sub(r"\s+", " ", clean).strip()
        excerpt = clean[:max_chars]

        _filing_text_cache[accession_number] = excerpt
        return excerpt

    def score_filing_significance(self, form_type: str, text_excerpt: str) -> dict:
        """
        Score the significance of a filing (0–100) based on form type and text keywords.
        Returns {"score": float, "form_type": str, "keywords_matched": list[str]}.
        """
        base = float(FORM_BASE_SCORES.get(form_type, 20))
        text_lower = text_excerpt.lower()
        keywords_matched = []

        for kw, multiplier in SIGNIFICANCE_KEYWORDS.items():
            if kw.lower() in text_lower:
                base *= multiplier
                keywords_matched.append(kw)

        if "dividend" in text_lower and "increase" in text_lower:
            if "dividend" not in keywords_matched and "increase" not in keywords_matched:
                base *= DIVIDEND_INCREASE_MULTIPLIER
                keywords_matched.append("dividend increase")

        return {
            "score":            round(min(100.0, base), 1),
            "form_type":        form_type,
            "keywords_matched": keywords_matched,
        }


# ── EdgarMacroScanner ─────────────────────────────────────────────────────────

class EdgarMacroScanner:
    """
    Full-text EDGAR search for sector-wide macro risks.

    Runs scan_sector_filings() for each keyword set in SECTOR_SCANS, then
    scores the combined result via score_sector_alert(). Results are cached
    for 6 hours — EDGAR filing search doesn't need more frequent polling.
    """

    def scan_sector_filings(
        self,
        keywords: list,
        form_type: str = "8-K",
        days_back: int = 3,
    ) -> list:
        """
        Full-text search EDGAR for filings mentioning keywords in the last days_back days.
        Returns a list of up to 20 matching filing dicts.
        """
        today    = datetime.now(timezone.utc).date()
        start_dt = (today - timedelta(days=days_back)).isoformat()
        end_dt   = today.isoformat()

        # Build quoted OR query: "supply chain disruption" OR "force majeure" OR ...
        query = " OR ".join(f'"{kw}"' for kw in keywords)
        params = urllib.parse.urlencode({
            "q":          query,
            "dateRange":  "custom",
            "startdt":    start_dt,
            "enddt":      end_dt,
            "forms":      form_type,
        })
        url  = f"{EDGAR_FULL_TEXT}/LATEST/search-index?{params}"
        resp = _edgar_get(url)
        if resp is None:
            return []

        try:
            data = resp.json()
        except Exception as exc:
            logger.warning(f"EDGAR full-text search JSON parse failed: {exc}")
            return []

        hits = data.get("hits", {}).get("hits", [])
        results = []
        for hit in hits[:20]:
            src = hit.get("_source", {})
            results.append({
                "entity_name":      src.get("entity_name", ""),
                "form_type":        src.get("form_type", form_type),
                "file_date":        src.get("file_date", ""),
                "period_of_report": src.get("period_of_report", ""),
            })
        return results

    def score_sector_alert(self, scan_results: dict) -> float:
        """
        Score 0–100. Each sector with >=3 filings in 3 days is "elevated".
        elevated_sectors count × 20 = score, capped at 100.
        """
        elevated = [
            sector
            for sector, results in scan_results.items()
            if len(results) >= 3
        ]
        return min(100.0, float(len(elevated) * 20))

    def get_macro_score(self) -> float:
        """
        Run all sector scans and return a composite macro-risk score 0–100.
        Results cached for 6 hours. Never raises — returns 0.0 on any error.
        """
        now = time.time()
        if now - _edgar_macro_score_cache["ts"] < 6 * 3600:
            return _edgar_macro_score_cache["score"]

        scan_results: dict = {}
        for sector, keywords in SECTOR_SCANS.items():
            try:
                scan_results[sector] = self.scan_sector_filings(keywords, days_back=3)
                logger.info(
                    f"EDGAR macro scan [{sector}]: {len(scan_results[sector])} filings"
                )
            except Exception as exc:
                logger.warning(f"EDGAR macro scan [{sector}] failed: {exc}")
                scan_results[sector] = []

        _sector_scan_cache["ts"]      = now
        _sector_scan_cache["results"] = scan_results

        score = self.score_sector_alert(scan_results)
        logger.info(f"EDGAR macro score: {score:.1f}/100")

        _edgar_macro_score_cache["ts"]    = now
        _edgar_macro_score_cache["score"] = score
        return score


# ── Shared scanner singleton (used by conflict_index.py) ──────────────────────

_macro_scanner = EdgarMacroScanner()


def get_edgar_macro_score() -> float:
    """
    Module-level accessor for conflict_index.py.
    Returns the latest EdgarMacroScanner score (0–100), cached 6h.
    """
    return _macro_scanner.get_macro_score()


def get_last_sector_scan_results() -> dict:
    """Return the raw sector scan results from the last macro scan run."""
    return dict(_sector_scan_cache.get("results", {}))


# ── Ollama 8-K enrichment (advisory only) ─────────────────────────────────────

_8k_llm_cache: dict = {}   # keyed by accession_number

_8K_PROMPT_TEMPLATE = (
    "You are a financial analyst. Classify this SEC 8-K filing excerpt. "
    "Return ONLY valid JSON, no other text: "
    '{{"event_type": "earnings|ma|leadership|contract|legal|other", '
    '"price_direction": "positive|negative|neutral", '
    '"magnitude": "major|moderate|minor", '
    '"affected_sectors": ["energy"|"defense"|"tech"|"financial"|"commodity"]}} '
    "Ticker: {ticker} "
    "Filing excerpt: {excerpt}"
)

_8K_LLM_DEFAULT = {
    "event_type":       "other",
    "price_direction":  "neutral",
    "magnitude":        "minor",
    "affected_sectors": [],
}


def parse_8k_with_llm(text_excerpt: str, ticker: str, accession_number: str = "") -> dict:
    """
    Use Ollama phi3:mini to classify materiality and price direction of an 8-K filing.
    Called ONLY for 8-K filings with base significance score >= 60.
    Results cached by accession_number.

    Used ONLY to enrich Telegram alerts and analyst worker context.
    NEVER modifies stop-loss, take-profit, or capital allocation.
    advisory_only = True always for EDGAR-derived signals.
    """
    cache_key = accession_number or hash(text_excerpt[:200])
    if cache_key and cache_key in _8k_llm_cache:
        return _8k_llm_cache[str(cache_key)]

    ollama_host = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
    prompt = _8K_PROMPT_TEMPLATE.format(
        ticker=ticker,
        excerpt=text_excerpt[:500],
    )

    try:
        resp = _requests.post(
            f"{ollama_host}/api/generate",
            json={"model": "phi3:mini", "prompt": prompt, "stream": False},
            timeout=30,
        )
        resp.raise_for_status()
        raw    = resp.json().get("response", "")
        parsed = json.loads(raw)
        result = {
            "event_type":       parsed.get("event_type",      "other"),
            "price_direction":  parsed.get("price_direction", "neutral"),
            "magnitude":        parsed.get("magnitude",       "minor"),
            "affected_sectors": parsed.get("affected_sectors", []),
        }
    except Exception as exc:
        logger.warning(f"parse_8k_with_llm failed ({ticker}/{accession_number}): {exc}")
        result = dict(_8K_LLM_DEFAULT)

    if cache_key:
        _8k_llm_cache[str(cache_key)] = result
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Standalone smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
    )

    print("\n" + "=" * 60)
    print("  MARA EDGAR FEED — SMOKE TEST")
    print("=" * 60)

    monitor = EdgarWatchlistMonitor()
    scanner = EdgarMacroScanner()

    # CIK lookup
    print("\n  CIK lookups:")
    for ticker in ["AAPL", "XOM", "BTC"]:
        cik = monitor.get_cik_for_ticker(ticker)
        print(f"    {ticker:6s} → {cik or 'not found (expected for crypto)'}")

    # Macro scan score
    print("\n  Macro sector scan (EDGAR full-text, last 3 days):")
    score = get_edgar_macro_score()
    print(f"    edgar_macro score: {score:.1f}/100")
    for sector, results in get_last_sector_scan_results().items():
        print(f"    {sector:14s}: {len(results)} filings")

    print("\n" + "=" * 60)
