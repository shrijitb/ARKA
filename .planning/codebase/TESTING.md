# Testing Patterns

**Analysis Date:** 2026-04-15

## Test Framework

**Runner:**
- pytest, configured via `/home/shrijit/arka/pytest.ini`
- Config: `asyncio_mode = auto` (all async tests auto-wrapped)
- Custom marker: `integration` (deselect with `-m "not integration"`)

**Assertion Library:**
- pytest built-in assertions (no separate library)
- `unittest.mock` (`MagicMock`, `AsyncMock`, `patch`) for mocking

**Run Commands:**
```bash
# ALWAYS use venv Python directly — system Python lacks fastapi/httpx
~/arka/.venv/bin/python -m pytest tests/ -v               # Run all tests
~/arka/.venv/bin/python -m pytest tests/ -m "not integration" -v  # Unit only
~/arka/.venv/bin/python -m pytest tests/test_safety_rails.py -v   # Single file

# Expected results: 120+ passed | 7 skipped | 0 failed
```

**Standalone runner:**
`tests/test_mara.py` also has a `_run()` function at the bottom that lets you run unit tests without pytest at all:
```bash
~/arka/.venv/bin/python tests/test_mara.py
```

## Test File Organization

**Location:**
All tests live in `/home/shrijit/arka/tests/` — not co-located with source files.

**Files:**
- `tests/test_mara.py` (1934 lines) — component unit and integration tests; covers conflict index scoring, capital allocator, strategy math, domain router, HMM classifier
- `tests/test_integration_dryrun.py` (737 lines) — in-process smoke tests for the full worker REST contract and hypervisor logic; no Docker, no real money
- `tests/test_safety_rails.py` (423 lines) — safety subsystem: `MarginReserveManager`, `ExpiryGuard`, `RiskManager` integration
- `tests/test_concurrency.py` (379 lines) — async race conditions and `CircuitBreaker` state machine transitions

**Naming:**
- Test classes: `TestPascalCase` — grouped by feature under test
- Test methods: `test_snake_case_describing_what_is_verified`
- Helper methods (private): `_make_client()`, `_load_module()`, `_dec()`, `_make_state()`, `_make_events()`

## Test Structure

**Class-based, `setup_method` for per-test reset:**
```python
class TestMarginReserveManager:
    """Tests for per-position margin call reserve system."""

    def setup_method(self):
        """Create fresh MarginReserveManager for each test."""
        self.mrm = MarginReserveManager()

    def test_compute_reserve_funding_arb(self):
        """Funding arb (delta-neutral) should use 15% reserve."""
        # $100 notional, 3x leverage → reserve = 100 * 0.15 / 3 = $5.00
        reserve = self.mrm.compute_reserve("funding_arb", 100.0, 3)
        assert reserve == 5.0
```

**Imports inside test methods** (for isolation against optional deps):
```python
def test_multiple_focused_queries(self):
    from data.feeds.conflict_index import GDELT_QUERIES
    assert len(GDELT_QUERIES) >= 2
```
This pattern is universal in `test_mara.py`. It avoids import-time failures when optional packages are missing.

**Integration tests use `@pytest.mark.integration` class decorator:**
```python
try:
    import pytest as _pytest
    _integration = _pytest.mark.integration
except ImportError:
    def _integration(cls): return cls  # no-op fallback

@_integration
class TestGdeltIntegration:
    def test_no_429(self):
        from data.feeds.conflict_index import _fetch_gdelt
        r = _fetch_gdelt()
        assert r.get("articles", 0) > 0
```

**Async tests use `@pytest.mark.asyncio`:**
```python
class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_circuit_breaker_state_transitions(self):
        cb = CircuitBreaker("test", failure_threshold=2, cooldown_seconds=1)
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        await asyncio.sleep(1.1)
        assert cb.can_execute() is True
```

