"""
data/feeds/aviation_client.py

OpenSky Network aircraft tracking — military activity and anomaly detection.

Monitors bounding boxes around active conflict zones for unusual military,
VIP, ISR, and private aviation patterns that precede market-moving events by
hours or days. Primary value: early warning before retail news flow.

Uses the OpenSky Network REST API (free, no key required; optional auth
raises daily quota from 400 to 4000 requests):
    https://opensky-network.org/api/states/all
    ?lamin=LAT_MIN&lomin=LON_MIN&lamax=LAT_MAX&lomax=LON_MAX

State vector field order (by index):
    0  icao24          hex transponder address
    1  callsign        flight callsign (may be None)
    2  origin_country  country of aircraft registration
    3  time_position   Unix timestamp of last position fix
    4  last_contact    Unix timestamp of last ADS-B message
    5  longitude
    6  latitude
    7  baro_altitude   metres (may be None if on ground)
    8  on_ground       bool
    9  velocity        m/s
    10 true_track      degrees clockwise from north
    11 vertical_rate   m/s
    12 sensors         (ignored)
    13 geo_altitude    metres
    14 squawk          Mode-C squawk code (string)
    15 spi             special purpose indicator
    16 position_source 0=ADS-B, 1=ASTERIX, 2=MLAT, 3=FLARM
    17 category        aircraft category (0=no info)

Environment variables:
    OPENSKY_USERNAME   — OpenSky account username (optional, free registration)
    OPENSKY_PASSWORD   — OpenSky account password (optional)

Rate limits:
    Anonymous:     10 req/min, 400 req/day
    Authenticated: 10 req/min, 4000 req/day

Anomaly types emitted:
    military_surge     — density of military callsigns exceeds zone baseline
    vip_movement       — SAM/state-mission aircraft detected (precede policy events)
    emergency_squawk   — aircraft squawking 7700/7600/7500 in a conflict zone
    isr_activity       — ISR/recon callsigns detected (signals active intelligence ops)
    mass_evacuation    — ≥3 EVAC callsigns (diplomatic or civilian evacuation underway)

Public API:
    fetch_aircraft_states(zones)         → list[dict]  raw state vectors per zone
    detect_aviation_anomalies(states)    → list[dict]  anomaly event dicts
    score_aviation(states)               → float 0-100 war-premium contribution
"""

from __future__ import annotations

import logging
import os
import time
from enum import Enum
from typing import Optional

import requests

logger = logging.getLogger(__name__)

OPENSKY_USERNAME = os.environ.get("OPENSKY_USERNAME", "")
OPENSKY_PASSWORD = os.environ.get("OPENSKY_PASSWORD", "")
_OPENSKY_BASE    = "https://opensky-network.org/api/states/all"
_REQUEST_TIMEOUT = 15
_MIN_REQUEST_GAP = 7.0   # seconds between zone queries — stay under 10 req/min

# ── Conflict zone bounding boxes ──────────────────────────────────────────────
# Format: [lat_min, lon_min, lat_max, lon_max]
# baseline_military: expected simultaneous military aircraft at any moment
AVIATION_ZONES: dict[str, dict] = {
    "levant_corridor": {
        "bbox":               [29.0,  33.0, 37.0,  42.0],
        "baseline_military":  3,
        "domains":            ["commodities", "us_equities"],
        "description":        "Israel/Lebanon/Syria/Jordan — air operations indicator",
    },
    "persian_gulf_approaches": {
        "bbox":               [22.0,  48.0, 30.0,  60.0],
        "baseline_military":  5,
        "domains":            ["commodities", "crypto_perps"],
        "description":        "Iran/Gulf states — Strait of Hormuz air access corridor",
    },
    "eastern_europe_front": {
        "bbox":               [46.0,  28.0, 52.0,  40.0],
        "baseline_military":  10,
        "domains":            ["commodities"],
        "description":        "Ukraine/Moldova/Romania — active war-premium zone",
    },
    "taiwan_strait_airspace": {
        "bbox":               [22.0, 117.0, 27.0, 123.0],
        "baseline_military":  4,
        "domains":            ["us_equities", "crypto_perps"],
        "description":        "Taiwan Strait — semiconductor/tech supply chain trigger",
    },
    "korean_peninsula": {
        "bbox":               [33.0, 124.0, 39.0, 131.0],
        "baseline_military":  5,
        "domains":            ["us_equities"],
        "description":        "Korean Peninsula — DPRK escalation air activity",
    },
    "south_china_sea": {
        "bbox":               [ 5.0, 108.0, 22.0, 121.0],
        "baseline_military":  6,
        "domains":            ["us_equities", "commodities"],
        "description":        "South China Sea — territorial dispute/patrol indicator",
    },
}

