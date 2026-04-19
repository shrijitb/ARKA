"""
hypervisor/di_container.py

Lightweight dependency injection container used by tests to swap real
dependencies with fakes/mocks without patching module globals.

Usage:
    from hypervisor.di_container import DIContainer

    container = DIContainer()
    container.register('state', HypervisorState())
    container.register('classifier', fake_classifier)

    dep = container.get('state')

Decision (COVER-06, 2026-04-16): The duplicate `Hypervisor` class that
mirrored `hypervisor/main.py` orchestration_loop was dead code — it was
never wired into production use and diverged silently from main.py with
each fix. Deleted. Tests use `hypervisor.main` directly via importlib
or TestClient, which exercises real production code paths.
"""

from __future__ import annotations

from typing import Any, Dict, TypeVar

T = TypeVar('T')


class DIContainer:
    """Simple dependency injection container."""

    def __init__(self):
        self._registry: Dict[str, Any] = {}
        self._factories: Dict[str, Any] = {}

    def register(self, key: str, instance: Any) -> None:
        """Register a singleton instance."""
        self._registry[key] = instance

    def register_factory(self, key: str, factory: Any) -> None:
        """Register a factory function that creates instances on first access."""
        self._factories[key] = factory

    def get(self, key: str, default: Any = None) -> Any:
        """Return the registered instance, creating it via factory if needed."""
        if key in self._registry:
            return self._registry[key]
        if key in self._factories:
            instance = self._factories[key]()
            self._registry[key] = instance
            return instance
        if default is not None:
            return default
        raise KeyError(f"Dependency '{key}' not registered")

    def get_or_create(self, key: str, factory: Any) -> Any:
        """Return the registered instance or create and register it."""
        try:
            return self.get(key)
        except KeyError:
            instance = factory()
            self.register(key, instance)
            return instance

    def clear(self) -> None:
        """Remove all registered instances and factories."""
        self._registry.clear()
        self._factories.clear()
