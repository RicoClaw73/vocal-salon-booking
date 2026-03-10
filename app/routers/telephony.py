"""
Telephony integration endpoints (Phase 5.3).

Provides a provider-agnostic webhook ingestion layer for inbound telephony
call events.  Events are normalised through the adapter layer, de-duplicated
via the idempotency guard, and routed through the existing voice pipeline.

Pilot controls:
  - TELEPHONY_ENABLED: gate all ingestion (returns 503 when off)
  - TELEPHONY_DRY_RUN: process events but suppress outbound side-effects
  - Payload size guardrails (TELEPHONY_MAX_PAYLOAD_BYTES)
  - Per-event observability logging and metrics

These endpoints sit alongside (not replace) the existing /voice/* endpoints.
Backward compatibility is preserved — /voice/turn etc. work as before.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_api_key
from app.config import settings
from app.conversation import conversation_manager
from app.database import get_db
from app.observability import StructuredLogger, metrics, new_request_id
from app.rate_limit import rate_limit_dependency
from app.session_store import (
    append_transcript_event,
    create_session as db_create_session,
    load_session as db_load_session,
    save_session as db_save_session,
)
from app.telephony_adapter import (
    CallEventType,
    InboundCallEvent,
    OutboundResponse,
    TelephonyAdapter,
    get_telephony_adapter,
    idempotency_guard,
)
from app.voice_schemas import SessionStatus, VoiceIntent

_slog = StructuredLogger(__name__)

router = APIRouter(
    prefix="/telephony",
    tags=["telephony"],
    dependencies=[Depends(require_api_key), Depends(rate_limit_dependency)],
)


# ── Greeting / goodbye templates (reuse from voice) ─────────────

_GREETING = (
    "Bonjour et bienvenue chez Maison Éclat ! "
    "Je peux vous aider à prendre rendez-vous, modifier ou annuler une réservation. "
    "Comment puis-je vous aider ?"
)

_GOODBYE = "Merci d'avoir appelé Maison Éclat. À bientôt !"


# ── Helper: resolve adapter from config ──────────────────────────

def _get_adapter() -> TelephonyAdapter:
    return get_telephony_adapter(
        provider=settings.TELEPHONY_PROVIDER,
        webhook_secret=settings.TELEPHONY_WEBHOOK_SECRET,
    )


# ── Pilot gate middleware ────────────────────────────────────────

def _check_telephony_enabled() -> None:
    """Raise 503 if telephony ingestion is disabled."""
    if not settings.TELEPHONY_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Telephony ingestion is disabled. Set TELEPHONY_ENABLED=true to activate.",
        )


# ── Primary webhook endpoint ─────────────────────────────────────


@router.post("/inbound")
async def telephony_inbound(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Primary telephony webhook endpoint.

    Accepts inbound call events from any configured provider.  The raw
    payload is parsed by the active adapter, de-duplicated, and routed
    through the voice pipeline.

    Pilot controls:
      - Returns 503 when TELEPHONY_ENABLED is False
      - In dry-run mode, processes events but suppresses side-effects
      - Enforces payload size limits
      - De-duplicates via event_id idempotency guard
    """
    rid = new_request_id()
    t0 = time.monotonic()

    # 1. Pilot gate
    _check_telephony_enabled()

    # 2. Payload size guardrail
    body = await request.body()
    if len(body) > settings.TELEPHONY_MAX_PAYLOAD_BYTES:
        metrics.inc("telephony_payload_rejected")
        _slog.warning(
            "telephony_payload_too_large",
            request_id=rid,
            size_bytes=len(body),
            limit_bytes=settings.TELEPHONY_MAX_PAYLOAD_BYTES,
        )
        raise HTTPException(
            status_code=413,
            detail=f"Payload too large: {len(body)} bytes (limit: {settings.TELEPHONY_MAX_PAYLOAD_BYTES}).",
        )

    # 3. Parse raw payload
    try:
        raw_payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        metrics.inc("telephony_payload_invalid")
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    # 4. Signature verification (provider-specific, no-op for local)
    adapter = _get_adapter()
    signature = request.headers.get("X-Telephony-Signature", "")
    if not adapter.validate_signature(body, signature):
        metrics.inc("telephony_signature_invalid")
        _slog.warning("telephony_signature_invalid", request_id=rid, provider=adapter.provider_name)
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    # 5. Parse into canonical event
    try:
        event = adapter.parse_inbound(raw_payload)
    except ValueError as e:
        metrics.inc("telephony_parse_error")
        _slog.warning("telephony_parse_error", request_id=rid, error=str(e))
        raise HTTPException(status_code=422, detail=str(e))

    # 6. Idempotency check
    if not idempotency_guard.check_and_mark(event.event_id):
        _slog.info(
            "telephony_event_duplicate",
            request_id=rid,
            event_id=event.event_id,
            event_type=event.event_type.value,
        )
        return {
            "status": "duplicate",
            "event_id": event.event_id,
            "message": "Event already processed.",
        }

    # 7. Route event through pipeline
    metrics.inc("telephony_events_received")
    metrics.inc(f"telephony_event_{event.event_type.value.replace('.', '_')}")

    _slog.info(
        "telephony_event_received",
        request_id=rid,
        event_id=event.event_id,
        event_type=event.event_type.value,
        provider=event.provider,
        session_id=event.session_id,
        dry_run=settings.TELEPHONY_DRY_RUN,
    )

    result = await _handle_event(event, adapter, db, rid)

    latency_ms = round((time.monotonic() - t0) * 1000, 2)
    metrics.record_latency("telephony_event_ms", latency_ms)

    _slog.info(
        "telephony_event_processed",
        request_id=rid,
        event_id=event.event_id,
        event_type=event.event_type.value,
        session_id=result.get("session_id"),
        latency_ms=latency_ms,
        dry_run=settings.TELEPHONY_DRY_RUN,
    )

    return result


