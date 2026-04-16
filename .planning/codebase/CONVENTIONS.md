# Coding Conventions

**Analysis Date:** 2026-04-15

## Naming Patterns

**Files:**
- Python modules: `snake_case.py` (e.g., `market_data.py`, `conflict_index.py`, `margin_reserve.py`)
- Worker APIs: always named `worker_api.py` in each worker directory
- Test files: `test_<feature>.py` (e.g., `test_safety_rails.py`, `test_concurrency.py`)
- React components: `PascalCase.jsx` (e.g., `RegimeMood.jsx`, `RiskMeter.jsx`)
- React hooks: `use<Name>.js` (e.g., `useArkaData.js`)
- React utilities: `camelCase.js` (e.g., `cn.js`, `api.js`, `glossary.js`)

**Classes:**
- PascalCase throughout: `RiskManager`, `RegimeAllocator`, `MarginReserveManager`, `CircuitBreaker`, `HypervisorState`, `DIContainer`
- Dataclasses named after what they return: `RiskVerdict`, `AllocationResult`, `RegimeResult`, `DomainDecision`
- Worker state classes: `WorkerRiskState`

**Functions and Methods:**
- `snake_case` for all functions and methods
- Private helpers prefixed with underscore: `_load_module()`, `_make_client()`, `_fetch_gdelt()`, `_score_market_proxy()`, `_install_stub()`
- Class instance setup: `setup_method()` used over `setUp()` (pytest class style)

**Constants:**
- `SCREAMING_SNAKE_CASE` for module-level constants: `MAX_DRAWDOWN_PCT`, `WORKER_REGISTRY`, `GDELT_SLEEP`, `ENTRY_THRESHOLD`, `DEFAULT_RESERVE_PCT`
- Private module-level constants (not exported) use leading underscore: `_MARKET_PROXY_WEIGHT`, `_OSINT_BASE_WEIGHTS`, `_FEEDS_AVAILABLE`, `_HERE`, `_PROJECT`

**Variables:**
- `snake_case` everywhere
- Single-letter loop variables only for numeric iteration (`i`, `k`) not for objects
- Descriptive names for all dict keys and typed return values

**Enums:**
- PascalCase class name, SCREAMING_SNAKE_CASE members: `class Regime(str, Enum)`, `RISK_ON`, `CRISIS`
- String enums used for JSON serialization compatibility: `class DomainAction(str, Enum)`, `class CircuitState(Enum)`

**Type Annotations:**
- All function signatures have type annotations on parameters and return types
- `Optional[T]` used for nullable fields (not `T | None`)
- `Dict[K, V]`, `List[T]` from `typing` (not built-in `dict[k, v]` — mix exists in newer files)
- `from __future__ import annotations` present in all files that use type annotations (39 files)

## Code Style

**Formatting:**
- No linter config files detected (no `.flake8`, `.pylintrc`, `pyproject.toml`, `ruff.toml`)
- Indentation: 4 spaces universally
- Line length: generally ≤ 100 characters; long assert messages use implicit string concatenation

**Alignment:**
- Dict literals and dataclass fields aligned on `=` and `:` for readability:
  ```python
  self.total_capital:     float            = INITIAL_CAPITAL_USD
  self.free_capital:      float            = INITIAL_CAPITAL_USD
  self.current_regime:    str              = "TRANSITION"
  ```
- Constants at module level follow same alignment pattern:
  ```python
  MAX_DRAWDOWN_PCT        = 0.20
  MAX_SINGLE_WORKER_PCT   = 0.50
  MAX_OPEN_POSITIONS      = 6
  ```

**Separators:**
- Section dividers use `# ──` comment bars (Unicode em-dash) to delineate logical blocks within a module
- Double-width dividers `# ═══` used between major test classes
- Format: `# ── Section Name ─────────────────────────────────────────────────`

## Import Organization

**Order:**
1. `from __future__ import annotations` (always first if present)
2. stdlib imports (alphabetical within group): `asyncio`, `json`, `logging`, `os`, `time`
3. Third-party imports: `fastapi`, `httpx`, `numpy`, `pydantic`, `structlog`
4. Local project imports: `from hypervisor.xxx import ...`, `from data.feeds.xxx import ...`

**Optional dependency pattern — wrap in try/except:**
```python
try:
    from data.feeds.funding_rates import get_all_current_rates as _get_live_rates
    _FEEDS_AVAILABLE = True
except ImportError:
    _FEEDS_AVAILABLE = False
```
Use the `_AVAILABLE` flag to gate live-path code; fall back to synthetic/stub data.

**Private import aliases:**
Optional packages aliased with trailing underscore or `_` prefix:
```python
import ccxt  # type: ignore[import-untyped]
import requests as _requests
from fredapi import Fred as _Fred
```

**Path Aliases:**
- No module-level aliases configured; all imports use full paths
- Within tests, `sys.path.insert(0, _PROJECT)` adds project root before test collection

## Error Handling

