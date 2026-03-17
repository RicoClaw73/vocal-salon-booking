"""
Observability utilities for the vocal-salon voice backend (Phase 4.4).

Provides:
  1. **Structured logging** — ``StructuredLogger`` wraps stdlib logging to
     emit key-value context (request_id, session_id, intent, outcome,
     latency_ms) alongside every log message.
  2. **In-memory metrics** — ``MetricsCollector`` keeps lightweight counters
     and timing histograms for operational visibility.  Read via the
     ``/api/v1/ops/metrics`` endpoint.

Both are local-first, zero-dependency utilities.  No paid services.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Generator

# ── Structured Logging ──────────────────────────────────────────


class StructuredLogger:
    """Wrapper that enriches stdlib log messages with structured context.

    Usage::

        slog = StructuredLogger("app.routers.voice")
        slog.info("turn_processed",
                   session_id="abc123", intent="book", latency_ms=42)
        # => INFO app.routers.voice | turn_processed | session_id=abc123 intent=book latency_ms=42
    """

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    @staticmethod
    def _format_kv(**kwargs: Any) -> str:
        parts = [f"{k}={v}" for k, v in kwargs.items() if v is not None]
        return " ".join(parts)

    def _log(self, level: int, event: str, **kwargs: Any) -> None:
        kv = self._format_kv(**kwargs)
        msg = f"{event} | {kv}" if kv else event
        self._logger.log(level, msg)

    def info(self, event: str, **kwargs: Any) -> None:
        self._log(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, event, **kwargs)

    def debug(self, event: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, event, **kwargs)


def new_request_id() -> str:
    """Generate a short, unique request ID for correlating logs."""
    return uuid.uuid4().hex[:10]


# ── Metrics Collector ───────────────────────────────────────────


@dataclass
class _LatencyStats:
    """Lightweight latency tracker (no external deps)."""
    count: int = 0
    total_ms: float = 0.0
    min_ms: float = float("inf")
    max_ms: float = 0.0

    def record(self, ms: float) -> None:
        self.count += 1
        self.total_ms += ms
        if ms < self.min_ms:
            self.min_ms = ms
        if ms > self.max_ms:
            self.max_ms = ms

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "total_ms": round(self.total_ms, 2),
            "min_ms": round(self.min_ms, 2) if self.count else 0.0,
            "max_ms": round(self.max_ms, 2),
            "avg_ms": round(self.avg_ms, 2),
        }


class MetricsCollector:
    """In-memory counters and latency stats for operational monitoring.

    Thread-safety: safe for single-worker async (FastAPI default).
    All writes are synchronous dict mutations inside the event loop.

    Counters:
        voice_turns, voice_fallbacks, bookings_created, bookings_cancelled,
        auth_failures, rate_limit_hits, sessions_started, sessions_completed,
        intent_<name> (per-intent counters)

    Latencies:
        voice_turn_ms — end-to-end voice turn processing time.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._latencies: dict[str, _LatencyStats] = defaultdict(_LatencyStats)
        self._started_at: datetime = datetime.now(timezone.utc)

    # ── Counters ──────────────────────────────────────────────

    def inc(self, name: str, n: int = 1) -> None:
        """Increment a named counter."""
        self._counters[name] += n

    def get_counter(self, name: str) -> int:
        return self._counters.get(name, 0)

    # ── Latency ───────────────────────────────────────────────

    def record_latency(self, name: str, ms: float) -> None:
        """Record a latency measurement (milliseconds)."""
        self._latencies[name].record(ms)

    @contextmanager
    def timer(self, name: str) -> Generator[None, None, None]:
        """Context manager that records elapsed time as latency."""
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            self.record_latency(name, elapsed_ms)

    # ── Snapshot ──────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of all metrics."""
        return {
            "uptime_seconds": round(
                (datetime.now(timezone.utc) - self._started_at).total_seconds(), 1
            ),
            "started_at": self._started_at.isoformat(),
            "counters": dict(sorted(self._counters.items())),
            "latencies": {
                k: v.snapshot() for k, v in sorted(self._latencies.items())
            },
        }

    def reset(self) -> None:
        """Clear all metrics (for tests)."""
        self._counters.clear()
        self._latencies.clear()
        self._started_at = datetime.now(timezone.utc)


# ── Module-level singleton ──────────────────────────────────────

metrics = MetricsCollector()
"""Shared metrics instance for the whole application."""

slog = StructuredLogger("app.observability")
"""Convenience logger for quick structured logging."""
