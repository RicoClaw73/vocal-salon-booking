"""
Tests for LLM-powered intent detection (app.llm_intent) and the async
dispatcher (app.intent.extract_intent_async).

All tests mock the OpenAI HTTP call — no real API key or network required.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.intent import extract_intent, extract_intent_async
from app.llm_intent import (
    LLMIntentError,
    LLMProviderError,
    LLMResponseError,
    LLMTimeoutError,
    _mask_key,
    _parse_llm_response,
    _validate_and_build,
    classify_intent_llm,
    is_llm_available,
)
from app.voice_schemas import VoiceIntent

# ── Helpers ─────────────────────────────────────────────────

def _make_openai_response(
    intent: str = "book",
    confidence: float = 0.95,
    entities: dict | None = None,
    status_code: int = 200,
) -> httpx.Response:
    """Build a mock httpx.Response mimicking OpenAI chat completion."""
    content = json.dumps({
        "intent": intent,
        "confidence": confidence,
        "entities": entities or {},
    })
    body = {
        "choices": [{"message": {"content": content}}],
    }
    return httpx.Response(
        status_code=status_code,
        json=body,
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
    )


def _make_error_response(status_code: int = 500, body: str = "Internal Server Error"):
    return httpx.Response(
        status_code=status_code,
        text=body,
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
    )


# ── Unit tests: helpers ─────────────────────────────────────

class TestMaskKey:

    def test_mask_normal_key(self):
        assert _mask_key("sk-proj-abc123") == "sk-p****"

    def test_mask_short_key(self):
        assert _mask_key("abc") == "****"

    def test_mask_empty(self):
        assert _mask_key("") == "****"


class TestParseResponse:

    def test_plain_json(self):
        result = _parse_llm_response('{"intent": "book", "confidence": 0.9}')
        assert result["intent"] == "book"

    def test_markdown_fenced_json(self):
        text = '```json\n{"intent": "cancel"}\n```'
        result = _parse_llm_response(text)
        assert result["intent"] == "cancel"

    def test_whitespace_tolerance(self):
        result = _parse_llm_response('  \n{"intent": "unknown"}  \n')
        assert result["intent"] == "unknown"

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_llm_response("not json at all")

    def test_non_object_raises(self):
        with pytest.raises(LLMResponseError, match="Expected JSON object"):
            _parse_llm_response('["a", "list"]')


class TestValidateAndBuild:

    def test_valid_result(self):
        parsed = {"intent": "book", "confidence": 0.88, "entities": {"service": "coupe"}}
        result = _validate_and_build(parsed, latency_ms=120.0)
        assert result.intent == VoiceIntent.book
        assert result.confidence == 0.88
        assert result.entities == {"service": "coupe"}
        assert result.latency_ms == 120.0

    def test_invalid_intent_raises(self):
        with pytest.raises(LLMResponseError, match="Invalid intent"):
            _validate_and_build({"intent": "fly_to_moon"}, latency_ms=50.0)

    def test_missing_intent_defaults_unknown(self):
        # "unknown" is a valid fallback if intent key missing
        result = _validate_and_build({"confidence": 0.1}, latency_ms=50.0)
        assert result.intent == VoiceIntent.unknown

    def test_confidence_clamped_high(self):
        result = _validate_and_build({"intent": "book", "confidence": 1.5}, latency_ms=0)
        assert result.confidence == 1.0

    def test_confidence_clamped_low(self):
        result = _validate_and_build({"intent": "book", "confidence": -0.5}, latency_ms=0)
        assert result.confidence == 0.0

    def test_invalid_confidence_defaults(self):
        result = _validate_and_build({"intent": "book", "confidence": "high"}, latency_ms=0)
        assert result.confidence == 0.5

    def test_entities_non_dict_ignored(self):
        result = _validate_and_build({"intent": "book", "entities": "nope"}, latency_ms=0)
        assert result.entities == {}


# ── Unit tests: is_llm_available ────────────────────────────

class TestIsLLMAvailable:

    def test_available_when_openai_configured(self):
        with patch("app.llm_intent.settings") as mock_settings:
            mock_settings.LLM_PROVIDER = "openai"
            mock_settings.OPENAI_API_KEY = "sk-test123"
            assert is_llm_available() is True

    def test_unavailable_when_provider_mock(self):
        with patch("app.llm_intent.settings") as mock_settings:
            mock_settings.LLM_PROVIDER = "mock"
            mock_settings.OPENAI_API_KEY = "sk-test123"
            assert is_llm_available() is False

    def test_unavailable_when_key_empty(self):
        with patch("app.llm_intent.settings") as mock_settings:
            mock_settings.LLM_PROVIDER = "openai"
            mock_settings.OPENAI_API_KEY = ""
            assert is_llm_available() is False


# ── Integration tests: classify_intent_llm ──────────────────

class TestClassifyIntentLLM:

    async def test_successful_classification(self):
        mock_response = _make_openai_response(
            intent="book",
            confidence=0.95,
            entities={"service": "coupe", "date": "2025-04-10"},
        )
        with patch("app.llm_intent.httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            result = await classify_intent_llm(
                "Je voudrais réserver une coupe le 10 avril",
                api_key="sk-test123",
            )

        assert result.intent == VoiceIntent.book
        assert result.confidence == 0.95
        assert result.entities["service"] == "coupe"
        assert result.latency_ms >= 0

    async def test_timeout_raises(self):
        with patch("app.llm_intent.httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.side_effect = httpx.ReadTimeout("timeout")
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            with pytest.raises(LLMTimeoutError):
                await classify_intent_llm("test", api_key="sk-test123")

    async def test_http_error_raises(self):
        with patch("app.llm_intent.httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.side_effect = httpx.ConnectError("connection refused")
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            with pytest.raises(LLMProviderError):
                await classify_intent_llm("test", api_key="sk-test123")

    async def test_non_200_raises(self):
        mock_response = _make_error_response(429, "Rate limit exceeded")
        with patch("app.llm_intent.httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            with pytest.raises(LLMProviderError, match="429"):
                await classify_intent_llm("test", api_key="sk-test123")

    async def test_invalid_json_response_raises(self):
        bad_response = httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": "not json"}}]},
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        with patch("app.llm_intent.httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = bad_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            with pytest.raises(LLMResponseError):
                await classify_intent_llm("test", api_key="sk-test123")

    async def test_missing_api_key_raises(self):
        with patch("app.llm_intent.settings") as mock_settings:
            mock_settings.OPENAI_API_KEY = ""
            mock_settings.LLM_MODEL = "gpt-4o"
            with pytest.raises(LLMIntentError, match="not configured"):
                await classify_intent_llm("test")

    async def test_cancel_intent(self):
        mock_response = _make_openai_response(
            intent="cancel",
            confidence=0.99,
            entities={"booking_id": 42},
        )
        with patch("app.llm_intent.httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            result = await classify_intent_llm(
                "Annuler mon rendez-vous #42",
                api_key="sk-test123",
            )

        assert result.intent == VoiceIntent.cancel
        assert result.entities["booking_id"] == 42


# ── Integration tests: extract_intent_async ─────────────────

class TestExtractIntentAsync:

    async def test_falls_back_to_rule_based_when_llm_unavailable(self):
        """When LLM is not configured, should use rule-based engine."""
        with patch("app.llm_intent.is_llm_available", return_value=False):
            result = await extract_intent_async("Je voudrais réserver une coupe")

        assert result.intent == VoiceIntent.book
        assert result.confidence == 1.0
        # Rule-based entities should be present
        assert result.entities.get("service_keyword") == "coupe"

    async def test_uses_llm_when_available(self):
        """When LLM is configured and succeeds, should use LLM result."""
        mock_llm_result = MagicMock()
        mock_llm_result.intent = VoiceIntent.book
        mock_llm_result.confidence = 0.92
        mock_llm_result.entities = {"service": "coupe", "date": "2025-04-10"}

        with (
            patch("app.llm_intent.is_llm_available", return_value=True),
            patch("app.llm_intent.classify_intent_llm", new_callable=AsyncMock,
                  return_value=mock_llm_result),
        ):
            result = await extract_intent_async(
                "Je voudrais réserver une coupe le 2025-04-10"
            )

        assert result.intent == VoiceIntent.book
        assert result.confidence == 0.92
        # Should have merged entities from both rule-based and LLM
        assert result.entities.get("service_keyword") == "coupe"
        assert result.entities.get("date") == "2025-04-10"

    async def test_fallback_on_llm_timeout(self):
        """On LLM timeout, should gracefully fall back to rule-based."""
        with (
            patch("app.llm_intent.is_llm_available", return_value=True),
            patch("app.llm_intent.classify_intent_llm", new_callable=AsyncMock,
                  side_effect=LLMTimeoutError("timeout")),
        ):
            result = await extract_intent_async("Je voudrais annuler mon rdv")

        assert result.intent == VoiceIntent.cancel
        assert result.confidence == 1.0

    async def test_fallback_on_llm_provider_error(self):
        """On provider HTTP error, should fall back to rule-based."""
        with (
            patch("app.llm_intent.is_llm_available", return_value=True),
            patch("app.llm_intent.classify_intent_llm", new_callable=AsyncMock,
                  side_effect=LLMProviderError("500 error")),
        ):
            result = await extract_intent_async(
                "Quand est-ce que le salon est ouvert ?"
            )

        assert result.intent == VoiceIntent.check_availability

    async def test_fallback_on_invalid_response(self):
        """On invalid LLM JSON, should fall back to rule-based."""
        with (
            patch("app.llm_intent.is_llm_available", return_value=True),
            patch("app.llm_intent.classify_intent_llm", new_callable=AsyncMock,
                  side_effect=LLMResponseError("bad json")),
        ):
            result = await extract_intent_async("I want to reschedule")

        assert result.intent == VoiceIntent.reschedule

    async def test_fallback_on_unexpected_exception(self):
        """Even unexpected errors should not break the pipeline."""
        with (
            patch("app.llm_intent.is_llm_available", return_value=True),
            patch("app.llm_intent.classify_intent_llm", new_callable=AsyncMock,
                  side_effect=RuntimeError("something totally unexpected")),
        ):
            result = await extract_intent_async("Bonjour")

        assert result.intent == VoiceIntent.unknown

    async def test_entity_merging_llm_overrides_date(self):
        """LLM entity values should override rule-based when present."""
        mock_llm_result = MagicMock()
        mock_llm_result.intent = VoiceIntent.book
        mock_llm_result.confidence = 0.9
        # LLM normalizes date to ISO format
        mock_llm_result.entities = {"date": "2025-03-15", "time": "14:30"}

        with (
            patch("app.llm_intent.is_llm_available", return_value=True),
            patch("app.llm_intent.classify_intent_llm", new_callable=AsyncMock,
                  return_value=mock_llm_result),
        ):
            result = await extract_intent_async(
                "Je voudrais réserver le 15/03/2025 à 14h30"
            )

        # LLM date overrides rule-based date
        assert result.entities["date"] == "2025-03-15"
        assert result.entities["time"] == "14:30"

    async def test_entity_merging_keeps_rule_based_extras(self):
        """Rule-based entities (service_category, genre, longueur) should be preserved."""
        mock_llm_result = MagicMock()
        mock_llm_result.intent = VoiceIntent.book
        mock_llm_result.confidence = 0.95
        mock_llm_result.entities = {"service": "coupe"}

        with (
            patch("app.llm_intent.is_llm_available", return_value=True),
            patch("app.llm_intent.classify_intent_llm", new_callable=AsyncMock,
                  return_value=mock_llm_result),
        ):
            result = await extract_intent_async("Coupe pour femme cheveux courts")

        # Rule-based entity extraction fills in genre and longueur
        assert result.entities.get("genre") == "F"
        assert result.entities.get("longueur") == "court"
        assert result.entities.get("service_category") == "coupe"

    async def test_booking_id_from_llm(self):
        """LLM can extract booking_id."""
        mock_llm_result = MagicMock()
        mock_llm_result.intent = VoiceIntent.cancel
        mock_llm_result.confidence = 0.98
        mock_llm_result.entities = {"booking_id": 42}

        with (
            patch("app.llm_intent.is_llm_available", return_value=True),
            patch("app.llm_intent.classify_intent_llm", new_callable=AsyncMock,
                  return_value=mock_llm_result),
        ):
            result = await extract_intent_async("Annuler la réservation #42")

        assert result.entities["booking_id"] == 42


# ── Backward compatibility: sync extract_intent unchanged ───

class TestSyncExtractIntentUnchanged:
    """Verify the synchronous API is completely unaffected."""

    def test_sync_book(self):
        result = extract_intent("Je voudrais réserver une coupe")
        assert result.intent == VoiceIntent.book
        assert result.confidence == 1.0

    def test_sync_cancel(self):
        result = extract_intent("Annuler mon rendez-vous")
        assert result.intent == VoiceIntent.cancel

    def test_sync_unknown(self):
        result = extract_intent("Bonjour")
        assert result.intent == VoiceIntent.unknown
        assert result.confidence == 0.0
