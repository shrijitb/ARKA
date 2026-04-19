# Bug Report: Service Layer (Orchestration & Core Logic)

## Overview
The service layer consists of the MARA Hypervisor (`hypervisor/main.py`) and supporting modules (regime classifier, allocator, risk manager, execution risk checker). This layer orchestrates workers, processes market data, computes regimes, allocates capital, and enforces risk limits. Multiple concurrency, state management, and logic flaws were identified.

## Detailed Findings

### 1. Race Conditions on Shared Mutable State
- **Files**: `hypervisor/main.py` (class `HypervisorState` and related functions)
- **Issue**: The hypervisor runs multiple concurrent async tasks:
  - Main orchestration loop (`orchestration_loop`)
  - EDGAR watchlist loop (`_edgar_watchlist_loop`)
  - APScheduler job for quarterly sweep (`_run_quarterly_sweep`)
  - Per‑source latency timers (`_prefetch_and_time_data_sources` spawns `asyncio.to_thread` calls)
  These tasks read and write shared attributes of `HypervisorState` (e.g., `state.worker_health`, `state.worker_status`, `state.cycle_count`, `state.halted`, `state.allocations`, `state.total_capital`, `state.free_capital`) without any synchronization primitives (e.g., `asyncio.Lock`).
- **Impact**:
  - Lost updates: two cycles may read the same worker health, both decide to allocate, leading to over‑allocation.
  - Inconsistent reads: a worker's status may be updated by `_pull_worker_status` while the risk manager is reading `state.worker_pnl`, causing stale or mixed‑cycle data.
  - Flag corruption: `state.halted` could be set by one task and cleared by another, causing the hypervisor to incorrectly resume or halt.
- **Evidence**: No `asyncio.Lock`, `asyncio.Semaphore`, or use of `asyncio.Queue` for state mutations. All state updates are direct attribute assignments.

### 2. Inconsistent Worker Health & Status Views
- **Issue**: Worker health (`state.worker_health`) is set by `_check_worker_health`. Status (`state.worker_status`, `state.worker_pnl`, etc.) is set by `_pull_worker_status`. These two functions are called in sequence within a cycle but are not atomic; a worker could become unhealthy after the health check but before the status pull, yet the hypervisor will still treat it as healthy for that cycle.
- **Impact**: Allocation or regime commands may be sent to a worker that is actually down, leading to timeouts and missed actions.
- **Evidence**: In `_run_cycle`:
  ```python
  await _check_worker_health()
  healthy = [w for w, h in state.worker_health.items() if h]
  await _pull_worker_status(healthy)
  ```
  Between the two awaits, the worker's actual health may change.

### 3. Unbounded Accumulation of History Lists
- **Files**: `hypervisor/main.py` (e.g., `state.last_edgar_alerts`, `state.execution_risk_state`, `self._history` in `RegimeClassifier`)
- **Issue**: 
  - `state.last_edgar_alerts` is replaced each scan, but the list of alerts within it can grow if many tickers trigger alerts; no limit is applied.
  - `state.execution_risk_state` stores results from the latest cycle only, but nested dicts (`slippage`, `liquidity`) could accumulate if not cleared.
  - `RegimeClassifier._history` is capped at 100 entries, which is acceptable, but the classifier also stores each `RegimeResult` indefinitely until the cap; still, the snapshot objects within may hold references to large data.
- **Impact**: Gradual memory increase over long runs, especially if alerts are frequent.
- **Evidence**: No explicit clearing or size limits on these lists/dicts beyond the classifier's 100‑entry cap.

### 4. Incorrect Confidence Decay on Held Regime
- **File**: `hypervisor/main.py` (line where low data quality triggers holding previous regime)
- **Issue**: When data quality is low (<0.5), the code creates a held regime result with:
  ```python
  confidence = self.current.confidence * 0.8   # decay confidence
  ```
  However, `self.current.confidence` may already be decayed from previous holds, leading to compounding decay and potentially dropping confidence to zero after several cycles of bad data, causing the hypervisor to ignore a valid regime simply due to a temporary data glitch.
- **Impact**: Over‑cautious behavior: the system may stick to an outdated regime longer than necessary, missing trading opportunities or failing to react to real market changes.
- **Evidence**: 
  ```python
  if snapshot.data_quality() < 0.5 and self.current is not None:
      held = RegimeResult(
          regime       = self.current.regime,
          snapshot     = snapshot,
          confidence   = self.current.confidence * 0.8,   # decay confidence
          triggered_by = ["held_low_data_quality"],
      )
  ```

