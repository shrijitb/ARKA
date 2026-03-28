"""
tests/test_integration_dryrun.py

MARA Integration Dry-Run Test Suite.

PURPOSE
-------
Smoke-tests the full MARA stack in-process. No Docker, no real money,
no real market data. Entire suite should complete in < 60s.

Questions answered:
  1. Do all workers respond to the MARA REST contract?
  2. Does the hypervisor correctly route regime/allocate/pause/resume?
  3. Does the risk manager block on every limit type?
  4. Does the capital allocator produce correct dollar splits per regime?
  5. Do worker /status responses contain every field the hypervisor reads?
  6. Does the classifier return a valid RegimeResult?

HOW TO RUN
----------
From ~/mara with venv active:
    pytest tests/test_integration_dryrun.py -v
    pytest tests/test_integration_dryrun.py -v --tb=short

FAILURE MODES
-------------
  SKIPPED  — optional dependency missing (e.g. nautilus_trader not installed).
             Suite still meaningful without it.
  FAILED   — module exists but import crashed, contract broken, or assertion
             failed. This is a real bug that must be fixed before paper trading.
  ERROR    — test itself crashed unexpectedly. File a bug against the test.

HOW TESTS ACTUALLY LOAD WORKERS
---------------------------------
Each worker FastAPI app is imported directly from its source file using
importlib, then wrapped in a FastAPI TestClient (httpx ASGI transport).
No network sockets. No uvicorn. No Docker.

If a worker file is MISSING  : test FAILS  — missing file is a build error.
If a worker file EXISTS but IMPORTS FAIL: test SKIPS with import error shown,
because the optional dependency (e.g. nautilus_trader) may not be installed
in the test environment.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

# ── Project root on sys.path ──────────────────────────────────────────────────
# File lives at ~/mara/tests/test_integration_dryrun.py → _PROJECT = ~/mara
_HERE    = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)


# ═════════════════════════════════════════════════════════════════════════════
# Module loading — loud on missing files, graceful on missing dependencies
# ═════════════════════════════════════════════════════════════════════════════

def _load_module(rel_path: str):
    """
    Load a Python module by path relative to ~/mara.

    Raises
    ------
    FileNotFoundError  — file missing → test should FAIL (build error).
    ImportError        — file exists, import dep missing → caller should pytest.skip().
    Exception          — any other crash → caller should pytest.fail().
    """
    abs_path = os.path.join(_PROJECT, rel_path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(
            f"File not found: {abs_path}\n"
            f"  This file must exist before running the dry-run suite.\n"
            f"  Expected at: ~/{os.path.relpath(abs_path, os.path.expanduser('~'))}"
        )
    spec = importlib.util.spec_from_file_location(
        rel_path.replace("/", ".").replace(".py", ""), abs_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_worker_app(worker_name: str, rel_path: str):
    """
    Load a worker's FastAPI app object.
    FAIL if the file is missing.
    SKIP if a dependency (nautilus_trader, etc.) is not installed.
    """
    try:
        mod = _load_module(rel_path)
    except FileNotFoundError as exc:
        pytest.fail(str(exc))
    except ImportError as exc:
        pytest.skip(f"{worker_name}: optional dependency not installed — {exc}")
    except Exception as exc:
        pytest.fail(f"{worker_name}: module import crashed — {type(exc).__name__}: {exc}")

    if not hasattr(mod, "app"):
        pytest.fail(
            f"{worker_name}: module loaded from {rel_path} "
            f"but has no 'app' attribute.\n"
            f"  Is this a FastAPI application?"
        )
    return mod.app


def _make_client(worker_name: str, rel_path: str):
    from fastapi.testclient import TestClient
    app = _load_worker_app(worker_name, rel_path)
    return TestClient(app, raise_server_exceptions=False)


# ── Worker source paths (relative to ~/mara) ─────────────────────────────────
WORKER_PATHS = {
    "nautilus":   "workers/nautilus/worker_api.py",
    "arbitrader": "workers/arbitrader/sidecar/main.py",
    "autohedge":  "workers/autohedge/worker_api.py",
    "polymarket": "workers/polymarket/adapter/main.py",
}

# Fields hypervisor reads in _pull_worker_status() — any missing silently → 0.0
REQUIRED_STATUS_FIELDS = {"pnl", "sharpe", "allocated_usd", "open_positions"}

# Fields a signal dict must contain for the hypervisor to use it
REQUIRED_SIGNAL_FIELDS = {"worker", "symbol", "direction", "confidence",
                           "regime_tags", "ttl_seconds"}


# ═════════════════════════════════════════════════════════════════════════════
# 1. Worker REST Contract
#    Parametrised over all 4 workers. Each test runs 4 times.
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(params=list(WORKER_PATHS.items()), ids=list(WORKER_PATHS.keys()))
def worker_client(request):
    """One TestClient per worker, built from the actual source file."""
    name, path = request.param
    return name, _make_client(name, path)


class TestWorkerContract:
    """
    Each worker must implement the full 8-endpoint MARA REST contract.

    FAILURE GUIDE
    ─────────────
    404 on /allocate          → add POST /allocate endpoint
    Missing /status fields    → add the field name to /status response dict
    422 Unprocessable Entity  → body schema mismatch on /allocate or /regime
    paused=True after resume  → worker forced paused by regime bias; check REGIME_BIAS
    mara_worker_active absent → add gauge to /metrics response
    """

    def test_health_returns_200_with_status_key(self, worker_client):
        name, c = worker_client
        resp = c.get("/health")
        assert resp.status_code == 200, \
            f"{name} GET /health → HTTP {resp.status_code} (expected 200)\n  body: {resp.text[:300]}"
        body = resp.json()
        assert "status" in body, (
            f"{name} /health response missing 'status' key\n"
            f"  got keys: {sorted(body.keys())}"
        )
        print(f"\n  [{name}] /health: status={body['status']!r}  all_keys={sorted(body.keys())}")

    def test_status_contains_all_hypervisor_required_fields(self, worker_client):
        name, c = worker_client
        resp = c.get("/status")
        assert resp.status_code == 200, \
            f"{name} GET /status → HTTP {resp.status_code}\n  body: {resp.text[:300]}"
        body = resp.json()
        missing = REQUIRED_STATUS_FIELDS - set(body.keys())
        assert not missing, (
            f"{name} /status missing fields that hypervisor reads: {sorted(missing)}\n"
            f"  present: {sorted(body.keys())}\n"
            f"  required: {sorted(REQUIRED_STATUS_FIELDS)}\n"
            f"  FIX: add these keys to the /status endpoint in {WORKER_PATHS[name]}"
        )
        values = {f: body[f] for f in REQUIRED_STATUS_FIELDS}
        print(f"\n  [{name}] /status required fields: {values}")

    def test_allocate_endpoint_exists_and_returns_200(self, worker_client):
        name, c = worker_client
        resp = c.post("/allocate", json={"amount_usd": 50.0, "paper_trading": True})
        assert resp.status_code == 200, (
            f"{name} POST /allocate → HTTP {resp.status_code}\n"
            f"  body: {resp.text[:400]}\n"
            f"  FIX: add 'POST /allocate' endpoint to {WORKER_PATHS[name]}\n"
            f"  Expected body: {{\"amount_usd\": float, \"paper_trading\": bool}}"
        )
        body = resp.json()
        assert "status" in body, \
            f"{name} /allocate must return a dict with 'status' key\n  got: {body}"
        print(f"\n  [{name}] /allocate response: {body}")

    def test_regime_broadcast_accepted_for_all_regimes(self, worker_client):
        name, c = worker_client
        regimes = ["WAR_PREMIUM", "CRISIS_ACUTE", "BULL_FROTHY", "BULL_CALM"]
        for regime in regimes:
            resp = c.post("/regime", json={
                "regime": regime, "confidence": 0.8, "paper_trading": True
            })
            assert resp.status_code == 200, (
                f"{name} POST /regime({regime}) → HTTP {resp.status_code}\n"
                f"  body: {resp.text[:200]}"
            )
        body = resp.json()
        print(f"\n  [{name}] /regime last response: {body}")

    def test_pause_sets_health_paused_true(self, worker_client):
        name, c = worker_client
        # Reset to known state first
        c.post("/regime", json={"regime": "BULL_CALM", "confidence": 1.0})
        c.post("/resume")
        resp = c.post("/pause")
        assert resp.status_code == 200, f"{name} /pause → HTTP {resp.status_code}"
        health = c.get("/health").json()
        paused = health.get("paused")
        assert paused is True, (
            f"{name} /health['paused'] should be True after POST /pause\n"
            f"  got paused={paused!r}\n"
            f"  full /health: {health}"
        )
        print(f"\n  [{name}] paused=True confirmed after /pause")

    def test_resume_clears_health_paused_flag(self, worker_client):
        name, c = worker_client
        c.post("/regime", json={"regime": "BULL_CALM", "confidence": 1.0})
        c.post("/pause")
        resp = c.post("/resume")
        assert resp.status_code == 200, f"{name} /resume → HTTP {resp.status_code}"
        health = c.get("/health").json()
        paused = health.get("paused")
        assert paused is False, (
            f"{name} /health['paused'] should be False after POST /resume\n"
            f"  got paused={paused!r}\n"
            f"  If worker stays paused: check that REGIME_BIAS['BULL_CALM'] != 'flat'\n"
            f"  full /health: {health}"
        )
        print(f"\n  [{name}] paused=False confirmed after /resume")

    def test_metrics_contains_mara_worker_active_gauge(self, worker_client):
        name, c = worker_client
        resp = c.get("/metrics")
        assert resp.status_code == 200, \
            f"{name} GET /metrics → HTTP {resp.status_code}"
        body = resp.text
        expected_gauge = f'mara_worker_active{{worker="{name}"}}'
        assert expected_gauge in body, (
            f"{name} /metrics missing required Prometheus gauge\n"
            f"  expected to find: {expected_gauge}\n"
            f"  got:\n    " + "\n    ".join(body.strip().splitlines()) + "\n"
            f"  FIX: add exactly this gauge to /metrics in {WORKER_PATHS[name]}"
        )
        print(f"\n  [{name}] gauge present: {expected_gauge}")


# ═════════════════════════════════════════════════════════════════════════════
# 2. Capital Allocator
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def allocator():
    mod = _load_module("hypervisor/allocator/capital.py")
    return mod.RegimeAllocator(total_capital=200.0)


ALL_REGIMES = [
    "WAR_PREMIUM", "CRISIS_ACUTE", "BEAR_RECESSION",
    "BULL_FROTHY", "REGIME_CHANGE", "SHADOW_DRIFT", "BULL_CALM",
]


class TestCapitalAllocator:
    """
    FAILURE GUIDE
    ─────────────
    allocated > 200          → MAX_DEPLOY_PCT > 1.0 or weights don't normalise to ≤1
    cash_reserve < 0         → same root cause
    orphan_in_capital        → capital.py REGIME_PROFILES has worker keys not in WORKER_REGISTRY
    nautilus gets allocation when unhealthy → _filter_healthy / registered_only check broken
    penalised >= baseline    → Sharpe penalty not applied, or weights re-normalised upward
    """

    @pytest.mark.parametrize("regime", ALL_REGIMES)
    def test_regime_allocations_within_capital(self, allocator, regime):
        result = allocator.compute(regime=regime)
        total  = sum(result.allocations.values())
        assert total <= 200.01, (
            f"[{regime}] allocated ${total:.4f} exceeds $200 total capital\n"
            f"  breakdown: {result.allocations}\n"
            f"  FIX: check MAX_DEPLOY_PCT and weight normalisation in capital.py"
        )
        assert result.cash_reserve >= -0.01, (
            f"[{regime}] cash_reserve=${result.cash_reserve:.4f} is negative\n"
            f"  breakdown: {result.allocations}"
        )
        breakdown = "  ".join(f"{k}:${v:.1f}" for k, v in result.allocations.items())
        print(f"\n  [{regime}] {breakdown}  cash=${result.cash_reserve:.1f}")

    def test_capital_keys_match_worker_registry(self, allocator):
        try:
            hyp_mod = _load_module("hypervisor/main.py")
            cap_mod = _load_module("hypervisor/allocator/capital.py")
        except ImportError as exc:
            pytest.skip(f"Module load skipped: {exc}")

        profile_keys  = set()
        for profile in cap_mod.REGIME_PROFILES.values():
            profile_keys.update(profile.keys())
        registry_keys = set(hyp_mod.WORKER_REGISTRY.keys())

        orphan_in_capital  = profile_keys - registry_keys
        orphan_in_registry = registry_keys - profile_keys

        assert not orphan_in_capital, (
            f"capital.py references workers not in WORKER_REGISTRY: {sorted(orphan_in_capital)}\n"
            f"  capital keys: {sorted(profile_keys)}\n"
            f"  registry keys: {sorted(registry_keys)}\n"
            f"  FIX: rename or remove orphan keys from REGIME_PROFILES in capital.py"
        )
        if orphan_in_registry:
            print(f"\n  WARNING: registry workers without capital profile: {sorted(orphan_in_registry)}")
        print(f"\n  Key alignment OK: capital={sorted(profile_keys)}  registry={sorted(registry_keys)}")

    def test_unhealthy_worker_receives_zero_allocation(self, allocator):
        result = allocator.compute(
            regime="BULL_CALM",
            worker_health={"nautilus": False, "arbitrader": True,
                           "polymarket": True,  "autohedge":  True},
        )
        alloc = result.allocations.get("nautilus", "KEY_ABSENT")
        assert "nautilus" not in result.allocations, (
            f"Unhealthy worker 'nautilus' got allocation=${alloc}\n"
            f"  full allocations: {result.allocations}\n"
            f"  FIX: ensure _filter_healthy / registered_only excludes health=False workers"
        )
        reason = (result.skipped_workers or {}).get("nautilus", "NOT_RECORDED")
        assert reason == "unhealthy", (
            f"skipped_workers['nautilus'] should be 'unhealthy', got: {reason!r}\n"
            f"  full skipped_workers: {result.skipped_workers}"
        )
        print(f"\n  nautilus excluded correctly. skipped={result.skipped_workers}")

    def test_low_sharpe_reduces_allocation(self, allocator):
        baseline  = allocator.compute(regime="BULL_CALM")
        penalised = allocator.compute(
            regime="BULL_CALM",
            worker_sharpe={"nautilus": 0.7},   # Below SHARPE_FULL_WEIGHT threshold
        )
        base_amt = baseline.allocations.get("nautilus", 0)
        pen_amt  = penalised.allocations.get("nautilus", 0)

        assert pen_amt <= base_amt, (
            f"Low-Sharpe nautilus should receive <= baseline allocation\n"
            f"  baseline=${base_amt:.2f}  after_penalty=${pen_amt:.2f}\n"
            f"  FIX: check Sharpe penalty logic in RegimeAllocator.compute()"
        )
        print(f"\n  Sharpe penalty: ${base_amt:.2f} → ${pen_amt:.2f}  delta=${base_amt - pen_amt:.2f}")

    def test_single_healthy_worker_stays_below_50pct(self, allocator):
        """Startup race: only one worker healthy → must not exceed 50% of capital.

        Without profile-weight normalisation, the eligible-only normaliser gives
        the single worker 100% of max_deploy (80% of capital), firing the risk
        manager's per-worker cap and halting the system on every cold start.
        """
        result = allocator.compute(
            regime="BULL_CALM",
            worker_health={
                "nautilus": False, "polymarket": False,
                "arbitrader": False, "core_dividends": False,
                "autohedge": True,
            },
        )
        alloc = result.allocations.get("autohedge", 0)
        assert alloc <= 100.0, (
            f"Single healthy worker (autohedge) received ${alloc:.2f} "
            f"which exceeds 50% ($100) of $200 capital.\n"
            f"  full allocations: {result.allocations}\n"
            f"  FIX: normalise against profile_nonzero_sum, not eligible sum"
        )
        print(f"\n  Single-worker startup: autohedge=${alloc:.2f} (limit=$100.00)")


# ═════════════════════════════════════════════════════════════════════════════
# 3. Risk Manager
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def rm():
    mod = _load_module("hypervisor/risk/manager.py")
    return mod.RiskManager(initial_capital=200.0)


class TestRiskManagerIntegration:
    """
    FAILURE GUIDE
    ─────────────
    safe=True on drawdown test   → MAX_DRAWDOWN_PCT too high, or peak not tracking
    wrong action string          → check RiskVerdict.action values in manager.py
    cooldown not persisting      → _halt_timestamp not set when halt fires
    pnl_floor not firing         → PNL_FLOOR_USD check uses wrong formula
    trim_worker fires wrong name → affected_worker not populated
    """

    def test_clean_portfolio_passes_all_checks(self, rm):
        verdict = rm.assess(total_capital=200.0, free_capital=60.0, open_positions=2)
        assert verdict.safe, (
            f"Clean portfolio should pass every risk check\n"
            f"  Got safe=False: {verdict.reason!r}\n"
            f"  Inputs: total=$200, free=$60 (30%), positions=2"
        )
        print(f"\n  Clean state verdict: {verdict.reason!r}")

    def test_portfolio_drawdown_triggers_halt_all(self, rm):
        # $148 from $200 initial → 26% drawdown > MAX_DRAWDOWN_PCT(20%)
        verdict = rm.assess(total_capital=148.0, free_capital=50.0, open_positions=2)
        assert not verdict.safe, (
            f"26% portfolio drawdown should fail risk check\n"
            f"  Got safe=True — drawdown check not triggering\n"
            f"  total=$148, peak=$200, drawdown=26%, limit=20%"
        )
        assert verdict.action == "halt_all", \
            f"Expected action='halt_all', got {verdict.action!r}\n  reason: {verdict.reason}"
        print(f"\n  Drawdown halt: {verdict.reason}")

    def test_halt_cooldown_persists_after_breach(self, rm):
        rm.assess(total_capital=148.0, free_capital=50.0, open_positions=2)  # trigger halt
        # Next call with healthy numbers should still be blocked by cooldown
        verdict2 = rm.assess(total_capital=200.0, free_capital=60.0, open_positions=1)
        assert not verdict2.safe, (
            f"Halt cooldown should block re-entry even with healthy numbers\n"
            f"  Got safe=True — cooldown not persisting between assess() calls\n"
            f"  FIX: check _halt_timestamp is set and compared in manager.py"
        )
        assert "cooldown" in verdict2.reason.lower(), (
            f"Cooldown reason should mention 'cooldown'\n"
            f"  got: {verdict2.reason!r}"
        )
        print(f"\n  Cooldown active: {verdict2.reason}")

    def test_pnl_floor_triggers_halt_all(self, rm):
        # initial=$200, total=$155 → pnl=-$45, floor=-$40
        verdict = rm.assess(total_capital=155.0, free_capital=50.0, open_positions=1)
        assert not verdict.safe, (
            f"P&L of -$45 should breach PNL_FLOOR_USD(-$40)\n"
            f"  Got safe=True\n"
            f"  total=$155 vs initial=$200 → pnl=-$45 < floor=-$40"
        )
        assert verdict.action == "halt_all", \
            f"Expected halt_all, got {verdict.action!r}"
        print(f"\n  PnL floor: {verdict.reason}")

    def test_too_many_positions_triggers_halt_all(self, rm):
        # 7 positions > MAX_OPEN_POSITIONS(6)
        verdict = rm.assess(total_capital=200.0, free_capital=60.0, open_positions=7)
        assert not verdict.safe, (
            f"7 open positions > MAX_OPEN_POSITIONS(6) should fail\n"
            f"  Got safe=True"
        )
        assert verdict.action == "halt_all"
        print(f"\n  Position limit: {verdict.reason}")

    def test_insufficient_free_capital_triggers_halt_all(self, rm):
        # $20/$200 = 10% free < MIN_FREE_PCT(15%)
        verdict = rm.assess(total_capital=200.0, free_capital=20.0, open_positions=2)
        assert not verdict.safe, (
            f"10% free capital ($20/$200) < MIN_FREE_PCT(15%) should fail\n"
            f"  Got safe=True"
        )
        assert verdict.action == "halt_all"
        print(f"\n  Free capital floor: {verdict.reason}")

    def test_single_worker_over_cap_triggers_trim_worker(self, rm):
        # nautilus=$130/$200 = 65% > MAX_SINGLE_WORKER_PCT(50%)
        verdict = rm.assess(
            total_capital=200.0, free_capital=40.0, open_positions=2,
            worker_allocated={"nautilus": 130.0},
        )
        assert not verdict.safe, (
            f"nautilus at 65% of capital > MAX_SINGLE_WORKER_PCT(50%) should fail\n"
            f"  Got safe=True"
        )
        assert verdict.action == "trim_worker", \
            f"Expected 'trim_worker', got: {verdict.action!r}"
        assert verdict.affected_worker == "nautilus", \
            f"Expected affected_worker='nautilus', got: {verdict.affected_worker!r}"
        print(f"\n  Trim worker: {verdict.reason}  affected={verdict.affected_worker}")

    def test_per_worker_drawdown_triggers_halt_worker(self, rm):
        rm.record_worker_allocation("arbitrader", 90.0)   # sets peak=$90
        # PnL of -$30 on $90 peak → 33% drawdown > WORKER_MAX_DRAWDOWN_PCT(30%)
        verdict = rm.assess(
            total_capital=200.0, free_capital=60.0, open_positions=2,
            worker_pnl={"arbitrader": -30.0},
            worker_allocated={"arbitrader": 60.0},
        )
        assert not verdict.safe, (
            f"arbitrader 33% worker drawdown > WORKER_MAX_DRAWDOWN_PCT(30%) should fail\n"
            f"  Got safe=True"
        )
        assert verdict.action == "halt_worker", \
            f"Expected 'halt_worker', got: {verdict.action!r}"
        assert verdict.affected_worker == "arbitrader", \
            f"Expected 'arbitrader', got: {verdict.affected_worker!r}"
        print(f"\n  Worker halt: {verdict.reason}  affected={verdict.affected_worker}")

    def test_reallocation_down_does_not_trigger_false_drawdown(self, rm):
        """Regression: when more workers join mid-run, hypervisor re-allocates each
        worker with a lower amount.  record_worker_allocation() must reset the peak
        so the reduction doesn't appear as a drawdown and halt the system."""
        # Cycle 1: only autohedge healthy, gets $60
        rm.record_worker_allocation("autohedge", 60.0)
        # Cycle 2: all workers join, autohedge scaled down to $19.2
        rm.record_worker_allocation("autohedge", 19.2)
        verdict = rm.assess(
            total_capital=200.0, free_capital=40.0, open_positions=0,
            worker_pnl={"autohedge": 0.0},
            worker_allocated={"autohedge": 19.2},
        )
        assert verdict.safe, (
            f"Re-allocation from $60→$19.2 (more workers joining) should NOT "
            f"trigger a drawdown halt.\n"
            f"  Got safe=False: {verdict.reason!r}\n"
            f"  FIX: record_worker_allocation() must reset peak to new entry+pnl"
        )
        print(f"\n  Re-allocation regression: {verdict.reason!r}")


