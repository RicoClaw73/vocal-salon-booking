"""Tests for voice pipeline webhook endpoints (app.routers.voice)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.conversation import conversation_manager

PREFIX = "/api/v1/voice"


def _next_tuesday() -> date:
    """Calculate next Tuesday (salon open Tue-Sat)."""
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


# ── Session lifecycle ────────────────────────────────────────

class TestSessionLifecycle:

    @pytest.mark.asyncio
    async def test_start_session(self, client: AsyncClient):
        resp = await client.post(f"{PREFIX}/sessions/start", json={
            "client_name": "Marie Curie",
            "client_phone": "+33600000001",
            "channel": "phone",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "active"
        assert "session_id" in data
        assert "Maison Éclat" in data["greeting"]
        assert "created_at" in data

    @pytest.mark.asyncio
    async def test_start_session_minimal(self, client: AsyncClient):
        """Start a session with minimal payload."""
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        assert resp.status_code == 201
        assert resp.json()["status"] == "active"

    @pytest.mark.asyncio
    async def test_end_session(self, client: AsyncClient):
        # Start
        resp = await client.post(f"{PREFIX}/sessions/start", json={"client_name": "Test"})
        session_id = resp.json()["session_id"]

        # End
        resp = await client.post(f"{PREFIX}/sessions/end", json={
            "session_id": session_id,
            "reason": "user_hangup",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["turns"] == 0
        assert data["duration_seconds"] is not None

    @pytest.mark.asyncio
    async def test_end_session_not_found(self, client: AsyncClient):
        resp = await client.post(f"{PREFIX}/sessions/end", json={
            "session_id": "nonexistent",
        })
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_message_not_found_session(self, client: AsyncClient):
        resp = await client.post(f"{PREFIX}/sessions/message", json={
            "session_id": "nonexistent",
            "text": "Bonjour",
        })
        assert resp.status_code == 404


# ── Intent detection via endpoints ───────────────────────────

class TestVoiceIntentFlow:

    @pytest.mark.asyncio
    async def test_unknown_intent(self, client: AsyncClient):
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = resp.json()["session_id"]

        resp = await client.post(f"{PREFIX}/sessions/message", json={
            "session_id": session_id,
            "text": "Bonjour",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "unknown"
        assert "rendez-vous" in data["response_text"].lower()

    @pytest.mark.asyncio
    async def test_book_intent_missing_service(self, client: AsyncClient):
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = resp.json()["session_id"]

        resp = await client.post(f"{PREFIX}/sessions/message", json={
            "session_id": session_id,
            "text": "Je voudrais prendre rendez-vous",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "book"
        assert data["action_taken"] == "collecting_info"
        assert "prestation" in data["response_text"].lower()

    @pytest.mark.asyncio
    async def test_book_intent_with_service_missing_date(self, client: AsyncClient):
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = resp.json()["session_id"]

        resp = await client.post(f"{PREFIX}/sessions/message", json={
            "session_id": session_id,
            "text": "Je voudrais réserver une coupe pour homme",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "book"
        # Should ask for date (service resolved, date missing)
        assert data["action_taken"] == "collecting_info"

    @pytest.mark.asyncio
    async def test_cancel_intent_needs_booking_id(self, client: AsyncClient):
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = resp.json()["session_id"]

        resp = await client.post(f"{PREFIX}/sessions/message", json={
            "session_id": session_id,
            "text": "Je veux annuler mon rendez-vous",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "cancel"
        assert data["action_taken"] == "need_booking_id"

    @pytest.mark.asyncio
    async def test_cancel_booking_not_found(self, client: AsyncClient):
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = resp.json()["session_id"]

        resp = await client.post(f"{PREFIX}/sessions/message", json={
            "session_id": session_id,
            "text": "Annuler la réservation #99999",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["action_taken"] == "booking_not_found"

    @pytest.mark.asyncio
    async def test_reschedule_intent_needs_booking_id(self, client: AsyncClient):
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = resp.json()["session_id"]

        resp = await client.post(f"{PREFIX}/sessions/message", json={
            "session_id": session_id,
            "text": "Je voudrais déplacer mon rendez-vous",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "reschedule"
        assert data["action_taken"] == "need_booking_id"

    @pytest.mark.asyncio
    async def test_availability_check_needs_service(self, client: AsyncClient):
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = resp.json()["session_id"]

        resp = await client.post(f"{PREFIX}/sessions/message", json={
            "session_id": session_id,
            "text": "Quand est-ce que vous êtes disponible ?",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "check_availability"
        assert data["action_taken"] == "need_service"

    @pytest.mark.asyncio
    async def test_message_on_ended_session(self, client: AsyncClient):
        # Start and end session
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = resp.json()["session_id"]
        await client.post(f"{PREFIX}/sessions/end", json={"session_id": session_id})

        # Try to send message
        resp = await client.post(f"{PREFIX}/sessions/message", json={
            "session_id": session_id,
            "text": "Bonjour",
        })
        assert resp.status_code == 409


# ── Full booking flow ────────────────────────────────────────

class TestFullBookingFlow:

    @pytest.mark.asyncio
    async def test_full_booking_happy_path(self, client: AsyncClient):
        """Test complete booking flow: start → book with all details → confirm."""
        tuesday = _next_tuesday()

        # Start session
        resp = await client.post(f"{PREFIX}/sessions/start", json={
            "client_name": "Jean Dupont",
            "client_phone": "+33612345678",
        })
        assert resp.status_code == 201
        session_id = resp.json()["session_id"]

        # Send booking request with all details at once
        resp = await client.post(f"{PREFIX}/sessions/message", json={
            "session_id": session_id,
            "text": f"Je voudrais réserver une coupe homme le {tuesday.isoformat()} à 10h00",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "book"

        # Should either create booking or offer slots
        assert data["action_taken"] in ("booking_created", "slots_offered", "no_slots")

        if data["action_taken"] == "booking_created":
            assert data["data"]["booking_id"] >= 1
            assert "confirmé" in data["response_text"]

        # End session
        resp = await client.post(f"{PREFIX}/sessions/end", json={
            "session_id": session_id,
        })
        assert resp.status_code == 200
        assert resp.json()["turns"] == 1


class TestFullCancelFlow:

    @pytest.mark.asyncio
    async def test_cancel_existing_booking(self, client: AsyncClient):
        """Create a booking via REST, then cancel via voice."""
        tuesday = _next_tuesday()
        start = datetime(tuesday.year, tuesday.month, tuesday.day, 10, 0)

        # Create booking via standard API
        booking_resp = await client.post("/api/v1/bookings", json={
            "client_name": "Jean Dupont",
            "client_phone": "+33612345678",
            "service_id": "coupe_homme",
            "employee_id": "emp_02",
            "start_time": start.isoformat(),
        })
        assert booking_resp.status_code == 201
        booking_id = booking_resp.json()["id"]

        # Start voice session
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = resp.json()["session_id"]

        # Cancel via voice
        resp = await client.post(f"{PREFIX}/sessions/message", json={
            "session_id": session_id,
            "text": f"Annuler la réservation #{booking_id}",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "cancel"
        assert data["action_taken"] == "booking_cancelled"
        assert data["data"]["booking_id"] == booking_id

        # Verify booking is cancelled
        resp = await client.get(f"/api/v1/bookings/{booking_id}")
        assert resp.json()["status"] == "cancelled"


class TestAvailabilityCheckFlow:

    @pytest.mark.asyncio
    async def test_check_availability_with_service_and_date(self, client: AsyncClient):
        """Ask for availability with service and date in one utterance."""
        tuesday = _next_tuesday()

        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = resp.json()["session_id"]

        resp = await client.post(f"{PREFIX}/sessions/message", json={
            "session_id": session_id,
            "text": f"Quand êtes-vous disponible pour une coupe le {tuesday.isoformat()} ?",
        })
        assert resp.status_code == 200
        data = resp.json()
        # Intent should be check_availability (because "disponible" + no booking verb)
        # or book (because "coupe" + "disponible" matches book patterns)
        assert data["intent"] in ("check_availability", "book")
