# Bug Report: API Layer (Hypervisor REST Endpoints)

## Overview
The MARA Hypervisor exposes a FastAPI-based REST API for monitoring, control, and telemetry. While useful for introspection and operational tooling, the API lacks essential security measures, input validation, and rate limiting, making it unsuitable for exposure beyond a trusted localhost or tightly controlled network.

## Detailed Findings

### 1. Absence of Authentication and Authorization
- **Files**: `hypervisor/main.py` (all `@app.*` endpoints)
- **Issue**: No authentication mechanism is in place. Any entity that can reach the hypervisor's port (default 8000) can:
  - Read sensitive state: `/status`, `/workers`, `/risk`, `/execution-risk`, `/thesis`, `/edgar-alerts`.
  - Mutate system state: `/halt`, `/resume`, `/workers/{worker}/pause`, `/workers/{worker}/resume`, `/watchlist` (POST), and even trigger a manual regime override indirectly via the classifier if exposed (though not directly, the classifier override is internal; still, state mutation is possible).
  - Potentially cause denial‑of‑service by spamming endpoints.
- **Impact**: 
  - Information leakage of PnL, allocations, regime, and internal signals.
  - Unauthorized control over trading operations (halt/resume workers, adjust watchlist).
  - If the hypervisor is bound to `0.0.0.0` (as typical in Docker Compose), any container on the same network—or worse, if the port is accidentally published—could compromise the system.
- **Evidence**: Endpoints such as:
  ```python
  @app.get("/status")
  async def status(): ...
  @app.post("/halt")
  async def manual_halt(): ...
  @app.post("/workers/{worker}/pause")
  async def pause_worker(worker: str): ...
  ```
  contain no `Depends(auth)` or similar security dependency.

### 2. Missing Rate Limiting
- **Issue**: No rate limiting on any endpoint. An attacker (or misbehaving script) could flood the hypervisor with requests, consuming CPU and network resources, potentially delaying the orchestration loop or causing timeouts in worker communications.
- **Impact**: 
  - Degraded orchestration cycle timing, leading to stale regime data or missed allocation windows.
  - Potential to exhaust worker HTTP clients if the hypervisor's outbound calls are delayed due to inbound request handling (though async, excessive inbound load still impacts the event loop).
- **Evidence**: No use of `fastapi_limiter`, `slowapi`, or custom middleware to limit requests per IP.

### 3. Insufficient Input Validation and Sanitization
- **Files**: 
  - `/watchlist` (POST) expects JSON with `ticker` field.
  - Other endpoints with path or query parameters (e.g., `/workers/{worker}/pause`).
- **Issue**: 
  - The `ticker` is only uppercased and stripped; no validation against a reasonable format (e.g., max length, allowed characters). An excessively long ticker could cause issues in downstream logging or EDGAR scans.
  - Path parameter `worker` is not validated against `WORKER_REGISTRY` keys until inside the function, which raises a 404 if unknown—acceptable—but could still be abused for enumeration.
  - No validation of JSON body size; a large payload could cause memory issues.
- **Impact**: 
  - Potential for log injection if ticker contains newlines or control characters (though logging uses `.info` with f‑strings, which may still be safe but could clutter logs).
  - Wasted resources processing nonsense requests.
- **Evidence**:
  ```python
  @app.post("/watchlist")
  async def add_to_watchlist(body: dict):
      ticker = body.get("ticker", "").upper().strip()
      if not ticker:
          raise HTTPException(status_code=400, detail="ticker required")
      ...
  ```
  No length or character set checks.

### 4. Information Exposure via Debug Endpoints
- **Issue**: Endpoints like `/execution-risk`, `/thesis`, `/edgar-alerts` expose internal algorithmic state and signals that could reveal the hypervisor's strategy or worker behavior to an observer.
- **Impact**: 
  - Competitors or malicious actors could infer trading signals, regime confidence, or execution risk thresholds.
  - While the system is presumed to run in a trusted environment, defense‑in-depth dictates that internal telemetry should be protected or obfuscated in production.
- **Evidence**: No special handling; these endpoints return raw dicts.

### 5. Lack of HTTPS/TLS Encryption
- **Issue**: The FastAPI server runs over plain HTTP. If the hypervisor is ever exposed beyond a trusted localhost (e.g., for remote monitoring), credentials, tokens, and sensitive data would be transmitted in clear text.
- **Impact**: 
  - Man‑in‑the‑middle attacks could capture PnL data, regime, or even inject malicious payloads if any endpoint ever accepted modifying commands (which they do).
- **Evidence**: No SSL context configuration in the `uvicorn` run command (seen in docs: `uvicorn hypervisor.main:app --host 0.0.0.0 --port 8000`).

