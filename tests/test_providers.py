"""Tests for STT/TTS provider abstractions (app.providers).

Phase 3 coverage (preserved):
  - MockSTTProvider / MockTTSProvider behaviour
  - Factory produces mock providers

Phase 4 additions:
  - Factory fallback: unknown provider → mock (with warning)
  - Factory fallback: real provider requested without credentials → mock
  - Real-provider adapter scaffolds are registered and instantiable
  - DeepgramSTTProvider / ElevenLabsTTSProvider basic unit tests
  - Config-driven provider selection via get_stt/tts_provider
"""

from __future__ import annotations

import logging

import pytest

from app.providers import (
    AudioFormat,
    DeepgramSTTProvider,
    ElevenLabsTTSProvider,
    MockSTTProvider,
    MockTTSProvider,
    STTProvider,
    TTSProvider,
    get_stt_provider,
    get_tts_provider,
)


# ── MockSTTProvider (Phase 3 — unchanged) ──────────────────


class TestMockSTTProvider:

    @pytest.mark.asyncio
    async def test_transcribe_returns_configured_text(self):
        stt = MockSTTProvider(default_transcript="Bonjour le monde")
        result = await stt.transcribe(b"fake_audio_data")
        assert result.transcript == "Bonjour le monde"
        assert result.confidence == 0.95
        assert result.language == "fr"
        assert result.provider == "mock"

    @pytest.mark.asyncio
    async def test_transcribe_empty_default(self):
        stt = MockSTTProvider()
        result = await stt.transcribe(b"audio")
        assert result.transcript == ""
        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_transcribe_estimates_duration(self):
        stt = MockSTTProvider(default_transcript="test")
        # 32000 bytes ≈ 1 second at 16kHz 16-bit mono
        result = await stt.transcribe(b"\x00" * 32000)
        assert result.duration_ms == 1000

    @pytest.mark.asyncio
    async def test_provider_name(self):
        assert MockSTTProvider().provider_name == "mock"


# ── MockTTSProvider (Phase 3 — unchanged) ──────────────────


class TestMockTTSProvider:

    @pytest.mark.asyncio
    async def test_synthesize_returns_metadata(self):
        tts = MockTTSProvider()
        result = await tts.synthesize("Bonjour et bienvenue")
        assert result.audio_url is None  # Mock doesn't generate real audio
        assert result.audio_format == AudioFormat.wav
        assert result.duration_ms > 0
        assert result.sample_rate == 22050
        assert result.provider == "mock"
        assert len(result.text_hash) == 16

    @pytest.mark.asyncio
    async def test_synthesize_duration_scales_with_text(self):
        tts = MockTTSProvider()
        short = await tts.synthesize("Bonjour")
        long = await tts.synthesize("Bonjour et bienvenue chez Maison Éclat comment allez vous")
        assert long.duration_ms > short.duration_ms

    @pytest.mark.asyncio
    async def test_provider_name(self):
        assert MockTTSProvider().provider_name == "mock"


# ── Factory — basic (Phase 3 — unchanged) ──────────────────


class TestProviderFactory:

    def test_get_stt_mock(self):
        provider = get_stt_provider("mock")
        assert isinstance(provider, STTProvider)
        assert provider.provider_name == "mock"

    def test_get_tts_mock(self):
        provider = get_tts_provider("mock")
        assert isinstance(provider, TTSProvider)
        assert provider.provider_name == "mock"


# ── Factory — fallback logic (Phase 4) ─────────────────────