# ── Military callsign prefix taxonomy ────────────────────────────────────────
# Maps prefix → (country/alliance, mission_type)
# Matches are prefix-based (startswith), case-insensitive, callsign stripped.
_MILITARY_CALLSIGN_MAP: dict[str, tuple[str, str]] = {
    # US Air Force — Air Mobility Command
    "RCH":    ("US",    "airlift"),      # Reach — heavy airlift
    "PAT":    ("US",    "medevac"),      # Patient Air Transport
    "EVAC":   ("US",    "evacuation"),
    # US Special Air Mission (state transport — VP, SECDEF, POTUS surrogates)
    "SAM":    ("US",    "vip"),
    "VENUS":  ("US",    "vip"),          # VP aircraft
    "EXEC":   ("US",    "vip"),
    # US tankers / ISR
    "JAKE":   ("US",    "tanker"),
    "POLO":   ("US",    "isr"),
    "STING":  ("US",    "isr"),
    "IRON":   ("NATO",  "exercise"),
    # UK RAF
    "RRR":    ("UK",    "military"),
    "ASCOT":  ("UK",    "airlift"),
    "COMET":  ("UK",    "isr"),
    "VIPER":  ("UK",    "tactical"),
    # NATO AWACSe/C2
    "NATO":   ("NATO",  "awacs"),
    "AWACS":  ("NATO",  "awacs"),
    # Russia — Russian Aerospace Forces
    "RFF":    ("Russia","military"),
    # Israel
    "ELI":    ("Israel","military"),     # IAF / El Al government flights
    "IAF":    ("Israel","military"),
    # China — PLAAF
    "CCA":    ("China", "military"),
    "B20":    ("China", "military"),
    # Generic military patterns
    "DUKE":   ("US",    "tactical"),
    "SWORD":  ("US",    "tactical"),
    "HAWK":   ("US",    "tactical"),
    "EAGLE":  ("US",    "tactical"),
    "GHOST":  ("US",    "tactical"),
    "MAGIC":  ("NATO",  "awacs"),
}

# Emergency squawk codes
_EMERGENCY_SQUAWKS = {"7700", "7600", "7500"}
# 7700 = general emergency, 7600 = radio failure, 7500 = hijack

# Anomaly thresholds
_MILITARY_SURGE_MULTIPLIER = 2.5   # military count ≥ 2.5× baseline → surge
_EVAC_MASS_THRESHOLD        = 3    # ≥ 3 EVAC callsigns simultaneously


# ── Zone Anomaly Finite State Machine ─────────────────────────────────────────

class ZoneState(str, Enum):
    """Observable states for a single aviation monitoring zone."""
    NORMAL     = "normal"      # baseline activity
    ELEVATED   = "elevated"    # mil count 1.5–2.5× OR VIP/ISR present
    SURGE      = "surge"       # mil count ≥ 2.5× baseline
    EMERGENCY  = "emergency"   # 77xx squawk active in zone
    EVACUATING = "evacuating"  # mass EVAC callsigns present


# Valid transitions: each state maps to the set of states it may move to.
# The FSM enforces these — invalid transitions are silently ignored.
_ZONE_TRANSITIONS: dict[ZoneState, frozenset] = {
    ZoneState.NORMAL:     frozenset({ZoneState.ELEVATED, ZoneState.EMERGENCY,
                                     ZoneState.EVACUATING}),
    ZoneState.ELEVATED:   frozenset({ZoneState.NORMAL,   ZoneState.SURGE,
                                     ZoneState.EMERGENCY, ZoneState.EVACUATING}),
    ZoneState.SURGE:      frozenset({ZoneState.ELEVATED, ZoneState.NORMAL,
                                     ZoneState.EMERGENCY}),
    ZoneState.EMERGENCY:  frozenset({ZoneState.SURGE,    ZoneState.ELEVATED,
                                     ZoneState.NORMAL}),
    ZoneState.EVACUATING: frozenset({ZoneState.NORMAL,   ZoneState.SURGE,
                                     ZoneState.ELEVATED}),
}

_STATE_SEVERITY: dict[ZoneState, int] = {
    ZoneState.NORMAL:     0,
    ZoneState.ELEVATED:   3,
    ZoneState.SURGE:      6,
    ZoneState.EMERGENCY:  8,
    ZoneState.EVACUATING: 6,
}


