# Bug Report: Integration and Deployment Layer

## Overview
This report covers the deployment and integration aspects of the MARA (Arka-updated) system, focusing on Docker Compose, environment variables, health checks, volume mounts, and inter-service communication. Issues identified could lead to deployment failures, misconfiguration, reduced resilience, and security risks.

## Detailed Findings

### 1. Docker Compose File Uses `latest` Tags
- **File**: `docker-compose.yml`
- **Issue**: Several services use the `latest` tag (e.g., `ollama/ollama:latest`, `prom/prometheus:latest`, `grafana/grafana-oss:latest`). This leads to non-reproducible builds and potential breaking changes upon image updates.
- **Impact**: 
  - Inconsistent behavior across deployments; a working system may break after a `docker compose pull`.
  - Difficulty in rolling back to a known-good version.
- **Evidence**: 
  ```yaml
  ollama:
    image: ollama/ollama:latest
  prometheus:
    image: prom/prometheus:latest
  grafana:
    image: grafana/grafana-oss:latest
  ```

### 2. Healthchecks Rely on External Tools Not Present in Images
- **Issue**: Some healthchecks assume the presence of `curl` or `python3` in the container, which may not be guaranteed.
  - Example: `worker-polymarket` healthcheck uses `python3` (acceptable as it's based on `python:3.11-slim`).
  - However, `worker-analyst` healthcheck uses `curl`, but the analyst worker image (built from `./workers/analyst`) may not include `curl` if the Dockerfile does not install it.
  - Similarly, `worker-arbitrader` healthcheck uses `curl`; the arbitrader image (Java-based) may lack `curl`.
- **Impact**: 
  - Healthcheck failures leading to container restarts marked as unhealthy, even if the application is running.
  - False negatives in service health.
- **Evidence**: 
  ```yaml
  worker-analyst:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8003/health"]
  worker-arbitrader:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8004/health"]
  ```
  Check the respective Dockerfiles for `curl` installation.

### 3. Healthcheck Intervals May Be Too Aggressive
- **Issue**: Healthcheck intervals are set as low as 15-30 seconds. Combined with timeouts and retries, this can lead to frequent healthcheck traffic.
- **Impact**: 
  - Unnecessary load on services, especially if the healthcheck endpoint performs non-trivial work.
  - Potential to contribute to rate-limiting if the endpoint is not designed for frequent polling.
- **Evidence**: 
  ```yaml
  hypervisor:
    healthcheck:
      interval: 30s
  worker-nautilus:
    healthcheck:
      interval: 30s
  ```

### 4. Volume Mounts Expose Host Paths with Potentially Sensitive Data
- **Issue**: 
  - The hypervisor service mounts `./config:/app/config:ro` and `./data/db:/app/data/db`.
  - The `./config` directory may contain configuration files with secrets (e.g., API keys) if not properly ignored by `.gitignore` or if `.env` is sourced into config.
  - The `./data/db` directory mounts the host's SQLite file directly into the container. While useful for persistence, it exposes the file to the container and may have permission issues.
- **Impact**: 
  - If the container is compromised, an attacker could read the SQLite database or configuration files.
  - Permission mismatches between host and container (e.g., container running as non-root) may cause read/write failures.
- **Evidence**: 
  ```yaml
  hypervisor:
    volumes:
      - ./config:/app/config:ro
      - ./data/db:/app/data/db
  ```

### 5. Environment File (`.env`) Not Versioned and May Contain Secrets
- **Issue**: The `docker-compose.yml` references `env_file: .env`. This file is not in the repository (likely ignored by `.gitignore`) but may contain sensitive information such as API keys, database passwords, or Telegram bot tokens.
- **Impact**: 
  - If the `.env` file is accidentally committed or exposed, secrets are leaked.
  - Lack of a `.env.example` or documentation makes onboarding difficult.
- **Evidence**: 
  ```yaml
  hypervisor:
    env_file: .env
  ```

### 6. Lack of Resource Constraints (CPU/Memory Limits)
- **Issue**: No `deploy.resources` or `mem_limit`/`cpu_limit` settings are defined for any service.
- **Impact**: 
  - A single service (e.g., a runaway worker) could consume all available host resources, leading to starvation of other services or the host OS.
  - In shared environments, this could affect co-located workloads.
- **Evidence**: No resource constraints in the `docker-compose.yml`.

### 7. Restart Policy May Cause Repeated Failures
- **Issue**: All services use `restart: unless-stopped`. While this is generally good for availability, it can lead to a crash-loop if a service fails to start due to a configuration error, consuming resources and logging repeatedly.
- **Impact**: 
  - Increased log noise and resource usage during failed startups.
  - Delayed operator awareness if monitoring is not in place.
- **Evidence**: 
  ```yaml
  restart: unless-stopped
  ```

### 8. Dependency Conditions May Be Too Strict or Too Weak
- **Issue**: 
  - Services depend on others with conditions like `service_started` or `service_healthy`.
  - Example: `worker-analyst` depends on `ollama` with `condition: service_started`, but does not wait for the model to be loaded or ready.
  - `worker-nautilus` depends on `hypervisor` being healthy, but the hypervisor healthcheck only checks its own `/health` endpoint, not the readiness of its dependencies (e.g., market data feeds).
- **Impact**: 
  - Services may start before their true dependencies are ready, leading to errors or degraded functionality.
  - Conversely, overly strict dependencies could cause unnecessary delays.
- **Evidence**: 
  ```yaml
  worker-analyst:
    depends_on:
      ollama:
        condition: service_started
  worker-nautilus:
    depends_on:
      hypervisor:
        condition: service_healthy
  ```

### 9. Ports Exposed to Host May Cause Conflicts
- **Issue**: Services expose ports directly to the host (e.g., `8000:8000` for hypervisor, `8001:8001` for nautilus). If multiple instances of the stack are run on the same host, port conflicts will occur.
- **Impact**: 
  - Inability to run multiple MARA instances (e.g., for testing) without modifying the compose file.
  - Potential conflict with other services on the host.
- **Evidence**: 
  ```yaml
  hypervisor:
    ports:
      - "8000:8000"
  ```

### 10. Lack of Logging Drivers or Log Rotation Configuration
- **Issue**: No logging configuration is specified in the compose file. Logs will use the default driver (usually json-file) with no size limits, potentially filling the disk.
- **Impact**: 
  - Uncontrolled log growth can consume all available disk space, leading to service failures or host instability.
- **Evidence**: No `logging:` section in any service.

### 11. Network Mode Uses Default Bridge (No Custom Network Defined)
- **Issue**: The compose file does not define a custom network, so all services use the default bridge network. While functional, this provides no network isolation and uses automatic IP assignment that may change on restart.
- **Impact**: 
  - Less predictable inter-service communication (though service names work via Docker's embedded DNS).
  - No ability to control network-specific options (e.g., MTU, aliases).
- **Evidence**: No `networks:` top-level key or service-level `networks:` (implicitly uses default).

### 12. Build Context May Include Unnecessary Files
- **Issue**: The build context for services is set to the project root (`.`) for some (e.g., hypervisor) or relative paths (e.g., `./workers/nautilus`). This means the entire repository is sent to the Docker daemon, increasing build time and potentially including sensitive files in the image if not properly `.dockerignored`.
- **Impact**: 
  - Slower builds.
  - Risk of including secrets or large files in the image via Docker cache.
- **Evidence**: 
  ```yaml
  hypervisor:
    build:
      context: .
  ```

### 13. Missing Healthcheck for Critical Services Like Ollama
- **Issue**: The `ollama` service has a healthcheck that uses `ollama list`, which is appropriate. However, the `worker-analyst` depends on `ollama` being started but not necessarily ready to serve models. The analyst worker may attempt to use the Ollama API before the model is loaded, leading to errors.
- **Impact**: 
  - Initialization errors or delayed readiness of the analyst worker.
- **Evidence**: As noted, the analyst worker only waits for `ollama` to start, not for it to be ready to accept inference requests.

### 14. Volume Permissions Not Explicitly Set
- **Issue**: The mounted volumes (e.g., `./data/db`) may have permissions that conflict with the container user. The Dockerfiles do not show explicit user creation or volume permission adjustments.
- **Impact**: 
  - Container may fail to start or write to volumes due to permission denied errors.
  - Requires manual adjustment of host directory permissions.
- **Evidence**: No `user:` directive in service definitions; Dockerfiles would need to be checked.

### 15. Lack of Docker Compose Version Specification
- **Issue**: The compose file does not specify a version (e.g., `version: '3.8'`). While recent Docker Compose defaults to a sensible version, it is best practice to be explicit.
- **Impact**: 
  - Potential confusion or unexpected behavior if the Compose implementation changes default versions.
- **Evidence**: No version line at the top of the file.

## Recommendations
1. **Pin Image Versions**
   - Replace `latest` with specific tags (e.g., `ollama/ollama:0.1.34`) or use digest-based references for immutability.
   - Maintain a separate `docker-compose.prod.yml` or use environment variables for tags.

2. **Ensure Healthchecks Use Available Tools**
   - Verify that each container's image includes the tools used in its healthcheck (e.g., add `curl` to the Dockerfile if needed).
   - Alternatively, use a simple application-level healthcheck endpoint that does not rely on external tools.

3. **Adjust Healthcheck Intervals and Timeouts**
   - Increase intervals to 60 seconds or more for low-risk services.
   - Set reasonable timeouts and retries to avoid flapping.

4. **Review Volume Mounts for Security and Permissions**
   - Consider using named volumes for persistent data (e.g., `db_data:/app/data/db`) instead of bind mounts to avoid permission issues and improve portability.
   - If bind mounts are necessary, ensure the host directory has appropriate permissions (e.g., `chown` to match container UID/GID).
   - For configuration, consider using Docker secrets or environment variables instead of mounting files, especially for secrets.

5. **Manage `.env` File Securely**
   - Provide a `.env.example` with non-sensitive placeholders.
   - Ensure `.env` is in `.gitignore`.
   - Consider using a secrets management solution (e.g., Docker Swarm secrets, HashiCorp Vault, or cloud provider secrets) for production.

6. **Add Resource Constraints**
   - Define `mem_limit` and `cpu_limit` for each service based on profiling.
   - Example: `mem_limit: 512m`, `cpus: "1.0"`.

7. **Implement a Circuit Breaker for Restarts**
   - Use a restart policy that limits the frequency of restarts (e.g., `restart: on-failure` with a maximum retry count) or rely on external monitoring to alert on crash loops.

8. **Refine Dependency Conditions**
   - For services that need more than just a started dependency (e.g., Ollama model readiness), consider using a script or init container that waits for the true readiness signal.
   - Alternatively, implement retry logic in the dependent service.

9. **Avoid Exposing Ports to Host When Not Necessary**
   - If inter-service communication is sufficient (via Docker network), remove `ports:` mappings and rely on the internal network.
   - If external access is needed (e.g., for Grafana or Prometheus), consider using a reverse proxy with authentication.

10. **Configure Logging Drivers**
    - Set `logging:` options to limit log size (e.g., `max-size: 10m`, `max-file: "3"`).
    - Example:
      ```yaml
      logging:
        driver: "json-file"
        options:
          max-size: "10m"
          max-file: "3"
      ```

11. **Define a Custom Network**
    - Create a dedicated network for the stack to improve clarity and control.
    - Example:
      ```yaml
      networks:
        mara-net:
          driver: bridge
      ```
      Then attach each service to `mara-net`.

12. **Use `.dockerignore` Files**
    - Create a `.dockerignore` in the build context to exclude unnecessary files (e.g., `.git`, `*.md`, `logs/`).
    - This reduces build context size and avoids including secrets.

13. **Add Readiness Checks for Dependent Services**
    - For the analyst worker, implement a loop that checks the Ollama API for model readiness before starting the main application.
    - Similarly, ensure the hypervisor's healthcheck can reflect downstream dependency health if desired.

14. **Explicitly Set Container User**
    - Define a non-root user in the Dockerfile and use `user:` in the service definition to run containers with least privilege.
    - Ensure volumes are accessible by that user.

15. **Specify Docker Compose Version**
    - Add `version: '3.8'` (or higher) at the top of the compose file to ensure compatibility and feature set.

## Additional Notes
Many of these issues are typical in development-oriented compose files. For production, consider adopting a more robust orchestration platform (e.g., Kubernetes) or at least applying the above recommendations to harden the deployment.

Addressing these points will improve the reliability, security, and maintainability of the MARA system in production environments.