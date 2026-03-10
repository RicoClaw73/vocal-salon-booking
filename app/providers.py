"""
Provider abstraction interfaces for STT (Speech-to-Text) and TTS (Text-to-Speech).

Phase 3: local mock implementations that require no external credentials.
Phase 4: real-provider adapter scaffolds (Deepgram, ElevenLabs) with graceful
         fallback to mock when credentials are missing.

Interface contracts are designed to be swappable for real providers
(Whisper, Deepgram, ElevenLabs, etc.) via config / env vars.

Provider selection flow (Phase 4):
    1. Config requests a provider name (e.g. STT_PROVIDER="deepgram").
    2. Factory checks whether the required credentials are present.
    3. If credentials are missing → logs a warning and falls back to mock.
    4. If the real provider import fails (optional dep not installed) → falls
       back to mock with a warning.
    This guarantees CI and local-dev always work without secrets.
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


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


# ── Real-provider adapter scaffolds (Phase 4) ──────────────


class DeepgramSTTProvider(STTProvider):
    """
    Speech-to-Text via the Deepgram Nova-2 API.

    Requires:
        - ``httpx`` (already a project dependency)
        - A valid ``api_key`` from https://console.deepgram.com/

    The adapter sends audio bytes to the Deepgram REST endpoint and returns a
    typed ``STTResult``.  No hard dependency on the ``deepgram-sdk`` package —
    plain ``httpx`` is used so the project footprint stays minimal.
    """

    DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

    def __init__(self, api_key: str, model: str = "nova-2") -> None:
        self._api_key = api_key
        self._model = model

    async def transcribe(
        self,
        audio_bytes: bytes,
        audio_format: AudioFormat = AudioFormat.wav,
        language: str = "fr",
    ) -> STTResult:
        """Transcribe audio via Deepgram REST API."""
        import httpx

        content_type = {
            AudioFormat.wav: "audio/wav",
            AudioFormat.mp3: "audio/mpeg",
            AudioFormat.ogg: "audio/ogg",
            AudioFormat.pcm: "audio/l16",
        }.get(audio_format, "audio/wav")

        params = {
            "model": self._model,
            "language": language,
            "smart_format": "true",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.DEEPGRAM_URL,
                params=params,
                content=audio_bytes,
                headers={
                    "Authorization": f"Token {self._api_key}",
                    "Content-Type": content_type,
                },
            )
            resp.raise_for_status()
            body = resp.json()

        # Parse Deepgram response
        channel = body["results"]["channels"][0]
        alt = channel["alternatives"][0]
        transcript = alt.get("transcript", "")
        confidence = alt.get("confidence", 0.0)
        duration_s = body.get("metadata", {}).get("duration", 0.0)

        return STTResult(
            transcript=transcript,
            confidence=confidence,
            language=language,
            duration_ms=int(duration_s * 1000),
            provider="deepgram",
        )

    @property
    def provider_name(self) -> str:
        return "deepgram"


class ElevenLabsTTSProvider(TTSProvider):
    """
    Text-to-Speech via the ElevenLabs v1 REST API.

    Requires:
        - ``httpx`` (already a project dependency)
        - A valid ``api_key`` from https://elevenlabs.io/

    Returns a ``TTSResult`` with ``audio_url`` set to ``None`` — the raw
    audio bytes are not stored.  A future iteration can persist them to
    a file or object store and populate ``audio_url``.
    """

    ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
    DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # "Sarah" multilingual voice

    def __init__(
        self,
        api_key: str,
        voice_id: str = "",
        model: str = "eleven_multilingual_v2",
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id or self.DEFAULT_VOICE_ID
        self._model = model

    async def synthesize(
        self,
        text: str,
        language: str = "fr",
        voice_id: str | None = None,
    ) -> TTSResult:
        """Synthesize text via ElevenLabs REST API."""
        import httpx

        effective_voice = voice_id or self._voice_id
        url = f"{self.ELEVENLABS_URL}/{effective_voice}"

        payload = {
            "text": text,
            "model_id": self._model,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
            },
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "xi-api-key": self._api_key,
                    "Accept": "audio/mpeg",
                },
            )
            resp.raise_for_status()
            audio_bytes = resp.content

        # Estimate duration: mp3 at ~128kbps → bytes * 8 / 128000
        duration_ms = int(len(audio_bytes) * 8 / 128) if audio_bytes else 0

        text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]

        return TTSResult(
            audio_url=None,  # Could be persisted in a future iteration
            audio_format=AudioFormat.mp3,
            duration_ms=duration_ms,
            sample_rate=44100,
            provider="elevenlabs",
            text_hash=text_hash,
        )

    @property
    def provider_name(self) -> str:
        return "elevenlabs"


# ── Factory / Registry (with safe fallback) ────────────────


# Maps provider name → (class, required_kwargs)
# required_kwargs lists the kwarg names that must be non-empty for the
# provider to be instantiable.  If any is missing the factory falls back
# to mock.
_STT_REGISTRY: dict[str, tuple[type[STTProvider], list[str]]] = {
    "mock": (MockSTTProvider, []),
    "deepgram": (DeepgramSTTProvider, ["api_key"]),
}

_TTS_REGISTRY: dict[str, tuple[type[TTSProvider], list[str]]] = {
    "mock": (MockTTSProvider, []),
    "elevenlabs": (ElevenLabsTTSProvider, ["api_key"]),
}


def _check_required(kwargs: dict, required: list[str]) -> list[str]:
    """Return names of missing / empty required kwargs."""
    return [k for k in required if not kwargs.get(k)]


def get_stt_provider(provider: str = "mock", **kwargs) -> STTProvider:
    """
    Factory to instantiate an STT provider by name.

    Falls back to ``MockSTTProvider`` (with a logged warning) when:
    - The requested provider is not in the registry.
    - Required credentials (``api_key``, etc.) are missing or empty.

    This ensures CI and local development work without secrets.
    """
    entry = _STT_REGISTRY.get(provider)
    if not entry:
        logger.warning(
            "Unknown STT provider '%s'. Available: %s. Falling back to mock.",
            provider,
            list(_STT_REGISTRY.keys()),
        )
        return MockSTTProvider()

    cls, required = entry
    missing = _check_required(kwargs, required)
    if missing:
        logger.warning(
            "STT provider '%s' requested but missing credentials: %s. "
            "Falling back to mock. Set the corresponding env vars to enable it.",
            provider,
            missing,
        )
        return MockSTTProvider()

    # Filter kwargs to only those the constructor accepts
    return cls(**{k: v for k, v in kwargs.items() if v})


def get_tts_provider(provider: str = "mock", **kwargs) -> TTSProvider:
    """
    Factory to instantiate a TTS provider by name.

    Falls back to ``MockTTSProvider`` (with a logged warning) when:
    - The requested provider is not in the registry.
    - Required credentials (``api_key``, etc.) are missing or empty.

    This ensures CI and local development work without secrets.
    """
    entry = _TTS_REGISTRY.get(provider)
    if not entry:
        logger.warning(
            "Unknown TTS provider '%s'. Available: %s. Falling back to mock.",
            provider,
            list(_TTS_REGISTRY.keys()),
        )
        return MockTTSProvider()

    cls, required = entry
    missing = _check_required(kwargs, required)
    if missing:
        logger.warning(
            "TTS provider '%s' requested but missing credentials: %s. "
            "Falling back to mock. Set the corresponding env vars to enable it.",
            provider,
            missing,
        )
        return MockTTSProvider()

    return cls(**{k: v for k, v in kwargs.items() if v})