# ═════════════════════════════════════════════════════════════════════════════
# 4. Hypervisor Cycle Logic
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def hyp():
    return _load_module("hypervisor/main.py")


class TestHypervisorCycle:
    """
    Tests hypervisor internal helpers directly. No uvicorn, no network.

    FAILURE GUIDE
    ─────────────
    AttributeError on state          → main.py import failed; check WORKER_REGISTRY
    total_capital math wrong         → _reconcile_capital formula error
    free_capital negative            → deployed > total
    open_positions wrong             → _count_open_positions not defaulting missing key to 0
    classifier AttributeError        → RegimeResult shape mismatch; check .regime.value
    classify_sync AttributeError     → method not added to RegimeClassifier
    """

    def test_worker_registry_keys_match_capital_profiles(self, hyp):
        cap = _load_module("hypervisor/allocator/capital.py")

        registry_keys = set(hyp.WORKER_REGISTRY.keys())
        profile_keys  = set()
        for profile in cap.REGIME_PROFILES.values():
            profile_keys.update(profile.keys())

        orphans = profile_keys - registry_keys
        assert not orphans, (
            f"capital.py REGIME_PROFILES has worker keys absent from WORKER_REGISTRY: {sorted(orphans)}\n"
            f"  capital.py keys: {sorted(profile_keys)}\n"
            f"  WORKER_REGISTRY keys: {sorted(registry_keys)}\n"
            f"  FIX: rename orphan keys in capital.py to match WORKER_REGISTRY"
        )
        print(f"\n  Key alignment OK: {sorted(registry_keys)}")

    def test_reconcile_capital_math(self, hyp):
        # Test live-mode math: PAPER_TRADING=False so PnL is added to capital.
        # _reconcile_capital reads state.allocations (authoritative hypervisor view),
        # not state.worker_allocated (lagging worker-reported view).
        orig_paper             = hyp.PAPER_TRADING
        hyp.PAPER_TRADING      = False
        hyp.INITIAL_CAPITAL_USD = 200.0
        hyp.state.worker_pnl   = {"nautilus": 5.0, "arbitrader": -2.0}
        hyp.state.allocations  = {"nautilus": 80.0, "arbitrader": 60.0}

        hyp._reconcile_capital()

        expected_total = 200.0 + 5.0 + (-2.0)    # 203.0
        expected_free  = 203.0 - (80.0 + 60.0)   # 63.0

        assert abs(hyp.state.total_capital - expected_total) < 0.01, (
            f"total_capital wrong\n"
            f"  expected: ${expected_total:.2f}\n"
            f"  got:      ${hyp.state.total_capital:.2f}\n"
            f"  formula:  INITIAL_CAPITAL + sum(worker_pnl)"
        )
        assert abs(hyp.state.free_capital - expected_free) < 0.01, (
            f"free_capital wrong\n"
            f"  expected: ${expected_free:.2f}\n"
            f"  got:      ${hyp.state.free_capital:.2f}\n"
            f"  formula:  total_capital - sum(state.allocations)"
        )
        print(f"\n  Capital reconciled: total=${hyp.state.total_capital:.2f}  free=${hyp.state.free_capital:.2f}")
        hyp.PAPER_TRADING = orig_paper

    def test_open_position_count_sums_across_workers(self, hyp):
        hyp.state.worker_status = {
            "nautilus":   {"open_positions": 2},
            "arbitrader": {"open_positions": 1},
            "polymarket": {"open_positions": 0},
            "autohedge":  {},    # Missing key — must default to 0, not KeyError
        }
        count = hyp._count_open_positions()
        assert count == 3, (
            f"Expected 3 total open positions (2+1+0+0), got {count}\n"
            f"  FIX: _count_open_positions must use .get('open_positions', 0)"
        )
        print(f"\n  Open positions: {count} (expected 3)")

    def test_classifier_returns_valid_regime_result(self, hyp):
        clf = hyp.classifier
        if not hasattr(clf, "classify_sync"):
            pytest.fail(
                "RegimeClassifier missing classify_sync() method.\n"
                "  Hypervisor calls: result = await asyncio.to_thread(classifier.classify_sync)\n"
                "  FIX: add def classify_sync(self) -> RegimeResult to RegimeClassifier"
            )

        try:
            result = clf.classify_sync()
        except Exception as exc:
            pytest.fail(f"classify_sync() raised {type(exc).__name__}: {exc}")

        assert hasattr(result, "regime"), (
            f"RegimeResult missing .regime attribute\n"
            f"  got type: {type(result).__name__}  value: {result!r}"
        )
        assert hasattr(result, "confidence"), \
            f"RegimeResult missing .confidence attribute"

        valid = {"WAR_PREMIUM", "CRISIS_ACUTE", "BEAR_RECESSION",
                 "BULL_FROTHY", "REGIME_CHANGE", "SHADOW_DRIFT", "BULL_CALM"}
        regime_val = result.regime.value
        assert regime_val in valid, \
            f"Unknown regime: {regime_val!r}\n  Valid set: {valid}"
        assert 0.0 <= result.confidence <= 1.0, \
            f"Confidence {result.confidence} outside [0.0, 1.0]"

        print(f"\n  Classifier: regime={regime_val}  confidence={result.confidence:.2%}"
              f"  triggered_by={getattr(result, 'triggered_by', 'N/A')}")


