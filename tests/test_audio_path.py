"""
Tests for real audio path handling in /voice/turn (Phase 5.2).

Covers:
  - audio_base64 decoded and routed through STT
  - Invalid base64 rejected with 422
  - Empty base64 payload rejected with 422
  - Audio metadata fields (format, sample_rate, encoding) validated
  - Backward compat: text-only mode unaffected
  - audio_base64 with text present: text takes precedence
  - tts_audio_url field present in response (None for mock)
  - Schema validation for audio_format, audio_sample_rate, audio_encoding
"""

from __future__ import annotations

import base64

import pytest
from httpx import AsyncClient

from app.conversation import conversation_manager

PREFIX = "/api/v1/voice"
TURN_URL = f"{PREFIX}/turn"


@pytest.fixture(autouse=True)
def _clear_sessions():
    conversation_manager._sessions.clear()
    yield
    conversation_manager._sessions.clear()


class TestAudioBase64Path:

    @pytest.mark.asyncio
    async def test_audio_base64_accepted(self, client: AsyncClient):
        """Valid base64 audio payload is decoded and processed."""
        # Simulate 16kHz mono PCM: 32000 bytes = 1 second
        fake_audio = b"\x00\x80" * 16000  # 32000 bytes
        b64_payload = base64.b64encode(fake_audio).decode("ascii")

        resp = await client.post(TURN_URL, json={
            "audio_base64": b64_payload,
            "audio_format": "wav",
            "audio_sample_rate": 16000,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["stt_meta"] is not None
        assert data["stt_meta"]["format"] == "wav"
        assert data["stt_meta"]["sample_rate"] == 16000
        assert data["stt_meta"]["provider"] == "mock"

    @pytest.mark.asyncio
    async def test_audio_base64_invalid_rejected(self, client: AsyncClient):
        """Non-base64 string rejected with 422."""
        resp = await client.post(TURN_URL, json={
            "audio_base64": "not!!!valid===base64",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_audio_base64_empty_rejected(self, client: AsyncClient):
        """Empty base64 (decodes to 0 bytes) rejected with 422."""
        resp = await client.post(TURN_URL, json={
            "audio_base64": "",  # empty string → empty bytes
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_audio_base64_with_metadata(self, client: AsyncClient):
        """Audio metadata fields correctly reflected in stt_meta."""
        fake_audio = b"\x00" * 1600
        b64 = base64.b64encode(fake_audio).decode()

        resp = await client.post(TURN_URL, json={
            "audio_base64": b64,
            "audio_format": "mp3",
            "audio_sample_rate": 44100,
            "audio_encoding": "mp3",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["stt_meta"]["format"] == "mp3"
        assert data["stt_meta"]["sample_rate"] == 44100

    @pytest.mark.asyncio
    async def test_text_takes_precedence_over_audio(self, client: AsyncClient):
        """When both text and audio_base64 are provided, text is used for intent."""
        fake_audio = b"\x00" * 1600
        b64 = base64.b64encode(fake_audio).decode()

        resp = await client.post(TURN_URL, json={
            "text": "Je voudrais réserver une coupe",
            "audio_base64": b64,
        })
        assert resp.status_code == 200
        data = resp.json()
        # Text-driven intent should detect "book"
        assert data["intent"] == "book"

    @pytest.mark.asyncio
    async def test_no_input_rejected(self, client: AsyncClient):
        """No text, no mock_transcript, no audio → 422."""
        resp = await client.post(TURN_URL, json={
            "channel": "test",
        })
        assert resp.status_code == 422


class TestAudioSchemaValidation:

    @pytest.mark.asyncio
    async def test_invalid_audio_format_rejected(self, client: AsyncClient):
        """Unsupported audio_format with audio_base64 → 422."""
        b64 = base64.b64encode(b"\x00" * 100).decode()
        resp = await client.post(TURN_URL, json={
            "audio_base64": b64,
            "audio_format": "flac",  # not supported
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_sample_rate_rejected(self, client: AsyncClient):
        """Unsupported audio_sample_rate with audio_base64 → 422."""
        b64 = base64.b64encode(b"\x00" * 100).decode()
        resp = await client.post(TURN_URL, json={
            "audio_base64": b64,
            "audio_sample_rate": 12000,  # not in valid set
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_encoding_rejected(self, client: AsyncClient):
        """Unsupported audio_encoding with audio_base64 → 422."""
        b64 = base64.b64encode(b"\x00" * 100).decode()
        resp = await client.post(TURN_URL, json={
            "audio_base64": b64,
            "audio_encoding": "speex",  # not supported
        })
        assert resp.status_code == 422


class TestTTSAudioUrlField:

    @pytest.mark.asyncio
    async def test_tts_audio_url_present_in_response(self, client: AsyncClient):
        """VoiceTurnResponse includes tts_audio_url field."""
        resp = await client.post(TURN_URL, json={"text": "Bonjour"})
        assert resp.status_code == 200
        data = resp.json()
        # For mock provider, tts_audio_url is None
        assert "tts_audio_url" in data
        assert data["tts_audio_url"] is None

    @pytest.mark.asyncio
    async def test_tts_audio_url_in_booking_response(self, client: AsyncClient):
        """tts_audio_url present even on intent-driven responses."""
        resp = await client.post(TURN_URL, json={
            "text": "Je voudrais réserver une coupe",
        })
        assert resp.status_code == 200
        assert "tts_audio_url" in resp.json()
