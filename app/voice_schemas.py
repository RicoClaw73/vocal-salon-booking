"""
Pydantic v2 schemas for the voice-pipeline integration layer.

Defines request/response models for voice session webhook-style events:
  - session.start   → open a new voice conversation
  - user_message     → process transcribed speech (STT output)
  - session.end      → close the conversation

These schemas are designed to sit between a local STT/TTS pipeline and the
existing booking API — no external paid services required.
"""

from __future__ import annotations

import enum
from datetime import datetime
from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────

class VoiceIntent(str, enum.Enum):
    """Intents the voice layer can detect from user speech."""
    book = "book"
    reschedule = "reschedule"
    cancel = "cancel"
    check_availability = "check_availability"
    unknown = "unknown"


class SessionStatus(str, enum.Enum):
    """Lifecycle states for a voice session."""
    active = "active"
    completed = "completed"
    expired = "expired"


# ── Session Start ────────────────────────────────────────────

class SessionStartRequest(BaseModel):
    """Webhook payload to open a new voice session."""
    client_name: str | None = Field(None, max_length=120, description="Caller name if known")
    client_phone: str | None = Field(None, max_length=30, description="Caller phone if known")
    channel: str = Field("phone", description="Originating channel: phone, web, test")


class SessionStartResponse(BaseModel):
    """Returned when a voice session is created."""
    session_id: str
    status: SessionStatus
    greeting: str = Field(..., description="Initial greeting to send to TTS")
    created_at: datetime


# ── User Message (STT → LLM → TTS) ─────────────────────────

class UserMessageRequest(BaseModel):
    """A transcribed user utterance to process."""
    session_id: str
    text: str = Field(..., min_length=1, description="Transcribed speech from STT")


class BookingDraft(BaseModel):
    """Fields collected so far for a booking-in-progress."""
    service_id: str | None = None
    service_label: str | None = None
    employee_id: str | None = None
    employee_name: str | None = None
    date: str | None = None  # YYYY-MM-DD
    time: str | None = None  # HH:MM
    client_name: str | None = None
    client_phone: str | None = None


class UserMessageResponse(BaseModel):
    """Response to a processed user message — drives TTS output."""
    session_id: str
    intent: VoiceIntent
    response_text: str = Field(..., description="Text to send to TTS")
    booking_draft: BookingDraft | None = None
    action_taken: str | None = Field(None, description="e.g. 'booking_created', 'slots_found'")
    data: dict | None = Field(None, description="Structured data (slots, booking details, etc.)")


# ── Session End ──────────────────────────────────────────────

class SessionEndRequest(BaseModel):
    """Webhook payload to close a voice session."""
    session_id: str
    reason: str = Field("user_hangup", description="Why the session ended")


class SessionEndResponse(BaseModel):
    """Returned when a voice session is closed."""
    session_id: str
    status: SessionStatus
    message: str
    turns: int = Field(..., description="Number of user messages processed")
    duration_seconds: float | None = None