# ── Event routing ────────────────────────────────────────────────


async def _handle_event(
    event: InboundCallEvent,
    adapter: TelephonyAdapter,
    db: AsyncSession,
    rid: str,
) -> dict:
    """Route a canonical event to the appropriate handler."""
    if event.event_type == CallEventType.call_started:
        return await _handle_call_started(event, adapter, db, rid)
    elif event.event_type in (
        CallEventType.utterance,
        CallEventType.dtmf,
        CallEventType.silence_timeout,
    ):
        return await _handle_utterance(event, adapter, db, rid)
    elif event.event_type == CallEventType.call_ended:
        return await _handle_call_ended(event, adapter, db, rid)
    else:
        return {"status": "ignored", "event_type": event.event_type.value}


async def _handle_call_started(
    event: InboundCallEvent,
    adapter: TelephonyAdapter,
    db: AsyncSession,
    rid: str,
) -> dict:
    """Handle call.started — create a new voice session."""
    state = await db_create_session(
        db,
        client_name=event.caller_name,
        client_phone=event.caller_number,
        channel=event.channel,
    )
    conversation_manager._sessions[state.session_id] = state
    await db.commit()

    metrics.inc("telephony_calls_started")
    metrics.inc("sessions_started")

    response = OutboundResponse(
        session_id=state.session_id,
        response_text=_GREETING,
        intent=None,
        action_taken="session_created",
        turn_number=0,
        dry_run=settings.TELEPHONY_DRY_RUN,
    )

    outbound = adapter.format_outbound(response)

    return {
        "status": "ok",
        "event_id": event.event_id,
        "session_id": state.session_id,
        "greeting": _GREETING,
        "dry_run": settings.TELEPHONY_DRY_RUN,
        "outbound": outbound,
    }


