"""
hypervisor/di_container.py

Dependency Injection container for the Arka Hypervisor.

This module provides a simple DI container that can be used to inject
dependencies into the Hypervisor for better testability and modularity.

Usage:
    from hypervisor.di_container import DIContainer
    from hypervisor.main import HypervisorState, RegimeClassifier, etc.
    
    # Create container with real dependencies
    container = DIContainer()
    container.register('state', HypervisorState())
    container.register('classifier', RegimeClassifier())
    container.register('allocator', RegimeAllocator(total_capital=200.0))
    container.register('risk_manager', RiskManager(initial_capital=200.0))
    container.register('repository', ArkaRepository(async_session))
    
    # Create hypervisor with injected dependencies
    hypervisor = Hypervisor(container)
    
    # For testing, inject mock dependencies
    mock_state = AsyncMock(spec=HypervisorState)
    container.register('state', mock_state)
    hypervisor = Hypervisor(container)
"""

from __future__ import annotations

from typing import Any, Dict, Type, TypeVar, Union

T = TypeVar('T')


class DIContainer:
    """Simple dependency injection container."""
    
    def __init__(self):
        self._registry: Dict[str, Any] = {}
        self._factories: Dict[str, callable] = {}
    
    def register(self, key: str, instance: Any) -> None:
        """Register a singleton instance."""
        self._registry[key] = instance
    
    def register_factory(self, key: str, factory: callable) -> None:
        """Register a factory function that creates instances."""
        self._factories[key] = factory
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get an instance by key."""
        if key in self._registry:
            return self._registry[key]
        if key in self._factories:
            # Create instance using factory
            instance = self._factories[key]()
            self._registry[key] = instance
            return instance
        if default is not None:
            return default
        raise KeyError(f"Dependency '{key}' not registered")
    
    def get_or_create(self, key: str, factory: callable) -> Any:
        """Get an instance or create it using the factory if not registered."""
        try:
            return self.get(key)
        except KeyError:
            instance = factory()
            self.register(key, instance)
            return instance
    
    def clear(self) -> None:
        """Clear all registered instances and factories."""
        self._registry.clear()
        self._factories.clear()


class Hypervisor:
    """Refactored Hypervisor that uses dependency injection."""
    
    def __init__(self, container: DIContainer):
        self.container = container
        
        # Inject dependencies
        self.state      = self.container.get('state')
        self.classifier = self.container.get('classifier')
        self.allocator  = self.container.get('allocator')
        self.risk_mgr   = self.container.get('risk_manager')
        self.repo       = self.container.get('repository')
        
        # Configuration
        self.initial_capital_usd = float(os.environ.get("INITIAL_CAPITAL_USD", 200.0))
        self.cycle_interval_sec  = int(os.environ.get("CYCLE_INTERVAL_SEC", 3600))
        self.worker_timeout_sec  = int(os.environ.get("WORKER_TIMEOUT_SEC", 10))
        self.min_trade_size_usd  = float(os.environ.get("MIN_TRADE_SIZE_USD", 10.0))
        self.paper_trading       = os.environ.get("ARKA_LIVE", "false").lower() != "true"
        
        # Worker registry
        self.worker_registry: Dict[str, str] = {
            "nautilus":             os.environ.get("NAUTILUS_URL",             "http://worker-nautilus:8001"),
            "prediction_markets":   os.environ.get("PREDICTION_MARKETS_URL",  "http://worker-prediction-markets:8002"),
            "analyst":              os.environ.get("ANALYST_URL",              "http://worker-analyst:8003"),
            "core_dividends":       os.environ.get("CORE_DIVIDENDS_URL",       "http://worker-core-dividends:8006"),
        }
        
        # Regimes that force all directional workers to pause new entries
        self.defensive_regimes = {"CRISIS"}
    
    async def orchestration_loop(self):
        """Main orchestration loop."""
        await asyncio.sleep(5)   # Give workers time to start on first boot
        while True:
            cycle_start = time.time()
            self.state.cycle_count += 1
            try:
                await self._run_cycle()
            except Exception as exc:
                logger.error(f"Cycle {self.state.cycle_count} failed: {exc}", exc_info=True)
            elapsed   = time.time() - cycle_start
            sleep_for = max(0, self.cycle_interval_sec - elapsed)
            logger.info(f"Cycle {self.state.cycle_count} done in {elapsed:.1f}s. Next in {sleep_for:.0f}s.")
            await asyncio.sleep(sleep_for)
    
    async def _run_cycle(self):
        """Execute a single cycle."""
        logger.info(f"─── Hypervisor Cycle {self.state.cycle_count} ───")
        self.state.last_cycle_at = time.time()

        # Step 1: Health-check
        await self._check_worker_health()
        snap = await self.state.get_snapshot()
        healthy = [w for w, h in snap["worker_health"].items() if h]
        logger.info(f"  Healthy workers: {healthy}")

        # Step 2: Pull status
        await self._pull_worker_status(healthy)
        await self.state.reconcile_capital()

        # Step 3: Classify regime
        regime_probs_array = None
        try:
            result = await asyncio.to_thread(self.classifier.classify_sync)
            await self.state.update_regime(
                result.regime.value,
                result.confidence,
                result.probabilities,
                result.circuit_breaker_active,
            )
            await self.repo.log_regime(
                result.regime.value,
                {},
                result.circuit_breaker_active,
            )
            from hypervisor.allocator.capital import HMM_STATE_LABELS
            import numpy as np
            regime_probs_array = np.array(
                [result.probabilities.get(lbl, 0.0) for lbl in HMM_STATE_LABELS],
                dtype=float,
            )
            logger.info(
                f"  Regime: {self.state.current_regime} ({self.state.regime_confidence:.0%})"
                f"  CB={self.state.circuit_breaker_active}"
            )
        except Exception as exc:
            logger.error(f"Regime classification failed: {exc} — holding {self.state.current_regime}")

        # Re-read snapshot after regime update
        snap = await self.state.get_snapshot()

        # Step 4: Risk check
        verdict = self.risk_mgr.assess(
            total_capital    = snap["total_capital"],
            free_capital     = snap["free_capital"],
            open_positions   = self._count_open_positions(),
            worker_pnl       = snap["worker_pnl"],
            worker_allocated = snap["allocations"],
        )
        async with self.state._lock:
            self.state.risk_verdict = verdict.reason

        if not verdict:
            logger.warning(f"  Risk gate FAIL: {verdict.reason}")
            async with self.state._lock:
                self.state.halted      = True
                self.state.halt_reason = verdict.reason
            if verdict.action == "halt_all":
                await self._broadcast_pause(healthy)
                return
            if verdict.action in ("halt_worker", "trim_worker") and verdict.affected_worker:
                await self._pause_worker(verdict.affected_worker)
                healthy = [w for w in healthy if w != verdict.affected_worker]
        else:
            async with self.state._lock:
                if self.state.halted:
                    logger.info("  Risk gate: CLEAR — resuming")
                    self.state.halted      = False
                    self.state.halt_reason = ""

        # Step 5: Allocate
        alloc = self.allocator.compute(
            regime          = snap["regime"],
            worker_health   = snap["worker_health"],
            worker_sharpe   = snap["worker_sharpe"],
            registered_only = healthy,
            probabilities   = regime_probs_array,
        )
        await self.state.update_allocations(alloc.allocations)
        logger.info(f"  {alloc.summary()}")

        # Step 6: Broadcast regime
        await self._broadcast_regime(healthy, snap["regime"], snap["regime_confidence"])

        # Step 7: Send allocations
        await self._send_allocations(alloc.allocations)

        # Step 8: Persist portfolio snapshot
        final_snap = await self.state.get_snapshot()
        deployed = sum(final_snap["allocations"].values())
        total = final_snap["total_capital"]
        await self.repo.snapshot_portfolio(
            total_value=total,
            cash_pct=(total - deployed) / total if total > 0 else 1.0,
            drawdown_pct=self.risk_mgr.summary(total, final_snap["free_capital"]).get("drawdown_pct", 0.0),
            regime=final_snap["regime"],
            allocations=final_snap["allocations"],
        )

        # Step 9: Resume workers that have allocation
        if self.state.current_regime not in self.defensive_regimes:
            for worker in healthy:
                if alloc.allocations.get(worker, 0) > 0:
                    await self._resume_worker(worker)
    
    # Worker communication methods would be refactored to use injected dependencies
    # For brevity, showing just one example:
    
    async def _check_worker_health(self):
        """Ping every registered worker /health endpoint concurrently."""
        workers = list(self.worker_registry.keys())
        urls    = [self.worker_registry[w] for w in workers]
        async with httpx.AsyncClient(timeout=self.worker_timeout_sec) as client:
            results = await asyncio.gather(
                *[client.get(f"{url}/health") for url in urls],
                return_exceptions=True,
            )
        for worker, result in zip(workers, results):
            if isinstance(result, Exception):
                await self.state.update_worker_health(worker, False)
                logger.warning(f"  {worker}: health check failed ({type(result).__name__}: {result})")
            else:
                ok = result.status_code == 200
                await self.state.update_worker_health(worker, ok)
                if not ok:
                    logger.warning(f"  {worker}: health returned HTTP {result.status_code}")


# Factory functions for creating dependencies
def create_di_container() -> DIContainer:
    """Create a DI container with real dependencies."""
    container = DIContainer()
    
    # Register state
    container.register('state', HypervisorState())
    
    # Register services
    container.register('classifier', RegimeClassifier())
    container.register('allocator', RegimeAllocator(total_capital=200.0))
    container.register('risk_manager', RiskManager(initial_capital=200.0))
    container.register('repository', ArkaRepository(async_session))
    
    return container


def create_test_container() -> DIContainer:
    """Create a DI container with mock dependencies for testing."""
    container = DIContainer()
    
    # Register mock state
    from unittest.mock import AsyncMock
    mock_state = AsyncMock()
    mock_state.cycle_count = 0
    mock_state.last_cycle_at = 0.0
    mock_state.started_at = time.time()
    mock_state.halted = False
    mock_state.halt_reason = ""
    mock_state.risk_verdict = "OK"
    mock_state.allocations = {}
    mock_state.regime_probabilities = {}
    mock_state.circuit_breaker_active = False
    mock_state.watchlist = []
    
    # Add async methods
    async def mock_update_worker_pnl(worker, pnl):
        pass
    async def mock_update_worker_sharpe(worker, sharpe):
        pass
    async def mock_update_worker_health(worker, healthy):
        pass
    async def mock_update_allocations(allocs):
        pass
    async def mock_update_regime(regime, confidence, probs, circuit_breaker):
        pass
    async def mock_get_snapshot():
        return {
            "worker_health": {},
            "worker_pnl": {},
            "worker_sharpe": {},
            "allocations": {},
            "regime": "RISK_ON",
            "regime_confidence": 0.8,
            "regime_probs": {},
            "circuit_breaker": False,
            "total_capital": 200.0,
            "free_capital": 200.0,
            "halted": False,
            "halt_reason": "",
            "risk_verdict": "OK",
        }
    async def mock_reconcile_capital():
        pass
    
    mock_state.update_worker_pnl = mock_update_worker_pnl
    mock_state.update_worker_sharpe = mock_update_worker_sharpe
    mock_state.update_worker_health = mock_update_worker_health
    mock_state.update_allocations = mock_update_allocations
    mock_state.update_regime = mock_update_regime
    mock_state.get_snapshot = mock_get_snapshot
    mock_state.reconcile_capital = mock_reconcile_capital
    
    container.register('state', mock_state)
    
    # Register mock services
    mock_classifier = AsyncMock()
    mock_allocator = AsyncMock()
    mock_risk_manager = AsyncMock()
    mock_repository = AsyncMock()
    
    container.register('classifier', mock_classifier)
    container.register('allocator', mock_allocator)
    container.register('risk_manager', mock_risk_manager)
    container.register('repository', mock_repository)
    
    return container


# Backward compatibility - create a global container for existing code
_global_container = create_di_container()


def get_global_container() -> DIContainer:
    """Get the global DI container."""
    return _global_container


def get_hypervisor() -> Hypervisor:
    """Get a hypervisor instance using the global container."""
    return Hypervisor(_global_container)