**Parametrized tests with `@pytest.mark.parametrize`:**
```python
ALL_REGIMES = ["RISK_ON", "RISK_OFF", "CRISIS", "TRANSITION"]

class TestCapitalAllocator:
    @pytest.mark.parametrize("regime", ALL_REGIMES)
    def test_regime_allocations_within_capital(self, allocator, regime):
        result = allocator.compute(regime=regime)
        total  = sum(result.allocations.values())
        assert total <= 200.01, (
            f"[{regime}] allocated ${total:.4f} exceeds $200 total capital\n"
            f"  breakdown: {result.allocations}\n"
            f"  FIX: check MAX_DEPLOY_PCT and weight normalisation in capital.py"
        )
```

## Mocking

**Framework:** `unittest.mock` — `patch`, `MagicMock`, `AsyncMock`

**`patch.object` for method-level mocking:**
```python
from unittest.mock import patch
with patch.object(ExpiryGuard, 'check_position', return_value={
    "instrument": "BTC-USDT-250627",
    "days_to_expiry": 5,
    "action": "warn",
    "reason": "5 days to expiry.",
}):
    result = self.eg.scan_all_positions(positions)
```

**`patch` for module-level attribute replacement:**
```python
with patch('hypervisor.risk.expiry_guard.date') as mock_date:
    mock_date.today.return_value = date(2025, 6, 1)
    mock_date.side_effect = lambda *args, **kwargs: date(*args, **kwargs)
    expiries = self.eg.get_upcoming_expiries(instruments, lookahead_days=60)
```

**Conftest module stubs** (in `/home/shrijit/arka/conftest.py`) for hypervisor import isolation:
- Injects lightweight `httpx`, `fastapi`, `fastapi.responses`, `structlog` stubs into `sys.modules` before test collection
- Stubs only installed if real packages are not importable — venv tests get real packages
- Stubs satisfy import-time requirements; tests that need real FastAPI use `TestClient` fixtures

**FastAPI TestClient** (in `test_integration_dryrun.py`) for REST contract testing without sockets:
```python
from fastapi.testclient import TestClient

def _make_client(worker_name: str, rel_path: str):
    app = _load_worker_app(worker_name, rel_path)
    return TestClient(app, raise_server_exceptions=False)

@pytest.fixture(params=list(WORKER_PATHS.items()), ids=list(WORKER_PATHS.keys()))
def worker_client(request):
    name, path = request.param
    return name, _make_client(name, path)
```

**What to mock:**
- External API calls (yfinance, GDELT, OKX REST) — use synthetic/fallback data in unit tests
- Date/time dependencies (e.g., `date.today()`) when testing time-sensitive logic
- Individual class methods when you need to isolate one component from its dependency

**What NOT to mock:**
- The class under test itself
- Pure algorithmic functions (`_score_market_proxy()`, `_redistribute_weights()`) — call directly
- FastAPI endpoint handlers — use `TestClient` against the real app instead

## Fixtures and Factories

**Module-level loaders** replace fixture factories for Python modules:
```python
def _load_module(rel_path: str):
    """Load a Python module by path relative to ~/arka."""
    abs_path = os.path.join(_PROJECT, rel_path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(...)
    spec = importlib.util.spec_from_file_location(...)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

@pytest.fixture
def allocator():
    mod = _load_module("hypervisor/allocator/capital.py")
    return mod.RegimeAllocator(total_capital=200.0)
```

**Test data factories** via helper methods on test classes:
```python
def _make_state(self, zone, callsign="", squawk="", on_ground=False):
    return {
        "icao24": "abc123", "callsign": callsign,
        "longitude": 35.0, "latitude": 32.0,
        "squawk": squawk, "zone": zone, ...
    }

def _dec(self, domain, action, modifier, confidence=0.8):
    return self.Decision(domain=domain, action=action, weight_modifier=modifier, ...)
```

**Location:** No separate `fixtures/` or `factories/` directory — helpers defined as private methods on the test class that uses them.

## Coverage

**Requirements:** No enforced coverage threshold; no `.coveragerc` or coverage config detected.