### 5. Manual Override Bypasses All Safety Checks
- **File**: `hypervisor/regime/classifier.py`
- **Issue**: The `override` method sets `self._override` to a `Regime` enum. The `classify` method then returns a result with `confidence = 1.0` and `triggered_by = ["manual_override"]`, completely ignoring the macro snapshot and data quality. While useful for testing, this creates a backdoor that could be abused if the override mechanism is exposed (e.g., via an admin API) without proper authorization.
- **Impact**: An attacker who can invoke the override (if exposed) could force the hypervisor into any regime, causing it to allocate capital according to arbitrary rules, potentially leading to large losses.
- **Evidence**: No authentication or authorization check around the override mechanism; it is purely programmatic.

### 6. Inconsistent Use of `None` for Sharpe Ratio
- **File**: `hypervisor/main.py` (`_pull_worker_status`)
- **Issue**: The code sets `state.worker_sharpe[worker] = _sharpe if _sharpe != 0.0 else None`. Later, the allocator (`hypervisor/allocator/capital.py`) likely expects a float or `None`. However, other parts (e.g., Prometheus metrics) treat missing Sharpe as `0.0`:
  ```python
  sharpe = state.worker_sharpe.get(worker) or 0.0
  ```
  This mismatch could cause the allocator to skip a worker due to `None` while metrics show 0.0, leading to confusion.
- **Impact**: Inconsistent interpretation of "no data" vs "zero Sharpe" may lead to incorrect capital allocation decisions.
- **Evidence**: 
  ```python
  _sharpe = float(data.get("sharpe", 0.0))
  state.worker_sharpe[worker] = _sharpe if _sharpe != 0.0 else None
  ```
  and in metrics:
  ```python
  sharpe = state.worker_sharpe.get(worker) or 0.0
  ```

### 7. Lack of Timeout and Retry Internalization for Worker Calls
- **Files**: Various functions in `hypervisor/main.py` that call workers via `httpx.AsyncClient` (e.g., `_check_worker_health`, `_pull_worker_status`, `_broadcast_regime`, `_send_allocations`).
- **Issue**: Each call creates a new `AsyncClient` with a fixed timeout (`WORKER_TIMEOUT_SEC`). There is no retry mechanism with exponential backoff or jitter. Transient network glitches cause immediate failure logs and may lead to workers being marked unhealthy or missing allocations.
- **Impact**: Reduced resilience to temporary network blips; increased false‑positive health failures.
- **Evidence**: 
  ```python
  async with httpx.AsyncClient(timeout=WORKER_TIMEOUT_SEC) as client:
      ...
  ```
  No `retry` parameter or surrounding retry loop.

### 8. Potential Deadlock in `asyncio.gather` with `return_exceptions=True`
- **File**: `hypervisor/main.py` (`_check_worker_health` and similar)
- **Issue**: While `return_exceptions=True` prevents one failing worker from raising an exception and cancelling the rest, the caller still processes the results and logs warnings. However, if a worker's endpoint hangs (does not respond until timeout), the `await asyncio.gather(...)` will wait for the full timeout for each worker, but they run concurrently, so the total wait is the longest timeout, not the sum. This is acceptable, but there is no per‑worker circuit breaker.
- **Impact**: A single slow or malicious worker endpoint could delay the entire health‑check cycle by up to `WORKER_TIMEOUT_SEC`, reducing the effective cycle frequency.
- **Evidence**: No per‑task timeout override; relies on the client timeout.

### 9. Incorrect Risk Manager Input: Using `state.allocations` (lagging) for Open Positions
- **File**: `hypervisor/main.py` (`_run_cycle` risk check)
- **Issue**: The risk manager is called with:
  ```python
  verdict = risk_mgr.assess(
      total_capital    = state.total_capital,
      free_capital     = state.free_capital,
      open_positions   = _count_open_positions(),
      worker_pnl       = state.worker_pnl,
      worker_allocated = state.allocations,  # authoritative; worker_allocated lags one cycle
  )
  ```
  The comment acknowledges that `state.allocations` lags one cycle, yet it is passed as `worker_allocated`. The risk manager may rely on this field for decisions (e.g., leverage checks). Using lagged allocation data could cause the risk manager to under‑ or over‑estimate current exposure.
- **Impact**: Risk limits may be incorrectly calculated, allowing excessive risk or unnecessarily constraining trading.
- **Evident**: The comment in the code: `# authoritative; worker_allocated lags one cycle`

