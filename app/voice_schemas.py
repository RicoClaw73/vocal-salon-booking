"""
Pydantic v2 schemas for the voice-pipeline integration layer.

Defines request/response models for voice session webhook-style events:
  - session.start   → open a new voice conversation
  - user_message     → process transcribed speech (STT output)
  - session.end      → close the conversation

These schemas are designed to sit between a local STT/TTS pipeline and the
existing booking API — no external paid services required.

Phase 5.2: Added audio payload metadata to VoiceTurnRequest (audio_format,
sample_rate, audio_encoding, audio_content_type) and tts_audio_url to
VoiceTurnResponse for real audio path readiness.
"""

from __future__ import annotations

import enum
from datetime import datetime
from pydantic import BaseModel, Field, model_validator


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


_VALID_AUDIO_FORMATS = {"wav", "mp3", "ogg", "pcm"}
_VALID_SAMPLE_RATES = {8000, 16000, 22050, 44100, 48000}
_VALID_ENCODINGS = {"linear16", "mulaw", "alaw", "opus", "mp3", "ogg_vorbis"}


class VoiceTurnRequest(BaseModel):
    """
    Unified voice turn request — accepts text OR audio payload metadata.

    **Text-only mode** (backward compat): pass ``text`` or ``mock_transcript``.
    **Audio mode** (Phase 5.2): pass ``audio_base64`` (base64-encoded audio bytes)
    along with ``audio_format``, ``audio_sample_rate``, and ``audio_encoding``
    to send real audio through the STT pipeline.

    At least one of ``text``, ``mock_transcript``, or ``audio_base64`` is required.
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
    # ── Audio payload metadata (Phase 5.2) ─────────────────────
    audio_base64: str | None = Field(
        None,
        description="Base64-encoded audio bytes for real STT processing.",
    )
    audio_format: str | None = Field(
        None,
        description="Audio format: wav, mp3, ogg, pcm.",
    )
    audio_sample_rate: int | None = Field(
        None,
        description="Audio sample rate in Hz (e.g. 16000, 44100).",
    )
    audio_encoding: str | None = Field(
        None,
        description="Audio encoding: linear16, mulaw, alaw, opus, mp3, ogg_vorbis.",
    )
    audio_content_type: str | None = Field(
        None,
        description="MIME content type (e.g. audio/wav, audio/mpeg). Informational.",
    )
    # ── End audio payload metadata ──────────────────────────────
    client_name: str | None = Field(None, max_length=120)
    client_phone: str | None = Field(None, max_length=30)
    channel: str = Field("phone", description="Originating channel: phone, web, test")

    @model_validator(mode="after")
    def _validate_audio_metadata(self) -> "VoiceTurnRequest":
        """Validate audio metadata fields when audio_base64 is provided."""
        if self.audio_base64 is not None:
            # Validate format
            if self.audio_format and self.audio_format not in _VALID_AUDIO_FORMATS:
                raise ValueError(
                    f"audio_format must be one of {sorted(_VALID_AUDIO_FORMATS)}, "
                    f"got '{self.audio_format}'"
                )
            # Validate sample rate
            if self.audio_sample_rate and self.audio_sample_rate not in _VALID_SAMPLE_RATES:
                raise ValueError(
                    f"audio_sample_rate must be one of {sorted(_VALID_SAMPLE_RATES)}, "
                    f"got {self.audio_sample_rate}"
                )
            # Validate encoding
            if self.audio_encoding and self.audio_encoding not in _VALID_ENCODINGS:
                raise ValueError(
                    f"audio_encoding must be one of {sorted(_VALID_ENCODINGS)}, "
                    f"got '{self.audio_encoding}'"
                )
        return self


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
    tts_audio_url: str | None = Field(
        None,
        description="URL/path to the persisted TTS audio artifact (Phase 5.2). "
        "None when TTS persistence is disabled or mock provider is used.",
    )
    provider_errors: list[dict] | None = Field(
        None,
        description="Provider error details if fallback/errors occurred (Phase 5.1)",
    )