class ZoneAnomalyFSM:
    """
    Finite state machine that tracks anomaly state for one aviation zone.

    Design principles:
    - Escalation (higher severity) requires two consecutive readings to confirm,
      preventing single-scan false positives from transient ADS-B noise.
    - De-escalation is immediate — safety-first: always assume the worst
      until evidence shows otherwise.
    - Invalid transitions (not in _ZONE_TRANSITIONS) are silently ignored so
      external callers cannot corrupt the machine.

    Typical call pattern (once per polling cycle):
        fsm = ZoneAnomalyFSM("levant_corridor", baseline_military=3)
        state = fsm.evaluate(military_count=8, has_emergency_squawk=False,
                             evac_count=0, has_isr=True, has_vip=False)
        print(fsm.state, fsm.severity)
    """

    def __init__(self, zone_name: str, baseline_military: int):
        self.zone_name  = zone_name
        self.baseline   = max(1, baseline_military)
        self.state      = ZoneState.NORMAL
        self._pending:  Optional[ZoneState] = None   # candidate awaiting confirmation

    # ── Core FSM mechanics ────────────────────────────────────────────────────

    def transition(self, target: ZoneState) -> bool:
        """
        Attempt an explicit state transition.
        Returns True if the transition occurred, False if invalid or no-op.
        """
        if target == self.state:
            self._pending = None
            return False
        if target not in _ZONE_TRANSITIONS[self.state]:
            return False
        self.state    = target
        self._pending = None
        return True

    # ── High-level evaluation ────────────────────────────────────────────────

    def evaluate(
        self,
        military_count:       int,
        has_emergency_squawk: bool,
        evac_count:           int,
        has_isr:              bool,
        has_vip:              bool,
    ) -> ZoneState:
        """
        Compute the next state from zone aircraft metrics and apply it.

        Returns the (possibly updated) current state.
        """
        # Derive target state from inputs — priority order matters
        if has_emergency_squawk:
            target = ZoneState.EMERGENCY
        elif evac_count >= _EVAC_MASS_THRESHOLD:
            target = ZoneState.EVACUATING
        elif military_count >= self.baseline * _MILITARY_SURGE_MULTIPLIER:
            # SURGE is only reachable from ELEVATED (not from NORMAL).
            # If the current state cannot reach SURGE directly, clamp to
            # ELEVATED — the next valid step on the escalation path.
            if ZoneState.SURGE in _ZONE_TRANSITIONS[self.state]:
                target = ZoneState.SURGE
            else:
                target = ZoneState.ELEVATED
        elif (military_count >= max(1, self.baseline * 1.5)
              or has_isr or has_vip):
            target = ZoneState.ELEVATED
        else:
            target = ZoneState.NORMAL

        is_escalation = (
            _STATE_SEVERITY[target] > _STATE_SEVERITY[self.state]
        )

        if not is_escalation:
            # De-escalation or lateral move — apply immediately (safety-first)
            self.transition(target)
        elif target in (ZoneState.EMERGENCY, ZoneState.EVACUATING):
            # Binary detections: 77xx squawk or mass EVAC callsigns are already
            # self-confirming events — no second reading needed.
            self.transition(target)
        elif self._pending == target:
            # Second consecutive reading confirming a count-based escalation
            self.transition(target)
        else:
            # First reading of potential escalation — hold, wait for confirmation
            self._pending = target

        return self.state

    @property
    def severity(self) -> int:
        """Current state severity (0–8)."""
        return _STATE_SEVERITY[self.state]

    def reset(self) -> None:
        """Return to NORMAL regardless of current state (used on stale data)."""
        self.state    = ZoneState.NORMAL
        self._pending = None


# Module-level FSM registry: persists state across polling cycles.
# Keyed by zone name; initialised lazily on first call to detect_aviation_anomalies.
_zone_fsms: dict[str, ZoneAnomalyFSM] = {}


def _parse_state(raw: list) -> dict:
    """Convert a raw OpenSky state vector list to a named dict."""
    if not raw or len(raw) < 14:
        return {}
    return {
        "icao24":         raw[0],
        "callsign":       (raw[1] or "").strip(),
        "origin_country": raw[2] or "",
        "longitude":      raw[5],
        "latitude":       raw[6],
        "baro_altitude":  raw[7],
        "on_ground":      raw[8],
        "velocity":       raw[9],
        "squawk":         raw[14] or "",
        "category":       raw[17] if len(raw) > 17 else 0,
    }


def _classify_callsign(callsign: str) -> Optional[tuple[str, str]]:
    """
    Return (country, mission_type) if callsign matches a known military prefix,
    else None.
    """
    cs = callsign.upper().strip()
    for prefix, info in _MILITARY_CALLSIGN_MAP.items():
        if cs.startswith(prefix):
            return info
    return None


