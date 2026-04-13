"""
data/feeds/company_researcher.py

Deep-dive company intelligence: SearXNG → ScrapeGraphAI extraction.

Triggered by OSINT events that reference specific companies, government
contracts, or supply-chain nodes. Goal: surface market-moving intelligence
hours before it reaches retail news flow.

Pipeline per event text:
    1. Extract candidate company/org names via LLM (Instructor+Ollama) or
       regex against a known defense/energy/semiconductor ticker list.
    2. SearXNG search: "[company] [event context] recent" (news + general).
    3. ScrapeGraphAI scrapes top N URLs → structured CompanyRiskProfile.
    4. Fallback: extract intelligence directly from SearXNG snippets when
       ScrapeGraphAI or Ollama are unavailable.

CompanyRiskProfile feeds into OSINTEvent objects with:
    source        = "company_intel"
    event_type    = "corporate_intelligence"

Environment variables:
    SEARXNG_URL    — SearXNG base URL (default: http://searxng:8080)
    OLLAMA_HOST    — Ollama endpoint  (default: http://localhost:11434)
    OLLAMA_MODEL   — Extraction model (default: qwen3:4b)

Public API:
    research_event(event_text, context, max_companies)
        → list[CompanyRiskProfile]
    process_company_research(profiles)
        → list[OSINTEvent]          (for osint_processor.run_pipeline)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

SEARXNG_URL  = os.environ.get("SEARXNG_URL",  "http://searxng:8080")
OLLAMA_HOST  = os.environ.get("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")

_MAX_URLS_PER_COMPANY = 3    # ScrapeGraphAI calls per company (rate limiting)
_SNIPPET_MAX_CHARS    = 600  # characters to use from SearXNG snippet


# ── Risk taxonomy ─────────────────────────────────────────────────────────────

RISK_LEVELS   = ("low", "moderate", "high", "critical")
EXPOSURE_TYPES = (
    "supplier",       # company supplies goods/services to a conflict party
    "customer",       # company's customers are in the affected region
    "regulator",      # company faces sanctions / regulatory action
    "conflict_party", # company directly involved (defense contractor, state-owned)
    "investor",       # company holds assets in the affected region
    "unknown",
)

_RISK_SEVERITY_MAP = {
    "low":      2,
    "moderate": 4,
    "high":     6,
    "critical": 8,
}

_EXPOSURE_TRAJECTORY_MAP = {
    "supplier":       "escalating",
    "customer":       "escalating",
    "regulator":      "escalating",
    "conflict_party": "escalating",
    "investor":       "stable",
    "unknown":        "stable",
}


@dataclass
class CompanyRiskProfile:
    """Structured intelligence extracted for one company from one OSINT event."""
    company_name:  str
    ticker:        str           = ""    # empty if not found
    risk_level:    str           = "moderate"
    exposure_type: str           = "unknown"
    evidence_url:  str           = ""
    summary:       str           = ""
    confidence:    float         = 0.4
    source:        str           = "snippet"  # snippet | scrapegraph | llm
    domains_at_risk: list[str]   = field(default_factory=list)

    def __post_init__(self):
        if self.risk_level not in RISK_LEVELS:
            self.risk_level = "moderate"
        if self.exposure_type not in EXPOSURE_TYPES:
            self.exposure_type = "unknown"


# ── Known tickers / company → sector map (defense, energy, semiconductors) ───
# Used for fast keyword extraction when LLM is unavailable.
_KNOWN_TICKERS: dict[str, dict] = {
    # Defense & aerospace
    "RTX":   {"name": "Raytheon Technologies",   "domains": ["us_equities"]},
    "LMT":   {"name": "Lockheed Martin",          "domains": ["us_equities"]},
    "NOC":   {"name": "Northrop Grumman",         "domains": ["us_equities"]},
    "BA":    {"name": "Boeing",                   "domains": ["us_equities"]},
    "GD":    {"name": "General Dynamics",         "domains": ["us_equities"]},
    "HII":   {"name": "Huntington Ingalls",       "domains": ["us_equities"]},
    "LDOS":  {"name": "Leidos",                   "domains": ["us_equities"]},
    "BAE":   {"name": "BAE Systems",              "domains": ["us_equities"]},
    # Energy
    "XOM":   {"name": "ExxonMobil",               "domains": ["commodities"]},
    "CVX":   {"name": "Chevron",                  "domains": ["commodities"]},
    "SLB":   {"name": "SLB (Schlumberger)",       "domains": ["commodities"]},
    "BP":    {"name": "BP",                       "domains": ["commodities"]},
    "SHEL":  {"name": "Shell",                    "domains": ["commodities"]},
    "TTE":   {"name": "TotalEnergies",            "domains": ["commodities"]},
    # Semiconductors / supply chain
    "TSM":   {"name": "TSMC",                     "domains": ["us_equities", "crypto_perps"]},
    "NVDA":  {"name": "Nvidia",                   "domains": ["us_equities"]},
    "INTC":  {"name": "Intel",                    "domains": ["us_equities"]},
    "AMD":   {"name": "AMD",                      "domains": ["us_equities"]},
    "ASML":  {"name": "ASML",                     "domains": ["us_equities"]},
    # Shipping / commodities
    "MAERSK":{"name": "Maersk",                   "domains": ["commodities"]},
    "ZIM":   {"name": "ZIM Integrated Shipping",  "domains": ["commodities"]},
    "BDRY":  {"name": "Breakwave Dry Bulk ETF",   "domains": ["commodities"]},
}

# Name → ticker reverse map for text matching
_NAME_TO_TICKER: dict[str, str] = {
    v["name"].lower(): k for k, v in _KNOWN_TICKERS.items()
}


def _extract_companies_keyword(text: str) -> list[str]:
    """
    Fast keyword-based company extraction from event text.
    Returns list of tickers found in the text.
    """
    lower = text.lower()
    found = []

    # Check ticker symbols (e.g. "RTX", "LMT" — uppercase 2-4 chars)
    for ticker in re.findall(r'\b([A-Z]{2,5})\b', text):
        if ticker in _KNOWN_TICKERS:
            found.append(ticker)

    # Check company names
    for name, ticker in _NAME_TO_TICKER.items():
        if name in lower and ticker not in found:
            found.append(ticker)

    return list(dict.fromkeys(found))   # deduplicate, preserve order


def _extract_companies_llm(text: str) -> list[str]:
    """
    LLM-based company extraction using Instructor+Ollama.
    Returns list of tickers. Returns [] on any failure.
    """
    try:
        import instructor
        from ollama import Client as OllamaClient
        from pydantic import BaseModel, Field

        class CompanyList(BaseModel):
            tickers: list[str] = Field(
                description=(
                    "List of stock tickers for companies mentioned or implied "
                    "by the event. Use standard ticker symbols (RTX, LMT, TSM, etc.). "
                    "Return only companies with clear material exposure. Max 5."
                )
            )

        raw    = OllamaClient(host=OLLAMA_HOST)
        client = instructor.from_ollama(raw, mode=instructor.Mode.JSON)

        result = client.chat.completions.create(
            model    = OLLAMA_MODEL,
            messages = [{
                "role":    "user",
                "content": (
                    f"Identify stock tickers of companies with material exposure "
                    f"to this geopolitical/supply-chain event:\n\n{text[:600]}\n\n"
                    f"Focus on defense contractors, energy companies, shipping firms, "
                    f"and semiconductor manufacturers."
                ),
            }],
            response_model = CompanyList,
        )
        return [t.upper().strip() for t in result.tickers if t.strip()]

    except ImportError:
        return []
    except Exception as exc:
        logger.debug(f"LLM company extraction failed: {exc}")
        return []


def _scrape_url(url: str, company: str) -> Optional[str]:
    """
    Use ScrapeGraphAI to extract company risk summary from a URL.
    Returns extracted text or None if ScrapeGraphAI unavailable.
    """
    try:
        from scrapegraphai.graphs import SmartScraperGraph

        graph_config = {
            "llm": {
                "model":       f"ollama/{OLLAMA_MODEL}",
                "base_url":    OLLAMA_HOST,
                "temperature": 0,
            },
            "verbose":  False,
            "headless": True,
        }

        prompt = (
            f"Extract: (1) what risk or opportunity this article represents for {company}, "
            f"(2) the company's exposure type (supplier/customer/regulator/conflict_party/investor), "
            f"(3) a one-sentence risk summary. Return as plain text."
        )

        graph  = SmartScraperGraph(
            prompt = prompt,
            source = url,
            config = graph_config,
        )
        result = graph.run()

        if isinstance(result, dict):
            # SmartScraperGraph may return a dict
            return str(result)
        return str(result)[:_SNIPPET_MAX_CHARS] if result else None

    except ImportError:
        logger.debug("scrapegraphai not installed — using snippet fallback")
        return None
    except Exception as exc:
        logger.debug(f"ScrapeGraphAI failed for {url}: {exc}")
        return None


def _profile_from_snippet(
    ticker: str,
    results: list[dict],
) -> CompanyRiskProfile:
    """
    Build a CompanyRiskProfile from SearXNG snippet text when
    ScrapeGraphAI is unavailable or fails.
    """
    info      = _KNOWN_TICKERS.get(ticker, {})
    company   = info.get("name", ticker)
    domains   = info.get("domains", ["us_equities"])
    evidence  = results[0].get("url", "") if results else ""

    # Concatenate snippet text
    combined = " ".join(
        r.get("content", "") or r.get("title", "")
        for r in results[:3]
    )[:_SNIPPET_MAX_CHARS]

    # Heuristic risk level from keyword presence
    lower = combined.lower()
    if any(w in lower for w in ("sanction", "ban", "restrict", "block")):
        risk   = "high"
    elif any(w in lower for w in ("contract", "award", "supply", "shortage")):
        risk   = "moderate"
    else:
        risk   = "low"

    # Exposure type heuristic
    if any(w in lower for w in ("contract", "defense", "military", "government")):
        exposure = "conflict_party"
    elif any(w in lower for w in ("supplier", "supply chain", "component")):
        exposure = "supplier"
    elif any(w in lower for w in ("sanction", "banned", "restricted")):
        exposure = "regulator"
    else:
        exposure = "unknown"

    return CompanyRiskProfile(
        company_name  = company,
        ticker        = ticker,
        risk_level    = risk,
        exposure_type = exposure,
        evidence_url  = evidence,
        summary       = combined[:200],
        confidence    = 0.4,
        source        = "snippet",
        domains_at_risk = domains,
    )


def _profile_from_scrape(
    ticker:    str,
    url:       str,
    extracted: str,
) -> CompanyRiskProfile:
    """Build a CompanyRiskProfile from ScrapeGraphAI extracted text."""
    info    = _KNOWN_TICKERS.get(ticker, {})
    company = info.get("name", ticker)
    domains = info.get("domains", ["us_equities"])
    lower   = extracted.lower()

    risk = "critical" if "critical" in lower else (
           "high"     if any(w in lower for w in ("high risk", "severe", "significant")) else (
           "moderate" if any(w in lower for w in ("moderate", "some risk", "possible")) else
           "low"
    ))

    for etype in EXPOSURE_TYPES:
        if etype.replace("_", " ") in lower or etype in lower:
            exposure = etype
            break
    else:
        exposure = "unknown"

    return CompanyRiskProfile(
        company_name  = company,
        ticker        = ticker,
        risk_level    = risk,
        exposure_type = exposure,
        evidence_url  = url,
        summary       = extracted[:300],
        confidence    = 0.65,
        source        = "scrapegraph",
        domains_at_risk = domains,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def research_event(
    event_text:    str,
    context:       str = "",
    max_companies: int = 4,
) -> list[CompanyRiskProfile]:
    """
    Run the full research pipeline for one OSINT event.

    1. Extract company tickers from event_text (LLM → keyword fallback).
    2. For each ticker: SearXNG search → ScrapeGraphAI scrape (→ snippet fallback).
    3. Return list of CompanyRiskProfile objects.

    Designed to be called from conflict_index._fetch_osint_layer() or
    osint_processor.run_pipeline() — both tolerate empty lists gracefully.
    """
    from data.feeds.searxng_client import search_ticker, search_event_companies

    if not event_text.strip():
        return []

    # Step 1: identify companies
    tickers = _extract_companies_llm(event_text)
    if not tickers:
        tickers = _extract_companies_keyword(event_text)
    tickers = tickers[:max_companies]

    if not tickers:
        logger.debug("company_researcher: no companies identified in event text")
        return []

    profiles: list[CompanyRiskProfile] = []
    ctx      = context or event_text[:80]

    for ticker in tickers:
        # Step 2: search
        results = search_ticker(ticker, context=ctx, max_results=_MAX_URLS_PER_COMPANY)
        if not results:
            results = search_event_companies(f"{ticker} {ctx}")

        if not results:
            continue

        # Step 3: scrape first URL, fall back to snippet
        top_url   = results[0].get("url", "")
        extracted = _scrape_url(top_url, ticker) if top_url else None

        if extracted:
            profiles.append(_profile_from_scrape(ticker, top_url, extracted))
        else:
            profiles.append(_profile_from_snippet(ticker, results))

    logger.info(
        f"company_researcher: {len(profiles)} profiles for "
        f"{len(tickers)} companies — "
        f"sources: {[p.source for p in profiles]}"
    )
    return profiles


def process_company_research(profiles: list[CompanyRiskProfile]) -> list:
    """
    Convert CompanyRiskProfile objects to OSINTEvent objects.

    Imported by osint_processor.run_pipeline(). Returns [] if profiles is empty.
    Avoids a circular import by importing OSINTEvent lazily.
    """
    if not profiles:
        return []

    from data.feeds.osint_processor import OSINTEvent, EventSeverity

    events = []
    for p in profiles:
        severity_int = _RISK_SEVERITY_MAP.get(p.risk_level, 4)
        trajectory   = _EXPOSURE_TRAJECTORY_MAP.get(p.exposure_type, "stable")

        events.append(OSINTEvent(
            source                = "company_intel",
            event_type            = "corporate_intelligence",
            severity              = EventSeverity(max(1, min(9, severity_int))),
            escalation_trajectory = trajectory,
            regions               = [],
            commodities_affected  = [],
            domains_at_risk       = p.domains_at_risk,
            raw_text              = (
                f"{p.company_name} ({p.ticker}): {p.summary}"
            )[:500],
            confidence            = p.confidence,
            llm_extracted         = p.source in ("scrapegraph", "llm"),
        ))

    return events


def score_company_intel(profiles: list[CompanyRiskProfile]) -> float:
    """
    Convert CompanyRiskProfile list to a 0-100 war-premium contribution score.

    High-risk profiles for companies with conflict-party or supplier exposure
    carry the most weight; investor exposure carries the least.
    """
    if not profiles:
        return 0.0

    _EXPOSURE_WEIGHT = {
        "conflict_party": 3.0,
        "supplier":       2.5,
        "customer":       2.0,
        "regulator":      2.0,
        "investor":       1.0,
        "unknown":        0.5,
    }

    score = 0.0
    for p in profiles:
        sev    = _RISK_SEVERITY_MAP.get(p.risk_level, 4)
        weight = _EXPOSURE_WEIGHT.get(p.exposure_type, 1.0)
        score += sev * weight * p.confidence

    return round(min(100.0, score), 1)
