"""
data/feeds/osint_processor.py

OSINT structured extraction pipeline.

Takes raw events from all 6 data sources and produces a unified list of
OSINTEvent objects suitable for domain routing and regime classification.

When Instructor + Ollama are available, a local LLM (Qwen3-4B on Pi,
Qwen3-8B on laptop) classifies each raw event into a structured format
using Pydantic schema enforcement.

When the LLM stack is unavailable, a keyword + heuristic fallback
still produces valid OSINTEvent objects — just without semantic reasoning.

Install:
    pip install instructor>=1.7.0
    ollama pull qwen3:4b   # Pi 5
    ollama pull qwen3:8b   # Laptop / workstation

Public API:
    process_gdelt(articles)                 → list[OSINTEvent]
    process_ucdp(events)                    → list[OSINTEvent]
    process_edgar(filings, insider_signals) → list[OSINTEvent]
    process_maritime(anomalies)             → list[OSINTEvent]
    process_environment(firms, quakes)      → list[OSINTEvent]
    run_pipeline(raw_sources)               → OSINTPipelineResult
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Optional

logger = logging.getLogger(__name__)

OLLAMA_HOST  = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")


# ── Event type taxonomy ───────────────────────────────────────────────────────

VALID_EVENT_TYPES = frozenset({
    "armed_conflict",
    "sanctions",
    "supply_disruption",
    "maritime_threat",
    "natural_disaster",
    "political_instability",
    "corporate_event",
    "infrastructure_attack",
    "infrastructure_fire",
    "earthquake",
    "military_aviation",      # unusual aircraft activity in/near conflict zone
    "corporate_intelligence", # pre-retail company/supply-chain research
})


class EventSeverity(IntEnum):
    MINIMAL    = 1
    LOW        = 2
    MODERATE   = 3
    NOTABLE    = 4
    SIGNIFICANT= 5
    HIGH       = 6
    SEVERE     = 7
    CRITICAL   = 8
    EXTREME    = 9


ESCALATION_TRAJECTORIES = {"escalating", "de-escalating", "stable"}


@dataclass
class OSINTEvent:
    """
    Unified geopolitical / supply-chain risk event.

    Consumed by domain_router.DomainRouter.evaluate() to produce
    domain entry/exit decisions.
    """
    source:                  str            # gdelt | ucdp | edgar | maritime | firms | usgs
    event_type:              str            # from VALID_EVENT_TYPES
    severity:                EventSeverity  # 1-9
    escalation_trajectory:   str           # escalating | de-escalating | stable
    regions:                 list[str]      = field(default_factory=list)
    commodities_affected:    list[str]      = field(default_factory=list)
    domains_at_risk:         list[str]      = field(default_factory=list)
    raw_text:                str            = ""
    timestamp:               str            = ""
    confidence:              float          = 0.5
    llm_extracted:           bool           = False  # True if LLM processed

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        if self.event_type not in VALID_EVENT_TYPES:
            self.event_type = "supply_disruption"   # safe default
        if self.escalation_trajectory not in ESCALATION_TRAJECTORIES:
            self.escalation_trajectory = "stable"


@dataclass
class OSINTPipelineResult:
    """All OSINT events from one pipeline run, plus per-source scores."""
    events:         list[OSINTEvent]  = field(default_factory=list)
    source_scores:  dict[str, float]  = field(default_factory=dict)
    run_timestamp:  str               = ""

    def __post_init__(self):
        if not self.run_timestamp:
            self.run_timestamp = datetime.now(timezone.utc).isoformat()

    @property
    def risk_event_count(self) -> int:
        return sum(1 for e in self.events
                   if e.escalation_trajectory == "escalating")

    @property
    def max_severity(self) -> int:
        return max((e.severity.value for e in self.events), default=0)


# ── Instructor / LLM availability ────────────────────────────────────────────

_instructor_client = None

def _get_llm_client():
    """Lazy-initialize Instructor+Ollama client. Returns None if unavailable."""
    global _instructor_client
    if _instructor_client is not None:
        return _instructor_client
    try:
        import instructor
        from ollama import Client as OllamaClient
        raw = OllamaClient(host=OLLAMA_HOST)
        _instructor_client = instructor.from_ollama(raw, mode=instructor.Mode.JSON)
        logger.info(f"OSINT processor: Instructor+Ollama initialized ({OLLAMA_MODEL})")
        return _instructor_client
    except ImportError:
        logger.debug("instructor or ollama package not installed — using keyword fallback")
        return None
    except Exception as exc:
        logger.debug(f"Instructor init failed: {exc} — using keyword fallback")
        return None


def _extract_with_llm(text: str, source: str) -> Optional[dict]:
    """
    Use Instructor+Ollama to extract structured event fields from raw text.

    Returns dict with keys: event_type, severity, escalation_trajectory,
    regions, commodities_affected, confidence
    Returns None on failure.
    """
    client = _get_llm_client()
    if client is None:
        return None

    try:
        from pydantic import BaseModel, Field

        class EventClassification(BaseModel):
            event_type: str = Field(
                description=f"One of: {', '.join(sorted(VALID_EVENT_TYPES))}"
            )
            severity: int = Field(
                ge=1, le=9,
                description="Severity 1 (minimal) to 9 (extreme)"
            )
            escalation_trajectory: str = Field(
                description="escalating, de-escalating, or stable"
            )
            regions: list[str] = Field(
                description="Affected countries or regions"
            )
            commodities_affected: list[str] = Field(
                description="Commodities at risk: oil, gas, grain, semiconductors, shipping, etc."
            )
            confidence: float = Field(
                ge=0.0, le=1.0,
                description="Your confidence in this classification"
            )

        prompt = (
            f"Classify this OSINT event (source: {source}):\n\n"
            f"{text[:800]}\n\n"
            "Identify: event type, severity (1-9), trajectory (escalating/de-escalating/stable), "
            "affected regions, and any commodities or supply chains at risk."
        )

        result = client.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_model=EventClassification,
        )

        return {
            "event_type":            result.event_type,
            "severity":              result.severity,
            "escalation_trajectory": result.escalation_trajectory,
            "regions":               result.regions,
            "commodities_affected":  result.commodities_affected,
            "confidence":            result.confidence,
            "llm_extracted":         True,
        }

    except Exception as exc:
        logger.debug(f"LLM extraction failed for '{text[:60]}...': {exc}")
        return None


# ── Keyword-based fallback extractor ─────────────────────────────────────────

_CONFLICT_KEYWORDS  = {"war", "airstrike", "bomb", "invasion", "attack", "strike",
                       "missile", "drone", "explosion", "conflict", "military", "armed"}
_SANCTION_KEYWORDS  = {"sanction", "embargo", "ban", "restriction", "export control"}
_SUPPLY_KEYWORDS    = {"shortage", "disruption", "blockade", "pipeline", "supply",
                       "chokepoint", "halt", "closure"}
_POLITICAL_KEYWORDS = {"coup", "protest", "election", "government", "regime change",
                       "political crisis", "instability"}
_ESCALATE_KEYWORDS  = {"escalat", "intensif", "widen", "expand", "surge", "spike",
                       "increase", "deterior"}
_DEESCALATE_KEYWORDS= {"ceasefire", "truce", "peace", "withdraw", "negotiate",
                       "de-escalat", "calm", "ease"}
_OIL_KEYWORDS       = {"oil", "crude", "petroleum", "opec", "brent", "wti", "refinery"}
_GAS_KEYWORDS       = {"gas", "lng", "pipeline", "natural gas"}
_GRAIN_KEYWORDS     = {"grain", "wheat", "corn", "agriculture", "food"}
_SEMI_KEYWORDS      = {"semiconductor", "chip", "wafer", "tsmc", "intel", "nvidia"}


def _keyword_classify(text: str) -> dict:
    """Return best-effort classification using keyword matching."""
    lower = text.lower()

    # Event type
    if any(k in lower for k in _CONFLICT_KEYWORDS):
        event_type = "armed_conflict"
    elif any(k in lower for k in _SANCTION_KEYWORDS):
        event_type = "sanctions"
    elif any(k in lower for k in _SUPPLY_KEYWORDS):
        event_type = "supply_disruption"
    elif any(k in lower for k in _POLITICAL_KEYWORDS):
        event_type = "political_instability"
    else:
        event_type = "supply_disruption"

    # Severity heuristic
    severity = 3
    if any(k in lower for k in ("hundreds killed", "mass casualt", "major attack")):
        severity = 7
    elif any(k in lower for k in ("killed", "dead", "casualt", "explosion")):
        severity = 5
    elif any(k in lower for k in ("threat", "tension", "warning")):
        severity = 4

    # Trajectory
    if any(k in lower for k in _DEESCALATE_KEYWORDS):
        trajectory = "de-escalating"
    elif any(k in lower for k in _ESCALATE_KEYWORDS):
        trajectory = "escalating"
    else:
        trajectory = "stable"

    # Commodities
    commodities = []
    if any(k in lower for k in _OIL_KEYWORDS):   commodities.append("oil")
    if any(k in lower for k in _GAS_KEYWORDS):   commodities.append("gas")
    if any(k in lower for k in _GRAIN_KEYWORDS): commodities.append("grain")
    if any(k in lower for k in _SEMI_KEYWORDS):  commodities.append("semiconductors")

    return {
        "event_type":            event_type,
        "severity":              severity,
        "escalation_trajectory": trajectory,
        "regions":               [],
        "commodities_affected":  commodities,
        "confidence":            0.4,
        "llm_extracted":         False,
    }


def _classify_text(text: str, source: str) -> dict:
    """Try LLM first, fall back to keyword matching."""
    result = _extract_with_llm(text, source)
    if result:
        return result
    return _keyword_classify(text)


# ── Per-source processors ─────────────────────────────────────────────────────

def process_gdelt(articles: list[dict]) -> list[OSINTEvent]:
    """Convert GDELT article list to OSINTEvent objects."""
    events = []
    # Limit to most recent 20 articles to avoid LLM overload
    for art in articles[:20]:
        title = art.get("title", "") or art.get("url", "")
        if not title:
            continue
        cls = _classify_text(title, "gdelt")
        events.append(OSINTEvent(
            source                = "gdelt",
            event_type            = cls["event_type"],
            severity              = EventSeverity(max(1, min(9, cls["severity"]))),
            escalation_trajectory = cls["escalation_trajectory"],
            regions               = cls["regions"],
            commodities_affected  = cls["commodities_affected"],
            raw_text              = title[:500],
            confidence            = cls["confidence"],
            llm_extracted         = cls["llm_extracted"],
        ))
    return events


def process_ucdp(ucdp_events: list[dict]) -> list[OSINTEvent]:
    """Convert UCDP GED events to OSINTEvent objects."""
    from data.feeds.ucdp_client import classify_ucdp_severity
    events = []
    for ev in ucdp_events[:30]:
        country  = ev.get("country", "unknown")
        conflict = ev.get("conflict_name", "")
        try:
            deaths = int(ev.get("deaths_best") or 0)
        except (ValueError, TypeError):
            deaths = 0
        text = f"{conflict} in {country}: {deaths} deaths"

        severity_int = classify_ucdp_severity(ev)
        trajectory   = "escalating" if deaths > 5 else "stable"

        events.append(OSINTEvent(
            source                = "ucdp",
            event_type            = "armed_conflict",
            severity              = EventSeverity(severity_int),
            escalation_trajectory = trajectory,
            regions               = [country],
            commodities_affected  = [],
            raw_text              = text,
            confidence            = 0.85,   # UCDP is high-quality academic data
            llm_extracted         = False,
        ))
    return events


def process_edgar(filings: list[dict], insider_signals: list[dict]) -> list[OSINTEvent]:
    """Convert SEC EDGAR outputs to OSINTEvent objects."""
    events = []
    for f in filings:
        desc  = f.get("description", "") or ""
        text  = f"SEC 8-K ({f.get('ticker', '')}): {desc}"
        cls   = _classify_text(text, "edgar")

        events.append(OSINTEvent(
            source                = "edgar",
            event_type            = "corporate_event",
            severity              = EventSeverity(max(1, min(9, cls["severity"]))),
            escalation_trajectory = cls["escalation_trajectory"],
            regions               = ["usa"],
            commodities_affected  = cls["commodities_affected"],
            raw_text              = text[:500],
            confidence            = cls["confidence"],
            llm_extracted         = cls["llm_extracted"],
        ))

    for sig in insider_signals:
        severity = EventSeverity.LOW if sig.get("signal") == "insider_cluster_buy" else EventSeverity.NOTABLE
        traj     = "de-escalating" if sig.get("signal") == "insider_cluster_buy" else "escalating"
        events.append(OSINTEvent(
            source                = "edgar",
            event_type            = "corporate_event",
            severity              = severity,
            escalation_trajectory = traj,
            regions               = ["usa"],
            raw_text              = f"{sig.get('signal', '')} {sig.get('ticker', '')}",
            confidence            = sig.get("strength", 0.5),
            llm_extracted         = False,
        ))

    return events


def process_maritime(anomalies: list[dict]) -> list[OSINTEvent]:
    """Convert AIS maritime anomalies to OSINTEvent objects."""
    events = []
    for a in anomalies:
        sev = EventSeverity(max(1, min(9, a.get("severity_int", 4))))
        traj = "escalating" if a["anomaly_type"] == "traffic_stoppage" else "stable"
        events.append(OSINTEvent(
            source                = "maritime",
            event_type            = "maritime_threat",
            severity              = sev,
            escalation_trajectory = traj,
            regions               = [a.get("chokepoint", "").replace("_", " ")],
            commodities_affected  = ["oil", "shipping"],
            domains_at_risk       = a.get("domains", ["commodities"]),
            raw_text              = a.get("description", ""),
            confidence            = 0.7,
            llm_extracted         = False,
        ))
    return events


def process_environment(firms_records: list[dict], quake_features: list[dict]) -> list[OSINTEvent]:
    """Convert NASA FIRMS + USGS events to OSINTEvent objects."""
    from data.feeds.environment_client import classify_firms_events, classify_earthquake_events

    firms_events  = classify_firms_events(firms_records)
    quake_events  = classify_earthquake_events(quake_features)
    events        = []

    for ev in firms_events + quake_events:
        etype = "infrastructure_fire" if ev["source"] == "nasa_firms" else "natural_disaster"
        events.append(OSINTEvent(
            source                = ev["source"],
            event_type            = etype,
            severity              = EventSeverity(max(1, min(9, ev.get("severity_int", 3)))),
            escalation_trajectory = "escalating",
            regions               = [ev.get("site", "") or ev.get("place", "")],
            commodities_affected  = ["oil"] if ev.get("domain") == "commodities" else [],
            domains_at_risk       = [ev.get("domain", "commodities")],
            raw_text              = ev.get("description", ""),
            confidence            = 0.75,
            llm_extracted         = False,
        ))
    return events


# ── Aviation processor ────────────────────────────────────────────────────────

# Map aviation anomaly_type → OSINTEvent escalation trajectory
_AVIATION_TRAJECTORY: dict[str, str] = {
    "military_surge":   "escalating",
    "emergency_squawk": "escalating",
    "mass_evacuation":  "escalating",
    "isr_activity":     "escalating",
    "vip_movement":     "stable",      # ambiguous — may be diplomatic
}

# Domains at risk per aviation anomaly type
_AVIATION_DOMAINS: dict[str, list[str]] = {
    "military_surge":   ["commodities", "crypto_perps"],
    "emergency_squawk": ["commodities", "crypto_perps", "us_equities"],
    "mass_evacuation":  ["us_equities", "commodities"],
    "isr_activity":     ["commodities", "us_equities"],
    "vip_movement":     ["prediction", "us_equities"],
}


def process_aviation(anomalies: list[dict]) -> list[OSINTEvent]:
    """
    Convert aviation anomaly dicts (from aviation_client.detect_aviation_anomalies)
    to OSINTEvent objects.

    Severity comes directly from the ZoneAnomalyFSM (already calibrated 0–9).
    Regions are derived from the zone name.
    """
    events = []
    for a in anomalies:
        zone     = a.get("zone", "unknown").replace("_", " ")
        anom     = a.get("anomaly_type", "military_surge")
        sev_int  = max(1, min(9, a.get("severity_int", 4)))
        traj     = _AVIATION_TRAJECTORY.get(anom, "stable")
        domains  = _AVIATION_DOMAINS.get(anom, ["commodities"])
        desc     = a.get("description", "")

        events.append(OSINTEvent(
            source                = "aviation",
            event_type            = "military_aviation",
            severity              = EventSeverity(sev_int),
            escalation_trajectory = traj,
            regions               = [zone],
            commodities_affected  = ["oil"] if "commodities" in domains else [],
            domains_at_risk       = a.get("domains", domains),
            raw_text              = desc[:500],
            confidence            = 0.75,   # OpenSky is real-time ADS-B — high quality
            llm_extracted         = False,
        ))
    return events


# ── Company intelligence processor ───────────────────────────────────────────

def process_company_research(profiles: list) -> list[OSINTEvent]:
    """
    Convert CompanyRiskProfile objects (from company_researcher.research_event)
    to OSINTEvent objects.

    Delegates to company_researcher.process_company_research to avoid
    duplicating the severity/trajectory mapping logic.
    """
    if not profiles:
        return []
    try:
        from data.feeds.company_researcher import process_company_research as _convert
        return _convert(profiles)
    except Exception as exc:
        logger.debug(f"company research processing failed: {exc}")
        return []


# ── Pipeline orchestrator ─────────────────────────────────────────────────────

def run_pipeline(
    gdelt_articles:      list[dict] | None = None,
    ucdp_events:         list[dict] | None = None,
    edgar_filings:       list[dict] | None = None,
    insider_signals:     list[dict] | None = None,
    maritime_anomalies:  list[dict] | None = None,
    firms_records:       list[dict] | None = None,
    quake_features:      list[dict] | None = None,
    aviation_anomalies:  list[dict] | None = None,   # from aviation_client
    company_profiles:    list       | None = None,   # list[CompanyRiskProfile]
) -> OSINTPipelineResult:
    """
    Aggregate all OSINT sources into a unified OSINTPipelineResult.

    Each source is optional — missing sources produce empty event lists
    and 0.0 scores (contributing to dynamic weight redistribution).

    New sources (April 2026):
        aviation_anomalies  — from aviation_client.detect_aviation_anomalies()
        company_profiles    — from company_researcher.research_event()
    """
    from data.feeds.gdelt_client import score_gdelt_articles
    from data.feeds.ucdp_client import score_ucdp_events
    from data.feeds.edgar_client import score_edgar_signals
    from data.feeds.maritime_client import score_maritime
    from data.feeds.environment_client import score_environment

    gdelt_articles     = gdelt_articles     or []
    ucdp_events        = ucdp_events        or []
    edgar_filings      = edgar_filings      or []
    insider_signals    = insider_signals    or []
    maritime_anomalies = maritime_anomalies or []
    firms_records      = firms_records      or []
    quake_features     = quake_features     or []
    aviation_anomalies = aviation_anomalies or []
    company_profiles   = company_profiles   or []

    all_events: list[OSINTEvent] = []
    source_scores: dict[str, float] = {}

    if gdelt_articles:
        all_events.extend(process_gdelt(gdelt_articles))
        source_scores["gdelt"] = score_gdelt_articles(gdelt_articles)
    else:
        source_scores["gdelt"] = 0.0

    if ucdp_events:
        all_events.extend(process_ucdp(ucdp_events))
        source_scores["ucdp"] = score_ucdp_events(ucdp_events)
    else:
        source_scores["ucdp"] = 0.0

    if edgar_filings or insider_signals:
        all_events.extend(process_edgar(edgar_filings, insider_signals))
        source_scores["edgar"] = score_edgar_signals(edgar_filings, insider_signals)
    else:
        source_scores["edgar"] = 0.0

    if maritime_anomalies:
        all_events.extend(process_maritime(maritime_anomalies))
        total_sev = sum(a.get("severity_int", 1) * 5 for a in maritime_anomalies)
        source_scores["maritime"] = min(100.0, float(total_sev))
    else:
        source_scores["maritime"] = 0.0

    if firms_records or quake_features:
        all_events.extend(process_environment(firms_records, quake_features))
        source_scores["environment"] = score_environment(firms_records, quake_features)
    else:
        source_scores["environment"] = 0.0

    if aviation_anomalies:
        all_events.extend(process_aviation(aviation_anomalies))
        total_sev = sum(a.get("severity_int", 1) * 6 for a in aviation_anomalies)
        source_scores["aviation"] = min(100.0, float(total_sev))
    else:
        source_scores["aviation"] = 0.0

    if company_profiles:
        all_events.extend(process_company_research(company_profiles))
        try:
            from data.feeds.company_researcher import score_company_intel
            source_scores["company_intel"] = score_company_intel(company_profiles)
        except Exception:
            source_scores["company_intel"] = 0.0
    else:
        source_scores["company_intel"] = 0.0

    logger.info(
        f"OSINT pipeline: {len(all_events)} events from {len(source_scores)} sources  "
        f"scores={source_scores}"
    )
    return OSINTPipelineResult(events=all_events, source_scores=source_scores)