# ═════════════════════════════════════════════════════════════════════════════
# 5. End-to-End Signal Schema
# ═════════════════════════════════════════════════════════════════════════════

SIGNAL_WORKER_PATHS = {
    "nautilus":   "workers/nautilus/worker_api.py",
    "arbitrader": "workers/arbitrader/sidecar/main.py",
    "autohedge":  "workers/autohedge/worker_api.py",
}


@pytest.fixture(
    params=list(SIGNAL_WORKER_PATHS.items()),
    ids=list(SIGNAL_WORKER_PATHS.keys()),
)
def signal_client(request):
    name, path = request.param
    c = _make_client(name, path)
    # Prime: capital + safe regime so signals can fire
    c.post("/allocate", json={"amount_usd": 100.0, "paper_trading": True})
    c.post("/regime",   json={"regime": "BULL_CALM", "confidence": 0.7, "paper_trading": True})
    c.post("/resume")
    return name, c


class TestEndToEndSignalSchema:
    """
    FAILURE GUIDE
    ─────────────
    /signal not 200          → endpoint missing or crashes on empty body
    Not a list               → /signal must return a JSON array (even if empty)
    Missing signal field     → add field to /signal response dict in that worker
    confidence out of range  → must be float in [0.0, 1.0]
    autohedge status wrong   → POST /execute must always return {"status": "advisory_only"}
    """

    def test_signal_response_is_a_list(self, signal_client):
        name, c = signal_client
        resp = c.post("/signal", json={"regime": "BULL_CALM"})
        assert resp.status_code == 200, (
            f"{name} POST /signal → HTTP {resp.status_code}\n"
            f"  body: {resp.text[:300]}"
        )
        signals = resp.json()
        assert isinstance(signals, list), (
            f"{name} /signal must return a JSON array (list)\n"
            f"  got type: {type(signals).__name__}\n"
            f"  body: {str(signals)[:300]}"
        )
        print(f"\n  [{name}] /signal returned {len(signals)} signal(s)")

    def test_each_signal_contains_required_fields(self, signal_client):
        name, c = signal_client
        signals = c.post("/signal", json={"regime": "BULL_CALM"}).json()
        if not isinstance(signals, list) or not signals:
            pytest.skip(f"{name}: no signals returned — no open positions to validate")

        for i, sig in enumerate(signals):
            missing = REQUIRED_SIGNAL_FIELDS - set(sig.keys())
            assert not missing, (
                f"{name} signal[{i}] missing fields: {sorted(missing)}\n"
                f"  present: {sorted(sig.keys())}\n"
                f"  all required: {sorted(REQUIRED_SIGNAL_FIELDS)}"
            )
            conf = sig.get("confidence")
            assert isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0, (
                f"{name} signal[{i}].confidence={conf!r} must be float in [0.0, 1.0]"
            )
        print(f"\n  [{name}] signal fields OK: {sorted(signals[0].keys())}")

    def test_autohedge_execute_always_advisory_only(self):
        """AutoHedge must never execute a trade — always returns advisory_only status."""
        c = _make_client("autohedge", "workers/autohedge/worker_api.py")
        resp = c.post("/execute", json={"ticker": "BTC/USDT", "action": "buy"})
        assert resp.status_code == 200, \
            f"autohedge POST /execute → HTTP {resp.status_code}\n  body: {resp.text[:300]}"
        body = resp.json()
        assert body.get("status") == "advisory_only", (
            f"AutoHedge /execute must return {{\"status\": \"advisory_only\"}}\n"
            f"  got: {body}\n"
            f"  FIX: POST /execute in autohedge/worker_api.py must always set status='advisory_only'"
        )
        print(f"\n  [autohedge] /execute correctly advisory_only: {body}")
