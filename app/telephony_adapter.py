"""
Telephony adapter abstraction layer (Phase 5.3 + 5.4).

Provider-agnostic interface for bridging external telephony call events
into the vocal-salon voice pipeline.  Two concrete paths:

  1. **LocalAdapter** — simulated provider for local dev/testing.
     Generates deterministic events with no external dependencies.
  2. **TwilioAdapter** / **VapiAdapter** — scaffold adapters that parse
     real webhook payloads into the canonical ``InboundCallEvent`` format.
     No credentials required by default; real provider logic is opt-in.

Key concepts:
  - ``InboundCallEvent``: canonical inbound event (provider-agnostic).
  - ``OutboundResponse``: canonical outbound response to telephony provider.
  - ``TelephonyAdapter``: abstract interface that concrete adapters implement.
  - Idempotency guard: ``EventIdempotencyGuard`` (in-memory) or
    ``RedisIdempotencyGuard`` (optional, when REDIS_URL is set).

Phase 5.4 additions:
  - ``RedisIdempotencyGuard``: Redis-backed idempotency for multi-worker deployments.
  - ``create_idempotency_guard()``: factory that picks the right backend.
  - Real Twilio signature verification algorithm.

Design:
  - Local-first default; no paid dependency required.
  - Redis is optional — graceful fallback to in-memory if unavailable.
  - Backward-compatible: existing /voice/* endpoints are untouched.
  - Reuses existing observability/ops/auth/rate-limit patterns.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.observability import StructuredLogger, metrics

_slog = StructuredLogger(__name__)


# ── Canonical Event Types ──────────────────────────────────────────


class CallEventType(str, Enum):
    """Canonical telephony call event types (provider-agnostic)."""
    call_started = "call.started"
    utterance = "utterance"
    dtmf = "dtmf"
    silence_timeout = "silence_timeout"
    call_ended = "call.ended"


@dataclass(frozen=True)
class InboundCallEvent:
    """
    Canonical inbound call event from any telephony provider.

    Every adapter normalises raw webhook payloads into this format.
    The ``event_id`` field is used for idempotency/replay protection.
    """
    event_id: str
    event_type: CallEventType
    session_id: str | None = None
    caller_number: str | None = None
    caller_name: str | None = None
    channel: str = "phone"
    transcript: str | None = None
    dtmf_digits: str | None = None
    reason: str | None = None
    provider: str = "local"
    raw_payload: dict = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def payload_size_bytes(self) -> int:
        """Rough estimate of raw payload size."""
        import json
        return len(json.dumps(self.raw_payload, default=str).encode())


@dataclass
class OutboundResponse:
    """
    Canonical outbound response to send back to the telephony provider.

    Contains the assistant's reply text, session state, and optional
    audio artifact reference for HTTP-friendly delivery.
    """
    session_id: str
    response_text: str
    intent: str | None = None
    action_taken: str | None = None
    turn_number: int = 0
    is_fallback: bool = False
    tts_audio_url: str | None = None
    booking_draft: dict | None = None
    data: dict | None = None
    dry_run: bool = False
    provider_errors: list[dict] | None = None


# ── Idempotency Guard ──────────────────────────────────────────────


class EventIdempotencyGuard:
    """
    In-memory idempotency guard for inbound telephony events.

    Tracks processed event IDs with timestamps.  Rejects duplicates
    within the TTL window (default: 24 hours).  Automatically prunes
    expired entries to bound memory usage.

    Thread-safety: safe for single-worker async (FastAPI default).
    """

    def __init__(self, ttl_hours: int = 24, max_entries: int = 100_000) -> None:
        self._ttl_seconds = ttl_hours * 3600
        self._max_entries = max_entries
        # OrderedDict for efficient LRU-style pruning
        self._seen: OrderedDict[str, float] = OrderedDict()

    def check_and_mark(self, event_id: str) -> bool:
        """
        Check if an event has already been processed.

        Returns True if the event is **new** (not a replay).
        Returns False if it's a duplicate (already seen within TTL).
        """
        now = time.monotonic()
        self._prune_expired(now)

        if event_id in self._seen:
            metrics.inc("telephony_event_replay_rejected")
            _slog.warning(
                "telephony_event_replay",
                event_id=event_id,
            )
            return False

        self._seen[event_id] = now
        self._seen.move_to_end(event_id)

        # Cap memory: evict oldest if over limit
        while len(self._seen) > self._max_entries:
            self._seen.popitem(last=False)

        return True

    def is_known(self, event_id: str) -> bool:
        """Check if an event ID has been seen (without marking it)."""
        return event_id in self._seen

    def _prune_expired(self, now: float) -> None:
        """Remove entries older than TTL."""
        cutoff = now - self._ttl_seconds
        # OrderedDict is insertion-ordered; oldest first
        while self._seen:
            oldest_key, oldest_time = next(iter(self._seen.items()))
            if oldest_time < cutoff:
                self._seen.popitem(last=False)
            else:
                break

    @property
    def size(self) -> int:
        return len(self._seen)

    def reset(self) -> None:
        """Clear all state (for tests)."""
        self._seen.clear()


class RedisIdempotencyGuard:
    """
    Redis-backed idempotency guard for multi-worker deployments (Phase 5.4).

    Uses Redis SET-NX with TTL to de-duplicate event IDs across workers.
    Falls back gracefully to allowing events through if Redis is unreachable
    (fail-open for availability — duplicates are a lesser evil than dropped calls).

    Requires the ``redis`` extra: ``pip install redis``.
    """

    def __init__(
        self,
        redis_url: str,
        key_prefix: str = "salon:idem:",
        ttl_seconds: int = 86400,
    ) -> None:
        self._redis_url = redis_url
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds
        self._redis = None  # Lazy connection
        self._available = True  # Track if Redis is reachable

    def _get_redis(self):
        """Lazy-init Redis connection."""
        if self._redis is None:
            try:
                import redis
                self._redis = redis.from_url(
                    self._redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                # Test connection
                self._redis.ping()
                self._available = True
                _slog.info("redis_idempotency_connected", url=self._redis_url[:20] + "...")
            except Exception as exc:
                _slog.warning(
                    "redis_idempotency_unavailable",
                    error=str(exc),
                    action="fail_open",
                )
                self._available = False
                self._redis = None
        return self._redis

    def check_and_mark(self, event_id: str) -> bool:
        """
        Check if an event has already been processed (Redis SET-NX).

        Returns True if the event is **new** (not a replay).
        Returns False if it's a duplicate (key already exists).
        On Redis failure, returns True (fail-open).
        """
        client = self._get_redis()
        if client is None:
            # Redis unavailable — fail open (allow event through)
            metrics.inc("telephony_redis_fallback")
            return True

        key = f"{self._key_prefix}{event_id}"
        try:
            was_set = client.set(key, "1", nx=True, ex=self._ttl_seconds)
            if not was_set:
                metrics.inc("telephony_event_replay_rejected")
                _slog.warning("telephony_event_replay", event_id=event_id, backend="redis")
                return False
            return True
        except Exception as exc:
            _slog.warning(
                "redis_idempotency_error",
                event_id=event_id,
                error=str(exc),
                action="fail_open",
            )
            metrics.inc("telephony_redis_fallback")
            self._available = False
            self._redis = None  # Force reconnect on next call
            return True

    def is_known(self, event_id: str) -> bool:
        """Check if an event ID exists in Redis (without marking)."""
        client = self._get_redis()
        if client is None:
            return False
        try:
            return client.exists(f"{self._key_prefix}{event_id}") > 0
        except Exception:
            return False

    @property
    def size(self) -> int:
        """Approximate count of tracked event IDs in Redis."""
        client = self._get_redis()
        if client is None:
            return 0
        try:
            cursor = 0
            count = 0
            while True:
                cursor, keys = client.scan(cursor, match=f"{self._key_prefix}*", count=1000)
                count += len(keys)
                if cursor == 0:
                    break
            return count
        except Exception:
            return 0

    def reset(self) -> None:
        """Clear all tracked events (for tests). Deletes matching keys."""
        client = self._get_redis()
        if client is None:
            return
        try:
            cursor = 0
            while True:
                cursor, keys = client.scan(cursor, match=f"{self._key_prefix}*", count=1000)
                if keys:
                    client.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            pass

    @property
    def is_available(self) -> bool:
        """Whether Redis is currently reachable."""
        return self._available


def create_idempotency_guard(
    redis_url: str = "",
    key_prefix: str = "salon:idem:",
    ttl_seconds: int = 86400,
    ttl_hours: int = 24,
    max_entries: int = 100_000,
) -> EventIdempotencyGuard | RedisIdempotencyGuard:
    """
    Factory: create the appropriate idempotency guard backend.

    - When ``redis_url`` is non-empty, attempts Redis backend.
    - Otherwise (or on import failure), falls back to in-memory guard.

    This is called once at module load; the result is used as a singleton.
    """
    if redis_url:
        try:
            import redis as _redis_check  # noqa: F811
            del _redis_check  # only needed to verify availability
            _slog.info(
                "idempotency_guard_backend",
                backend="redis",
                prefix=key_prefix,
                ttl_seconds=ttl_seconds,
            )
            return RedisIdempotencyGuard(
                redis_url=redis_url,
                key_prefix=key_prefix,
                ttl_seconds=ttl_seconds,
            )
        except ImportError:
            _slog.warning(
                "redis_import_failed",
                action="falling_back_to_in_memory",
                hint="Install redis: pip install 'vocal-salon-api[redis]'",
            )

    _slog.info("idempotency_guard_backend", backend="in_memory", ttl_hours=ttl_hours)
    return EventIdempotencyGuard(ttl_hours=ttl_hours, max_entries=max_entries)


# ── Abstract Adapter Interface ─────────────────────────────────────


class TelephonyAdapter(ABC):
    """
    Abstract telephony adapter interface.

    Concrete adapters implement ``parse_inbound`` to convert raw
    webhook payloads into canonical ``InboundCallEvent`` objects,
    and ``format_outbound`` to convert ``OutboundResponse`` into
    the provider-specific response format.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider identifier."""
        ...

    @abstractmethod
    def parse_inbound(self, raw_payload: dict) -> InboundCallEvent:
        """
        Parse a raw webhook payload into a canonical InboundCallEvent.

        Raises ValueError if the payload is malformed or missing
        required fields.
        """
        ...

    @abstractmethod
    def format_outbound(self, response: OutboundResponse) -> dict:
        """
        Format a canonical OutboundResponse into provider-specific
        response payload (JSON-serialisable dict).
        """
        ...

    def validate_signature(self, raw_body: bytes, signature: str) -> bool:
        """
        Validate inbound webhook signature.

        Default implementation returns True (no verification).
        Override in real-provider adapters.
        """
        return True


# ── Concrete: Local Simulated Adapter ──────────────────────────────


class LocalAdapter(TelephonyAdapter):
    """
    Simulated telephony adapter for local development and testing.

    Accepts a simple JSON payload format and generates deterministic
    event IDs.  No external dependencies.
    """

    @property
    def provider_name(self) -> str:
        return "local"

    def parse_inbound(self, raw_payload: dict) -> InboundCallEvent:
        """
        Parse local-format payload.

        Expected fields:
          - event_type: str (call.started, utterance, dtmf, silence_timeout, call.ended)
          - event_id: str (optional, auto-generated if missing)
          - session_id: str (optional for call.started)
          - transcript: str (for utterance events)
          - caller_number, caller_name, channel, reason, dtmf_digits
        """
        event_type_str = raw_payload.get("event_type")
        if not event_type_str:
            raise ValueError("Missing required field: event_type")

        try:
            event_type = CallEventType(event_type_str)
        except ValueError:
            raise ValueError(
                f"Invalid event_type: '{event_type_str}'. "
                f"Valid types: {[e.value for e in CallEventType]}"
            )

        event_id = raw_payload.get("event_id") or uuid.uuid4().hex[:16]

        return InboundCallEvent(
            event_id=event_id,
            event_type=event_type,
            session_id=raw_payload.get("session_id"),
            caller_number=raw_payload.get("caller_number"),
            caller_name=raw_payload.get("caller_name"),
            channel=raw_payload.get("channel", "phone"),
            transcript=raw_payload.get("transcript"),
            dtmf_digits=raw_payload.get("dtmf_digits"),
            reason=raw_payload.get("reason"),
            provider="local",
            raw_payload=raw_payload,
        )

    def format_outbound(self, response: OutboundResponse) -> dict:
        """Format as simple JSON (mirrors internal structure)."""
        return {
            "session_id": response.session_id,
            "response_text": response.response_text,
            "intent": response.intent,
            "action_taken": response.action_taken,
            "turn_number": response.turn_number,
            "is_fallback": response.is_fallback,
            "tts_audio_url": response.tts_audio_url,
            "dry_run": response.dry_run,
            "provider_errors": response.provider_errors,
        }


# ── Concrete: Twilio-Style Webhook Scaffold ────────────────────────


class TwilioAdapter(TelephonyAdapter):
    """
    Adapter for Twilio-compatible webhook payloads.

    Parses the Twilio webhook contract into canonical events.
    No Twilio SDK dependency — uses plain dict parsing + stdlib HMAC.

    Signature verification implements the real Twilio algorithm:
      1. Start with the full webhook URL.
      2. Append POST body parameters sorted by key.
      3. HMAC-SHA1 of the resulting string with the auth token.
      4. Base64-encode the digest → must match X-Twilio-Signature header.

    When ``webhook_secret`` is empty, verification is skipped (dev mode).
    When ``webhook_url`` is empty but secret is set, falls back to simple
    HMAC-SHA256 of the raw body (backward compat / non-Twilio proxies).
    """

    def __init__(
        self,
        webhook_secret: str = "",
        webhook_url: str = "",
    ) -> None:
        self._webhook_secret = webhook_secret
        self._webhook_url = webhook_url

    @property
    def provider_name(self) -> str:
        return "twilio"

    def parse_inbound(self, raw_payload: dict) -> InboundCallEvent:
        """
        Parse Twilio-style webhook payload.

        Twilio sends form-encoded data; we expect it pre-parsed as dict.
        Key fields: CallSid, CallStatus, From, SpeechResult, Digits.
        """
        call_sid = raw_payload.get("CallSid") or raw_payload.get("call_sid", "")
        if not call_sid:
            raise ValueError("Missing required field: CallSid")

        # Determine event type from payload markers
        call_status = raw_payload.get("CallStatus", "").lower()
        speech_result = raw_payload.get("SpeechResult") or raw_payload.get("transcript")
        digits = raw_payload.get("Digits") or raw_payload.get("dtmf_digits")

        if call_status in ("ringing", "initiated"):
            event_type = CallEventType.call_started
        elif call_status in ("completed", "canceled", "busy", "no-answer", "failed"):
            event_type = CallEventType.call_ended
        elif digits:
            event_type = CallEventType.dtmf
        elif speech_result:
            event_type = CallEventType.utterance
        else:
            event_type = CallEventType.utterance  # default for status updates

        # Generate deterministic event_id from CallSid + status/content
        content_hash = hashlib.sha256(
            f"{call_sid}:{call_status}:{speech_result or ''}:{digits or ''}".encode()
        ).hexdigest()[:12]
        event_id = raw_payload.get("event_id") or f"twilio-{call_sid[:8]}-{content_hash}"

        return InboundCallEvent(
            event_id=event_id,
            event_type=event_type,
            session_id=raw_payload.get("session_id") or call_sid,
            caller_number=raw_payload.get("From") or raw_payload.get("caller_number"),
            caller_name=raw_payload.get("CallerName") or raw_payload.get("caller_name"),
            channel="twilio",
            transcript=speech_result,
            dtmf_digits=digits,
            reason=call_status if event_type == CallEventType.call_ended else None,
            provider="twilio",
            raw_payload=raw_payload,
        )

    def format_outbound(self, response: OutboundResponse) -> dict:
        """
        Format as TwiML-compatible response structure.

        In real production, this would generate actual TwiML XML.
        For the scaffold, we return a JSON representation that a
        TwiML generator can consume.
        """
        result: dict[str, Any] = {
            "twiml_actions": [],
            "session_id": response.session_id,
            "dry_run": response.dry_run,
        }

        if response.response_text:
            action: dict[str, Any] = {"verb": "Say", "text": response.response_text}
            if response.tts_audio_url:
                action = {"verb": "Play", "url": response.tts_audio_url}
            result["twiml_actions"].append(action)

        # After speaking, gather next input
        result["twiml_actions"].append({
            "verb": "Gather",
            "input": "speech dtmf",
            "timeout": 5,
            "language": "fr-FR",
        })

        return result

    def validate_signature(
        self,
        raw_body: bytes,
        signature: str,
        *,
        url: str | None = None,
        params: dict[str, str] | None = None,
    ) -> bool:
        """
        Validate inbound Twilio webhook signature.

        **Fail-closed**: when webhook_secret is configured but signature is
        missing or invalid, returns False.

        **Dev mode**: when webhook_secret is empty, always returns True.

        Two verification modes:

        1. **Real Twilio mode** (url + params provided, or webhook_url configured):
           Implements Twilio's documented algorithm:
             - data_to_sign = url + sorted POST params joined as key=value
             - HMAC-SHA1(auth_token, data_to_sign)
             - base64-encode the digest
             - compare with X-Twilio-Signature header

        2. **Simple HMAC mode** (no url / webhook_url):
           Falls back to HMAC-SHA256 of raw body — useful when running behind
           a proxy that strips the original URL, or for non-Twilio providers
           using the Twilio adapter format.
        """
        if not self._webhook_secret:
            return True

        # Fail closed: no signature provided when secret is set
        if not signature:
            _slog.warning(
                "twilio_signature_missing",
                has_secret=True,
            )
            return False

        effective_url = url or self._webhook_url

        if effective_url and params is not None:
            # Real Twilio signature algorithm
            return _verify_twilio_signature(
                auth_token=self._webhook_secret,
                url=effective_url,
                params=params,
                signature=signature,
            )

        # Fallback: simple HMAC-SHA256 of raw body
        expected = hmac.new(
            self._webhook_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


def _verify_twilio_signature(
    auth_token: str,
    url: str,
    params: dict[str, str],
    signature: str,
) -> bool:
    """
    Implement Twilio's request signature validation algorithm.

    Reference: https://www.twilio.com/docs/usage/security#validating-requests

    1. Take the full URL of the request.
    2. Iterate over POST body parameters sorted alphabetically by key.
    3. Append each parameter name and value (no separator) to the URL.
    4. Hash the result with HMAC-SHA1 using the auth token as the key.
    5. Base64-encode the hash.
    6. Compare to the X-Twilio-Signature header.
    """
    import base64

    # Build data string: URL + sorted params
    data = url
    for key in sorted(params.keys()):
        data += key + params[key]

    # HMAC-SHA1
    mac = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    expected = base64.b64encode(mac.digest()).decode("utf-8")

    return hmac.compare_digest(expected, signature)


# ── Concrete: Vapi-Style Webhook Scaffold ──────────────────────────


class VapiAdapter(TelephonyAdapter):
    """
    Scaffold adapter for Vapi-compatible webhook payloads.

    Vapi sends structured JSON events with a ``type`` field.
    No Vapi SDK dependency — plain dict parsing.
    """

    def __init__(self, webhook_secret: str = "") -> None:
        self._webhook_secret = webhook_secret

    @property
    def provider_name(self) -> str:
        return "vapi"

    def parse_inbound(self, raw_payload: dict) -> InboundCallEvent:
        """
        Parse Vapi-style webhook payload.

        Expected Vapi event structure:
          {"type": "call-started"|"speech"|"dtmf"|"call-ended", "call": {...}, ...}
        """
        msg_type = raw_payload.get("type", "")
        call_data = raw_payload.get("call", {})
        call_id = call_data.get("id") or raw_payload.get("call_id", "")

        if not msg_type:
            raise ValueError("Missing required field: type")

        type_map = {
            "call-started": CallEventType.call_started,
            "assistant-request": CallEventType.call_started,
            "speech": CallEventType.utterance,
            "transcript": CallEventType.utterance,
            "conversation-update": CallEventType.utterance,
            "dtmf": CallEventType.dtmf,
            "call-ended": CallEventType.call_ended,
            "end-of-call-report": CallEventType.call_ended,
            "hang": CallEventType.call_ended,
        }
        event_type = type_map.get(msg_type)
        if event_type is None:
            raise ValueError(
                f"Unsupported Vapi event type: '{msg_type}'. "
                f"Supported: {list(type_map.keys())}"
            )

        # Extract transcript from nested structure
        transcript = raw_payload.get("transcript")
        if not transcript:
            speech = raw_payload.get("speech")
            if isinstance(speech, dict):
                transcript = speech.get("text")
            elif isinstance(speech, str):
                transcript = speech

        event_id = raw_payload.get("event_id") or f"vapi-{call_id[:8]}-{uuid.uuid4().hex[:8]}"

        customer = call_data.get("customer", {})
        return InboundCallEvent(
            event_id=event_id,
            event_type=event_type,
            session_id=raw_payload.get("session_id") or call_id or None,
            caller_number=customer.get("number") or raw_payload.get("caller_number"),
            caller_name=customer.get("name") or raw_payload.get("caller_name"),
            channel="vapi",
            transcript=transcript,
            dtmf_digits=raw_payload.get("digits"),
            reason=raw_payload.get("reason") or raw_payload.get("endedReason"),
            provider="vapi",
            raw_payload=raw_payload,
        )

    def format_outbound(self, response: OutboundResponse) -> dict:
        """Format as Vapi-compatible assistant response."""
        result: dict[str, Any] = {
            "assistant": {
                "firstMessage": response.response_text,
            },
            "session_id": response.session_id,
            "dry_run": response.dry_run,
        }
        if response.tts_audio_url:
            result["assistant"]["audioUrl"] = response.tts_audio_url
        return result

    def validate_signature(self, raw_body: bytes, signature: str) -> bool:
        if not self._webhook_secret:
            return True
        import hmac
        expected = hmac.new(
            self._webhook_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


# ── Adapter Factory ────────────────────────────────────────────────


_ADAPTER_REGISTRY: dict[str, type[TelephonyAdapter]] = {
    "local": LocalAdapter,
    "twilio": TwilioAdapter,
    "vapi": VapiAdapter,
}


def get_telephony_adapter(
    provider: str = "local",
    webhook_secret: str = "",
    webhook_url: str = "",
) -> TelephonyAdapter:
    """
    Factory to instantiate a telephony adapter by provider name.

    Falls back to LocalAdapter if the provider is unknown.
    """
    cls = _ADAPTER_REGISTRY.get(provider)
    if cls is None:
        _slog.warning(
            "unknown_telephony_provider",
            provider=provider,
            available=list(_ADAPTER_REGISTRY.keys()),
        )
        return LocalAdapter()

    if provider == "twilio":
        return cls(webhook_secret=webhook_secret, webhook_url=webhook_url)  # type: ignore[call-arg]
    if provider == "vapi":
        return cls(webhook_secret=webhook_secret)  # type: ignore[call-arg]
    return cls()


# ── Module-level singletons ────────────────────────────────────────


def _init_idempotency_guard() -> EventIdempotencyGuard | RedisIdempotencyGuard:
    """Initialise the idempotency guard from app settings (deferred import)."""
    try:
        from app.config import settings
        return create_idempotency_guard(
            redis_url=settings.REDIS_URL,
            key_prefix=settings.REDIS_KEY_PREFIX,
            ttl_seconds=settings.REDIS_IDEMPOTENCY_TTL_SECONDS,
            ttl_hours=settings.TELEPHONY_EVENT_TTL_HOURS,
            max_entries=100_000,
        )
    except Exception:
        # Settings not available (e.g. during isolated import) — safe default
        return EventIdempotencyGuard()


idempotency_guard = _init_idempotency_guard()
"""Shared idempotency guard for telephony events (in-memory or Redis)."""
