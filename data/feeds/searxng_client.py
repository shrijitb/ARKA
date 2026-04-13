"""
data/feeds/searxng_client.py

SearXNG metasearch client — deep-dive event and company intelligence.

Queries a self-hosted SearXNG instance (Docker service `arka-searxng`)
to surface breaking news, company exposure, and supply-chain intelligence
before it reaches mainstream financial terminals. All searches stay local —
no external rate limits, no API keys, no data leakage.

SearXNG JSON API:
    GET /search?q={query}&format=json&categories=news,general&time_range=day
    → {"results": [{"title", "url", "content", "score", "category", ...}]}

Environment variables:
    SEARXNG_URL   — SearXNG base URL (default: http://searxng:8080)

Public API:
    search(query, categories, time_range, max_results) → list[dict]
    search_event_companies(event_text)                 → list[dict]
    search_ticker(ticker, context)                     → list[dict]
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlencode, urljoin

import requests

logger = logging.getLogger(__name__)

SEARXNG_URL     = os.environ.get("SEARXNG_URL", "http://searxng:8080")
_SEARCH_TIMEOUT = 10   # seconds
_MAX_RESULTS    = 10

# Categories SearXNG supports
CATEGORIES_NEWS    = "news"
CATEGORIES_GENERAL = "general"
CATEGORIES_FINANCE = "finance"    # requires engine config in settings.yml

# Time range tokens accepted by SearXNG
TIME_DAY   = "day"
TIME_WEEK  = "week"
TIME_MONTH = "month"
TIME_NONE  = ""          # no restriction


def search(
    query:       str,
    categories:  str  = CATEGORIES_NEWS,
    time_range:  str  = TIME_DAY,
    max_results: int  = _MAX_RESULTS,
    language:    str  = "en",
) -> list[dict]:
    """
    Execute a SearXNG search and return a list of result dicts.

    Each result dict contains at minimum:
        title    (str)
        url      (str)
        content  (str)  — snippet / lead paragraph
        score    (float) — relevance score from SearXNG
        engines  (list[str]) — which search engines returned this result

    Returns [] if:
      - SEARXNG_URL service is unreachable
      - JSON parse fails
      - Non-200 response
    """
    if not query.strip():
        return []

    params = {
        "q":          query,
        "format":     "json",
        "categories": categories,
        "language":   language,
    }
    if time_range:
        params["time_range"] = time_range

    url = urljoin(SEARXNG_URL, "/search") + "?" + urlencode(params)

    try:
        resp = requests.get(url, timeout=_SEARCH_TIMEOUT)
        if not resp.ok:
            logger.debug(f"SearXNG: HTTP {resp.status_code} for query '{query[:60]}'")
            return []
        data    = resp.json()
        results = data.get("results", [])[:max_results]
        logger.debug(f"SearXNG: {len(results)} results for '{query[:60]}'")
        return results

    except requests.exceptions.ConnectionError:
        logger.debug(f"SearXNG: connection refused — is the service running at {SEARXNG_URL}?")
        return []
    except requests.exceptions.Timeout:
        logger.debug(f"SearXNG: timeout for query '{query[:60]}'")
        return []
    except Exception as exc:
        logger.debug(f"SearXNG: unexpected error for '{query[:60]}': {exc}")
        return []


def search_event_companies(event_text: str, max_results: int = 8) -> list[dict]:
    """
    Search for company coverage of a geopolitical or supply-chain event.

    Constructs a news query targeting corporate and market impact coverage.
    Returns SearXNG result dicts sorted by relevance score.
    """
    # Trim raw event text to key noun phrases — first 120 chars is enough context
    snippet = event_text[:120].strip().rstrip(".,;")
    query   = f"{snippet} company impact supply chain"
    return search(query, categories=CATEGORIES_NEWS, time_range=TIME_WEEK,
                  max_results=max_results)


def search_ticker(ticker: str, context: str = "", max_results: int = 6) -> list[dict]:
    """
    Search for recent news about a specific ticker or company name.

    Args:
        ticker:   Stock ticker or company name (e.g. "RTX", "Raytheon")
        context:  Optional context to narrow results (e.g. "defense contract Ukraine")
    """
    query = f"{ticker} {context}".strip() if context else ticker
    return search(query, categories=CATEGORIES_NEWS, time_range=TIME_WEEK,
                  max_results=max_results)


def search_contract_award(keyword: str) -> list[dict]:
    """
    Search for government contract awards and defence procurement news.

    Designed to surface military/government contracts before they appear
    in earnings calls or SEC filings — typically 24-72h lead time.
    """
    query = f"{keyword} government contract award defense procurement"
    return search(query, categories=CATEGORIES_NEWS, time_range=TIME_WEEK,
                  max_results=8)
