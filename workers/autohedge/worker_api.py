"""
workers/autohedge/worker_api.py

AutoHedge Worker — AI Advisory Pipeline.
FastAPI service running on port 8003.

What this does:
    Wraps AutoHedge's Director → Quant → Risk agent pipeline.
    Accepts macro snapshots and regime signals from the Hypervisor.
    Returns advisory signals: confidence score, suggested action, rationale.
    NEVER executes orders. advisory_only = True in settings.yaml.

Agents used (Execution agent stripped — Solana only, unusable here):
    Director  — generates trading thesis from regime + macro context
    Quant     — validates thesis with technical analysis
    Risk      — assesses position sizing and drawdown risk

LLM backend:
    Uses Ollama (phi3:mini) via ollama_patch.py — see that file for details.
    Falls back to direct litellm call if autohedge agent init fails.

Regime framing:
    Each regime gets a tailored system prompt so the agents reason in context.
    WAR_PREMIUM  → defense momentum, commodity hedges, safe havens
    CRISIS_ACUTE → capital preservation, what to close, hedge sizing
    BEAR_RECESSION → short opportunities, defensive rotation
    BULL_FROTHY  → risk management in overextended conditions
    BULL_CALM    → balanced optimization

REST contract (MARA standard):
    GET  /health    liveness + agent status
    GET  /status    current regime, last signal, agent state
    GET  /metrics   Prometheus text format
    POST /signal    run advisory pipeline, return recommendations
    POST /execute   always returns advisory_only — never executes
    POST /pause     halt analysis cycles
    POST /resume    re-enable analysis
    POST /regime    update regime framing for agent prompts
"""

from __future__ import annotations

import asyncio
import os
import time
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import structlog
from fastapi import FastAPI

# ── Apply Ollama patch BEFORE importing autohedge ────────────────────────────
from ollama_patch import call_ollama, get_ollama_model, OLLAMA_MODEL, OLLAMA_HOST

logger = structlog.get_logger(__name__)

WORKER_NAME = "autohedge"

# ── Regime-specific system prompts ───────────────────────────────────────────
# These frame the agents' analysis in the current macro context.
# Deliberately concise — phi3:mini has limited context window.

REGIME_PROMPTS: Dict[str, str] = {
    "WAR_PREMIUM": (
        "You are a risk-first portfolio advisor. Current regime: WAR_PREMIUM. "
        "Geopolitical conflict is elevated. Defense ETFs (NATO, SHLD, PPA, ITA) "
        "and gold are outperforming. Focus on: (1) defense sector momentum, "
        "(2) commodity hedges, (3) safe haven positioning. "
        "Be conservative with directional crypto exposure."
    ),
    "CRISIS_ACUTE": (
        "You are a risk-first portfolio advisor. Current regime: CRISIS_ACUTE. "
        "Market panic. VIX is spiking. Capital preservation is the only goal. "
        "Focus on: (1) what positions should be closed or hedged immediately, "
        "(2) minimum safe cash buffer, (3) delta-neutral strategies only. "
        "Do NOT recommend new long positions."
    ),
    "BEAR_RECESSION": (
        "You are a risk-first portfolio advisor. Current regime: BEAR_RECESSION. "
        "Sustained downturn. Yield curve inverted. BDI declining. "
        "Focus on: (1) short opportunities in overvalued sectors, "
        "(2) defensive rotation (bonds, utilities, gold), "
        "(3) risk sizing for extended drawdown conditions."
    ),
    "BULL_FROTHY": (
        "You are a risk-first portfolio advisor. Current regime: BULL_FROTHY. "
        "Euphoric bull market. VIX is low, crypto funding rates elevated. "
        "Focus on: (1) risk management in overextended conditions, "
        "(2) when to reduce exposure, (3) momentum strategies with tight stops. "
        "Warn about crowded trades."
    ),
    "REGIME_CHANGE": (
        "You are a risk-first portfolio advisor. Current regime: REGIME_CHANGE. "
        "Markets in transition. BDI is diverging sharply. Direction unclear. "
        "Focus on: (1) delta-neutral strategies, (2) reducing directional exposure, "
        "(3) monitoring signals for the next regime. Caution above all."
    ),
    "SHADOW_DRIFT": (
        "You are a risk-first portfolio advisor. Current regime: SHADOW_DRIFT. "
        "Hidden pressure building. BDI moving abnormally while VIX appears calm. "
        "Focus on: (1) identifying where the hidden stress is building, "
        "(2) early defensive positioning, (3) monitoring shipping and commodities."
    ),
    "BULL_CALM": (
        "You are a risk-first portfolio advisor. Current regime: BULL_CALM. "
        "Stable conditions. No stress signals. "
        "Focus on: (1) balanced portfolio optimization, "
        "(2) capturing systematic alpha across asset classes, "
        "(3) maintaining appropriate diversification."
    ),
}


