"""Tests for Phase 5.1 provider readiness, error classification, and safe wrappers."""

from __future__ import annotations

import pytest

from app.providers import (
    AudioFormat,
    MockSTTProvider,
    MockTTSProvider,
    ProviderErrorKind,
    ProviderOutcome,
    ProviderStatus,
    STTResult,
    TTSResult,
    check_provider_readiness,
    safe_synthesize,
    safe_transcribe,
)


# ── check_provider_readiness ──────────────────────────────────


class TestProviderReadiness:
    """Tests for the check_provider_readiness utility."""

    def test_all_mock_is_ready(self):
        result = check_provider_readiness(
            stt_requested="mock", stt_api_key="",
            tts_requested="mock", tts_api_key="",
        )
        assert result["all_ready"] is True
        assert len(result["providers"]) == 2
        for p in result["providers"]:
            assert p["active"] == "mock"
            assert p["is_fallback"] is False
            assert p["ready"] is True

    def test_real_provider_with_key_is_ready(self):
        result = check_provider_readiness(
            stt_requested="deepgram", stt_api_key="dg_live_xxx",
            tts_requested="elevenlabs", tts_api_key="sk_xxx",
        )
        assert result["all_ready"] is True
        stt = result["providers"][0]
        assert stt["requested"] == "deepgram"
        assert stt["active"] == "deepgram"
        assert stt["credentials_present"] is True
        assert stt["is_fallback"] is False

    def test_real_provider_without_key_not_ready(self):
        result = check_provider_readiness(
            stt_requested="deepgram", stt_api_key="",
            tts_requested="elevenlabs", tts_api_key="",
        )
        assert result["all_ready"] is False
        for p in result["providers"]:
            assert p["is_fallback"] is True
            assert p["active"] == "mock"
            assert p["ready"] is False

    def test_unknown_provider_falls_back_ready(self):
        result = check_provider_readiness(
            stt_requested="nonexistent", stt_api_key="key",
            tts_requested="mock", tts_api_key="",
        )
        assert result["all_ready"] is True
        stt = result["providers"][0]
        assert stt["active"] == "mock"
        assert stt["is_fallback"] is True

    def test_mixed_readiness(self):
        result = check_provider_readiness(
            stt_requested="deepgram", stt_api_key="key",
            tts_requested="elevenlabs", tts_api_key="",
        )
        assert result["all_ready"] is False
        stt = result["providers"][0]
        tts = result["providers"][1]
        assert stt["ready"] is True
        assert tts["ready"] is False


# ── Error classification helpers ──────────────────────────────


class TestProviderErrorKind:
    """Verify ProviderErrorKind enum and ProviderOutcome dataclass."""

    def test_error_kinds_exist(self):
        assert ProviderErrorKind.config_missing == "config_missing"
        assert ProviderErrorKind.provider_timeout == "provider_timeout"
        assert ProviderErrorKind.provider_http_error == "provider_http_error"
        assert ProviderErrorKind.provider_error == "provider_error"
        assert ProviderErrorKind.fallback_used == "fallback_used"

    def test_outcome_success(self):
        o = ProviderOutcome(success=True)
        assert o.success is True
        assert o.error_kind is None
        assert o.fallback_used is False

    def test_outcome_failure(self):
        o = ProviderOutcome(
            success=False,
            error_kind=ProviderErrorKind.provider_timeout,
            error_detail="ReadTimeout",
        )
        assert o.success is False
        assert o.error_kind == ProviderErrorKind.provider_timeout


# ── safe_transcribe ───────────────────────────────────────────


class TestSafeTranscribe:
    """Tests for the safe_transcribe wrapper."""

    @pytest.mark.asyncio
    async def test_success_path(self):
        stt = MockSTTProvider(default_transcript="Bonjour")
        result, outcome = await safe_transcribe(stt, b"audio")
        assert outcome.success is True
        assert outcome.error_kind is None
        assert outcome.fallback_used is False
        assert result.transcript == "Bonjour"

    @pytest.mark.asyncio
    async def test_failure_without_fallback(self):
        class FailingSTT(MockSTTProvider):
            async def transcribe(self, *args, **kwargs):
                raise RuntimeError("provider down")

        result, outcome = await safe_transcribe(FailingSTT(), b"audio")
        assert outcome.success is False
        assert outcome.error_kind == ProviderErrorKind.provider_error
        assert "provider down" in outcome.error_detail
        assert result.transcript == ""

    @pytest.mark.asyncio
    async def test_failure_with_fallback(self):
        class FailingSTT(MockSTTProvider):
            async def transcribe(self, *args, **kwargs):
                raise RuntimeError("provider down")

        fallback = MockSTTProvider(default_transcript="fallback text")
        result, outcome = await safe_transcribe(
            FailingSTT(), b"audio", fallback=fallback,
        )
        assert outcome.success is True
        assert outcome.fallback_used is True
        assert outcome.error_kind == ProviderErrorKind.provider_error
        assert result.transcript == "fallback text"

    @pytest.mark.asyncio
    async def test_timeout_classification(self):
        """httpx TimeoutException is classified as provider_timeout."""
        try:
            import httpx
        except ImportError:
            pytest.skip("httpx not installed")

        class TimeoutSTT(MockSTTProvider):
            async def transcribe(self, *args, **kwargs):
                raise httpx.ReadTimeout("read timeout")

        result, outcome = await safe_transcribe(TimeoutSTT(), b"audio")
        assert outcome.error_kind == ProviderErrorKind.provider_timeout


# ── safe_synthesize ───────────────────────────────────────────


class TestSafeSynthesize:
    """Tests for the safe_synthesize wrapper."""

    @pytest.mark.asyncio
    async def test_success_path(self):
        tts = MockTTSProvider()
        result, outcome = await safe_synthesize(tts, "Bonjour")
        assert outcome.success is True
        assert result.provider == "mock"

    @pytest.mark.asyncio
    async def test_failure_with_fallback(self):
        class FailingTTS(MockTTSProvider):
            async def synthesize(self, *args, **kwargs):
                raise ConnectionError("network error")

        fallback = MockTTSProvider()
        result, outcome = await safe_synthesize(
            FailingTTS(), "Bonjour", fallback=fallback,
        )
        assert outcome.success is True
        assert outcome.fallback_used is True
        assert result.provider == "mock"

    @pytest.mark.asyncio
    async def test_failure_without_fallback(self):
        class FailingTTS(MockTTSProvider):
            async def synthesize(self, *args, **kwargs):
                raise ConnectionError("network error")

        result, outcome = await safe_synthesize(FailingTTS(), "Bonjour")
        assert outcome.success is False
        assert outcome.error_kind == ProviderErrorKind.provider_error
        assert result.provider == "error"