**View Coverage:**
```bash
~/arka/.venv/bin/python -m pytest tests/ --cov=hypervisor --cov=data --cov=workers --cov-report=term-missing
```

## Test Types

**Unit Tests (no network, no .env, <5s total):**
- Scope: Pure logic functions — scoring algorithms, weight redistribution, allocation math, risk limit checks
- Location: `tests/test_mara.py` (non-integration classes), `tests/test_safety_rails.py`, `tests/test_concurrency.py`
- Approach: Import function directly, call with synthetic data, assert result

**Integration Tests (need .env + network, ~60s):**
- Scope: Real external API calls (GDELT, full conflict index score)
- Location: `tests/test_mara.py` — classes decorated with `@_integration`
- Deselect with: `pytest -m "not integration"`

**REST Contract Tests (in-process, no Docker):**
- Scope: All 8 worker REST endpoints; hypervisor capital math; risk manager limits
- Location: `tests/test_integration_dryrun.py`
- Approach: Load worker `app` via `importlib`, wrap with `TestClient`, exercise all endpoints
- FAIL behavior: Missing file → `pytest.fail()`. Missing dependency → `pytest.skip()` (not a bug)

**Concurrency / State Tests:**
- Scope: `CircuitBreaker` state transitions, `HypervisorState` concurrent writes, async race conditions
- Location: `tests/test_concurrency.py`
- All tests are `async def` with `@pytest.mark.asyncio`

**E2E Tests:**
- Not present — no Playwright, Cypress, or Selenium setup detected

## Skip Behavior

**Expected skips (7 total):**
Tests skip (not fail) for missing optional dependencies:
- OKX live integration — only live exchange, always skipped in test env
- ACLED CAST / live events — free tier 403 (permanent limitation)
- `nautilus_trader` not installed in test env — worker loads in stub mode, REST layer fully exercised

**Skip pattern:**
```python
try:
    import hmmlearn as _hmmlearn
    _HMMLEARN_OK = True
except ImportError:
    _HMMLEARN_OK = False

_skipif_no_hmmlearn = pytest.mark.skipif(
    not _HMMLEARN_OK, reason="hmmlearn not installed"
)
```

## Common Patterns

**Boundary/bounds testing:**
```python
def test_bounded(self):
    assert 0 <= self.score({"defense_momentum": 1.0, "gold_oil_ratio": 200.0, "vix": 80.0}) <= 100
    assert 0 <= self.score({}) <= 100  # empty input must still be safe
```

**Weight normalization assertions (floating point):**
```python
assert abs(_MARKET_PROXY_WEIGHT + _OSINT_LAYER_WEIGHT - 1.0) < 1e-9
assert abs(sum(_OSINT_BASE_WEIGHTS.values()) - 1.0) < 1e-9
```

**Async state sequencing** (test multi-step FSM transitions):
```python
r1 = detect_aviation_anomalies(mil_states)  # Read 1: no anomaly
assert aviation_client._zone_fsms[zone].state == ZoneState.NORMAL

r2 = detect_aviation_anomalies(mil_states)  # Read 2: ELEVATED confirmed
assert aviation_client._zone_fsms[zone].state == ZoneState.ELEVATED
```

**State reset in `setup_method`:**
When testing modules with module-level state (e.g., FSMs, caches), reset in `setup_method`:
```python
def setup_method(self):
    from data.feeds import aviation_client
    aviation_client._zone_fsms.clear()
```

**Assertion messages include diagnostic context and fix guidance:**
```python
assert not orphan_in_capital, (
    f"capital.py references workers not in WORKER_REGISTRY: {sorted(orphan_in_capital)}\n"
    f"  capital keys: {sorted(profile_keys)}\n"
    f"  registry keys: {sorted(registry_keys)}\n"
    f"  FIX: rename or remove orphan keys from ALLOCATION_PROFILES in capital.py"
)
```

**Error testing:**
```python
with pytest.raises(RuntimeError):
    await cb.execute(lambda: 1/0)

assert cb.state == CircuitState.OPEN
```

---

*Testing analysis: 2026-04-15*
