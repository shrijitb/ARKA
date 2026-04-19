# Bug Report: Data Access Layer

## Overview
The MARA (Arka-updated) system defines a SQLite schema for persistence but does not use it in any worker or the hypervisor. All state is kept in-memory, leading to data loss on restart and limiting audit capabilities.

## Detailed Findings

### 1. Missing Database Integration
- **File**: `data/db/schema.sql`
- **Issue**: Schema defines tables `regime_log`, `signals`, `orders`, `portfolio_state` but no code inserts, updates, or queries these tables.
- **Impact**: 
  - No persistence of trading signals, orders, or portfolio state across restarts.
  - Inability to perform historical analysis, backtesting, or compliance auditing.
  - Recovery from failure requires re‑building state from scratch, potentially causing missed trades or incorrect capital allocation.
- **Evidence**: Search for `sqlite3`, `execute`, `cursor`, `connection` in the codebase yields no results (see grep output).

### 2. Unbounded In‑Process Cache
- **File**: `data/feeds/market_data.py`
- **Issue**: The `_cache` dictionary stores fetched data keyed by arbitrary strings (e.g., `f"bdi_{period}"`, `f"crypto_{exchange}_{symbol}_{timeframe}"`). While each entry has a TTL, the dictionary never removes expired entries; they are lazily ignored on next access.
- **Impact**: 
  - Memory leak if the service runs for a long time with a high variety of unique keys (e.g., during backtesting over many symbols/periods).
  - Gradual increase in RAM usage until the process is restarted.
- **Evidence**: 
  ```python
  _cache: dict[str, tuple[float, object]] = {}
  ...
  def _cached(key: str, ttl: int, fn):
      now = time.time()
      if key in _cache:
          ts, val = _cache[key]
          if now - ts < ttl:
              return val
      val = fn()
      _cache[key] = (now, val)
      return val
  ```
  No cleanup routine or size limit.

### 3. Lack of Connection Pooling / Reuse
- **Issue**: Even if database access were added, the current pattern of creating ad‑hoc connections (if using `sqlite3.connect()`) without pooling could lead to file‑locking issues under concurrent access from multiple workers.
- **Impact**: Potential database locked errors, failed writes, and degraded performance.

### 4. No Migration / Versioning Strategy
- **Issue**: The schema is static; no mechanism exists to evolve the schema over time (e.g., adding columns, indexes).
- **Impact**: Manual schema changes risk breaking the application; no automated migration path.

### 5. Inconsistent Timestamp Handling
- **Issue**: The schema uses `REAL` for timestamps (Unix epoch with fractional seconds). The codebase mixes `time.time()` (returns float) and `pd.to_datetime` (returns Timestamp objects). Inconsistent handling could lead to precision loss or bugs when storing/retrieving.
- **Impact**: Data corruption or incorrect time‑based queries.

## Recommendations
1. **Introduce a Persistence Layer**
   - Use SQLite with a connection pool (e.g., SQLAlchemy Core) or switch to Postgres for better concurrency.
   - Implement DAO classes for each table (`regime_log`, `signals`, etc.).
   - Persist signals, orders, and portfolio state at the point they are generated (e.g., when a worker emits a signal, when the hypervisor allocates capital, when an order is filled).

2. **Bound the Cache**
   - Replace the simple dict with an LRU cache (e.g., `functools.lru_cache` on the fetch functions) or use a library like `cachetools.TTLCache` that automatically evicts expired entries.
   - Set a maximum size to prevent unbounded growth.

3. **Add Connection Management**
   - If using SQLite, enable WAL mode (already set via PRAGMA) and use a connection per thread/worker or a shared pool with proper locking.
   - Consider using `apsw` or `sqlite3` with `check_same_thread=False` and a queue for writes.

4. **Implement Alembic‑style Migrations**
   - Even for SQLite, use a migration tool (e.g., `alembic` with SQLite support) to version the schema and apply changes safely.

5. **Standardize Timestamp Representation**
   - Store timestamps as INTEGER milliseconds since epoch (or REAL seconds) and convert consistently at the DAO boundary.
   - Use helper functions to convert between `datetime`, `pd.Timestamp`, and Unix float.

6. **Add Health Checks for Persistence**
   - On startup, verify database file accessibility and schema version.
   - Expose a `/db-health` endpoint that runs a simple query.

## Additional Notes
- The absence of persistence is a critical gap for a trading system that must survive restarts, provide audit trails, and support post‑trade analysis.
- Addressing these issues will significantly improve reliability, operability, and compliance readiness.