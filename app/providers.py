"""
Provider abstraction interfaces for STT (Speech-to-Text) and TTS (Text-to-Speech).

Phase 3: local mock implementations that require no external credentials.
Interface contracts are designed to be swappable for real providers
(Whisper, Deepgram, ElevenLabs, etc.) in later phases.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


# ── Data types ──────────────────────────────────────────────


class AudioFormat(str, Enum):
    """Supported audio formats for STT/TTS."""
    wav = "wav"
    mp3 = "mp3"
    ogg = "ogg"
    pcm = "pcm"


@dataclass(frozen=True)
class STTResult:
    """Result of speech-to-text transcription."""
    transcript: str
    confidence: float           # 0.0–1.0
    language: str               # ISO 639-1, e.g. "fr"
    duration_ms: int            # Audio duration in milliseconds
    provider: str               # e.g. "mock", "whisper", "deepgram"


@dataclass(frozen=True)
class TTSResult:
    """Result of text-to-speech synthesis."""
    audio_url: str | None       # URL or path to generated audio (None for mock)
    audio_format: AudioFormat
    duration_ms: int            # Estimated audio duration in milliseconds
    sample_rate: int            # e.g. 16000, 22050, 44100
    provider: str               # e.g. "mock", "elevenlabs", "google"
    text_hash: str              # Hash of input text for cache keying


# ── STT Interface ───────────────────────────────────────────


class STTProvider(ABC):
    """Abstract interface for Speech-to-Text providers."""

    @abstractmethod
    async def transcribe(
        self,
        audio_bytes: bytes,
        audio_format: AudioFormat = AudioFormat.wav,
        language: str = "fr",
    ) -> STTResult:
        """
        Transcribe audio bytes to text.

        Args:
            audio_bytes: Raw audio data.
            audio_format: Format of the audio data.
            language: Expected language (ISO 639-1).

        Returns:
            STTResult with transcript and metadata.
        """
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider identifier."""
        ...


class TTSProvider(ABC):
    """Abstract interface for Text-to-Speech providers."""

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        language: str = "fr",
        voice_id: str | None = None,
    ) -> TTSResult:
        """
        Synthesize text to audio.

        Args:
            text: Text to convert to speech.
            language: Target language (ISO 639-1).
            voice_id: Optional voice identifier (provider-specific).

        Returns:
            TTSResult with audio metadata.
        """
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider identifier."""
        ...


# ── Mock implementations (local, no credentials) ───────────


class MockSTTProvider(STTProvider):
    """
    Mock STT that returns a pre-configured transcript.

    Used for local development and testing — no external services needed.
    Accepts an optional transcript to return; if audio_bytes are provided,
    they are ignored (mock doesn't process real audio).
    """

    def __init__(self, default_transcript: str = "") -> None:
        self._default_transcript = default_transcript

    async def transcribe(
        self,
        audio_bytes: bytes,
        audio_format: AudioFormat = AudioFormat.wav,
        language: str = "fr",
    ) -> STTResult:
        """Return mock transcript. Simulates ~200ms processing latency estimation."""
        # Estimate duration from audio bytes (rough: 16kHz, 16-bit mono = 32KB/s)
        estimated_duration_ms = max(len(audio_bytes) * 1000 // 32000, 100) if audio_bytes else 0

        return STTResult(
            transcript=self._default_transcript,
            confidence=0.95 if self._default_transcript else 0.0,
            language=language,
            duration_ms=estimated_duration_ms,
            provider="mock",
        )

    @property
    def provider_name(self) -> str:
        return "mock"


class MockTTSProvider(TTSProvider):
    """
    Mock TTS that returns audio metadata without generating real audio.

    Used for local development and testing — no external services needed.
    Estimates audio duration from text length (~150ms per word for French).
    """

    MS_PER_WORD: int = 150  # Average speaking rate in French

    async def synthesize(
        self,
        text: str,
        language: str = "fr",
        voice_id: str | None = None,
    ) -> TTSResult:
        """Return mock audio metadata. No real audio is generated."""
        word_count = len(text.split())
        estimated_duration = word_count * self.MS_PER_WORD

        text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]

        return TTSResult(
            audio_url=None,  # No real audio in mock mode
            audio_format=AudioFormat.wav,
            duration_ms=estimated_duration,
            sample_rate=22050,
            provider="mock",
            text_hash=text_hash,
        )

    @property
    def provider_name(self) -> str:
        return "mock"


# ── Factory / Registry ──────────────────────────────────────


def get_stt_provider(provider: str = "mock", **kwargs) -> STTProvider:
    """Factory to instantiate an STT provider by name."""
    providers: dict[str, type[STTProvider]] = {
        "mock": MockSTTProvider,
    }
    cls = providers.get(provider)
    if not cls:
        raise ValueError(f"Unknown STT provider: '{provider}'. Available: {list(providers.keys())}")
    return cls(**kwargs)


def get_tts_provider(provider: str = "mock", **kwargs) -> TTSProvider:
    """Factory to instantiate a TTS provider by name."""
    providers: dict[str, type[TTSProvider]] = {
        "mock": MockTTSProvider,
    }
    cls = providers.get(provider)
    if not cls:
        raise ValueError(f"Unknown TTS provider: '{provider}'. Available: {list(providers.keys())}")
    return cls(**kwargs)
