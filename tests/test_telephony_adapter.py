"""
Tests for telephony adapter layer, router, idempotency, dry-run,
payload guardrails, shadow mode, Redis idempotency, Twilio real
signature validation, and backward compatibility (Phase 5.3 + 5.4).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from app.config import settings
from app.telephony_adapter import (
    CallEventType,
    EventIdempotencyGuard,
    LocalAdapter,
    OutboundResponse,
    RedisIdempotencyGuard,
    TwilioAdapter,
    VapiAdapter,
    _verify_twilio_signature,
    create_idempotency_guard,
    get_telephony_adapter,
)

# ═══════════════════════════════════════════════════════════════════
# Unit tests: Adapter parsing
# ═══════════════════════════════════════════════════════════════════


class TestLocalAdapter:
    """Tests for the local simulated telephony adapter."""

    def test_parse_call_started(self):
        adapter = LocalAdapter()
        event = adapter.parse_inbound({
            "event_type": "call.started",
            "event_id": "evt-001",
            "caller_number": "+33612345678",
            "caller_name": "Marie",
            "channel": "phone",
        })
        assert event.event_type == CallEventType.call_started
        assert event.event_id == "evt-001"
        assert event.caller_number == "+33612345678"
        assert event.caller_name == "Marie"
        assert event.channel == "phone"
        assert event.provider == "local"

    def test_parse_utterance(self):
        adapter = LocalAdapter()
        event = adapter.parse_inbound({
            "event_type": "utterance",
            "event_id": "evt-002",
            "session_id": "sess-123",
            "transcript": "Je voudrais une coupe",
        })
        assert event.event_type == CallEventType.utterance
        assert event.transcript == "Je voudrais une coupe"
        assert event.session_id == "sess-123"

    def test_parse_dtmf(self):
        adapter = LocalAdapter()
        event = adapter.parse_inbound({
            "event_type": "dtmf",
            "event_id": "evt-003",
            "session_id": "sess-123",
            "dtmf_digits": "123",
        })
        assert event.event_type == CallEventType.dtmf
        assert event.dtmf_digits == "123"

    def test_parse_call_ended(self):
        adapter = LocalAdapter()
        event = adapter.parse_inbound({
            "event_type": "call.ended",
            "event_id": "evt-004",
            "session_id": "sess-123",
            "reason": "user_hangup",
        })
        assert event.event_type == CallEventType.call_ended
        assert event.reason == "user_hangup"

    def test_parse_auto_event_id(self):
        adapter = LocalAdapter()
        event = adapter.parse_inbound({"event_type": "utterance", "transcript": "hello"})
        assert event.event_id  # auto-generated
        assert len(event.event_id) == 16

    def test_parse_missing_event_type(self):
        adapter = LocalAdapter()
        with pytest.raises(ValueError, match="Missing required field: event_type"):
            adapter.parse_inbound({"transcript": "hello"})

    def test_parse_invalid_event_type(self):
        adapter = LocalAdapter()
        with pytest.raises(ValueError, match="Invalid event_type"):
            adapter.parse_inbound({"event_type": "invalid_type"})

    def test_format_outbound(self):
        adapter = LocalAdapter()
        response = OutboundResponse(
            session_id="sess-123",
            response_text="Bonjour!",
            intent="book",
            action_taken="collecting_info",
            turn_number=1,
            dry_run=True,
        )
        out = adapter.format_outbound(response)
        assert out["session_id"] == "sess-123"
        assert out["response_text"] == "Bonjour!"
        assert out["dry_run"] is True


class TestTwilioAdapter:
    """Tests for the Twilio scaffold adapter."""

    def test_parse_call_started(self):
        adapter = TwilioAdapter()
        event = adapter.parse_inbound({
            "CallSid": "CA12345678",
            "CallStatus": "ringing",
            "From": "+33612345678",
            "CallerName": "Marie",
        })
        assert event.event_type == CallEventType.call_started
        assert event.caller_number == "+33612345678"
        assert event.caller_name == "Marie"
        assert event.channel == "twilio"
        assert event.provider == "twilio"

    def test_parse_speech(self):
        adapter = TwilioAdapter()
        event = adapter.parse_inbound({
            "CallSid": "CA12345678",
            "CallStatus": "in-progress",
            "SpeechResult": "Je voudrais réserver",
        })
        assert event.event_type == CallEventType.utterance
        assert event.transcript == "Je voudrais réserver"

    def test_parse_dtmf(self):
        adapter = TwilioAdapter()
        event = adapter.parse_inbound({
            "CallSid": "CA12345678",
            "Digits": "42",
        })
        assert event.event_type == CallEventType.dtmf
        assert event.dtmf_digits == "42"

    def test_parse_call_ended(self):
        adapter = TwilioAdapter()
        event = adapter.parse_inbound({
            "CallSid": "CA12345678",
            "CallStatus": "completed",
        })
        assert event.event_type == CallEventType.call_ended
        assert event.reason == "completed"

    def test_parse_missing_call_sid(self):
        adapter = TwilioAdapter()
        with pytest.raises(ValueError, match="Missing required field: CallSid"):
            adapter.parse_inbound({"CallStatus": "ringing"})

    def test_format_outbound_with_tts(self):
        adapter = TwilioAdapter()
        response = OutboundResponse(
            session_id="sess-123",
            response_text="Bonjour!",
            tts_audio_url="http://example.com/audio.mp3",
        )
        out = adapter.format_outbound(response)
        assert out["twiml_actions"][0]["verb"] == "Play"
        assert out["twiml_actions"][0]["url"] == "http://example.com/audio.mp3"

    def test_format_outbound_without_tts(self):
        adapter = TwilioAdapter()
        response = OutboundResponse(
            session_id="sess-123",
            response_text="Bonjour!",
        )
        out = adapter.format_outbound(response)
        assert out["twiml_actions"][0]["verb"] == "Say"
        assert out["twiml_actions"][0]["text"] == "Bonjour!"

    def test_signature_validation_disabled(self):
        adapter = TwilioAdapter(webhook_secret="")
        assert adapter.validate_signature(b"payload", "any-sig") is True

    def test_signature_validation_enabled(self):
        import hashlib
        import hmac

        secret = "my-secret"
        adapter = TwilioAdapter(webhook_secret=secret)
        body = b'{"test": "data"}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert adapter.validate_signature(body, sig) is True
        assert adapter.validate_signature(body, "wrong-sig") is False


class TestVapiAdapter:
    """Tests for the Vapi scaffold adapter."""

    def test_parse_call_started(self):
        adapter = VapiAdapter()
        event = adapter.parse_inbound({
            "type": "call-started",
            "call": {
                "id": "vapi-call-001",
                "customer": {"number": "+33612345678", "name": "Marie"},
            },
        })
        assert event.event_type == CallEventType.call_started
        assert event.caller_number == "+33612345678"
        assert event.channel == "vapi"

    def test_parse_speech(self):
        adapter = VapiAdapter()
        event = adapter.parse_inbound({
            "type": "transcript",
            "call": {"id": "vapi-call-001"},
            "transcript": "Je voudrais réserver",
        })
        assert event.event_type == CallEventType.utterance
        assert event.transcript == "Je voudrais réserver"

    def test_parse_call_ended(self):
        adapter = VapiAdapter()
        event = adapter.parse_inbound({
            "type": "call-ended",
            "call": {"id": "vapi-call-001"},
            "endedReason": "customer-ended-call",
        })
        assert event.event_type == CallEventType.call_ended
        assert event.reason == "customer-ended-call"

    def test_parse_missing_type(self):
        adapter = VapiAdapter()
        with pytest.raises(ValueError, match="Missing required field: type"):
            adapter.parse_inbound({"call": {"id": "123"}})

    def test_parse_unsupported_type(self):
        adapter = VapiAdapter()
        with pytest.raises(ValueError, match="Unsupported Vapi event type"):
            adapter.parse_inbound({"type": "unknown-event"})

    def test_format_outbound(self):
        adapter = VapiAdapter()
        response = OutboundResponse(
            session_id="sess-123",
            response_text="Bienvenue!",
            tts_audio_url="http://example.com/audio.mp3",
        )
        out = adapter.format_outbound(response)
        assert out["assistant"]["firstMessage"] == "Bienvenue!"
        assert out["assistant"]["audioUrl"] == "http://example.com/audio.mp3"


class TestAdapterFactory:
    """Tests for the adapter factory."""

    def test_get_local_adapter(self):
        adapter = get_telephony_adapter("local")
        assert adapter.provider_name == "local"

    def test_get_twilio_adapter(self):
        adapter = get_telephony_adapter("twilio", webhook_secret="secret")
        assert adapter.provider_name == "twilio"

    def test_get_vapi_adapter(self):
        adapter = get_telephony_adapter("vapi")
        assert adapter.provider_name == "vapi"

    def test_get_unknown_falls_back_to_local(self):
        adapter = get_telephony_adapter("nonexistent")
        assert adapter.provider_name == "local"


# ═══════════════════════════════════════════════════════════════════
# Unit tests: Idempotency Guard
# ═══════════════════════════════════════════════════════════════════


class TestEventIdempotencyGuard:
    """Tests for the event idempotency/replay protection guard."""

    def test_new_event_accepted(self):
        guard = EventIdempotencyGuard()
        assert guard.check_and_mark("evt-001") is True
        assert guard.size == 1

    def test_duplicate_event_rejected(self):
        guard = EventIdempotencyGuard()
        assert guard.check_and_mark("evt-001") is True
        assert guard.check_and_mark("evt-001") is False  # duplicate

    def test_different_events_accepted(self):
        guard = EventIdempotencyGuard()
        assert guard.check_and_mark("evt-001") is True
        assert guard.check_and_mark("evt-002") is True
        assert guard.size == 2

    def test_is_known(self):
        guard = EventIdempotencyGuard()
        assert guard.is_known("evt-001") is False
        guard.check_and_mark("evt-001")
        assert guard.is_known("evt-001") is True

    def test_max_entries_cap(self):
        guard = EventIdempotencyGuard(max_entries=5)
        for i in range(10):
            guard.check_and_mark(f"evt-{i:03d}")
        assert guard.size == 5

    def test_reset(self):
        guard = EventIdempotencyGuard()
        guard.check_and_mark("evt-001")
        guard.reset()
        assert guard.size == 0
        assert guard.check_and_mark("evt-001") is True  # accepted again after reset

    def test_ttl_expiry(self):
        """Events expire after TTL."""
        guard = EventIdempotencyGuard(ttl_hours=0)  # TTL = 0 → everything expires
        guard.check_and_mark("evt-001")
        # Force prune with a future time
        guard._prune_expired(time.monotonic() + 1)
        assert guard.size == 0


# ═══════════════════════════════════════════════════════════════════
# Integration tests: Telephony router endpoints
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestTelephonyRouter:
    """Integration tests for the /telephony/* endpoints."""

    async def test_inbound_disabled_returns_503(self, client: AsyncClient):
        """When TELEPHONY_ENABLED=False, /inbound returns 503."""
        original = settings.TELEPHONY_ENABLED
        settings.TELEPHONY_ENABLED = False
        try:
            resp = await client.post(
                "/api/v1/telephony/inbound",
                json={"event_type": "call.started"},
            )
            assert resp.status_code == 503
            assert "disabled" in resp.json()["detail"].lower()
        finally:
            settings.TELEPHONY_ENABLED = original

    async def test_inbound_call_started(self, client: AsyncClient):
        """call.started creates a session and returns greeting."""
        original_enabled = settings.TELEPHONY_ENABLED
        original_dry = settings.TELEPHONY_DRY_RUN
        settings.TELEPHONY_ENABLED = True
        settings.TELEPHONY_DRY_RUN = False
        try:
            resp = await client.post(
                "/api/v1/telephony/inbound",
                json={
                    "event_type": "call.started",
                    "event_id": "test-call-001",
                    "caller_number": "+33612345678",
                    "caller_name": "Marie Test",
                    "channel": "phone",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["session_id"]
            assert "Maison Éclat" in data["greeting"]
            assert data["dry_run"] is False
        finally:
            settings.TELEPHONY_ENABLED = original_enabled
            settings.TELEPHONY_DRY_RUN = original_dry

    async def test_inbound_utterance_booking(self, client: AsyncClient):
        """utterance event routes through voice pipeline."""
        original_enabled = settings.TELEPHONY_ENABLED
        settings.TELEPHONY_ENABLED = True
        try:
            # Start a call
            start_resp = await client.post(
                "/api/v1/telephony/inbound",
                json={
                    "event_type": "call.started",
                    "event_id": "test-flow-001",
                    "caller_name": "Marie",
                },
            )
            session_id = start_resp.json()["session_id"]

            # Send utterance
            resp = await client.post(
                "/api/v1/telephony/inbound",
                json={
                    "event_type": "utterance",
                    "event_id": "test-flow-002",
                    "session_id": session_id,
                    "transcript": "Je voudrais prendre rendez-vous pour une coupe",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["intent"] == "book"
            assert data["session_id"] == session_id
            assert data["turn_number"] >= 1
        finally:
            settings.TELEPHONY_ENABLED = original_enabled

    async def test_inbound_call_ended(self, client: AsyncClient):
        """call.ended closes the session."""
        original_enabled = settings.TELEPHONY_ENABLED
        settings.TELEPHONY_ENABLED = True
        try:
            # Start a call
            start_resp = await client.post(
                "/api/v1/telephony/inbound",
                json={"event_type": "call.started", "event_id": "end-test-001"},
            )
            session_id = start_resp.json()["session_id"]

            # End call
            resp = await client.post(
                "/api/v1/telephony/inbound",
                json={
                    "event_type": "call.ended",
                    "event_id": "end-test-002",
                    "session_id": session_id,
                    "reason": "user_hangup",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert "duration_seconds" in data
        finally:
            settings.TELEPHONY_ENABLED = original_enabled

    async def test_idempotency_rejects_duplicate(self, client: AsyncClient):
        """Duplicate event_id returns 'duplicate' status."""
        original_enabled = settings.TELEPHONY_ENABLED
        settings.TELEPHONY_ENABLED = True
        try:
            payload = {
                "event_type": "call.started",
                "event_id": "dedup-test-001",
            }
            resp1 = await client.post("/api/v1/telephony/inbound", json=payload)
            assert resp1.status_code == 200
            assert resp1.json()["status"] == "ok"

            resp2 = await client.post("/api/v1/telephony/inbound", json=payload)
            assert resp2.status_code == 200
            assert resp2.json()["status"] == "duplicate"
        finally:
            settings.TELEPHONY_ENABLED = original_enabled

    async def test_dry_run_mode(self, client: AsyncClient):
        """In dry-run mode, events are processed but marked as dry_run."""
        original_enabled = settings.TELEPHONY_ENABLED
        original_dry = settings.TELEPHONY_DRY_RUN
        settings.TELEPHONY_ENABLED = True
        settings.TELEPHONY_DRY_RUN = True
        try:
            resp = await client.post(
                "/api/v1/telephony/inbound",
                json={
                    "event_type": "call.started",
                    "event_id": "dry-run-001",
                    "caller_name": "Test DryRun",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["dry_run"] is True
            assert data["session_id"]  # session still created
        finally:
            settings.TELEPHONY_ENABLED = original_enabled
            settings.TELEPHONY_DRY_RUN = original_dry

    async def test_payload_too_large_rejected(self, client: AsyncClient):
        """Oversized payloads return 413."""
        original_enabled = settings.TELEPHONY_ENABLED
        original_max = settings.TELEPHONY_MAX_PAYLOAD_BYTES
        settings.TELEPHONY_ENABLED = True
        settings.TELEPHONY_MAX_PAYLOAD_BYTES = 50  # very small limit
        try:
            resp = await client.post(
                "/api/v1/telephony/inbound",
                json={
                    "event_type": "call.started",
                    "event_id": "big-payload",
                    "caller_name": "A" * 100,
                },
            )
            assert resp.status_code == 413
        finally:
            settings.TELEPHONY_ENABLED = original_enabled
            settings.TELEPHONY_MAX_PAYLOAD_BYTES = original_max

    async def test_invalid_json_returns_400(self, client: AsyncClient):
        """Non-JSON body returns 400."""
        original_enabled = settings.TELEPHONY_ENABLED
        settings.TELEPHONY_ENABLED = True
        try:
            resp = await client.post(
                "/api/v1/telephony/inbound",
                content=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status_code == 400
        finally:
            settings.TELEPHONY_ENABLED = original_enabled

    async def test_parse_error_returns_422(self, client: AsyncClient):
        """Malformed event payload returns 422."""
        original_enabled = settings.TELEPHONY_ENABLED
        settings.TELEPHONY_ENABLED = True
        try:
            resp = await client.post(
                "/api/v1/telephony/inbound",
                json={"event_type": "invalid_type", "event_id": "parse-err-001"},
            )
            assert resp.status_code == 422
        finally:
            settings.TELEPHONY_ENABLED = original_enabled

    async def test_status_endpoint(self, client: AsyncClient):
        """GET /telephony/status returns pilot configuration."""
        resp = await client.get("/api/v1/telephony/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        assert "dry_run" in data
        assert "provider" in data
        assert "counters" in data
        assert "idempotency_guard_size" in data

    async def test_retention_prune(self, client: AsyncClient):
        """POST /telephony/retention/prune works when enabled."""
        original_enabled = settings.TELEPHONY_ENABLED
        settings.TELEPHONY_ENABLED = True
        try:
            resp = await client.post("/api/v1/telephony/retention/prune")
            assert resp.status_code == 200
            data = resp.json()
            assert "pruned" in data
            assert "remaining" in data
        finally:
            settings.TELEPHONY_ENABLED = original_enabled

    async def test_full_call_flow_e2e(self, client: AsyncClient):
        """End-to-end: call.started → utterances → call.ended."""
        original_enabled = settings.TELEPHONY_ENABLED
        settings.TELEPHONY_ENABLED = True
        try:
            # 1. Start call
            start = await client.post("/api/v1/telephony/inbound", json={
                "event_type": "call.started",
                "event_id": "e2e-start",
                "caller_name": "E2E Test",
            })
            assert start.status_code == 200
            session_id = start.json()["session_id"]

            # 2. Booking utterance
            utt1 = await client.post("/api/v1/telephony/inbound", json={
                "event_type": "utterance",
                "event_id": "e2e-utt1",
                "session_id": session_id,
                "transcript": "Je voudrais réserver une coupe femme",
            })
            assert utt1.status_code == 200
            assert utt1.json()["intent"] == "book"

            # 3. Unknown utterance → fallback
            utt2 = await client.post("/api/v1/telephony/inbound", json={
                "event_type": "utterance",
                "event_id": "e2e-utt2",
                "session_id": session_id,
                "transcript": "asdfjkl random noise",
            })
            assert utt2.status_code == 200
            # Should continue with book intent since one was active
            assert utt2.json()["session_id"] == session_id

            # 4. End call
            end = await client.post("/api/v1/telephony/inbound", json={
                "event_type": "call.ended",
                "event_id": "e2e-end",
                "session_id": session_id,
            })
            assert end.status_code == 200
            assert end.json()["turns"] >= 2
        finally:
            settings.TELEPHONY_ENABLED = original_enabled

    async def test_utterance_without_session_returns_422(self, client: AsyncClient):
        """utterance without session_id returns 422."""
        original_enabled = settings.TELEPHONY_ENABLED
        settings.TELEPHONY_ENABLED = True
        try:
            resp = await client.post("/api/v1/telephony/inbound", json={
                "event_type": "utterance",
                "event_id": "no-session-001",
                "transcript": "hello",
            })
            assert resp.status_code == 422
        finally:
            settings.TELEPHONY_ENABLED = original_enabled

    async def test_utterance_nonexistent_session_returns_404(self, client: AsyncClient):
        """utterance with unknown session_id returns 404."""
        original_enabled = settings.TELEPHONY_ENABLED
        settings.TELEPHONY_ENABLED = True
        try:
            resp = await client.post("/api/v1/telephony/inbound", json={
                "event_type": "utterance",
                "event_id": "bad-session-001",
                "session_id": "nonexistent-session",
                "transcript": "hello",
            })
            assert resp.status_code == 404
        finally:
            settings.TELEPHONY_ENABLED = original_enabled

    async def test_status_reports_shadow_mode(self, client: AsyncClient):
        """GET /telephony/status includes shadow_mode field."""
        resp = await client.get("/api/v1/telephony/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "shadow_mode" in data
        assert "idempotency_backend" in data
        assert data["idempotency_backend"] in ("in_memory", "redis")

    async def test_status_reports_shadow_suppressed_counter(self, client: AsyncClient):
        """GET /telephony/status includes shadow_suppressed counter."""
        resp = await client.get("/api/v1/telephony/status")
        data = resp.json()
        assert "shadow_suppressed" in data["counters"]

    async def test_shadow_mode_tags_response(self, client: AsyncClient):
        """In shadow mode, utterance responses include shadow_mode=True."""
        original_enabled = settings.TELEPHONY_ENABLED
        original_shadow = settings.TELEPHONY_SHADOW_MODE
        settings.TELEPHONY_ENABLED = True
        settings.TELEPHONY_SHADOW_MODE = True
        try:
            # Start call
            start = await client.post("/api/v1/telephony/inbound", json={
                "event_type": "call.started",
                "event_id": "shadow-test-001",
                "caller_name": "Shadow Test",
            })
            session_id = start.json()["session_id"]

            # Send utterance
            resp = await client.post("/api/v1/telephony/inbound", json={
                "event_type": "utterance",
                "event_id": "shadow-test-002",
                "session_id": session_id,
                "transcript": "Je voudrais réserver une coupe",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["shadow_mode"] is True
        finally:
            settings.TELEPHONY_ENABLED = original_enabled
            settings.TELEPHONY_SHADOW_MODE = original_shadow

    async def test_shadow_mode_off_no_tag(self, client: AsyncClient):
        """When shadow mode is off, response has no shadow_mode key."""
        original_enabled = settings.TELEPHONY_ENABLED
        original_shadow = settings.TELEPHONY_SHADOW_MODE
        settings.TELEPHONY_ENABLED = True
        settings.TELEPHONY_SHADOW_MODE = False
        try:
            start = await client.post("/api/v1/telephony/inbound", json={
                "event_type": "call.started",
                "event_id": "no-shadow-001",
                "caller_name": "No Shadow",
            })
            session_id = start.json()["session_id"]

            resp = await client.post("/api/v1/telephony/inbound", json={
                "event_type": "utterance",
                "event_id": "no-shadow-002",
                "session_id": session_id,
                "transcript": "Je voudrais réserver",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert "shadow_mode" not in data
        finally:
            settings.TELEPHONY_ENABLED = original_enabled
            settings.TELEPHONY_SHADOW_MODE = original_shadow


# ═══════════════════════════════════════════════════════════════════
# Phase 5.4: Twilio real signature validation
# ═══════════════════════════════════════════════════════════════════


class TestTwilioRealSignature:
    """Tests for the real Twilio signature verification algorithm."""

    def test_verify_twilio_signature_valid(self):
        """Known-good Twilio signature passes."""
        auth_token = "test-auth-token-12345"
        url = "https://myapp.example.com/api/v1/telephony/inbound"
        params = {"CallSid": "CA123", "CallStatus": "ringing", "From": "+33612345678"}

        # Compute expected signature using the algorithm
        data = url
        for key in sorted(params.keys()):
            data += key + params[key]
        mac = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1)
        expected_sig = base64.b64encode(mac.digest()).decode()

        assert _verify_twilio_signature(auth_token, url, params, expected_sig) is True

    def test_verify_twilio_signature_invalid(self):
        """Wrong signature fails."""
        assert _verify_twilio_signature(
            auth_token="secret",
            url="https://example.com/webhook",
            params={"CallSid": "CA123"},
            signature="wrong-signature",
        ) is False

    def test_verify_twilio_signature_empty_params(self):
        """Empty params dict works (e.g. GET requests)."""
        auth_token = "secret"
        url = "https://example.com/webhook"
        params: dict[str, str] = {}

        data = url
        mac = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1)
        expected_sig = base64.b64encode(mac.digest()).decode()

        assert _verify_twilio_signature(auth_token, url, params, expected_sig) is True

    def test_twilio_adapter_uses_real_algorithm_with_url(self):
        """TwilioAdapter uses real Twilio algorithm when webhook_url is set."""
        auth_token = "my-twilio-token"
        webhook_url = "https://salon.example.com/api/v1/telephony/inbound"
        adapter = TwilioAdapter(webhook_secret=auth_token, webhook_url=webhook_url)

        params = {"CallSid": "CA999", "From": "+33699999999"}
        data = webhook_url
        for key in sorted(params.keys()):
            data += key + params[key]
        mac = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1)
        valid_sig = base64.b64encode(mac.digest()).decode()

        # Pass as keyword — real Twilio mode
        assert adapter.validate_signature(b"", valid_sig, url=webhook_url, params=params) is True
        assert adapter.validate_signature(b"", "bad-sig", url=webhook_url, params=params) is False

    def test_twilio_adapter_falls_back_to_hmac_sha256(self):
        """Without URL, TwilioAdapter falls back to HMAC-SHA256 of raw body."""
        secret = "fallback-secret"
        adapter = TwilioAdapter(webhook_secret=secret)  # no webhook_url

        body = b'{"CallSid": "CA123"}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        assert adapter.validate_signature(body, sig) is True
        assert adapter.validate_signature(body, "wrong") is False

    def test_twilio_adapter_no_signature_when_secret_set(self):
        """Missing signature is rejected when secret is configured."""
        adapter = TwilioAdapter(webhook_secret="configured")
        assert adapter.validate_signature(b"payload", "") is False


# ═══════════════════════════════════════════════════════════════════
# Phase 5.4: Redis idempotency guard
# ═══════════════════════════════════════════════════════════════════


class TestRedisIdempotencyGuard:
    """Tests for RedisIdempotencyGuard (mocked Redis — no real server needed)."""

    def _make_guard(self, mock_redis=None):
        """Create a RedisIdempotencyGuard with a mocked Redis client."""
        guard = RedisIdempotencyGuard(
            redis_url="redis://fake:6379/0",
            key_prefix="test:idem:",
            ttl_seconds=3600,
        )
        if mock_redis is not None:
            guard._redis = mock_redis
            guard._available = True
        return guard

    def test_new_event_accepted(self):
        mock_redis = MagicMock()
        mock_redis.set.return_value = True  # SET NX succeeded
        guard = self._make_guard(mock_redis)

        assert guard.check_and_mark("evt-100") is True
        mock_redis.set.assert_called_once_with("test:idem:evt-100", "1", nx=True, ex=3600)

    def test_duplicate_event_rejected(self):
        mock_redis = MagicMock()
        mock_redis.set.return_value = None  # SET NX failed (key exists)
        guard = self._make_guard(mock_redis)

        assert guard.check_and_mark("evt-100") is False

    def test_is_known_true(self):
        mock_redis = MagicMock()
        mock_redis.exists.return_value = 1
        guard = self._make_guard(mock_redis)

        assert guard.is_known("evt-100") is True

    def test_is_known_false(self):
        mock_redis = MagicMock()
        mock_redis.exists.return_value = 0
        guard = self._make_guard(mock_redis)

        assert guard.is_known("evt-100") is False

    def test_fail_open_on_redis_error(self):
        """Redis failure → event is allowed through (fail-open)."""
        mock_redis = MagicMock()
        mock_redis.set.side_effect = Exception("Connection refused")
        guard = self._make_guard(mock_redis)

        assert guard.check_and_mark("evt-100") is True
        assert guard._available is False

    def test_fail_open_when_unavailable(self):
        """When Redis was never reachable, events pass through."""
        guard = RedisIdempotencyGuard(
            redis_url="redis://fake:6379/0",
            key_prefix="test:idem:",
            ttl_seconds=3600,
        )
        guard._available = False
        # _get_redis will try to connect and fail — should still return True
        assert guard.check_and_mark("evt-100") is True

    def test_size_with_redis(self):
        mock_redis = MagicMock()
        mock_redis.scan.return_value = (0, ["test:idem:a", "test:idem:b"])
        guard = self._make_guard(mock_redis)

        assert guard.size == 2

    def test_reset_clears_keys(self):
        mock_redis = MagicMock()
        mock_redis.scan.return_value = (0, ["test:idem:a", "test:idem:b"])
        guard = self._make_guard(mock_redis)

        guard.reset()
        mock_redis.delete.assert_called_once_with("test:idem:a", "test:idem:b")

    def test_is_available_property(self):
        guard = self._make_guard(MagicMock())
        assert guard.is_available is True


class TestIdempotencyGuardFactory:
    """Tests for create_idempotency_guard factory."""

    def test_no_redis_url_returns_in_memory(self):
        guard = create_idempotency_guard(redis_url="")
        assert isinstance(guard, EventIdempotencyGuard)

    def test_redis_url_without_redis_package_falls_back(self):
        """When redis package is not importable, factory falls back to in-memory."""
        with patch.dict("sys.modules", {"redis": None}):
            guard = create_idempotency_guard(redis_url="redis://localhost:6379/0")
            assert isinstance(guard, EventIdempotencyGuard)

    def test_redis_url_with_redis_package_returns_redis_guard(self):
        """When redis package is available, factory returns RedisIdempotencyGuard."""
        mock_redis_mod = MagicMock()
        with patch.dict("sys.modules", {"redis": mock_redis_mod}):
            guard = create_idempotency_guard(redis_url="redis://localhost:6379/0")
            assert isinstance(guard, RedisIdempotencyGuard)


# ═══════════════════════════════════════════════════════════════════
# Backward compatibility: existing /voice/* still works
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestBackwardCompatibility:
    """Ensure existing /voice/* endpoints are unaffected by telephony changes."""

    async def test_voice_sessions_start_still_works(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/voice/sessions/start",
            json={"channel": "test", "client_name": "Backward Compat"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["session_id"]
        assert data["status"] == "active"

    async def test_voice_turn_still_works(self, client: AsyncClient):
        # Create session via voice
        start = await client.post(
            "/api/v1/voice/sessions/start",
            json={"channel": "test"},
        )
        session_id = start.json()["session_id"]

        # Send voice turn
        resp = await client.post(
            "/api/v1/voice/turn",
            json={"session_id": session_id, "text": "Je voudrais une coupe"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "book"
        assert data["session_id"] == session_id

    async def test_voice_sessions_end_still_works(self, client: AsyncClient):
        start = await client.post(
            "/api/v1/voice/sessions/start",
            json={"channel": "test"},
        )
        session_id = start.json()["session_id"]

        resp = await client.post(
            "/api/v1/voice/sessions/end",
            json={"session_id": session_id, "reason": "test"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    async def test_health_endpoint_still_works(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    async def test_ops_metrics_still_works(self, client: AsyncClient):
        resp = await client.get("/api/v1/ops/metrics")
        assert resp.status_code == 200