class AdvisorState:
    """Holds all mutable state for the advisory pipeline."""

    def __init__(self):
        self.current_regime:     str             = "BULL_CALM"
        self.paused:             bool            = False
        self.agents_ready:       bool            = False
        self.agent_init_error:   Optional[str]   = None
        self.last_signal:        Optional[dict]  = None
        self.last_signal_time:   float           = 0.0
        self.signals_generated:  int             = 0
        self.start_time:         float           = time.time()

        # AutoHedge agent pipeline (may be None if init fails — fallback used)
        self._director = None
        self._quant    = None
        self._risk     = None

    def uptime_seconds(self) -> float:
        return time.time() - self.start_time

    def is_healthy(self) -> bool:
        if self.paused:
            return True
        # Healthy if either the full agent pipeline OR the fallback is available
        return self.agents_ready or (OLLAMA_HOST is not None)

    # ── Agent Initialisation ──────────────────────────────────────────────────

    def init_agents(self):
        """
        Attempt to initialise AutoHedge's Director, Quant, and Risk agents.

        AutoHedge uses swarms under the hood. If it fails (version mismatch,
        dependency issue), we set agents_ready=False and fall back to direct
        Ollama calls in run_pipeline(). The REST contract is fully preserved
        either way — the difference is only in how the LLM is called.
        """
        try:
            # Import after ollama_patch has redirected the LLM calls
            from autohedge import DirectorAgent, QuantAgent, RiskAgent  # type: ignore

            model = get_ollama_model()

            self._director = DirectorAgent(model=model)
            self._quant    = QuantAgent(model=model)
            self._risk     = RiskAgent(model=model)

            self.agents_ready = True
            logger.info("autohedge_agents_ready", model=model, backend=OLLAMA_HOST)

        except ImportError as exc:
            self.agent_init_error = f"autohedge not installed: {exc}"
            logger.warning("autohedge_import_failed", error=str(exc),
                           fallback="direct Ollama via litellm")

        except Exception as exc:
            self.agent_init_error = f"agent init failed: {exc}"
            logger.warning("autohedge_agent_init_failed", error=str(exc),
                           fallback="direct Ollama via litellm")

    # ── Advisory Pipeline ─────────────────────────────────────────────────────

    async def run_pipeline(
        self,
        ticker:   str,
        context:  str,
        regime:   str,
    ) -> Dict[str, Any]:
        """
        Run the Director → Quant → Risk advisory pipeline.

        If autohedge agents are available, uses the full agent chain.
        Otherwise falls back to a single direct Ollama call that covers
        the same ground in one prompt.

        Returns a structured advisory dict.
        """
        system_prompt = REGIME_PROMPTS.get(regime, REGIME_PROMPTS["BULL_CALM"])
        start = time.time()

        if self.agents_ready:
            result = await asyncio.get_event_loop().run_in_executor(
                None, self._run_agent_chain, ticker, context, system_prompt
            )
        else:
            result = await asyncio.get_event_loop().run_in_executor(
                None, self._run_ollama_fallback, ticker, context, system_prompt
            )

        latency_ms = int((time.time() - start) * 1000)
        result["latency_ms"]    = latency_ms
        result["regime"]        = regime
        result["ticker"]        = ticker
        result["advisory_only"] = True
        result["model"]         = OLLAMA_MODEL

        self.last_signal      = result
        self.last_signal_time = time.time()
        self.signals_generated += 1

        return result

    def _run_agent_chain(
        self, ticker: str, context: str, system_prompt: str
    ) -> Dict[str, Any]:
        """Full AutoHedge agent pipeline: Director → Quant → Risk."""
        try:
            thesis = self._director.run(
                f"Regime context: {system_prompt}\n\n"
                f"Market data: {context}\n\n"
                f"Asset: {ticker}. Generate a trading thesis."
            )
            quant_analysis = self._quant.run(
                f"Thesis: {thesis}\n\nValidate technically. Is the thesis supported?"
            )
            risk_assessment = self._risk.run(
                f"Thesis: {thesis}\nQuant: {quant_analysis}\n\n"
                f"What position size and risk level is appropriate? "
                f"Return: action (long/short/neutral/hedge), "
                f"confidence (0.0-1.0), suggested_size_pct (0.0-1.0), rationale."
            )
            return {
                "source":          "autohedge_agents",
                "thesis":          str(thesis)[:400],
                "quant_analysis":  str(quant_analysis)[:400],
                "risk_assessment": str(risk_assessment)[:400],
                **self._parse_risk_output(str(risk_assessment)),
            }
        except Exception as exc:
            logger.error("agent_chain_failed", error=str(exc))
            return self._run_ollama_fallback(
                ticker, context, system_prompt, error=str(exc)
            )

    def _run_ollama_fallback(
        self,
        ticker:       str,
        context:      str,
        system_prompt: str,
        error:        str = "",
    ) -> Dict[str, Any]:
        """
        Single-prompt Ollama call covering Director+Quant+Risk in one shot.
        Used when autohedge agents are unavailable or fail.
        phi3:mini is small — keep prompts tight and ask for structured output.
        """
        prompt = (
            f"{system_prompt}\n\n"
            f"Asset: {ticker}\n"
            f"Market context: {context}\n\n"
            "Respond with exactly 4 lines:\n"
            "ACTION: (long | short | neutral | hedge)\n"
            "CONFIDENCE: (0.0 to 1.0)\n"
            "SIZE_PCT: (0.0 to 1.0, fraction of allocated capital)\n"
            "RATIONALE: (one sentence)\n"
        )
        raw = call_ollama(prompt)
        parsed = self._parse_structured_response(raw)
        return {
            "source":    "ollama_fallback",
            "raw":       raw[:500],
            "init_error": error,
            **parsed,
        }

    @staticmethod
    def _parse_risk_output(text: str) -> Dict[str, Any]:
        """Best-effort parse of Risk agent freeform output."""
        result = {
            "action":             "neutral",
            "confidence":         0.5,
            "suggested_size_pct": 0.05,
            "rationale":          text[:200],
        }
        text_lower = text.lower()
        for action in ("long", "short", "hedge", "neutral"):
            if action in text_lower:
                result["action"] = action
                break
        import re
        conf_match = re.search(r"confidence[:\s]*([\d.]+)", text_lower)
        if conf_match:
            try:
                result["confidence"] = min(1.0, float(conf_match.group(1)))
            except ValueError:
                pass
        size_match = re.search(r"size[_\s]pct[:\s]*([\d.]+)", text_lower)
        if size_match:
            try:
                result["suggested_size_pct"] = min(1.0, float(size_match.group(1)))
            except ValueError:
                pass
        return result

    @staticmethod
    def _parse_structured_response(text: str) -> Dict[str, Any]:
        """Parse the 4-line structured response from the fallback prompt."""
        result = {
            "action":             "neutral",
            "confidence":         0.5,
            "suggested_size_pct": 0.05,
            "rationale":          "No rationale extracted.",
        }
        for line in text.splitlines():
            line = line.strip()
            if line.upper().startswith("ACTION:"):
                val = line.split(":", 1)[-1].strip().lower()
                if val in ("long", "short", "hedge", "neutral"):
                    result["action"] = val
            elif line.upper().startswith("CONFIDENCE:"):
                try:
                    result["confidence"] = min(1.0, float(line.split(":", 1)[-1].strip()))
                except ValueError:
                    pass
            elif line.upper().startswith("SIZE_PCT:"):
                try:
                    result["suggested_size_pct"] = min(1.0, float(line.split(":", 1)[-1].strip()))
                except ValueError:
                    pass
            elif line.upper().startswith("RATIONALE:"):
                result["rationale"] = line.split(":", 1)[-1].strip()[:300]
        return result


