"""
workers/analyst/worker_api.py

MARA Analyst Worker — Regime-Aware Advisory via Ollama phi3:mini.

Calls Ollama directly over HTTP. No litellm, no swarms.
All outputs are advisory_only=True and never touch capital allocation.

REST contract (standard MARA worker interface):
  GET  /health    → status, ollama_reachable, last_thesis_age_seconds
  GET  /status    → pnl=0, sharpe=None, allocated_usd, last_thesis
  GET  /metrics   → Prometheus text
  POST /signal    → regime-aware thesis from phi3:mini (cached 5 min)
  POST /execute   → always advisory_only, never executes
  POST /allocate  → records allocation amount
  POST /pause     → halts signal generation
  POST /resume    → resumes signal generation
  POST /regime    → updates regime context
"""

import json
import logging
import math
import os
import time

import httpx
import structlog
from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import (
    CollectorRegistry,
    Gauge,
    Counter,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = structlog.get_logger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
OLLAMA_HOST  = os.getenv("OLLAMA_HOST",  "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "phi3:mini")

# ── Module-level state ─────────────────────────────────────────────────────────
_paused:                bool  = False
_allocated_usd:         float = 0.0
_paper_trading:         bool  = True
_current_regime:        str   = "BULL_CALM"
_regime_confidence:     float = 0.5
_pnl:                   float = 0.0
_last_thesis:           str   = ""
_last_thesis_timestamp: float = 0.0
_thesis_cache_seconds:  int   = 300    # only call Ollama every 5 minutes max
_call_count:            int   = 0
_ollama_reachable:      bool  = True

# ── Prometheus registry (isolated — avoids collision with default global) ───────
_registry = CollectorRegistry()

_g_pnl = Gauge(
    "mara_worker_pnl_usd", "Worker PnL in USD",
    ["worker"], registry=_registry,
)
_g_allocated = Gauge(
    "mara_worker_allocated_usd", "Worker allocated capital in USD",
    ["worker"], registry=_registry,
)
_g_sharpe = Gauge(
    "mara_worker_sharpe", "Worker Sharpe ratio",
    ["worker"], registry=_registry,
)
_g_positions = Gauge(
    "mara_worker_open_positions", "Worker open positions",
    ["worker"], registry=_registry,
)
_g_paused = Gauge(
    "mara_worker_paused", "Worker paused flag",
    ["worker"], registry=_registry,
)
_g_active = Gauge(
    "mara_worker_active", "1 if worker is running and unpaused",
    ["worker"], registry=_registry,
)
_g_ollama_reachable = Gauge(
    "mara_analyst_ollama_reachable", "1 if Ollama reachable",
    registry=_registry,
)
_g_thesis_age = Gauge(
    "mara_analyst_thesis_age_seconds", "Seconds since last thesis was generated",
    registry=_registry,
)
_c_call_count = Counter(
    "mara_analyst_call_count_total", "Total Ollama calls made",
    registry=_registry,
)

# Initialise label sets so Prometheus sees them from the first scrape
_g_pnl.labels(worker="analyst")
_g_allocated.labels(worker="analyst")
_g_sharpe.labels(worker="analyst")
_g_positions.labels(worker="analyst")
_g_paused.labels(worker="analyst")
_g_active.labels(worker="analyst")


# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(title="MARA Analyst Worker", version="1.0.0")


# ── Prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(
    regime: str,
    confidence: float,
    war_premium_score: float,
    worker_states: dict,
    watchlist: list,
) -> str:
    worker_lines = []
    for name, ws in (worker_states or {}).items():
        sharpe_str = f"{ws.get('sharpe', 0.0):.2f}" if ws.get("sharpe") is not None else "N/A"
        worker_lines.append(
            f"  - {name}: pnl=${ws.get('pnl', 0.0):.2f}, "
            f"sharpe={sharpe_str}, allocated=${ws.get('allocated_usd', 0.0):.2f}"
        )
    workers_text = "\n".join(worker_lines) if worker_lines else "  (no worker data)"
    watchlist_text = ", ".join(watchlist) if watchlist else "empty"

    return (
        f"You are a regime-aware market analyst for MARA, an autonomous trading "
        f"system. You do not make trading decisions. You generate a concise thesis "
        f"that explains the current market state.\n\n"
        f"Current regime: {regime} (confidence: {confidence:.0%})\n"
        f"War premium score: {war_premium_score:.1f}/100\n\n"
        f"Worker performance this cycle:\n{workers_text}\n\n"
        f"Active watchlist: {watchlist_text}\n\n"
        f"Tasks:\n"
        f"1. In 2-3 sentences, explain what the current regime means for the portfolio.\n"
        f"2. Identify which worker is performing best and why this makes sense given "
        f"the regime.\n"
        f"3. Flag any watchlist ticker that may be relevant to the current regime score.\n\n"
        f"Return ONLY a JSON object with these exact keys, no other text:\n"
        f'{{"thesis": "2-3 sentence regime explanation",\n'
        f'  "top_worker": "worker_name or null",\n'
        f'  "flagged_tickers": ["TICKER", ...],\n'
        f'  "confidence_note": "one sentence on what could change the regime"}}'
    )


# ── Ollama call ────────────────────────────────────────────────────────────────

def _call_ollama(prompt: str, regime: str, confidence: float) -> dict:
    """
    POST to Ollama /api/generate. Returns parsed dict.
    On any failure returns a safe default without raising.
    """
    global _ollama_reachable, _call_count
    _call_count += 1
    _c_call_count.inc()

    try:
        resp = httpx.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=45.0,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
        parsed = json.loads(raw)
        _ollama_reachable = True
        return {
            "thesis":          parsed.get("thesis", ""),
            "top_worker":      parsed.get("top_worker"),
            "flagged_tickers": parsed.get("flagged_tickers", []),
            "confidence_note": parsed.get("confidence_note", ""),
        }
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as exc:
        logger.warning("analyst: Ollama unreachable", error=str(exc))
        _ollama_reachable = False
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("analyst: Ollama response parse failed", error=str(exc))
        _ollama_reachable = True   # reachable but returned bad JSON

    return {
        "thesis": f"[Ollama unavailable] Regime: {regime} at {confidence:.0%} confidence.",
        "top_worker":      None,
        "flagged_tickers": [],
        "confidence_note": "",
    }


# ── REST endpoints ─────────────────────────────────────────────────────────────

@app.post("/regime")
async def set_regime(body: dict):
    global _current_regime, _regime_confidence, _paper_trading
    _current_regime    = body.get("regime",       _current_regime)
    _regime_confidence = body.get("confidence",   _regime_confidence)
    _paper_trading     = body.get("paper_trading", _paper_trading)
    return {"status": "ok"}


@app.post("/signal")
async def signal(body: dict):
    global _last_thesis, _last_thesis_timestamp

    if _paused:
        return []

    regime            = body.get("regime",            _current_regime)
    confidence        = float(body.get("confidence",  _regime_confidence))
    war_premium_score = float(body.get("war_premium_score", 0.0))
    worker_states     = body.get("worker_states",     {})
    watchlist         = body.get("watchlist",         [])

    should_call = (time.time() - _last_thesis_timestamp) > _thesis_cache_seconds

    if should_call:
        prompt = _build_prompt(regime, confidence, war_premium_score, worker_states, watchlist)
        parsed = _call_ollama(prompt, regime, confidence)
        _last_thesis           = parsed["thesis"]
        _last_thesis_timestamp = time.time()
    else:
        # Return cached result — rebuild parsed from last thesis
        parsed = {
            "thesis":          _last_thesis,
            "top_worker":      None,
            "flagged_tickers": [],
            "confidence_note": "",
        }

    return [{
        "action":            "HOLD",
        "advisory_only":     True,
        "rationale":         _last_thesis,
        "top_worker":        parsed.get("top_worker"),
        "flagged_tickers":   parsed.get("flagged_tickers", []),
        "confidence_note":   parsed.get("confidence_note", ""),
        "ollama_reachable":  _ollama_reachable,
        "thesis_age_seconds": int(time.time() - _last_thesis_timestamp),
    }]


@app.post("/execute")
async def execute(body: dict):
    return {"status": "advisory_only", "message": "Analyst worker does not execute trades."}


@app.post("/allocate")
async def allocate(body: dict):
    global _allocated_usd, _paper_trading
    _allocated_usd = float(body.get("amount_usd", _allocated_usd))
    _paper_trading = bool(body.get("paper_trading", _paper_trading))
    return {"status": "ok", "allocated_usd": _allocated_usd}


@app.post("/pause")
async def pause():
    global _paused
    _paused = True
    return {"status": "paused"}


@app.post("/resume")
async def resume():
    global _paused
    _paused = False
    return {"status": "resumed"}


@app.get("/health")
async def health():
    return {
        "status":                 "ok",
        "paused":                 _paused,
        "ollama_reachable":       _ollama_reachable,
        "last_thesis_age_seconds": int(time.time() - _last_thesis_timestamp),
        "model":                  OLLAMA_MODEL,
    }


@app.get("/status")
async def status():
    return {
        "pnl":              0.0,
        "sharpe":           None,
        "allocated_usd":    _allocated_usd,
        "open_positions":   0,
        "last_thesis":      _last_thesis,
        "ollama_reachable": _ollama_reachable,
    }


@app.get("/metrics")
def metrics():
    _g_pnl.labels(worker="analyst").set(0.0)
    _g_allocated.labels(worker="analyst").set(_allocated_usd)
    _g_sharpe.labels(worker="analyst").set(float("nan"))
    _g_positions.labels(worker="analyst").set(0)
    _g_paused.labels(worker="analyst").set(1 if _paused else 0)
    _g_active.labels(worker="analyst").set(0 if _paused else 1)
    _g_ollama_reachable.set(1 if _ollama_reachable else 0)
    _g_thesis_age.set(time.time() - _last_thesis_timestamp)
    content = generate_latest(_registry).decode("utf-8")
    return Response(content=content, media_type=CONTENT_TYPE_LATEST)
