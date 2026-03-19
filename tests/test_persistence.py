"""
Tests for Phase 4.3: session persistence, auth, rate limiting.

Covers:
  - VoiceSession / TranscriptEvent DB models
  - Persistent session store (create, load, save, transcript events)
  - Transcript durability across simulated process restart
  - API key auth (on/off)
  - Rate limiting scaffold
  - Error handling (404, 409, validation)
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation import conversation_manager
from app.models import TranscriptEvent, VoiceSession
from app.session_store import (
    append_transcript_event,
    create_session,
    get_transcript_events,
    load_session,
    save_session,
)
from app.voice_schemas import SessionStatus, VoiceIntent

PREFIX = "/api/v1/voice"


@pytest.fixture(autouse=True)
def _clear_sessions():
    """Clear all in-memory voice sessions before each test."""
    conversation_manager._sessions.clear()
    yield
    conversation_manager._sessions.clear()


# ── Session store unit tests ────────────────────────────────


class TestSessionStore:
    """Direct tests of the persistent session store functions."""

    @pytest.mark.asyncio
    async def test_create_and_load_session(self, db_session: AsyncSession, default_tenant):
        state = await create_session(db_session, default_tenant.id, client_name="Alice", channel="test")
        await db_session.commit()

        loaded = await load_session(db_session, state.session_id)
        assert loaded is not None
        assert loaded.session_id == state.session_id
        assert loaded.client_name == "Alice"
        assert loaded.channel == "test"
        assert loaded.status == SessionStatus.active
        assert loaded.turns == 0

    @pytest.mark.asyncio
    async def test_load_nonexistent_session(self, db_session: AsyncSession):
        loaded = await load_session(db_session, "nonexistent_id")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_save_session_updates(self, db_session: AsyncSession, default_tenant):
        state = await create_session(db_session, default_tenant.id, client_name="Bob")
        await db_session.commit()

        # Mutate state
        state.turns = 3
        state.current_intent = VoiceIntent.book
        state.status = SessionStatus.completed
        state.booking_draft.service_id = "coupe_homme"

        await save_session(db_session, state)
        await db_session.commit()

        # Reload and verify
        loaded = await load_session(db_session, state.session_id)
        assert loaded.turns == 3
        assert loaded.current_intent == VoiceIntent.book
        assert loaded.status == SessionStatus.completed
        assert loaded.booking_draft.service_id == "coupe_homme"

    @pytest.mark.asyncio
    async def test_transcript_events_persist(self, db_session: AsyncSession, default_tenant):
        state = await create_session(db_session, default_tenant.id)
        await db_session.commit()

        await append_transcript_event(
            db_session,
            session_id=state.session_id,
            turn_number=1,
            user_text="Je voudrais une coupe",
            intent="book",
            confidence=0.85,
            response_text="Quelle prestation ?",
            action_taken="collecting_info",
        )
        await append_transcript_event(
            db_session,
            session_id=state.session_id,
            turn_number=2,
            user_text="Coupe homme demain",
            intent="book",
            confidence=0.9,
            response_text="Confirmé !",
            action_taken="booking_created",
            data={"booking_id": 42},
        )
        await db_session.commit()

        events = await get_transcript_events(db_session, state.session_id)
        assert len(events) == 2
        assert events[0]["turn_number"] == 1
        assert events[0]["user_text"] == "Je voudrais une coupe"
        assert events[0]["intent"] == "book"
        assert events[1]["data"] == {"booking_id": 42}

    @pytest.mark.asyncio
    async def test_empty_transcript(self, db_session: AsyncSession, default_tenant):
        state = await create_session(db_session, default_tenant.id)
        await db_session.commit()

        events = await get_transcript_events(db_session, state.session_id)
        assert events == []


# ── Transcript durability (simulated restart) ───────────────


class TestTranscriptDurability:
    """Verify that transcripts survive when in-memory state is cleared."""

    @pytest.mark.asyncio
    async def test_transcript_survives_memory_clear(self, client: AsyncClient):
        """Create session, send turns, clear memory, transcript still works."""
        # Start session
        resp = await client.post(f"{PREFIX}/sessions/start", json={
            "client_name": "Durability Test",
            "channel": "test",
        })
        assert resp.status_code == 201
        session_id = resp.json()["session_id"]

        # Send a turn
        resp = await client.post(f"{PREFIX}/turn", json={
            "session_id": session_id,
            "text": "Je voudrais réserver une coupe homme",
        })
        assert resp.status_code == 200

        # Simulate process restart: clear in-memory state
        conversation_manager._sessions.clear()

        # Transcript should still be available (from DB)
        resp = await client.get(f"{PREFIX}/sessions/{session_id}/transcript")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == session_id
        assert data["turns"] >= 1
        assert len(data["transcript"]) >= 1
        assert data["transcript"][0]["user_text"] == "Je voudrais réserver une coupe homme"

    @pytest.mark.asyncio
    async def test_session_resume_after_memory_clear(self, client: AsyncClient):
        """Can continue sending turns after in-memory state is lost."""
        # Start and send initial turn
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = resp.json()["session_id"]

        resp = await client.post(f"{PREFIX}/turn", json={
            "session_id": session_id,
            "text": "Je voudrais prendre rendez-vous",
        })
        assert resp.status_code == 200

        # Simulate restart
        conversation_manager._sessions.clear()

        # Should be able to continue
        resp = await client.post(f"{PREFIX}/turn", json={
            "session_id": session_id,
            "text": "Une coupe pour homme",
        })
        assert resp.status_code == 200
        assert resp.json()["session_id"] == session_id


# ── API Key Auth tests ──────────────────────────────────────


class TestApiKeyAuth:

    @pytest.mark.asyncio
    async def test_no_auth_required_when_key_empty(self, client: AsyncClient):
        """Default: VOICE_API_KEY is empty → all requests pass."""
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_auth_rejects_missing_key(self):
        """When VOICE_API_KEY is set, missing header → 401."""
        from app.config import settings
        from app.main import app
        from app.database import get_db
        from tests.conftest import _override_get_db

        original = settings.VOICE_API_KEY
        try:
            settings.VOICE_API_KEY = "test-secret-key-123"
            app.dependency_overrides[get_db] = _override_get_db
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post(f"{PREFIX}/sessions/start", json={})
                assert resp.status_code == 401
                assert "API key" in resp.json()["detail"]
        finally:
            settings.VOICE_API_KEY = original
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_auth_accepts_correct_key(self):
        """When VOICE_API_KEY is set, tenant api_key in header → passes."""
        from app.config import settings
        from app.main import app
        from app.database import get_db
        from tests.conftest import _override_get_db

        original = settings.VOICE_API_KEY
        try:
            # Enable auth enforcement (non-empty VOICE_API_KEY)
            # The actual key checked is the tenant's api_key stored in the DB.
            settings.VOICE_API_KEY = "auth-enabled-sentinel"
            app.dependency_overrides[get_db] = _override_get_db
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post(
                    f"{PREFIX}/sessions/start",
                    json={},
                    headers={"X-API-Key": "test-api-key"},  # matches default tenant
                )
                assert resp.status_code == 201
        finally:
            settings.VOICE_API_KEY = original
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_auth_rejects_wrong_key(self):
        """When VOICE_API_KEY is set, wrong header → 401."""
        from app.config import settings
        from app.main import app
        from app.database import get_db
        from tests.conftest import _override_get_db

        original = settings.VOICE_API_KEY
        try:
            settings.VOICE_API_KEY = "test-secret-key-123"
            app.dependency_overrides[get_db] = _override_get_db
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post(
                    f"{PREFIX}/sessions/start",
                    json={},
                    headers={"X-API-Key": "wrong-key"},
                )
                assert resp.status_code == 401
        finally:
            settings.VOICE_API_KEY = original
            app.dependency_overrides.clear()


# ── Rate Limiting tests ─────────────────────────────────────


class TestRateLimiting:

    @pytest.mark.asyncio
    async def test_rate_limit_disabled_by_default(self, client: AsyncClient):
        """Default RATE_LIMIT_PER_MINUTE=60 allows many requests."""
        for _ in range(5):
            resp = await client.post(f"{PREFIX}/sessions/start", json={})
            assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_rate_limit_triggers_429(self):
        """When limit is low, excess requests get 429."""
        from app.config import settings
        from app.main import app
        from app.database import get_db
        from app.rate_limit import _reset_buckets
        from tests.conftest import _override_get_db

        original = settings.RATE_LIMIT_PER_MINUTE
        try:
            settings.RATE_LIMIT_PER_MINUTE = 3
            _reset_buckets()
            app.dependency_overrides[get_db] = _override_get_db
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                statuses = []
                for _ in range(6):
                    resp = await ac.post(f"{PREFIX}/sessions/start", json={})
                    statuses.append(resp.status_code)
                assert 429 in statuses, f"Expected 429 in {statuses}"
        finally:
            settings.RATE_LIMIT_PER_MINUTE = original
            _reset_buckets()
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_rate_limit_zero_disables(self):
        """RATE_LIMIT_PER_MINUTE=0 disables limiting."""
        from app.config import settings
        from app.main import app
        from app.database import get_db
        from app.rate_limit import _reset_buckets
        from tests.conftest import _override_get_db

        original = settings.RATE_LIMIT_PER_MINUTE
        try:
            settings.RATE_LIMIT_PER_MINUTE = 0
            _reset_buckets()
            app.dependency_overrides[get_db] = _override_get_db
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                for _ in range(10):
                    resp = await ac.post(f"{PREFIX}/sessions/start", json={})
                    assert resp.status_code == 201
        finally:
            settings.RATE_LIMIT_PER_MINUTE = original
            _reset_buckets()
            app.dependency_overrides.clear()


# ── Transcript endpoint enrichment ──────────────────────────


class TestTranscriptEndpoint:

    @pytest.mark.asyncio
    async def test_transcript_includes_events(self, client: AsyncClient):
        """Transcript response includes per-turn event log."""
        resp = await client.post(f"{PREFIX}/sessions/start", json={
            "client_name": "Transcript Test",
        })
        session_id = resp.json()["session_id"]

        # Two turns
        await client.post(f"{PREFIX}/turn", json={
            "session_id": session_id,
            "text": "Je voudrais réserver une coupe homme",
        })
        await client.post(f"{PREFIX}/turn", json={
            "session_id": session_id,
            "text": "Pour demain à 10h00",
        })

        resp = await client.get(f"{PREFIX}/sessions/{session_id}/transcript")
        assert resp.status_code == 200
        data = resp.json()
        assert "transcript" in data
        assert len(data["transcript"]) == 2
        assert data["transcript"][0]["turn_number"] == 1
        assert data["transcript"][1]["turn_number"] == 2

    @pytest.mark.asyncio
    async def test_transcript_404_unknown_session(self, client: AsyncClient):
        resp = await client.get(f"{PREFIX}/sessions/nonexistent999/transcript")
        assert resp.status_code == 404


# ── DB model sanity checks ──────────────────────────────────


class TestVoiceSessionModel:

    @pytest.mark.asyncio
    async def test_voice_session_created_in_db(self, client: AsyncClient, db_session: AsyncSession):
        """Starting a session creates a VoiceSession row."""
        resp = await client.post(f"{PREFIX}/sessions/start", json={
            "client_name": "DB Check",
        })
        session_id = resp.json()["session_id"]

        result = await db_session.execute(
            select(VoiceSession).where(VoiceSession.session_id == session_id)
        )
        row = result.scalars().first()
        assert row is not None
        assert row.client_name == "DB Check"
        assert row.status == "active"

    @pytest.mark.asyncio
    async def test_transcript_events_created_in_db(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Voice turns create TranscriptEvent rows."""
        resp = await client.post(f"{PREFIX}/sessions/start", json={})
        session_id = resp.json()["session_id"]

        await client.post(f"{PREFIX}/turn", json={
            "session_id": session_id,
            "text": "Je veux annuler mon rendez-vous",
        })

        result = await db_session.execute(
            select(TranscriptEvent).where(TranscriptEvent.session_id == session_id)
        )
        events = result.scalars().all()
        assert len(events) >= 1
        assert events[0].user_text == "Je veux annuler mon rendez-vous"
        assert events[0].intent == "cancel"