**Exception Hierarchy:**
All Arka-specific exceptions extend `ArkaError` in `hypervisor/errors.py`:
- `WorkerUnreachableError` — worker health check failed or HTTP timed out
- `ExternalAPIError` — data source (yfinance, FRED, GDELT, OKX) failed
- `RiskLimitBreachedError` — RiskManager rejected an allocation
- `RegimeClassificationError` — HMM classifier failed; previous regime held
- `ConfigurationError` — missing env var or YAML config

**Pattern:**
- Recoverable errors (network, external API): log + continue with cached/fallback data
- Non-recoverable errors: log + hold previous state until next cycle
- Database operations always wrapped in `try/except Exception as exc` with `logger.warning()`
- Never swallow `KeyboardInterrupt` or `SystemExit`

**FastAPI error returns:**
```python
raise HTTPException(status_code=404, detail="Worker not found")
raise HTTPException(status_code=400, detail="Invalid regime")
```

**Assertion messages in tests include FIX guidance:**
```python
assert not missing, (
    f"{name} /status missing fields: {sorted(missing)}\n"
    f"  FIX: add these keys to the /status endpoint in {WORKER_PATHS[name]}"
)
```

## Logging

**Framework:** Mixed — `logging.getLogger(__name__)` for most modules, `structlog.get_logger(__name__)` for worker APIs and the audit subsystem.

**Standard logging (hypervisor core, data feeds):**
```python
import logging
logger = logging.getLogger(__name__)
logger.info(f"RiskManager initialized | Max drawdown: {MAX_DRAWDOWN_PCT*100:.0f}%")
logger.warning("Allocator: no eligible workers for %s. Staying cash.", regime)
```

**structlog (worker APIs, audit trail):**
```python
import structlog
logger = structlog.get_logger(__name__)
audit_log = structlog.get_logger("arka.audit")
```

**Audit logging** is separate and writes to both stdout and `data/audit.jsonl` (rotating, 10MB max, 5 backups). All state-changing events use named audit helper functions from `hypervisor/audit.py`: `audit_regime_change()`, `audit_allocation_update()`, `audit_worker_paused()`, etc.

**Log level usage:**
- `DEBUG`: detailed allocation math, per-position reserve calculations
- `INFO`: cycle status, regime changes, worker health results, allocation summaries
- `WARNING`: degraded state (unhealthy worker, low Sharpe penalty applied, DB write failure)
- `ERROR`: non-recoverable failures with `exc_info=True` for stack trace

**Hypervisor startup config** in `hypervisor/main.py`:
```python
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
```

## Comments

**Module Docstrings:**
Every module has a top-level docstring explaining:
1. What the module does (1-2 sentences)
2. Key design decisions, limitations, or context
3. Usage example or run command for standalone verification
4. PHASE 3 stubs noted inline with `# PHASE 3:` comments

**Inline comments:**
- Section headers use aligned `# ──` dividers
- Complex math explained inline (e.g., funding yield formula in `funding_arb.py`)
- Known workarounds documented at point of use (e.g., BDI proxy, OKX symbol format)
- `# type: ignore[...]` used with explanation for untyped third-party packages

**Test docstrings:**
Each test class has a docstring explaining purpose. Individual test methods have one-line docstrings for non-obvious cases; obvious tests have no docstring.

**FAILURE GUIDE pattern** in integration tests — each test class includes diagnostic hints:
```python
"""
FAILURE GUIDE
─────────────
404 on /allocate          → add POST /allocate endpoint
Missing /status fields    → add the field name to /status response dict
"""
```

## Module Design

**Exports:**
- No `__all__` defined — all public names importable by convention (single underscore for private)
- Worker APIs always expose `app` (FastAPI instance) as module-level name for TestClient

**Dataclasses for return types:**
All multi-field results use `@dataclass` instead of plain dicts: `AllocationResult`, `RiskVerdict`, `RegimeResult`, `DomainDecision`, `WorkerRiskState`

**Pydantic models for request validation** in FastAPI endpoints:
```python
class AllocateRequest(BaseModel):
    amount_usd: float = Field(..., gt=0, le=100000)
    paper_trading: bool = True
```

**Worker constants pattern:**
Each worker defines `WORKER_NAME = "nautilus"` and `REGIME_BIAS: Dict[str, str]` at module level.

## Frontend Conventions (React/JSX)

**Component style:**
- Functional components only, no class components
- Named exports for utility functions (`export function cn`), default exports for components (`export default function RegimeMood`)
- Props destructured at function signature: `function RegimeMood({ regime })`

**Tailwind usage:**
- Never hardcode hex colors in JSX — always use custom token classes (`text-cream`, `bg-card`, `bg-profit`)
- Dynamic/runtime values use inline `style=` attribute (bar widths, angles, sparklines)
- Conditional classes via `cn()` from `src/utils/cn.js` — never string concatenation
- CSS animations defined as `@keyframes` in `src/styles/global.css` with `.anim-*` classes

**State and effects:**
- `useState` / `useEffect` / `useCallback` from React
- Custom hooks in `src/hooks/` with `use` prefix
- API calls always use `arkaFetch` wrapper (not raw `fetch`) for auth header injection

---

*Convention analysis: 2026-04-15*
