"""
Tests for the circuit-breaker / failure-backoff strategy (Phase 5.2).

Covers:
  - State transitions: closed → open → half_open → closed
  - Failure threshold triggers trip
  - Exponential backoff cooldown calculation
  - Half-open probe success → close
  - Half-open probe failure → reopen with increased backoff
  - Reset clears all state
  - Snapshot returns correct JSON-friendly dict
  - Metrics emitted on trip / close events
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState
from app.observability import metrics


@pytest.fixture
def cb() -> CircuitBreaker:
    """Fresh circuit breaker with fast cooldown for tests."""
    return CircuitBreaker(
        role="test",
        config=CircuitBreakerConfig(
            failure_threshold=2,
            base_cooldown_s=0.05,  # 50ms — fast enough for tests
            max_cooldown_s=1.0,
            backoff_multiplier=2.0,
            success_threshold=1,
        ),
    )


class TestCircuitBreakerStates:

    def test_starts_closed(self, cb: CircuitBreaker):
        assert cb.state == CircuitState.closed
        assert cb.should_allow_request() is True

    def test_single_failure_stays_closed(self, cb: CircuitBreaker):
        cb.record_failure()
        assert cb.state == CircuitState.closed
        assert cb.should_allow_request() is True

    def test_threshold_failures_trip_to_open(self, cb: CircuitBreaker):
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.open
        assert cb.should_allow_request() is False

    def test_success_resets_failure_count(self, cb: CircuitBreaker):
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        # Only 1 consecutive failure — still closed
        assert cb.state == CircuitState.closed

    def test_open_transitions_to_half_open_after_cooldown(self, cb: CircuitBreaker):
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.open

        # Wait for cooldown
        time.sleep(0.07)
        assert cb.state == CircuitState.half_open
        assert cb.should_allow_request() is True

    def test_half_open_success_closes(self, cb: CircuitBreaker):
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.07)
        assert cb.state == CircuitState.half_open

        cb.record_success()
        assert cb.state == CircuitState.closed

    def test_half_open_failure_reopens(self, cb: CircuitBreaker):
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.07)
        assert cb.state == CircuitState.half_open

        cb.record_failure()
        assert cb.state == CircuitState.open
        # Second trip — should_allow is False
        assert cb.should_allow_request() is False


class TestExponentialBackoff:

    def test_first_trip_uses_base_cooldown(self, cb: CircuitBreaker):
        cb.record_failure()
        cb.record_failure()
        # Access raw internal value (snapshot rounds to 1 decimal)
        assert cb._current_cooldown_s == pytest.approx(0.05, abs=0.001)
        assert cb.snapshot()["trip_count"] == 1

    def test_second_trip_doubles_cooldown(self, cb: CircuitBreaker):
        # First trip
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.07)
        # Half-open → probe failure → second trip
        cb.record_failure()
        assert cb._current_cooldown_s == pytest.approx(0.1, abs=0.01)
        assert cb.snapshot()["trip_count"] == 2

    def test_cooldown_capped_at_max(self):
        cb = CircuitBreaker(
            role="test",
            config=CircuitBreakerConfig(
                failure_threshold=1,
                base_cooldown_s=0.5,
                max_cooldown_s=1.0,
                backoff_multiplier=10.0,
                success_threshold=1,
            ),
        )
        # Trip 1: 0.5s
        cb.record_failure()
        assert cb.snapshot()["current_cooldown_s"] == pytest.approx(0.5, abs=0.05)
        # Wait and trip again → would be 5.0s but capped at 1.0
        with patch("time.monotonic", return_value=time.monotonic() + 10):
            _ = cb.state  # trigger half_open
        cb.record_failure()
        assert cb.snapshot()["current_cooldown_s"] == pytest.approx(1.0, abs=0.05)


class TestSnapshot:

    def test_snapshot_keys(self, cb: CircuitBreaker):
        snap = cb.snapshot()
        expected_keys = {"role", "state", "failure_count", "trip_count",
                         "current_cooldown_s", "seconds_until_probe"}
        assert set(snap.keys()) == expected_keys

    def test_snapshot_initial_values(self, cb: CircuitBreaker):
        snap = cb.snapshot()
        assert snap["role"] == "test"
        assert snap["state"] == "closed"
        assert snap["failure_count"] == 0
        assert snap["trip_count"] == 0
        assert snap["seconds_until_probe"] == 0.0


class TestReset:

    def test_reset_clears_tripped_state(self, cb: CircuitBreaker):
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.open
        cb.reset()
        assert cb.state == CircuitState.closed
        assert cb.snapshot()["failure_count"] == 0
        assert cb.snapshot()["trip_count"] == 0


class TestCircuitBreakerMetrics:

    def test_trip_emits_metric(self, cb: CircuitBreaker):
        metrics.reset()
        cb.record_failure()
        cb.record_failure()
        assert metrics.get_counter("cb_test_tripped") == 1

    def test_close_emits_metric(self, cb: CircuitBreaker):
        metrics.reset()
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.07)
        # Must read .state to trigger open → half_open transition
        assert cb.state == CircuitState.half_open
        cb.record_success()
        assert metrics.get_counter("cb_test_closed") == 1
