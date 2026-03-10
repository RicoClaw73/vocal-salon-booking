"""
Tests for app.observability — structured logging & metrics collector.

All tests are deterministic, no external services.
"""

from __future__ import annotations

import logging
import time

import pytest

from app.observability import MetricsCollector, StructuredLogger, new_request_id


# ── StructuredLogger ────────────────────────────────────────────


class TestStructuredLogger:
    """Unit tests for the StructuredLogger wrapper."""

    def test_info_with_kwargs(self, caplog: pytest.LogCaptureFixture) -> None:
        slog = StructuredLogger("test.slog")
        with caplog.at_level(logging.INFO, logger="test.slog"):
            slog.info("event_ok", session_id="abc", intent="book")
        assert "event_ok" in caplog.text
        assert "session_id=abc" in caplog.text
        assert "intent=book" in caplog.text

    def test_warning_level(self, caplog: pytest.LogCaptureFixture) -> None:
        slog = StructuredLogger("test.slog.warn")
        with caplog.at_level(logging.WARNING, logger="test.slog.warn"):
            slog.warning("rate_exceeded", ip="127.0.0.1")
        assert "rate_exceeded" in caplog.text
        assert "ip=127.0.0.1" in caplog.text

    def test_none_values_excluded(self, caplog: pytest.LogCaptureFixture) -> None:
        slog = StructuredLogger("test.slog.none")
        with caplog.at_level(logging.INFO, logger="test.slog.none"):
            slog.info("partial", a="yes", b=None, c="three")
        assert "a=yes" in caplog.text
        assert "b=" not in caplog.text
        assert "c=three" in caplog.text

    def test_event_only_no_kwargs(self, caplog: pytest.LogCaptureFixture) -> None:
        slog = StructuredLogger("test.slog.bare")
        with caplog.at_level(logging.INFO, logger="test.slog.bare"):
            slog.info("bare_event")
        assert "bare_event" in caplog.text

    def test_error_level(self, caplog: pytest.LogCaptureFixture) -> None:
        slog = StructuredLogger("test.slog.err")
        with caplog.at_level(logging.ERROR, logger="test.slog.err"):
            slog.error("db_fail", reason="timeout")
        assert "db_fail" in caplog.text
        assert "reason=timeout" in caplog.text


# ── new_request_id ──────────────────────────────────────────────


class TestRequestId:
    def test_length(self) -> None:
        rid = new_request_id()
        assert len(rid) == 10

    def test_uniqueness(self) -> None:
        ids = {new_request_id() for _ in range(100)}
        assert len(ids) == 100  # all unique

    def test_hex_chars(self) -> None:
        rid = new_request_id()
        assert all(c in "0123456789abcdef" for c in rid)


# ── MetricsCollector ────────────────────────────────────────────


class TestMetricsCollector:
    def test_inc_and_get_counter(self) -> None:
        m = MetricsCollector()
        assert m.get_counter("foo") == 0
        m.inc("foo")
        assert m.get_counter("foo") == 1
        m.inc("foo", 5)
        assert m.get_counter("foo") == 6

    def test_multiple_counters_independent(self) -> None:
        m = MetricsCollector()
        m.inc("a")
        m.inc("b", 3)
        assert m.get_counter("a") == 1
        assert m.get_counter("b") == 3
        assert m.get_counter("c") == 0

    def test_record_latency(self) -> None:
        m = MetricsCollector()
        m.record_latency("turn", 10.0)
        m.record_latency("turn", 20.0)
        m.record_latency("turn", 30.0)
        snap = m.snapshot()
        lat = snap["latencies"]["turn"]
        assert lat["count"] == 3
        assert lat["min_ms"] == 10.0
        assert lat["max_ms"] == 30.0
        assert lat["avg_ms"] == 20.0

    def test_timer_context_manager(self) -> None:
        m = MetricsCollector()
        with m.timer("test_op"):
            time.sleep(0.01)  # 10ms
        snap = m.snapshot()
        lat = snap["latencies"]["test_op"]
        assert lat["count"] == 1
        assert lat["min_ms"] >= 5.0  # at least ~5ms (generous tolerance)
        assert lat["max_ms"] < 5000.0  # sanity check

    def test_snapshot_structure(self) -> None:
        m = MetricsCollector()
        m.inc("voice_turns", 3)
        m.record_latency("voice_turn_ms", 42.0)
        snap = m.snapshot()
        assert "uptime_seconds" in snap
        assert "started_at" in snap
        assert "counters" in snap
        assert "latencies" in snap
        assert snap["counters"]["voice_turns"] == 3

    def test_reset(self) -> None:
        m = MetricsCollector()
        m.inc("x", 10)
        m.record_latency("y", 50.0)
        m.reset()
        assert m.get_counter("x") == 0
        snap = m.snapshot()
        assert len(snap["counters"]) == 0
        assert len(snap["latencies"]) == 0

    def test_latency_empty_stats(self) -> None:
        m = MetricsCollector()
        # No recordings yet — snapshot should still work
        m.record_latency("empty", 0.0)  # edge: zero latency
        snap = m.snapshot()
        assert snap["latencies"]["empty"]["count"] == 1
        assert snap["latencies"]["empty"]["avg_ms"] == 0.0
