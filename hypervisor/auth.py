"""
hypervisor/auth.py

Lightweight Bearer-token authentication for all hypervisor endpoints.

Key lifecycle:
  1. On startup, get_or_create_api_key() checks ARKA_API_KEY in env.
  2. If absent, a cryptographically random 32-byte token is generated,
     written to .env (append-only), and stored in os.environ.
  3. APIKeyMiddleware rejects any request without the matching Bearer token,
     except for paths listed in EXEMPT_PATHS.

Dashboard integration:
  - /setup/status (exempt) returns the api_key field so the dashboard can
    read it on first load during the setup wizard and store it in localStorage.
  - All subsequent dashboard requests include  Authorization: Bearer <key>.

Internal health checks (Docker healthcheck, Prometheus scrape) must use
/health or /metrics which are also exempt.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Checks Authorization: Bearer <key> on every request except EXEMPT_PATHS.
    Returns HTTP 401 on missing or wrong key.
    """

    # Paths that skip auth — health probes, setup wizard initial calls
    EXEMPT_PATHS: frozenset[str] = frozenset({
        "/health",
        "/metrics",
        "/setup/status",
        "/system/hardware",
    })

    def __init__(self, app, api_key: str) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not (auth.startswith("Bearer ") and auth[7:] == self._api_key):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

        return await call_next(request)


def get_or_create_api_key() -> str:
    """
    Return ARKA_API_KEY from environment, generating one if absent.

    The new key is appended to .env in the application working directory
    (/app inside Docker, project root in local dev) so it survives restarts.
    """
    key = os.environ.get("ARKA_API_KEY", "").strip()
    if key:
        return key

    key = secrets.token_urlsafe(32)
    os.environ["ARKA_API_KEY"] = key

    # Best-effort persist to .env — failures are non-fatal (key is in env already)
    for candidate in (Path("/app/.env"), Path(".env")):
        if candidate.exists():
            try:
                with candidate.open("a") as fh:
                    fh.write(f"\nARKA_API_KEY={key}\n")
                logger.info("Generated ARKA_API_KEY and appended to %s", candidate)
            except OSError as exc:
                logger.warning("Could not write ARKA_API_KEY to %s: %s", candidate, exc)
            break
    else:
        logger.warning(
            "Generated ARKA_API_KEY but no .env found to persist it. "
            "Set ARKA_API_KEY=%s in your environment.", key
        )

    return key