### 6. No Audit Logging for State‑Changing Endpoints
- **Issue**: Endpoints that modify state (`/halt`, `/resume`, `/workers/*/pause`, `/workers/*/resume`, `/watchlist`) log only at `INFO` level (if at all) but do not record who invoked the action or when, beyond the implicit request log.
- **Impact**: 
  - In a shared environment, it is impossible to trace which user or service triggered a halt or resume, hindering incident response.
- **Evidence**: Logging statements like `logger.warning("MANUAL HALT triggered via API")` do not include requestor identity.

### 7. Potential for Replay Attacks (if tokens ever added)
- **Issue**: Should authentication be added in the future, endpoints lack nonce or timestamp validation, making them vulnerable to replay attacks if not using state‑safe protocols (e.g., JWT with short expiry).
- **Impact**: Could allow an attacker to re‑use a captured valid token to repeat actions.

### 8. Missing API Versioning
- **Issue**: All endpoints are under the root path with no version prefix (e.g., `/api/v1/`). This makes it difficult to introduce breaking changes without disrupting existing clients.
- **Impact**: 
  - Forward‑compatibility concerns; any change to endpoint structure or response format is a breaking change.
- **Evidence**: Routes like `@app.get("/status")` have no version.

### 9. Inconsistent Use of Pydantic Models for Request/Response Validation
- **Issue**: While some endpoints implicitly rely on Pydantic via FastAPI's automatic parsing (e.g., `body: dict`), many do not define explicit request or response models, leading to undocumented and unverified contracts.
- **Impact**: 
  - Clients may send malformed JSON that still passes (e.g., extra fields) or miss required fields, relying on manual checks.
  - No automatic generation of OpenAPI schema with detailed field descriptions (though FastAPI does generate schema from function signatures, the lack of models reduces clarity).
- **Evidence**: 
  ```python
  @app.post("/halt")
  async def manual_halt(): ...  # no body, but could be extended
  @app.post("/workers/{worker}/pause")
  async def pause_worker(worker: str): ...  # path param only
  ```
  For POST `/watchlist`, `body: dict` is used instead of a Pydantic model.

### 10. Lack of Custom Exception Handlers for Consistent Error Responses
- **Issue**: The application relies on FastAPI's default exception handling. While functional, there is no centralized error formatting (e.g., always returning JSON with `error` and `message` fields).
- **Impact**: 
  - Inconsistent error payloads may complicate client error handling.
- **Evidence**: No `@app.exception_handler` definitions.

## Recommendations
1. **Add Authentication and Authorization**
   - Implement a simple API key or bearer token check via a FastAPI dependency.
   - For higher security, consider mutual TLS (mTLS) if the hypervisor is only ever accessed by specific services.
   - Define roles (e.g., `viewer`, `operator`) and restrict endpoints accordingly (e.g., `/halt` only for operators).

2. **Introduce Rate Limiting**
   - Use a middleware like `slowapi` or `fastapi-limiter` to limit requests per IP (e.g., 10 requests/second) with stricter limits on mutation endpoints.

3. **Enforce Input Validation with Pydantic Models**
   - Create Pydantic models for each request body (e.g., `WatchlistAdd` with `ticker: str = Field(max_length=10, regex=r'^[A-Z0-9.\-]+$')`).
   - Use `Path` and `Query` validators for path and query parameters.
   - Set maximum JSON body size via `starlette.requests.Request.client_max_size` or middleware.

4. **Secure Telemetry Endpoints**
   - Either protect sensitive endpoints (`/execution-risk`, `/thesis`, `/edgar-alerts`) behind the same authentication, or consider removing them from external exposure in favor of internal metrics (Prometheus) which can be scraped privately.

5. **Enable HTTPS/TLS**
   - Terminate TLS at a reverse proxy (e.g., NGINX, Traefik) in front of the hypervisor, or configure Uvicorn with SSL certificates.
   - Ensure that internal service‑to‑service communication (if ever cross‑host) also uses TLS.

6. **Implement Audit Logging for Mutating Endpoints**
   - Log the authenticated user (or API key ID), timestamp, endpoint, and outcome for all state‑changing requests.
   - Consider sending audit logs to a separate secure system.

7. **Add API Versioning**
   - Prefix all routes with `/api/v1/` (or similar) to allow future evolution.
   - Example: `@app.get("/api/v1/status")`.

8. **Standardize Error Responses**
   - Add exception handlers that return a consistent JSON structure: `{"error": "error_type", "message": "human-readable message", "details": Optional[dict]}`.

9. **Review and Harden Dependency Exposure**
   - Ensure that the hypervisor's port is not inadvertently published to the public internet in Docker Compose or Kubernetes manifests.
   - Use network policies or firewall rules to restrict access to trusted subnets.

10. **Write Security Tests**
    - Develop test cases that attempt to access endpoints without authentication, flood with requests, send malformed inputs, and verify that appropriate errors are returned.

## Additional Notes
The API layer currently serves as a convenient introspection tool for developers and operators. By layering on authentication, rate limiting, and validation, it can become a secure operational interface suitable for production use while preserving its utility.