# ── Application State ─────────────────────────────────────────────────────────

state = AdvisorState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise agents at startup. Graceful on failure."""
    logger.info("autohedge_worker_starting", ollama=OLLAMA_HOST, model=OLLAMA_MODEL)
    await asyncio.get_event_loop().run_in_executor(None, state.init_agents)
    if state.agents_ready:
        logger.info("autohedge_pipeline_ready", source="autohedge_agents")
    else:
        logger.warning(
            "autohedge_fallback_active",
            reason=state.agent_init_error,
            fallback="direct litellm→Ollama",
        )
    yield
    logger.info("autohedge_worker_shutdown")


app = FastAPI(lifespan=lifespan)


# ── REST Contract ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status":       "ok" if state.is_healthy() else "degraded",
        "worker":       WORKER_NAME,
        "paused":       state.paused,
        "regime":       state.current_regime,
        "agents_ready": state.agents_ready,
        "backend":      "autohedge_agents" if state.agents_ready else "ollama_fallback",
        "model":        OLLAMA_MODEL,
        "init_error":   state.agent_init_error,
    }


@app.get("/status")
def status():
    return {
        "worker":            WORKER_NAME,
        "regime":            state.current_regime,
        "paused":            state.paused,
        "agents_ready":      state.agents_ready,
        "signals_generated": state.signals_generated,
        "last_signal_age_s": round(time.time() - state.last_signal_time, 1)
                             if state.last_signal_time else None,
        "last_signal":       state.last_signal,
        "uptime_s":          round(state.uptime_seconds(), 1),
        "advisory_only":     True,
        # Required by hypervisor for risk/sharpe tracking
        "pnl":               0.0,
        "sharpe":            0.0,
        "allocated_usd":     getattr(state, "allocated_usd", 0.0),
        "open_positions":    0,
    }


@app.post("/signal")
async def signal(body: dict):
    """
    Run the advisory pipeline and return a signal to the Hypervisor.

    Expected body:
        {
          "regime":   "WAR_PREMIUM",
          "snapshot": { "vix": 29.5, "gold_oil_ratio": 56.8, ... },
          "tickers":  ["BTC/USDT", "ETH/USDT"]   (optional)
        }

    Returns a list of advisory signals (one per ticker, or a portfolio-level one).
    """
    if state.paused:
        return []

    regime   = body.get("regime", state.current_regime)
    snapshot = body.get("snapshot", {})
    tickers  = body.get("tickers", ["PORTFOLIO"])

    context = (
        f"VIX={snapshot.get('vix', '?')} | "
        f"Gold/Oil={snapshot.get('gold_oil_ratio', '?')} | "
        f"BDI slope={snapshot.get('bdi_slope_12w', '?')} | "
        f"Yield curve={snapshot.get('yield_curve', '?')} | "
        f"Defense momentum={snapshot.get('defense_momentum_20d', '?')} | "
        f"BTC funding={snapshot.get('btc_funding_rate', '?')}"
    )

    signals = []
    for ticker in tickers[:3]:   # Cap at 3 — phi3:mini is slow
        advisory = await state.run_pipeline(ticker, context, regime)
        signals.append({
            "worker":             WORKER_NAME,
            "symbol":             ticker,
            "direction":          advisory.get("action", "neutral"),
            "confidence":         advisory.get("confidence", 0.5),
            "suggested_size_pct": advisory.get("suggested_size_pct", 0.05),
            "regime_tags":        [regime],
            "ttl_seconds":        300,
            "advisory_only":      True,
            "rationale":          advisory.get("rationale", ""),
            "latency_ms":         advisory.get("latency_ms", 0),
            "source":             advisory.get("source", "unknown"),
        })

    logger.info("signals_generated", count=len(signals), regime=regime)
    return signals


@app.post("/execute")
def execute(body: dict):
    """
    Advisory worker never executes. Always returns advisory_only status.
    The Hypervisor knows this from settings.yaml advisory_only: true.
    """
    return {
        "status":        "advisory_only",
        "worker":        WORKER_NAME,
        "message":       "AutoHedge is advisory-only. Use Nautilus or Arbitrader for execution.",
        "last_advisory": state.last_signal,
    }


@app.post("/regime")
async def update_regime(body: dict):
    """Receive regime change from Hypervisor. Updates system prompt framing."""
    new_regime = body.get("regime")
    if not new_regime or new_regime == state.current_regime:
        return {"status": "no_change"}

    old_regime = state.current_regime
    state.current_regime = new_regime

    logger.info("regime_updated", old=old_regime, new=new_regime,
                prompt_framing=REGIME_PROMPTS.get(new_regime, "")[:80])

    return {
        "status":        "updated",
        "regime":        new_regime,
        "prompt_framing": REGIME_PROMPTS.get(new_regime, "")[:100],
    }


@app.post("/allocate")
async def allocate(body: dict):
    """Receive capital allocation from Hypervisor."""
    amount = float(body.get("amount_usd", 0.0))
    state.allocated_usd = amount
    return {"status": "ok", "worker": "autohedge", "allocated_usd": amount}


@app.post("/pause")
def pause():
    state.paused = True
    logger.info("autohedge_paused")
    return {"status": "paused"}


@app.post("/resume")
def resume():
    state.paused = False
    logger.info("autohedge_resumed")
    return {"status": "resumed"}


@app.get("/metrics")
def metrics():
    from fastapi.responses import Response as _Response
    active     = 0 if state.paused else 1
    paused_int = 1 if state.paused else 0
    backend    = 1.0 if state.agents_ready else 0.0
    alloc      = getattr(state, "allocated_usd", 0.0)

    lines = [
        # ── Standard labeled gauges ──────────────────────────────────────────
        # AutoHedge is advisory-only: pnl always 0, no positions
        f'mara_worker_pnl_usd{{worker="autohedge"}} 0.0',
        f'mara_worker_allocated_usd{{worker="autohedge"}} {alloc:.2f}',
        f'mara_worker_sharpe{{worker="autohedge"}} 0.0',
        f'mara_worker_open_positions{{worker="autohedge"}} 0',
        f'mara_worker_paused{{worker="autohedge"}} {paused_int}',
        # ── Legacy ───────────────────────────────────────────────────────────
        f'mara_worker_active{{worker="autohedge"}} {active}',
        f'mara_autohedge_agents_ready {backend}',
        f'mara_autohedge_signals_total {state.signals_generated}',
        f'mara_autohedge_uptime_seconds {state.uptime_seconds():.1f}',
    ]
    content = "\n".join(lines) + "\n"
    return _Response(content=content, media_type="text/plain")
