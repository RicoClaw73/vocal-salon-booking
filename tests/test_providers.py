"""Tests for STT/TTS provider abstractions (app.providers)."""

from __future__ import annotations

import pytest

from app.providers import (
    AudioFormat,
    MockSTTProvider,
    MockTTSProvider,
    STTProvider,
    TTSProvider,
    get_stt_provider,
    get_tts_provider,
)


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


class TestProviderFactory:

    def test_get_stt_mock(self):
        provider = get_stt_provider("mock")
        assert isinstance(provider, STTProvider)
        assert provider.provider_name == "mock"

    def test_get_tts_mock(self):
        provider = get_tts_provider("mock")
        assert isinstance(provider, TTSProvider)
        assert provider.provider_name == "mock"

    def test_get_stt_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown STT provider"):
            get_stt_provider("whisper")

    def test_get_tts_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown TTS provider"):
            get_tts_provider("elevenlabs")
