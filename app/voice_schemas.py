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


# ── Voice Turn (Phase 3 – orchestration endpoint) ──────────

class AudioMeta(BaseModel):
    """Metadata about an audio segment (mock or real)."""
    format: str = Field("wav", description="Audio format: wav, mp3, ogg, pcm")
    duration_ms: int = Field(0, description="Audio duration in milliseconds")
    sample_rate: int = Field(22050, description="Audio sample rate in Hz")
    provider: str = Field("mock", description="STT/TTS provider used")


class VoiceTurnRequest(BaseModel):
    """
    Unified voice turn request — accepts text OR mock transcript payload.

    For real pipelines: audio_bytes would be transcribed via STT.
    For testing/dev: pass text directly and skip STT.
    """
    session_id: str | None = Field(
        None,
        description="Existing session ID. If None, a new session is created automatically.",
    )
    text: str | None = Field(
        None,
        description="Pre-transcribed text (skips STT). Use for testing or text-only mode.",
    )
    mock_transcript: str | None = Field(
        None,
        description="Mock transcript to simulate STT output. Equivalent to text.",
    )
    client_name: str | None = Field(None, max_length=120)
    client_phone: str | None = Field(None, max_length=30)
    channel: str = Field("phone", description="Originating channel: phone, web, test")


class VoiceTurnResponse(BaseModel):
    """
    Response from a single voice turn through the full loop.

    Contains the assistant's reply text, intent metadata, and optional
    audio metadata for the TTS output.
    """
    session_id: str
    turn_number: int
    intent: VoiceIntent
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Intent detection confidence"
    )
    response_text: str = Field(..., description="Assistant reply text (sent to TTS)")
    is_fallback: bool = Field(
        False,
        description="True if the response used the low-confidence fallback strategy",
    )
    booking_draft: BookingDraft | None = None
    action_taken: str | None = None
    data: dict | None = None
    stt_meta: AudioMeta | None = Field(
        None, description="STT input audio metadata (when audio was processed)"
    )
    tts_meta: AudioMeta | None = Field(
        None, description="TTS output audio metadata"
    )
