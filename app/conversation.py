"""
In-memory conversation state manager for voice sessions.

Tracks per-session state:
  - Current intent being fulfilled
  - Booking draft fields collected so far
  - Conversation turn count
  - Timestamps for session lifecycle

Designed for single-process local deployment.  For multi-process / HA,
replace the dict store with Redis or similar.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.voice_schemas import BookingDraft, SessionStatus, VoiceIntent


@dataclass
class ConversationState:
    """Mutable state for a single voice session."""
    session_id: str
    tenant_id: int = 0
    status: SessionStatus = SessionStatus.active
    current_intent: VoiceIntent | None = None
    booking_draft: BookingDraft = field(default_factory=BookingDraft)
    turns: int = 0
    client_name: str | None = None
    client_phone: str | None = None
    channel: str = "phone"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # LLM conversation history (OpenAI messages format, no system message)
    messages: list = field(default_factory=list)
    # RGPD consent: None=pending, True=accepted (implicit), False=refused
    consent_given: bool | None = None
    consent_at: datetime | None = None
    # Persistent fallback counter — survives Twilio retries via DB
    consecutive_fallbacks: int = 0

    def touch(self) -> None:
        """Update last_activity timestamp."""
        self.last_activity = datetime.now(timezone.utc)

    def increment_turn(self) -> None:
        """Record a new user message turn."""
        self.turns += 1
        self.touch()

    def update_draft(self, **kwargs) -> None:
        """Merge new field values into the booking draft."""
        for key, value in kwargs.items():
            if value is not None and hasattr(self.booking_draft, key):
                setattr(self.booking_draft, key, value)
        # Also propagate client info to draft if available
        if self.client_name and not self.booking_draft.client_name:
            self.booking_draft.client_name = self.client_name
        if self.client_phone and not self.booking_draft.client_phone:
            self.booking_draft.client_phone = self.client_phone

    def missing_booking_fields(self) -> list[str]:
        """Return list of required booking fields not yet filled."""
        required = ["service_id", "date", "time"]
        return [f for f in required if not getattr(self.booking_draft, f)]

    @property
    def duration_seconds(self) -> float:
        """Session duration in seconds."""
        return (self.last_activity - self.created_at).total_seconds()


class ConversationManager:
    """
    In-memory store for active voice sessions.

    Thread-safety note: safe for single-worker async (FastAPI default).
    For multi-worker, replace _sessions dict with shared store.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ConversationState] = {}

    def create_session(
        self,
        client_name: str | None = None,
        client_phone: str | None = None,
        channel: str = "phone",
    ) -> ConversationState:
        """Create and register a new conversation session."""
        session_id = uuid.uuid4().hex[:12]
        state = ConversationState(
            session_id=session_id,
            client_name=client_name,
            client_phone=client_phone,
            channel=channel,
        )
        self._sessions[session_id] = state
        return state

    def get_session(self, session_id: str) -> ConversationState | None:
        """Retrieve a session by ID, or None if not found / expired."""
        return self._sessions.get(session_id)

    def end_session(self, session_id: str) -> ConversationState | None:
        """Mark a session as completed and return its final state."""
        state = self._sessions.get(session_id)
        if state:
            state.status = SessionStatus.completed
            state.touch()
        return state

    def remove_session(self, session_id: str) -> None:
        """Remove a session from the store entirely."""
        self._sessions.pop(session_id, None)

    @property
    def active_count(self) -> int:
        """Number of currently active sessions."""
        return sum(1 for s in self._sessions.values() if s.status == SessionStatus.active)

    def list_sessions(self) -> list[ConversationState]:
        """Return all sessions (for debugging)."""
        return list(self._sessions.values())


# ── Module-level singleton ───────────────────────────────────
# Shared across the FastAPI app lifetime.

conversation_manager = ConversationManager()