def fetch_aircraft_states(zones: Optional[dict] = None) -> list[dict]:
    """
    Query OpenSky for aircraft states in each monitored conflict zone.

    Returns a flat list of enriched state dicts, each with an added
    'zone' key indicating which conflict zone it was found in.

    Returns [] if:
      - requests not installed (already in requirements.txt)
      - all zone queries fail (e.g. rate limited)
      - network unavailable
    """
    if zones is None:
        zones = AVIATION_ZONES

    auth = (OPENSKY_USERNAME, OPENSKY_PASSWORD) if OPENSKY_USERNAME else None
    all_states: list[dict] = []
    last_request = 0.0

    for zone_name, zone_cfg in zones.items():
        # Rate-limit: ensure gap between requests
        elapsed = time.monotonic() - last_request
        if elapsed < _MIN_REQUEST_GAP:
            time.sleep(_MIN_REQUEST_GAP - elapsed)

        lat_min, lon_min, lat_max, lon_max = zone_cfg["bbox"]
        params = {
            "lamin": lat_min, "lomin": lon_min,
            "lamax": lat_max, "lomax": lon_max,
        }
        try:
            resp = requests.get(
                _OPENSKY_BASE,
                params=params,
                auth=auth,
                timeout=_REQUEST_TIMEOUT,
            )
            last_request = time.monotonic()

            if resp.status_code == 429:
                logger.warning("OpenSky: rate limited (429) — stopping zone queries")
                break
            if resp.status_code == 401:
                logger.warning("OpenSky: auth failed (401) — check OPENSKY_USERNAME/PASSWORD")
                # Fall back to anonymous (auth=None) on next iteration
                auth = None
                continue
            if not resp.ok:
                logger.debug(f"OpenSky zone={zone_name}: HTTP {resp.status_code}")
                continue

            data   = resp.json()
            states = data.get("states") or []
            for raw in states:
                parsed = _parse_state(raw)
                if parsed:
                    parsed["zone"] = zone_name
                    parsed["zone_description"] = zone_cfg["description"]
                    parsed["zone_domains"]      = zone_cfg["domains"]
                    all_states.append(parsed)

            logger.debug(f"OpenSky zone={zone_name}: {len(states)} aircraft")

        except requests.exceptions.RequestException as exc:
            logger.debug(f"OpenSky zone={zone_name} request failed: {exc}")
        except Exception as exc:
            logger.debug(f"OpenSky zone={zone_name} unexpected error: {exc}")

    logger.info(f"OpenSky: {len(all_states)} aircraft across {len(zones)} zones")
    return all_states


def _classify_zone_aircraft(zone_states: list[dict]) -> dict:
    """
    Classify all aircraft in a zone scan into mission buckets.
    Returns counts and callsign lists needed by the FSM.
    """
    military:  list[dict] = []
    vip:       list[dict] = []
    evac:      list[dict] = []
    isr:       list[dict] = []
    emergency: list[dict] = []

    for ac in zone_states:
        cs     = ac.get("callsign", "")
        squawk = ac.get("squawk", "")

        if squawk in _EMERGENCY_SQUAWKS:
            emergency.append(ac)

        cls = _classify_callsign(cs)
        if cls:
            _, mission = cls
            if mission == "vip":
                vip.append(ac)
            elif mission == "evacuation":
                evac.append(ac)
            elif mission == "isr":
                isr.append(ac)
            elif mission in ("military", "airlift", "tanker",
                             "tactical", "awacs", "medevac", "exercise"):
                military.append(ac)

    return {
        "military":  military,
        "vip":       vip,
        "evac":      evac,
        "isr":       isr,
        "emergency": emergency,
    }