class TestProviderFallback:
    """Factory falls back to mock when credentials are missing or provider is unknown."""

    def test_stt_unknown_provider_falls_back_to_mock(self, caplog):
        """Unknown provider name → mock + warning (not ValueError)."""
        with caplog.at_level(logging.WARNING, logger="app.providers"):
            provider = get_stt_provider("nonexistent_provider")
        assert isinstance(provider, MockSTTProvider)
        assert provider.provider_name == "mock"
        assert "Unknown STT provider" in caplog.text

    def test_tts_unknown_provider_falls_back_to_mock(self, caplog):
        """Unknown provider name → mock + warning (not ValueError)."""
        with caplog.at_level(logging.WARNING, logger="app.providers"):
            provider = get_tts_provider("nonexistent_provider")
        assert isinstance(provider, MockTTSProvider)
        assert provider.provider_name == "mock"
        assert "Unknown TTS provider" in caplog.text

    def test_stt_deepgram_without_key_falls_back(self, caplog):
        """Deepgram requested but no api_key → mock + warning."""
        with caplog.at_level(logging.WARNING, logger="app.providers"):
            provider = get_stt_provider("deepgram")
        assert isinstance(provider, MockSTTProvider)
        assert "missing credentials" in caplog.text.lower()

    def test_stt_deepgram_with_empty_key_falls_back(self, caplog):
        """Deepgram requested with empty api_key → mock + warning."""
        with caplog.at_level(logging.WARNING, logger="app.providers"):
            provider = get_stt_provider("deepgram", api_key="")
        assert isinstance(provider, MockSTTProvider)
        assert "missing credentials" in caplog.text.lower()

    def test_tts_elevenlabs_without_key_falls_back(self, caplog):
        """ElevenLabs requested but no api_key → mock + warning."""
        with caplog.at_level(logging.WARNING, logger="app.providers"):
            provider = get_tts_provider("elevenlabs")
        assert isinstance(provider, MockTTSProvider)
        assert "missing credentials" in caplog.text.lower()

    def test_tts_elevenlabs_with_empty_key_falls_back(self, caplog):
        """ElevenLabs requested with empty api_key → mock + warning."""
        with caplog.at_level(logging.WARNING, logger="app.providers"):
            provider = get_tts_provider("elevenlabs", api_key="")
        assert isinstance(provider, MockTTSProvider)
        assert "missing credentials" in caplog.text.lower()


# ── Real provider instantiation (Phase 4) ──────────────────


class TestRealProviderScaffold:
    """Real providers can be instantiated with credentials (no network calls)."""

    def test_deepgram_stt_instantiates_with_key(self):
        """Deepgram provider instantiates when api_key is provided."""
        provider = get_stt_provider("deepgram", api_key="test-key-abc123")
        assert isinstance(provider, DeepgramSTTProvider)
        assert provider.provider_name == "deepgram"

    def test_elevenlabs_tts_instantiates_with_key(self):
        """ElevenLabs provider instantiates when api_key is provided."""
        provider = get_tts_provider("elevenlabs", api_key="test-key-abc123")
        assert isinstance(provider, ElevenLabsTTSProvider)
        assert provider.provider_name == "elevenlabs"

    def test_deepgram_stt_accepts_model_override(self):
        """Deepgram accepts optional model kwarg."""
        provider = get_stt_provider("deepgram", api_key="key", model="nova-2-general")
        assert isinstance(provider, DeepgramSTTProvider)

    def test_elevenlabs_tts_accepts_voice_id(self):
        """ElevenLabs accepts optional voice_id kwarg."""
        provider = get_tts_provider(
            "elevenlabs", api_key="key", voice_id="custom-voice-id"
        )
        assert isinstance(provider, ElevenLabsTTSProvider)

    def test_deepgram_is_stt_provider(self):
        """DeepgramSTTProvider satisfies STTProvider interface."""
        assert issubclass(DeepgramSTTProvider, STTProvider)

    def test_elevenlabs_is_tts_provider(self):
        """ElevenLabsTTSProvider satisfies TTSProvider interface."""
        assert issubclass(ElevenLabsTTSProvider, TTSProvider)


# ── Config-driven selection (Phase 4) ──────────────────────


class TestConfigDrivenSelection:
    """Verify that mock default works and real provider selection is seamless."""

    def test_default_provider_is_mock(self):
        """With no args, factory returns mock."""
        assert get_stt_provider().provider_name == "mock"
        assert get_tts_provider().provider_name == "mock"

    def test_mock_explicit(self):
        """Explicit mock selection works."""
        stt = get_stt_provider("mock", default_transcript="hello")
        assert isinstance(stt, MockSTTProvider)
        tts = get_tts_provider("mock")
        assert isinstance(tts, MockTTSProvider)

    def test_real_provider_with_credentials_returns_real(self):
        """When credentials are present, factory returns the real provider."""
        stt = get_stt_provider("deepgram", api_key="valid-key")
        assert stt.provider_name == "deepgram"
        tts = get_tts_provider("elevenlabs", api_key="valid-key")
        assert tts.provider_name == "elevenlabs"