### 10. Execution Risk Checker State Persistence Across Cycles
- **File**: `hypervisor/main.py` (`state.execution_risk_state`)
- **Issue**: The execution risk checker (`execution_risk`) is a singleton instantiated at module level. Its internal state (`last_slippage_results`, `last_liquidity_results`) is updated each cycle via `check_slippage` and `check_liquidity`. The hypervisor then copies parts of this state into `state.execution_risk_state`. However, the execution_risk object itself retains historical data unless explicitly cleared, potentially leading to cross‑cycle contamination if the methods ever append to internal lists.
- **Impact**: If the execution risk checker ever accumulates data (e.g., for latency p95 calculation), old data could skew current metrics.
- **Evidence**: Need to inspect `hypervisor/risk/execution_risk.py`; but given the pattern, risk exists.

### 11. Missing Circuit Breaker for Repeated Worker Failures
- **Issue**: If a worker repeatedly fails health checks, the hypervisor logs warnings each cycle but continues to include it in the `healthy` list only when it passes. There is no threshold after which the worker is automatically considered unhealthy and excluded from allocation attempts for a longer cool‑down period.
- **Impact**: Flapping workers (e.g., restarting every few cycles) cause churn in allocations and increased log noise.
- **Evidence**: No failure counting or back‑off logic.

### 12. Inconsistent Units for Capital Throughout Codebase
- **Issue**: Some places treat capital as USD float (e.g., `INITIAL_CAPITAL_USD = 200.0`). Other places may implicitly assume cents or other units (no evidence found, but worth noting). Consistency is assumed but not enforced.
- **Impact**: If a misinterpretation occurs, orders could be sized incorrectly (e.g., 200 cents vs 200 dollars).
- **Evidence**: No explicit unit conversion constants; rely on comments.

## Recommendations
1. **Protect Shared State with Asyncio Locks**
   - Introduce one or more `asyncio.Lock` instances to guard mutations of `HypervisorState`. For example, a lock for worker health/status, another for capital/allocations, and a separate lock for regime/confidence.
   - Alternatively, refactor to use a single state update transaction per cycle: gather all data into immutable structures, then atomically replace the state.

2. **Make Health and Status Checks Atomic**
   - Combine health and status retrieval into a single RPC call per worker (if possible) or, at minimum, hold a lock from the start of health check through status pull for each worker.

3. **Bound All Accumulating Structures**
   - Apply size limits to `state.last_edgar_alerts` (e.g., keep only last N alerts per ticker).
   - Ensure `execution_risk` internal lists are capped (use `collections.deque` with maxlen).
   - Consider using a TTLCache with max size for the market data cache.

4. **Revise Regime Holding Logic**
   - Instead of multiplicative decay, use a fixed confidence reduction (e.g., `confidence = max(self.current.confidence - 0.1, 0.0)`) or hold the previous confidence unchanged until data quality recovers.

5. **Secure Manual Override Mechanism**
   - If override is intended for debugging only, remove it from production builds or guard it behind an environment variable or authentication token.
   - If kept, add an API endpoint with strong authentication (e.g., mutual TLS or API key) and audit logging.

6. **Standardize Sharpe Ratio Representation**
   - Use `Optional[float]` throughout; treat `None` as "no data". Update Prometheus exposition to export `NaN` or skip the metric when `None`.

7. **Add Retry with Exponential Backoff for Worker Calls**
   - Use a library like `tenacity` or implement a custom retry wrapper for `httpx` calls, distinguishing between transient errors (network, 5xx) and permanent errors (4xx, timeout due to dead worker).

8. **Introduce Per‑Worker Circuit Breaker**
   - Track consecutive failures; after a threshold (e.g., 3 failures), temporarily blacklist the worker for a cool‑down period (e.g., 5 minutes) before retrying health checks.

9. **Pass Current Allocations to Risk Manager**
   - Compute `current_allocations` by merging `state.allocations` with any in‑flight allocations from the current cycle (if any) or use the most recent worker‑reported allocated USD from `_pull_worker_status` (which is more timely than the hypervisor's lagged copy).

10. **Audit Execution Risk Checker for State Leaks**
    - Review `hypervisor/risk/execution_risk.py` to ensure no internal lists grow without bound; add explicit clearing or use fixed‑size buffers.

11. **Add Unit and Integration Tests for Concurrency Scenarios**
    - Simulate concurrent health checks, status pulls, and regime changes to verify lock correctness and absence of races.

12. **Enforce Capital Units via Type Aliases or Documentation**
    - Define `type USD = float` (or use a simple class) and annotate functions; add docstrings specifying units.

## Additional Notes
Many of these issues stem from the hypervisor’s evolution as a research prototype. Addressing them will transform the service layer into a robust, production‑grade orchestrator suitable for unattended operation.