def detect_aviation_anomalies(states: list[dict]) -> list[dict]:
    """
    Analyse aircraft states for anomalies that correlate with geopolitical events.

    Uses the per-zone ZoneAnomalyFSM (module-level registry) to debounce
    transient ADS-B noise. Escalation requires two consecutive elevated
    readings; de-escalation is immediate.

    Returns list of anomaly dicts, each with:
      zone, anomaly_type, severity_int (1-9), domains, description,
      aircraft_count, baseline, details (callsigns/squawks)
    """
    if not states:
        return []

    # Group aircraft by zone
    by_zone: dict[str, list[dict]] = {}
    for s in states:
        by_zone.setdefault(s.get("zone", "unknown"), []).append(s)

    anomalies: list[dict] = []

    for zone_name, zone_states in by_zone.items():
        zone_cfg    = AVIATION_ZONES.get(zone_name, {})
        baseline    = zone_cfg.get("baseline_military", 5)
        domains     = zone_cfg.get("domains", ["commodities"])
        desc_prefix = zone_cfg.get("description", zone_name)

        # Lazy-init FSM for this zone
        if zone_name not in _zone_fsms:
            _zone_fsms[zone_name] = ZoneAnomalyFSM(zone_name, baseline)

        fsm     = _zone_fsms[zone_name]
        buckets = _classify_zone_aircraft(zone_states)

        mil_count = len(buckets["military"])
        new_state = fsm.evaluate(
            military_count       = mil_count,
            has_emergency_squawk = len(buckets["emergency"]) > 0,
            evac_count           = len(buckets["evac"]),
            has_isr              = len(buckets["isr"])  > 0,
            has_vip              = len(buckets["vip"])  > 0,
        )

        if new_state == ZoneState.NORMAL:
            continue   # nothing to report

        # Map FSM state → anomaly_type and description
        if new_state == ZoneState.EMERGENCY:
            squawks = [a["squawk"] for a in buckets["emergency"]]
            anomalies.append({
                "zone":           zone_name,
                "anomaly_type":   "emergency_squawk",
                "severity_int":   fsm.severity,
                "domains":        domains,
                "aircraft_count": len(buckets["emergency"]),
                "baseline":       0,
                "details":        squawks,
                "description": (
                    f"{desc_prefix}: {len(buckets['emergency'])} aircraft squawking "
                    f"emergency codes {set(squawks)}"
                ),
            })

        elif new_state == ZoneState.EVACUATING:
            anomalies.append({
                "zone":           zone_name,
                "anomaly_type":   "mass_evacuation",
                "severity_int":   fsm.severity,
                "domains":        domains,
                "aircraft_count": len(buckets["evac"]),
                "baseline":       0,
                "details":        [a["callsign"] for a in buckets["evac"]],
                "description": (
                    f"{desc_prefix}: {len(buckets['evac'])} EVAC callsigns — "
                    f"mass evacuation underway, imminent escalation likely"
                ),
            })

        elif new_state == ZoneState.SURGE:
            anomalies.append({
                "zone":           zone_name,
                "anomaly_type":   "military_surge",
                "severity_int":   fsm.severity,
                "domains":        domains,
                "aircraft_count": mil_count,
                "baseline":       baseline,
                "details":        [a["callsign"] for a in buckets["military"][:10]],
                "description": (
                    f"{desc_prefix}: {mil_count} military aircraft "
                    f"(baseline {baseline}, ratio {mil_count/baseline:.1f}×) — "
                    f"confirmed pre-conflict build-up"
                ),
            })

        elif new_state == ZoneState.ELEVATED:
            # Could be VIP, ISR, mild military uptick, or any combination
            anom_type = "isr_activity" if buckets["isr"] else (
                "vip_movement" if buckets["vip"] else "military_surge"
            )
            details = (
                [a["callsign"] for a in buckets["isr"]]  or
                [a["callsign"] for a in buckets["vip"]]  or
                [a["callsign"] for a in buckets["military"][:5]]
            )
            anomalies.append({
                "zone":           zone_name,
                "anomaly_type":   anom_type,
                "severity_int":   fsm.severity,
                "domains":        domains,
                "aircraft_count": len(zone_states),
                "baseline":       baseline,
                "details":        details,
                "description": (
                    f"{desc_prefix}: elevated activity ({anom_type.replace('_',' ')}) — "
                    f"mil={mil_count} isr={len(buckets['isr'])} vip={len(buckets['vip'])}"
                ),
            })

    if anomalies:
        logger.info(f"OpenSky: {len(anomalies)} aviation anomalies "
                    f"({', '.join(a['anomaly_type'] for a in anomalies)})")
    return anomalies


def score_aviation(states: list[dict]) -> float:
    """
    Convert aircraft states to a 0-100 war-premium contribution score.

    Severity weighting per anomaly type:
        military_surge   → severity × 6  (primary early-warning signal)
        emergency_squawk → severity × 5
        mass_evacuation  → severity × 5
        isr_activity     → severity × 3  (intelligence ops, not kinetic yet)
        vip_movement     → severity × 2  (ambiguous — could be diplomatic)
    """
    anomalies = detect_aviation_anomalies(states)
    if not anomalies:
        return 0.0

    _WEIGHTS = {
        "military_surge":   6,
        "emergency_squawk": 5,
        "mass_evacuation":  5,
        "isr_activity":     3,
        "vip_movement":     2,
    }
    score = 0.0
    for a in anomalies:
        w = _WEIGHTS.get(a["anomaly_type"], 2)
        score += a.get("severity_int", 1) * w

    return round(min(100.0, score), 1)
