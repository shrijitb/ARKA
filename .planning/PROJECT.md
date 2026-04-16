# Arka — Stabilization & Hardening Milestone

## What This Is

Arka is a regime-aware multi-agent trading system currently in paper trading. This milestone is a pre-Phase-3 stabilization pass: a thorough audit of the entire Python trading core and hypervisor to surface hidden bugs, close test coverage gaps, harden safety rails, eliminate fragile patterns, and validate stability under sustained paper trading conditions. The goal is high confidence before real money is involved.

## Core Value

No silent failure reaches Phase 3 — every bug, race condition, and untested safety path is found and fixed before live trading begins.

## Requirements

### Validated

- ✓ Hypervisor FastAPI orchestrator with regime classification and capital allocation — existing
- ✓ 4-state HMM classifier (RISK_ON / RISK_OFF / CRISIS / TRANSITION) — existing
- ✓ 5-worker architecture (nautilus, prediction_markets, analyst, core_dividends, telegram_bot) — existing
- ✓ RiskManager with drawdown / VaR / cooldown / position caps — existing
- ✓ Safety rails: margin_reserve.py, expiry_guard.py, circuit_breaker.py — existing (untested)
- ✓ Audit logging (audit.py), DI container (di_container.py), auth (auth.py) — existing (untested)
- ✓ Data feeds: market_data.py, conflict_index.py, funding_rates.py, order_book.py — existing
- ✓ Test suite: 120+ passed / 7 skipped / 0 failed — existing baseline
- ✓ Arka Visual Dashboard (React 18 + Tailwind v4) — F-10 done

### Active

- [ ] **Code review pass** — Static analysis of all Python files in hypervisor/ and data/feeds/ for bugs, silent failures, and unsafe patterns
- [ ] **Safety rails test coverage** — tests/test_safety_rails.py and tests/test_concurrency.py exercised and gaps filled; margin_reserve, expiry_guard, and circuit_breaker all covered
- [ ] **Concurrency / race condition audit** — Hypervisor 60s cycle + APScheduler + async workers reviewed and tested for state conflicts
- [ ] **Silent data failure hardening** — yfinance/FRED bad-data paths (None, NaN, stale, empty) traced and guarded throughout the pipeline
- [ ] **New file integration** — audit.py, auth.py, di_container.py, errors.py, db/ wired in and tested; not dead code
- [ ] **Test suite expanded** — 150+ passing tests, all new safety/concurrency files covered
- [ ] **Paper trading stress test** — 24–48h paper trading run with log monitoring, no crashes or silent failures
- [ ] **Fragile pattern remediation** — Code smells and patterns identified by CONCERNS.md addressed

### Out of Scope

- `workers/stocksharp/` — Phase 3 only; do not touch until live trading wiring begins
- Dashboard UI (`dashboard/`) — F-10 is complete; no frontend changes in this milestone
- Live trading enablement — `PAPER_TRADING = True` stays until stress test passes
- New features — this milestone is hardening only, no net-new functionality

## Context

- **Current state:** Paper trading active, March 2026 classifier output RISK_ON 80% confidence
- **Stack:** Python 3.x, FastAPI, NautilusTrader, APScheduler, python-telegram-bot, yfinance, FRED, GDELT, ACLED
- **Runtime:** WSL2 Ubuntu-24.04 (dev), Raspberry Pi 5 ARM64 (prod target)
- **Test baseline:** 120+ passed / 7 skipped / 0 failed. 7 skips are expected (OKX live, ACLED free tier, IBKR Phase 3, Polymarket Phase 3)
- **Codebase map:** `.planning/codebase/` — 7 documents from April 2026 mapping
- **Known new/untested files:** `hypervisor/audit.py`, `hypervisor/auth.py`, `hypervisor/circuit_breaker.py`, `hypervisor/db/`, `hypervisor/di_container.py`, `hypervisor/errors.py`, `hypervisor/risk/expiry_guard.py`, `hypervisor/risk/margin_reserve.py`, `data/feeds/circuit_breaker.py`, `tests/test_safety_rails.py`, `tests/test_concurrency.py`
- **CONCERNS.md** surfaced specific fragile areas; roadmap should address each

## Constraints

- **Safety:** `PAPER_TRADING = True` and `USE_LIVE_RATES = False` must not be changed during this milestone
- **Stack:** No new dependencies without justification — keep requirements.txt lean for Pi 5 deployment
- **Secrets:** No credentials committed; `.env` stays gitignored
- **Test runner:** Always `~/.venv/bin/python -m pytest tests/ -v` — never bare `pytest`
- **Docker:** No `platform:` flags; build context for hypervisor must be project root `.`
- **Phase 3 boundary:** `workers/stocksharp/` is frozen; `RECESSION_PAIRS` signals stay `advisory_only=True`

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Thorough / fine-grained phases | Pre-live hardening requires high confidence, not speed | — Pending |
| Out-of-scope: dashboard + stocksharp | F-10 done; stocksharp is Phase 3 territory | — Pending |
| Done criteria: tests + stress test | Code passing tests alone is insufficient for trading systems | — Pending |
| Target: 150+ passing tests | Current 120+ baseline; new safety/concurrency files need coverage | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-04-15 after initialization*
