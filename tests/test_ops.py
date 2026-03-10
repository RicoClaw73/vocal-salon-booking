"""
Integration tests for the /api/v1/ops/* endpoints (Phase 4.4).

Tests the metrics, session review, diagnostics, and failure summary
endpoints.  All deterministic, using in-memory SQLite + test fixtures.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.conversation import conversation_manager
from app.observability import metrics

pytestmark = pytest.mark.asyncio


# ── Helpers ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_conversation_cache() -> None:
    """Reset the in-memory session cache before each test."""
    conversation_manager._sessions.clear()


async def _start_session(client: AsyncClient) -> str:
    """Helper: create a voice session and return its ID."""
    resp = await client.post(
        "/api/v1/voice/sessions/start",
        json={"channel": "test"},
    )
    assert resp.status_code == 201
    return resp.json()["session_id"]


async def _send_turn(client: AsyncClient, text: str, session_id: str | None = None) -> dict:
    """Helper: send a voice turn and return the response."""
    payload: dict = {"text": text}
    if session_id:
        payload["session_id"] = session_id
    resp = await client.post("/api/v1/voice/turn", json=payload)
    assert resp.status_code == 200
    return resp.json()


# ── GET /ops/metrics ────────────────────────────────────────────


class TestMetricsEndpoint:
    async def test_metrics_returns_structure(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/ops/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "uptime_seconds" in data
        assert "started_at" in data
        assert "counters" in data
        assert "latencies" in data

    async def test_metrics_reflect_voice_turns(self, client: AsyncClient) -> None:
        # Perform some voice turns
        sid = await _start_session(client)
        await _send_turn(client, "Je voudrais réserver une coupe", session_id=sid)
        await _send_turn(client, "blablabla xyz random", session_id=sid)

        resp = await client.get("/api/v1/ops/metrics")
        data = resp.json()
        counters = data["counters"]
        assert counters.get("sessions_started", 0) >= 1
        assert counters.get("voice_turns", 0) >= 2

    async def test_metrics_track_fallbacks(self, client: AsyncClient) -> None:
        sid = await _start_session(client)
        # Send gibberish to trigger fallback
        await _send_turn(client, "zzz qqq xxx nonsense", session_id=sid)

        resp = await client.get("/api/v1/ops/metrics")
        counters = resp.json()["counters"]
        assert counters.get("voice_fallbacks", 0) >= 1


# ── GET /ops/sessions/recent ────────────────────────────────────


class TestRecentSessions:
    async def test_recent_returns_sessions(self, client: AsyncClient) -> None:
        sid = await _start_session(client)
        resp = await client.get("/api/v1/ops/sessions/recent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        session_ids = [s["session_id"] for s in data["sessions"]]
        assert sid in session_ids

    async def test_recent_no_sensitive_data(self, client: AsyncClient) -> None:
        """Ensure client_name and client_phone are NOT in the response."""
        await client.post(
            "/api/v1/voice/sessions/start",
            json={"client_name": "Alice Secret", "client_phone": "+33612345678", "channel": "test"},
        )
        resp = await client.get("/api/v1/ops/sessions/recent")
        data = resp.json()
        for session in data["sessions"]:
            assert "client_name" not in session
            assert "client_phone" not in session

    async def test_recent_filter_by_status(self, client: AsyncClient) -> None:
        sid = await _start_session(client)
        # End the session
        await client.post(
            "/api/v1/voice/sessions/end",
            json={"session_id": sid},
        )
        resp = await client.get("/api/v1/ops/sessions/recent?status=completed")
        assert resp.status_code == 200
        for s in resp.json()["sessions"]:
            assert s["status"] == "completed"

    async def test_recent_limit(self, client: AsyncClient) -> None:
        for _ in range(3):
            await _start_session(client)
        resp = await client.get("/api/v1/ops/sessions/recent?limit=2")
        assert resp.json()["count"] <= 2


# ── GET /ops/sessions/{session_id}/diag ─────────────────────────


class TestSessionDiagnostics:
    async def test_diag_returns_timeline(self, client: AsyncClient) -> None:
        sid = await _start_session(client)
        await _send_turn(client, "Je veux réserver une coupe", session_id=sid)
        await _send_turn(client, "Le 2025-06-15", session_id=sid)

        resp = await client.get(f"/api/v1/ops/sessions/{sid}/diag")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == sid
        assert data["turns"] >= 2
        assert len(data["timeline"]) >= 2
        assert "intents_seen" in data
        assert "actions_seen" in data
        assert "fallback_count" in data

    async def test_diag_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/ops/sessions/nonexistent/diag")
        assert resp.status_code == 404

    async def test_diag_no_sensitive_data(self, client: AsyncClient) -> None:
        """Diagnostics should not include client names or phone."""
        resp_start = await client.post(
            "/api/v1/voice/sessions/start",
            json={"client_name": "Secret Name", "client_phone": "+331234", "channel": "test"},
        )
        sid = resp_start.json()["session_id"]
        resp = await client.get(f"/api/v1/ops/sessions/{sid}/diag")
        text = resp.text
        assert "Secret Name" not in text
        assert "+331234" not in text


# ── GET /ops/failures/summary ───────────────────────────────────


class TestFailureSummary:
    async def test_failure_summary_structure(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/ops/failures/summary?hours=24")
        assert resp.status_code == 200
        data = resp.json()
        assert "window_hours" in data
        assert "total_turns" in data
        assert "fallback_turns" in data
        assert "fallback_rate" in data
        assert "high_fallback_sessions" in data
        assert "action_distribution" in data

    async def test_failure_summary_after_fallbacks(self, client: AsyncClient) -> None:
        sid = await _start_session(client)
        # Send multiple gibberish turns to trigger fallbacks
        for _ in range(3):
            await _send_turn(client, "zzz nonsense", session_id=sid)

        resp = await client.get("/api/v1/ops/failures/summary?hours=1")
        data = resp.json()
        assert data["fallback_turns"] >= 3
        assert data["fallback_rate"] > 0


# ── Health endpoint metrics integration ─────────────────────────


class TestHealthWithMetrics:
    async def test_health_includes_metrics_fields(self, client: AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "uptime_seconds" in data
        assert "voice_turns" in data
        assert "active_sessions" in data
        assert data["status"] == "ok"
        assert data["database"] == "ok"
