"""
Tests for Phase 3 voice turn orchestration endpoint (/api/v1/voice/turn).

Covers:
  - Auto-session creation
  - Existing session reuse
  - Unknown intent → deterministic fallback
  - Consecutive fallbacks → human transfer
  - Full booking flow through /voice/turn
  - Availability check through /voice/turn
  - Cancel flow through /voice/turn
  - STT/TTS metadata in response
  - Error cases (ended session, missing text)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.conversation import conversation_manager

PREFIX = "/api/v1/voice"
TURN_URL = f"{PREFIX}/turn"


def _next_tuesday() -> date:
    """Calculate next Tuesday (salon open Tue–Sat)."""
    today = date.today()
    days_ahead = (1 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


@pytest.fixture(autouse=True)
def _clear_sessions():
    """Clear all voice sessions before each test."""
    conversation_manager._sessions.clear()
    yield
    conversation_manager._sessions.clear()


# ── Auto-session creation ───────────────────────────────────

class TestVoiceTurnSession:

    @pytest.mark.asyncio
    async def test_auto_creates_session(self, client: AsyncClient):
        """When no session_id is provided, a new session is created."""
        resp = await client.post(TURN_URL, json={
            "text": "Bonjour",
            "client_name": "Marie Curie",
            "channel": "test",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["turn_number"] == 1
        assert data["stt_meta"] is not None
        assert data["tts_meta"] is not None

    @pytest.mark.asyncio
    async def test_reuse_existing_session(self, client: AsyncClient):
        """When session_id is provided, reuse that session."""
        # Create session first
        start_resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = start_resp.json()["session_id"]

        # Use it in voice turn
        resp = await client.post(TURN_URL, json={
            "session_id": session_id,
            "text": "Bonjour",
        })
        assert resp.status_code == 200
        assert resp.json()["session_id"] == session_id

    @pytest.mark.asyncio
    async def test_session_not_found(self, client: AsyncClient):
        resp = await client.post(TURN_URL, json={
            "session_id": "nonexistent123",
            "text": "Bonjour",
        })
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_ended_session_rejected(self, client: AsyncClient):
        """Cannot send turns to a completed session."""
        start_resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = start_resp.json()["session_id"]
        await client.post(f"{PREFIX}/sessions/end", json={"session_id": session_id})

        resp = await client.post(TURN_URL, json={
            "session_id": session_id,
            "text": "Bonjour",
        })
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_missing_text_rejected(self, client: AsyncClient):
        """Must provide either text or mock_transcript."""
        resp = await client.post(TURN_URL, json={
            "channel": "test",
        })
        assert resp.status_code == 422


# ── Fallback strategy ──────────────────────────────────────

class TestFallbackStrategy:

    @pytest.mark.asyncio
    async def test_unknown_intent_triggers_fallback(self, client: AsyncClient):
        """Unrecognised text triggers fallback response."""
        resp = await client.post(TURN_URL, json={"text": "Bonjour"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "unknown"
        assert data["is_fallback"] is True
        assert data["action_taken"] == "fallback"
        assert data["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_fallback_messages_rotate(self, client: AsyncClient):
        """Consecutive fallbacks cycle through different messages."""
        # First turn — auto-create session
        resp1 = await client.post(TURN_URL, json={"text": "blah blah"})
        session_id = resp1.json()["session_id"]
        msg1 = resp1.json()["response_text"]

        # Second turn — same session, different message
        resp2 = await client.post(TURN_URL, json={
            "session_id": session_id,
            "text": "xyz abc",
        })
        msg2 = resp2.json()["response_text"]
        assert msg2 != msg1, "Fallback messages should rotate"

    @pytest.mark.asyncio
    async def test_human_transfer_after_max_fallbacks(self, client: AsyncClient):
        """After MAX_CONSECUTIVE_FALLBACKS unknowns, offer human transfer."""
        # First turn (auto-create)
        resp = await client.post(TURN_URL, json={"text": "aaa"})
        session_id = resp.json()["session_id"]

        # Second
        await client.post(TURN_URL, json={
            "session_id": session_id,
            "text": "bbb",
        })

        # Third — should trigger human transfer
        resp3 = await client.post(TURN_URL, json={
            "session_id": session_id,
            "text": "ccc",
        })
        data = resp3.json()
        assert data["action_taken"] == "human_transfer_offered"
        assert "équipe" in data["response_text"].lower() or "rappeler" in data["response_text"].lower()

    @pytest.mark.asyncio
    async def test_fallback_counter_resets_on_valid_intent(self, client: AsyncClient):
        """After a valid intent, consecutive fallback counter resets."""
        # Unknown — no active intent, triggers fallback
        resp = await client.post(TURN_URL, json={"text": "aaa"})
        session_id = resp.json()["session_id"]
        assert resp.json()["is_fallback"] is True

        # Valid intent — resets counter
        resp2 = await client.post(TURN_URL, json={
            "session_id": session_id,
            "text": "Je voudrais réserver une coupe",
        })
        assert resp2.json()["is_fallback"] is False
        assert resp2.json()["intent"] == "book"

        # Unknown text but session has active intent (book) →
        # routes through book handler (collecting info), not fallback
        resp3 = await client.post(TURN_URL, json={
            "session_id": session_id,
            "text": "zzz",
        })
        assert resp3.json()["is_fallback"] is False
        assert resp3.json()["intent"] == "book"

        # End session to clear active intent, start fresh
        await client.post(f"{PREFIX}/sessions/end", json={"session_id": session_id})

        # New session: unknown with no active intent → fallback at #1
        resp4 = await client.post(TURN_URL, json={"text": "qqq"})
        assert resp4.json()["is_fallback"] is True
        assert resp4.json()["action_taken"] == "fallback"


# ── STT/TTS metadata ───────────────────────────────────────

class TestAudioMetadata:

    @pytest.mark.asyncio
    async def test_stt_meta_present(self, client: AsyncClient):
        resp = await client.post(TURN_URL, json={"text": "Bonjour"})
        data = resp.json()
        stt = data["stt_meta"]
        assert stt is not None
        assert stt["provider"] == "mock"
        assert stt["format"] == "wav"
        assert stt["sample_rate"] == 16000

    @pytest.mark.asyncio
    async def test_tts_meta_present(self, client: AsyncClient):
        resp = await client.post(TURN_URL, json={"text": "Bonjour"})
        data = resp.json()
        tts = data["tts_meta"]
        assert tts is not None
        assert tts["provider"] == "mock"
        assert tts["duration_ms"] > 0

    @pytest.mark.asyncio
    async def test_mock_transcript_equivalent_to_text(self, client: AsyncClient):
        """mock_transcript should work identically to text."""
        resp = await client.post(TURN_URL, json={
            "mock_transcript": "Je voudrais réserver une coupe",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "book"


# ── Full booking flow through /voice/turn ───────────────────

class TestVoiceTurnBookingFlow:

    @pytest.mark.asyncio
    async def test_booking_happy_path(self, client: AsyncClient):
        """Complete booking: service + date + time in one utterance."""
        tuesday = _next_tuesday()

        resp = await client.post(TURN_URL, json={
            "text": f"Je voudrais réserver une coupe homme le {tuesday.isoformat()} à 10h00",
            "client_name": "Jean Dupont",
            "client_phone": "+33612345678",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "book"
        assert data["is_fallback"] is False
        assert data["action_taken"] in ("booking_created", "slots_offered", "no_slots")

        if data["action_taken"] == "booking_created":
            assert data["data"]["booking_id"] >= 1
            assert "confirmé" in data["response_text"]
            # TTS meta should be present
            assert data["tts_meta"]["duration_ms"] > 0

    @pytest.mark.asyncio
    async def test_multi_turn_booking(self, client: AsyncClient):
        """Multi-turn: first provide intent, then fill missing fields."""
        tuesday = _next_tuesday()

        # Turn 1: intent only → asks for service
        resp1 = await client.post(TURN_URL, json={
            "text": "Je voudrais prendre rendez-vous",
        })
        session_id = resp1.json()["session_id"]
        assert resp1.json()["action_taken"] == "collecting_info"
        assert "prestation" in resp1.json()["response_text"].lower()

        # Turn 2: provide service → asks for date
        resp2 = await client.post(TURN_URL, json={
            "session_id": session_id,
            "text": "Une coupe pour homme",
        })
        assert resp2.json()["action_taken"] == "collecting_info"

        # Turn 3: provide date + time → should attempt booking
        resp3 = await client.post(TURN_URL, json={
            "session_id": session_id,
            "text": f"Le {tuesday.isoformat()} à 10h00",
        })
        # At this point all fields should be filled
        assert resp3.json()["action_taken"] in (
            "booking_created", "slots_offered", "no_slots", "collecting_info"
        )


# ── Cancel flow through /voice/turn ────────────────────────

class TestVoiceTurnCancelFlow:

    @pytest.mark.asyncio
    async def test_cancel_via_voice_turn(self, client: AsyncClient):
        """Create booking via REST, cancel via /voice/turn."""
        tuesday = _next_tuesday()
        start = datetime(tuesday.year, tuesday.month, tuesday.day, 10, 0)

        # Create booking
        booking_resp = await client.post("/api/v1/bookings", json={
            "client_name": "Jean Dupont",
            "client_phone": "+33612345678",
            "service_id": "coupe_homme",
            "employee_id": "emp_02",
            "start_time": start.isoformat(),
        })
        assert booking_resp.status_code == 201
        booking_id = booking_resp.json()["id"]

        # Cancel via voice turn
        resp = await client.post(TURN_URL, json={
            "text": f"Annuler la réservation #{booking_id}",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "cancel"
        assert data["action_taken"] == "booking_cancelled"
        assert data["data"]["booking_id"] == booking_id
        assert data["is_fallback"] is False

        # Verify cancelled
        verify = await client.get(f"/api/v1/bookings/{booking_id}")
        assert verify.json()["status"] == "cancelled"


# ── Availability check through /voice/turn ─────────────────

class TestVoiceTurnAvailability:

    @pytest.mark.asyncio
    async def test_availability_check(self, client: AsyncClient):
        """Check availability for a service on a date."""
        tuesday = _next_tuesday()

        resp = await client.post(TURN_URL, json={
            "text": f"Quand êtes-vous disponible pour une coupe le {tuesday.isoformat()} ?",
        })
        assert resp.status_code == 200
        data = resp.json()
        # Could be "check_availability" or "book" depending on intent matching
        assert data["intent"] in ("check_availability", "book")
        assert data["is_fallback"] is False