async def _handle_utterance(
    event: InboundCallEvent,
    adapter: TelephonyAdapter,
    db: AsyncSession,
    rid: str,
) -> dict:
    """
    Handle utterance/dtmf/silence events — route through voice pipeline.

    Reuses the existing intent extraction, handler routing, and session
    persistence logic from the voice module.  In dry-run mode, processes
    fully but marks the response as dry_run=True (no actual TTS delivery).
    """
    from app.intent import extract_intent
    from app.routers.voice import (
        _INTENT_HANDLERS,
        _merge_entities_to_draft,
        FALLBACK_CONFIDENCE_THRESHOLD,
        _FALLBACK_RESPONSES,
        MAX_CONSECUTIVE_FALLBACKS,
        _HUMAN_TRANSFER_MSG,
    )

    # Resolve session
    session_id = event.session_id
    if not session_id:
        raise HTTPException(status_code=422, detail="session_id required for utterance events.")

    state = await db_load_session(db, session_id)
    if state is None:
        state = conversation_manager.get_session(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")

    if state.status != SessionStatus.active:
        raise HTTPException(status_code=409, detail="Session already ended.")

    # Resolve text from event
    if event.event_type == CallEventType.dtmf:
        user_text = f"[DTMF: {event.dtmf_digits or ''}]"
    elif event.event_type == CallEventType.silence_timeout:
        user_text = "..."
    else:
        user_text = event.transcript or ""

    if not user_text:
        raise HTTPException(status_code=422, detail="No transcript or input in event.")

    state.increment_turn()

    # Intent extraction
    intent_result = extract_intent(user_text)
    intent = intent_result.intent
    confidence = intent_result.confidence
    entities = intent_result.entities

    # Fallback handling
    has_active_intent = (
        state.current_intent is not None and state.current_intent != VoiceIntent.unknown
    )
    is_fallback = False

    if (confidence < FALLBACK_CONFIDENCE_THRESHOLD or intent == VoiceIntent.unknown) \
            and not has_active_intent:
        is_fallback = True
        consecutive = getattr(state, "_consecutive_fallbacks", 0) + 1
        state._consecutive_fallbacks = consecutive  # type: ignore[attr-defined]

        if consecutive >= MAX_CONSECUTIVE_FALLBACKS:
            response_text = _HUMAN_TRANSFER_MSG
            action_taken = "human_transfer_offered"
        else:
            idx = (consecutive - 1) % len(_FALLBACK_RESPONSES)
            response_text = _FALLBACK_RESPONSES[idx]
            action_taken = "fallback"

        metrics.inc("voice_fallbacks")
    else:
        state._consecutive_fallbacks = 0  # type: ignore[attr-defined]

        if intent != VoiceIntent.unknown:
            state.current_intent = intent
        _merge_entities_to_draft(state, entities)

        handler = _INTENT_HANDLERS.get(
            state.current_intent or VoiceIntent.unknown,
        )
        if handler is None:
            from app.routers.voice import _handle_unknown
            handler = _handle_unknown
        response_text, action_taken, data = await handler(state, entities, db)

    # Persist state + transcript
    conversation_manager._sessions[state.session_id] = state
    await db_save_session(db, state)
    await append_transcript_event(
        db,
        session_id=state.session_id,
        turn_number=state.turns,
        user_text=user_text,
        intent=(state.current_intent or VoiceIntent.unknown).value,
        confidence=confidence,
        response_text=response_text,
        action_taken=action_taken if not is_fallback else action_taken,
        is_fallback=is_fallback,
        data=data if not is_fallback else None,
    )
    await db.commit()

    metrics.inc("voice_turns")
    metrics.inc("telephony_utterances_processed")

    resolved_intent = (state.current_intent or VoiceIntent.unknown).value

    response = OutboundResponse(
        session_id=state.session_id,
        response_text=response_text,
        intent=resolved_intent,
        action_taken=action_taken,
        turn_number=state.turns,
        is_fallback=is_fallback,
        dry_run=settings.TELEPHONY_DRY_RUN,
        data=data if not is_fallback else None,
    )

    outbound = adapter.format_outbound(response)

    return {
        "status": "ok",
        "event_id": event.event_id,
        "session_id": state.session_id,
        "intent": resolved_intent,
        "confidence": confidence,
        "response_text": response_text,
        "action_taken": action_taken,
        "is_fallback": is_fallback,
        "turn_number": state.turns,
        "dry_run": settings.TELEPHONY_DRY_RUN,
        "outbound": outbound,
    }


async def _handle_call_ended(
    event: InboundCallEvent,
    adapter: TelephonyAdapter,
    db: AsyncSession,
    rid: str,
) -> dict:
    """Handle call.ended — close the voice session."""
    session_id = event.session_id
    if not session_id:
        return {"status": "ok", "event_id": event.event_id, "message": "No session to end."}

    state = await db_load_session(db, session_id)
    if state is None:
        state = conversation_manager.get_session(session_id)
    if state is None:
        return {"status": "ok", "event_id": event.event_id, "message": "Session not found; already cleaned up."}

    state.status = SessionStatus.completed
    state.touch()
    conversation_manager.end_session(session_id)
    conversation_manager._sessions[session_id] = state
    await db_save_session(db, state)
    await db.commit()

    metrics.inc("sessions_completed")
    metrics.inc("telephony_calls_ended")

    response = OutboundResponse(
        session_id=state.session_id,
        response_text=_GOODBYE,
        action_taken="session_ended",
        turn_number=state.turns,
        dry_run=settings.TELEPHONY_DRY_RUN,
    )

    outbound = adapter.format_outbound(response)

    return {
        "status": "ok",
        "event_id": event.event_id,
        "session_id": state.session_id,
        "turns": state.turns,
        "duration_seconds": round(state.duration_seconds, 2),
        "dry_run": settings.TELEPHONY_DRY_RUN,
        "outbound": outbound,
    }


# ── Pilot control endpoints ─────────────────────────────────────


@router.get("/status")
async def telephony_status() -> dict:
    """
    Report telephony integration status.

    Returns current pilot settings, adapter info, and event counts.
    """
    adapter = _get_adapter()
    snap = metrics.snapshot()
    counters = snap.get("counters", {})

    return {
        "enabled": settings.TELEPHONY_ENABLED,
        "dry_run": settings.TELEPHONY_DRY_RUN,
        "provider": adapter.provider_name,
        "max_payload_bytes": settings.TELEPHONY_MAX_PAYLOAD_BYTES,
        "event_ttl_hours": settings.TELEPHONY_EVENT_TTL_HOURS,
        "idempotency_guard_size": idempotency_guard.size,
        "counters": {
            "events_received": counters.get("telephony_events_received", 0),
            "calls_started": counters.get("telephony_calls_started", 0),
            "calls_ended": counters.get("telephony_calls_ended", 0),
            "utterances_processed": counters.get("telephony_utterances_processed", 0),
            "replays_rejected": counters.get("telephony_event_replay_rejected", 0),
            "payload_rejected": counters.get("telephony_payload_rejected", 0),
            "parse_errors": counters.get("telephony_parse_error", 0),
        },
        "latency": snap.get("latencies", {}).get("telephony_event_ms"),
    }


@router.post("/retention/prune")
async def prune_event_ids() -> dict:
    """
    Manually trigger pruning of expired event IDs from the idempotency guard.

    This is normally automatic, but can be called explicitly for cleanup.
    """
    _check_telephony_enabled()
    before = idempotency_guard.size
    idempotency_guard._prune_expired(time.monotonic())
    after = idempotency_guard.size
    return {
        "pruned": before - after,
        "remaining": after,
    }
