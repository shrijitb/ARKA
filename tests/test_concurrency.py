"""
tests/test_concurrency.py

Comprehensive test suite for race conditions, circuit breaker state transitions,
and concurrent state access in the Arka Hypervisor.
"""

import asyncio
import time
from typing import List

import pytest

from hypervisor.circuit_breaker import CircuitBreaker, CircuitState
from hypervisor.main import HypervisorState


class TestCircuitBreaker:
    """Test circuit breaker state transitions and concurrent access."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_state_transitions(self):
        """Test that circuit breaker state transitions work correctly."""
        cb = CircuitBreaker("test", failure_threshold=2, cooldown_seconds=1)
        
        # Initial state should be CLOSED
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute() is True
        
        # First failure - still CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute() is True
        
        # Second failure - should trip to OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.can_execute() is False
        
        # Wait for cooldown
        await asyncio.sleep(1.1)
        
        # After cooldown, should be HALF_OPEN
        assert cb.can_execute() is True
        assert cb.state == CircuitState.HALF_OPEN
        
        # Success should return to CLOSED
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute() is True

    @pytest.mark.asyncio
    async def test_circuit_breaker_concurrent_failures(self):
        """Test that concurrent failures are handled correctly."""
        cb = CircuitBreaker("test", failure_threshold=3, cooldown_seconds=1)
        
        async def fail_fast():
            for _ in range(3):
                cb.record_failure()
                await asyncio.sleep(0.01)
        
        # Run multiple concurrent failure recorders
        await asyncio.gather(fail_fast(), fail_fast(), fail_fast())
        
        # Should be OPEN after 3 failures
        assert cb.state == CircuitState.OPEN
        assert cb._failure_count >= 3

    @pytest.mark.asyncio
    async def test_circuit_breaker_execute_method(self):
        """Test the execute method with success and failure scenarios."""
        cb = CircuitBreaker("test", failure_threshold=2, cooldown_seconds=1)
        
        # Test successful execution
        result = await cb.execute(lambda: "success")
        assert result == "success"
        assert cb.state == CircuitState.CLOSED
        
        # Test failed execution
        with pytest.raises(RuntimeError):
            await cb.execute(lambda: 1/0)
        
        assert cb.state == CircuitState.CLOSED  # First failure, still closed
        
        # Second failure should open circuit
        with pytest.raises(RuntimeError):
            await cb.execute(lambda: 1/0)
        
        assert cb.state == CircuitState.OPEN
        
        # After cooldown, should allow one test request
        await asyncio.sleep(1.1)
        assert cb.can_execute() is True
        
        # Test request should fail and keep circuit open
        with pytest.raises(RuntimeError):
            await cb.execute(lambda: 1/0)
        
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_circuit_breaker_fallback(self):
        """Test that fallback values are returned when circuit is open."""
        cb = CircuitBreaker("test", failure_threshold=1, cooldown_seconds=1)
        
        # Open the circuit
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        
        # Should return fallback when circuit is open
        result = await cb.execute(lambda: "should_not_run", fallback="fallback_value")
        assert result == "fallback_value"
        
        # Should return cached value when circuit is open
        cb.set_cached_value("cached_value")
        result = await cb.execute(lambda: "should_not_run")
        assert result == "cached_value"


class TestHypervisorStateConcurrency:
    """Test concurrent access to HypervisorState."""

    @pytest.mark.asyncio
    async def test_concurrent_pnl_updates(self):
        """100 concurrent updates must not lose any write."""
        state = HypervisorState()
        
        async def update(i):
            await state.update_worker_pnl(f"worker_{i % 4}", float(i))
        
        await asyncio.gather(*[update(i) for i in range(100)])
        
        snapshot = await state.get_snapshot()
        # Last write per worker wins, but no KeyError or corruption
        assert len(snapshot["worker_pnl"]) == 4
        
        # Verify that the last update for each worker was applied
        for i in range(4):
            worker_name = f"worker_{i}"
            expected_value = 96 + i  # Last update for each worker
            assert snapshot["worker_pnl"][worker_name] == expected_value

    @pytest.mark.asyncio
    async def test_snapshot_during_allocation_update(self):
        """Snapshot must return consistent state even during writes."""
        state = HypervisorState()
        results = []

        async def writer():
            for i in range(50):
                await state.update_allocations({
                    "nautilus": 0.4 + i * 0.001,
                    "prediction_markets": 0.2,
                })
                await asyncio.sleep(0.001)

        async def reader():
            for _ in range(50):
                snap = await state.get_snapshot()
                allocs = snap["allocations"]
                # Must never see a partial dict (e.g., nautilus updated but not prediction_markets)
                if allocs:
                    assert "nautilus" in allocs
                    assert "prediction_markets" in allocs
                results.append(snap)
                await asyncio.sleep(0.001)

        await asyncio.gather(writer(), reader())
        assert len(results) == 50

    @pytest.mark.asyncio
    async def test_concurrent_regime_updates(self):
        """Test concurrent regime updates with proper locking."""
        state = HypervisorState()
        
        async def update_regime(i):
            await state.update_regime(
                regime=f"REGIME_{i % 3}",
                confidence=0.8 + (i * 0.01),
                probs={"RISK_ON": 0.5, "RISK_OFF": 0.3, "CRISIS": 0.2},
                circuit_breaker=False,
            )
        
        # Run concurrent regime updates
        await asyncio.gather(*[update_regime(i) for i in range(20)])
        
        snapshot = await state.get_snapshot()
        # Should have valid regime data
        assert snapshot["regime"] in ["REGIME_0", "REGIME_1", "REGIME_2"]
        assert 0.8 <= snapshot["regime_confidence"] <= 1.0
        assert len(snapshot["regime_probs"]) == 3

    @pytest.mark.asyncio
    async def test_concurrent_health_and_status_updates(self):
        """Test concurrent health and status updates."""
        state = HypervisorState()
        
        async def update_health(worker_id):
            for i in range(10):
                await state.update_worker_health(f"worker_{worker_id}", i % 2 == 0)
                await asyncio.sleep(0.001)
        
        async def update_sharpe(worker_id):
            for i in range(10):
                await state.update_worker_sharpe(f"worker_{worker_id}", float(i) / 10)
                await asyncio.sleep(0.001)
        
        # Run concurrent updates
        tasks = []
        for i in range(4):
            tasks.append(update_health(i))
            tasks.append(update_sharpe(i))
        
        await asyncio.gather(*tasks)
        
        snapshot = await state.get_snapshot()
        assert len(snapshot["worker_health"]) == 4
        assert len(snapshot["worker_sharpe"]) == 4

    @pytest.mark.asyncio
    async def test_concurrent_capital_reconciliation(self):
        """Test that capital reconciliation works correctly under concurrent load."""
        state = HypervisorState()
        
        async def simulate_trading(worker_id):
            for i in range(20):
                # Simulate PNL updates
                pnl = float(worker_id * 10 + i)
                await state.update_worker_pnl(f"worker_{worker_id}", pnl)
                
                # Simulate allocation updates
                alloc = {f"worker_{j}": 50.0 + j * 10 for j in range(4)}
                await state.update_allocations(alloc)
                
                # Reconcile capital
                await state.reconcile_capital()
                
                await asyncio.sleep(0.001)
        
        # Run concurrent trading simulation
        await asyncio.gather(*[simulate_trading(i) for i in range(4)])
        
        snapshot = await state.get_snapshot()
        
        # Verify capital reconciliation
        total_pnl = sum(snapshot["worker_pnl"].values())
        expected_total = 200.0 + total_pnl  # Initial capital + total PNL
        assert abs(snapshot["total_capital"] - expected_total) < 0.01
        
        deployed = sum(snapshot["allocations"].values())
        assert abs(snapshot["free_capital"] - (snapshot["total_capital"] - deployed)) < 0.01


class TestIntegrationConcurrency:
    """Integration tests for concurrent operations."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_with_state_updates(self):
        """Test circuit breaker operations while state is being updated."""
        state = HypervisorState()
        cb = CircuitBreaker("test", failure_threshold=2, cooldown_seconds=1)
        
        async def state_updates():
            for i in range(50):
                await state.update_worker_pnl(f"worker_{i % 3}", float(i))
                await state.update_allocations({f"worker_{j}": 10.0 for j in range(3)})
                await asyncio.sleep(0.001)
        
        async def circuit_operations():
            for i in range(10):
                try:
                    # This should succeed initially
                    result = await cb.execute(lambda: f"success_{i}")
                    assert result == f"success_{i}"
                except RuntimeError:
                    # After failures, should use fallback
                    result = await cb.execute(lambda: "should_fail", fallback=f"fallback_{i}")
                    assert result == f"fallback_{i}"
                
                if i < 2:
                    cb.record_failure()
                else:
                    cb.record_success()
                
                await asyncio.sleep(0.01)
        
        # Run both concurrently
        await asyncio.gather(state_updates(), circuit_operations())
        
        # Verify both systems are in valid states
        snapshot = await state.get_snapshot()
        assert len(snapshot["worker_pnl"]) == 3
        assert len(snapshot["allocations"]) == 3
        
        # Circuit breaker should be in a valid state
        assert cb.state in [CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN]

    @pytest.mark.asyncio
    async def test_health_check_concurrency(self):
        """Test that health checks work correctly under concurrent load."""
        state = HypervisorState()
        
        async def health_check():
            for _ in range(100):
                snap = await state.get_snapshot()
                # Verify snapshot is consistent
                assert isinstance(snap["worker_health"], dict)
                assert isinstance(snap["worker_pnl"], dict)
                assert isinstance(snap["allocations"], dict)
                await asyncio.sleep(0.001)
        
        async def concurrent_updates():
            for i in range(100):
                await state.update_worker_health(f"worker_{i % 4}", i % 2 == 0)
                await state.update_worker_pnl(f"worker_{i % 4}", float(i))
                await state.update_allocations({f"worker_{j}": float(j * 10) for j in range(4)})
                await asyncio.sleep(0.001)
        
        # Run health checks and updates concurrently
        await asyncio.gather(health_check(), concurrent_updates())
        
        # Final verification
        final_snap = await state.get_snapshot()
        assert len(final_snap["worker_health"]) == 4
        assert len(final_snap["worker_pnl"]) == 4
        assert len(final_snap["allocations"]) == 4


