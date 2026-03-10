"""
Circuit-breaker / failure-backoff strategy for STT and TTS providers (Phase 5.2).

Implements a lightweight, in-memory circuit breaker per provider role:
  - **closed** (normal): requests pass through to the provider.
  - **open** (tripped): requests are immediately short-circuited to the
    fallback provider — no network call is attempted.
  - **half-open** (probe): after a cool-down period one request is allowed
    through; if it succeeds the breaker closes, otherwise it reopens.

Backoff: when the breaker trips, a configurable cool-down timer begins.
Successive trips apply exponential backoff (up to a cap) before allowing
the next half-open probe.

Design constraints:
  - Local-first, in-memory, zero external deps.
  - Thread-safe for single-worker async (FastAPI default).
  - Reuses the existing ``ProviderOutcome`` / ``ProviderErrorKind`` types.
  - Emits structured logs and metrics through existing observability infra.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.observability import StructuredLogger, metrics

_slog = StructuredLogger(__name__)


class CircuitState(str, Enum):
    """Circuit breaker states."""
    closed = "closed"
    open = "open"
    half_open = "half_open"


@dataclass
class CircuitBreakerConfig:
    """Tunable parameters for the circuit breaker."""
    failure_threshold: int = 3
    """Number of consecutive failures before tripping the breaker."""
    base_cooldown_s: float = 10.0
    """Initial cooldown (seconds) before a half-open probe."""
    max_cooldown_s: float = 120.0
    """Cap for exponential backoff cooldown."""
    backoff_multiplier: float = 2.0
    """Multiplier applied to cooldown after each successive trip."""
    success_threshold: int = 1
    """Consecutive successes in half-open state to close the breaker."""


@dataclass
class CircuitBreaker:
    """
    Per-role (STT or TTS) circuit breaker with exponential backoff.

    Usage::

        cb = CircuitBreaker(role="stt")

        if cb.should_allow_request():
            try:
                result = await provider.transcribe(...)
                cb.record_success()
            except Exception:
                cb.record_failure()
                # use fallback ...
        else:
            # short-circuit to fallback
            ...
    """

    role: str  # "stt" or "tts"
    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)

    # ── Internal state ────────────────────────────────────────
    _state: CircuitState = field(default=CircuitState.closed, init=False)
    _failure_count: int = field(default=0, init=False)
    _success_count: int = field(default=0, init=False)
    _trip_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _current_cooldown_s: float = field(default=0.0, init=False)

    # ── Public API ────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        """Current breaker state (may transition to half_open on read)."""
        if self._state == CircuitState.open:
            if self._cooldown_elapsed():
                self._transition(CircuitState.half_open)
        return self._state

    def should_allow_request(self) -> bool:
        """Return True if the request should be sent to the real provider."""
        current = self.state  # triggers half-open transition check
        if current == CircuitState.closed:
            return True
        if current == CircuitState.half_open:
            return True  # allow the probe request
        # open → short-circuit
        return False

    def record_success(self) -> None:
        """Record a successful provider call."""
        if self._state == CircuitState.half_open:
            self._success_count += 1
            if self._success_count >= self.config.success_threshold:
                self._transition(CircuitState.closed)
                self._trip_count = 0  # reset backoff on full recovery
                _slog.info(
                    "circuit_breaker_closed",
                    role=self.role,
                    after_trips=self._trip_count,
                )
                metrics.inc(f"cb_{self.role}_closed")
        # In closed state, just reset failure count
        self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed provider call."""
        self._failure_count += 1
        self._success_count = 0
        self._last_failure_time = time.monotonic()

        if self._state == CircuitState.half_open:
            # Probe failed → reopen with increased backoff
            self._trip()
            return

        if self._failure_count >= self.config.failure_threshold:
            self._trip()

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of breaker state."""
        return {
            "role": self.role,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "trip_count": self._trip_count,
            "current_cooldown_s": round(self._current_cooldown_s, 1),
            "seconds_until_probe": max(
                0.0,
                round(self._seconds_until_probe(), 1),
            ),
        }

    def reset(self) -> None:
        """Reset breaker to initial closed state (for tests)."""
        self._state = CircuitState.closed
        self._failure_count = 0
        self._success_count = 0
        self._trip_count = 0
        self._last_failure_time = 0.0
        self._current_cooldown_s = 0.0

    # ── Internals ─────────────────────────────────────────────

    def _trip(self) -> None:
        """Open the breaker and compute backoff cooldown."""
        self._trip_count += 1
        self._current_cooldown_s = min(
            self.config.base_cooldown_s
            * (self.config.backoff_multiplier ** (self._trip_count - 1)),
            self.config.max_cooldown_s,
        )
        self._transition(CircuitState.open)

        _slog.warning(
            "circuit_breaker_tripped",
            role=self.role,
            trip_count=self._trip_count,
            failure_count=self._failure_count,
            cooldown_s=round(self._current_cooldown_s, 1),
        )
        metrics.inc(f"cb_{self.role}_tripped")

    def _cooldown_elapsed(self) -> bool:
        """Check if the cooldown period has passed since last failure."""
        if self._last_failure_time == 0.0:
            return True
        return (time.monotonic() - self._last_failure_time) >= self._current_cooldown_s

    def _seconds_until_probe(self) -> float:
        """Seconds remaining before the next half-open probe."""
        if self._state != CircuitState.open:
            return 0.0
        elapsed = time.monotonic() - self._last_failure_time
        return max(0.0, self._current_cooldown_s - elapsed)

    def _transition(self, new_state: CircuitState) -> None:
        """Transition to a new state, resetting per-state counters."""
        old = self._state
        self._state = new_state
        if new_state == CircuitState.half_open:
            self._success_count = 0
        elif new_state == CircuitState.closed:
            self._failure_count = 0
            self._success_count = 0
        _slog.debug(
            "circuit_breaker_transition",
            role=self.role,
            old_state=old.value,
            new_state=new_state.value,
        )


# ── Module-level singletons ─────────────────────────────────────

stt_circuit_breaker = CircuitBreaker(role="stt")
"""Circuit breaker for the STT provider."""

tts_circuit_breaker = CircuitBreaker(role="tts")
"""Circuit breaker for the TTS provider."""
