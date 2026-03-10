"""Tests for the telephony event simulator (Phase 5.1).

Tests the event mapper and runs scenarios through the FastAPI test client.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.conversation import conversation_manager
from app.telephony_simulator import (
    CallEvent,
    TelephonyEvent,
    TelephonySimulator,
    map_event_to_requests,
    scenario_booking_flow,
    scenario_cancel_flow,
    scenario_fallback_flow,
)


# ── Clear sessions between tests ──────────────────────────────


@pytest.fixture(autouse=True)
def _clear_sessions():
    conversation_manager._sessions.clear()
    yield
    conversation_manager._sessions.clear()


# ── Event mapper unit tests ───────────────────────────────────


class TestMapEventToRequests:
    """Test that telephony events produce correct API request specs."""

    def test_call_started(self):
        event = CallEvent(
            event=TelephonyEvent.call_started,
            payload={"caller_name": "Alice", "caller_number": "+33600000000"},
        )
        reqs = map_event_to_requests(event)
        assert len(reqs) == 1
        assert reqs[0]["method"] == "POST"
        assert reqs[0]["path"] == "/api/v1/voice/sessions/start"
        assert reqs[0]["json"]["client_name"] == "Alice"
        assert reqs[0]["json"]["client_phone"] == "+33600000000"

    def test_utterance(self):
        event = CallEvent(
            event=TelephonyEvent.utterance,
            payload={"transcript": "Bonjour"},
        )
        reqs = map_event_to_requests(event, session_id="sess-123")
        assert len(reqs) == 1
        assert reqs[0]["path"] == "/api/v1/voice/turn"
        assert reqs[0]["json"]["session_id"] == "sess-123"
        assert reqs[0]["json"]["text"] == "Bonjour"

    def test_call_ended(self):
        event = CallEvent(
            event=TelephonyEvent.call_ended,
            payload={"reason": "user_hangup"},
        )
        reqs = map_event_to_requests(event, session_id="sess-123")
        assert len(reqs) == 1
        assert reqs[0]["path"] == "/api/v1/voice/sessions/end"
        assert reqs[0]["json"]["reason"] == "user_hangup"

    def test_call_ended_no_session(self):
        event = CallEvent(event=TelephonyEvent.call_ended, payload={})
        reqs = map_event_to_requests(event, session_id=None)
        assert reqs == []

    def test_dtmf(self):
        event = CallEvent(
            event=TelephonyEvent.dtmf,
            payload={"digits": "42"},
        )
        reqs = map_event_to_requests(event, session_id="s1")
        assert reqs[0]["json"]["text"] == "[DTMF: 42]"

    def test_silence_timeout(self):
        event = CallEvent(event=TelephonyEvent.silence_timeout, payload={})
        reqs = map_event_to_requests(event, session_id="s1")
        assert reqs[0]["json"]["text"] == "..."


# ── Scenario definitions ─────────────────────────────────────


class TestScenarios:
    """Verify scenario factories produce valid event sequences."""

    def test_booking_flow_structure(self):
        events = scenario_booking_flow()
        assert events[0].event == TelephonyEvent.call_started
        assert events[-1].event == TelephonyEvent.call_ended
        # At least one utterance in between
        utterances = [e for e in events if e.event == TelephonyEvent.utterance]
        assert len(utterances) >= 1

    def test_cancel_flow_structure(self):
        events = scenario_cancel_flow()
        assert events[0].event == TelephonyEvent.call_started
        assert events[-1].event == TelephonyEvent.call_ended

    def test_fallback_flow_structure(self):
        events = scenario_fallback_flow()
        assert events[0].event == TelephonyEvent.call_started
        utterances = [e for e in events if e.event == TelephonyEvent.utterance]
        assert len(utterances) >= 3  # enough to trigger human transfer


# ── End-to-end simulator run ─────────────────────────────────


class TestSimulatorE2E:
    """Run scenarios through the FastAPI test client."""

    @pytest.mark.asyncio
    async def test_fallback_scenario(self, client: AsyncClient):
        """Fallback scenario: unintelligible → fallback → human transfer."""
        sim = TelephonySimulator(client=client)
        results = await sim.run(scenario_fallback_flow())

        # Should have results for each event that maps to API calls
        assert len(results) >= 4  # start + 3 utterances + end (at least)

        # First result = session start
        assert results[0]["status_code"] == 201
        assert "session_id" in results[0]["response"]

        # Last utterance should have triggered human_transfer_offered
        utterance_results = [r for r in results if r["event"] == "utterance"]
        last_utterance = utterance_results[-1]
        assert last_utterance["response"].get("action_taken") in (
            "human_transfer_offered", "fallback",
        )


# ── Provider status endpoint ─────────────────────────────────


class TestOpsProviderStatus:
    """Test the /ops/providers/status endpoint."""

    @pytest.mark.asyncio
    async def test_provider_status_endpoint(self, client: AsyncClient):
        resp = await client.get("/api/v1/ops/providers/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data
        assert "all_ready" in data
        assert len(data["providers"]) == 2

    @pytest.mark.asyncio
    async def test_provider_status_mock_defaults(self, client: AsyncClient):
        """With default config (mock), all providers should be ready."""
        resp = await client.get("/api/v1/ops/providers/status")
        data = resp.json()
        assert data["all_ready"] is True
        for p in data["providers"]:
            assert p["active"] == "mock"


# ── Smoke test endpoint ──────────────────────────────────────


class TestOpsSmokTest:
    """Test the /ops/providers/smoke-test endpoint."""

    @pytest.mark.asyncio
    async def test_smoke_test_endpoint(self, client: AsyncClient):
        resp = await client.post("/api/v1/ops/providers/smoke-test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] in ("pass", "degraded", "fail")
        assert "steps" in data
        assert len(data["steps"]) == 2  # stt + tts
        for step in data["steps"]:
            assert step["success"] is True
            assert "latency_ms" in step

    @pytest.mark.asyncio
    async def test_smoke_test_mock_passes(self, client: AsyncClient):
        """With mock providers, smoke test should pass cleanly."""
        resp = await client.post("/api/v1/ops/providers/smoke-test")
        data = resp.json()
        assert data["overall"] == "pass"
        assert all(not s["fallback_used"] for s in data["steps"])
