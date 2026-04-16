"""
hypervisor/errors.py

Arka exception hierarchy. Raise these instead of bare Exception so the
main loop can distinguish recoverable from fatal failures.

Recoverable (log + continue cycle with cached/fallback data):
  ExternalAPIError, WorkerUnreachableError

Non-recoverable (halt cycle, keep previous regime):
  RegimeClassificationError

Fatal (bubble up to orchestration_loop, log, skip to next cycle):
  ArkaError (base)
"""


class ArkaError(Exception):
    """Base for all Arka system errors."""


class WorkerUnreachableError(ArkaError):
    """A worker failed its health check or HTTP call timed out."""


class ExternalAPIError(ArkaError):
    """An external data source (yfinance, FRED, GDELT, OKX, etc.) failed."""


class RiskLimitBreachedError(ArkaError):
    """The RiskManager rejected an allocation or action."""


class RegimeClassificationError(ArkaError):
    """HMM classifier failed; previous regime is held until next cycle."""


class ConfigurationError(ArkaError):
    """Missing or invalid configuration (env var, YAML file, etc.)."""