class TestPerformance:
    """Performance tests for concurrent operations."""

    @pytest.mark.asyncio
    async def test_state_update_performance(self):
        """Test performance of state updates under load."""
        state = HypervisorState()
        
        start_time = time.time()
        
        # Run 1000 concurrent updates
        async def update_worker(i):
            await state.update_worker_pnl(f"worker_{i % 10}", float(i))
            await state.update_worker_health(f"worker_{i % 10}", True)
        
        await asyncio.gather(*[update_worker(i) for i in range(1000)])
        
        end_time = time.time()
        duration = end_time - start_time
        
        # Should complete in reasonable time (less than 5 seconds)
        assert duration < 5.0
        
        # Verify all updates were applied
        snapshot = await state.get_snapshot()
        assert len(snapshot["worker_pnl"]) == 10
        assert len(snapshot["worker_health"]) == 10

    @pytest.mark.asyncio
    async def test_circuit_breaker_performance(self):
        """Test performance of circuit breaker under load."""
        cb = CircuitBreaker("test", failure_threshold=10, cooldown_seconds=1)
        
        start_time = time.time()
        
        # Run 1000 concurrent operations
        async def test_operation():
            try:
                await cb.execute(lambda: "success")
            except RuntimeError:
                pass
        
        await asyncio.gather(*[test_operation() for _ in range(1000)])
        
        end_time = time.time()
        duration = end_time - start_time
        
        # Should complete in reasonable time
        assert duration < 2.0
        
        # Circuit breaker should still be functional
        assert cb.state == CircuitState.CLOSED