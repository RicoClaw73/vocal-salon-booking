"""
Voice pipeline webhook-style endpoints.

POST /voice/sessions/start    – open a new voice conversation
POST /voice/sessions/message  – process a transcribed user utterance
POST /voice/sessions/end      – close a voice session
POST /voice/turn              – Phase 3: unified voice turn orchestration
                                (STT → intent → conversation → TTS)
GET  /voice/sessions/{id}/transcript – Phase 4.2→4.3: fetch session state + transcript

These endpoints form the integration layer between a local STT/TTS pipeline
and the existing salon booking API.  No external services required.

Phase 4.3 changes:
  - Session state is persisted to the database (voice_sessions table).
  - Transcript events are persisted (transcript_events table) so transcripts
    survive process restarts.
  - The in-memory ConversationManager is kept as a hot-cache / fallback for
    backward compatibility.  DB is the source of truth.
  - API key auth (optional) and rate limiting (scaffold) added as deps.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_tenant
from app.circuit_breaker import stt_circuit_breaker, tts_circuit_breaker
from app.config import settings
from app.conversation import ConversationState, conversation_manager
from app.database import get_db
from app.intent import extract_intent_async
from app.models import Booking, BookingStatus, Employee, EmployeeCompetency, Service, Tenant
from app.observability import StructuredLogger, metrics, new_request_id
from app.providers import (
    MockSTTProvider,
    MockTTSProvider,
    ProviderErrorKind,
    ProviderOutcome,
    STTProvider,
    TTSProvider,
    get_stt_provider,
    get_tts_provider,
    safe_synthesize,
    safe_transcribe,
)
from app.rate_limit import rate_limit_dependency
from app.settings_service import get_tenant_settings
from app.session_store import (
    append_transcript_event,
    get_transcript_events,
)
from app.session_store import (
    create_session as db_create_session,
)
from app.session_store import (
    load_session as db_load_session,
)
from app.session_store import (
    save_session as db_save_session,
)
from app.slot_engine import find_available_slots, validate_booking_request
from app.voice_schemas import (
    AudioMeta,
    SessionEndRequest,
    SessionEndResponse,
    SessionStartRequest,
    SessionStartResponse,
    SessionStatus,
    UserMessageRequest,
    UserMessageResponse,
    VoiceIntent,
    VoiceTurnRequest,
    VoiceTurnResponse,
)

logger = logging.getLogger(__name__)
_slog = StructuredLogger(__name__)

router = APIRouter(
    prefix="/voice",
    tags=["voice"],
    dependencies=[Depends(rate_limit_dependency)],
)

# ── Greeting templates ───────────────────────────────────────

# ── Fallback configuration ──────────────────────────────────

FALLBACK_CONFIDENCE_THRESHOLD = 0.5
"""Confidence below this triggers the deterministic fallback response."""

_FALLBACK_RESPONSES: list[str] = [
    (
        "Je n'ai pas bien compris votre demande. Je peux vous aider à :\n"
        "• Prendre un rendez-vous\n"
        "• Modifier ou annuler un rendez-vous\n"
        "• Vérifier les disponibilités\n"
        "• Répondre à vos questions sur le salon (adresse, horaires, tarifs…)\n"
        "Pourriez-vous reformuler ?"
    ),
    (
        "Pardon, je n'ai pas saisi. Vous pouvez me dire par exemple : "
        "\"je voudrais réserver une coupe\", \"annuler ma réservation numéro 5\", "
        "ou \"quels sont vos horaires ?\"."
    ),
    (
        "Je suis désolé, je ne comprends toujours pas. "
        "Essayez de me dire quel service vous intéresse (coupe, couleur, balayage…), "
        "votre numéro de réservation, ou posez-moi une question sur le salon."
    ),
]
"""Rotating fallback messages — vary phrasing to avoid frustrating the caller."""

MAX_CONSECUTIVE_FALLBACKS = 3
"""After this many consecutive unknowns, offer to transfer to a human."""

def _human_transfer_msg() -> str:
    phone_info = f" au {settings.TWILIO_TRANSFER_NUMBER}" if settings.TWILIO_TRANSFER_NUMBER else ""
    return (
        "Il semble que j'aie du mal à vous comprendre. "
        "Souhaitez-vous être mis en relation avec un membre de notre équipe ?"
        f" Vous pouvez aussi nous rappeler directement{phone_info}."
    )


# ── Provider singletons (config-driven, mock default) ──────

def _init_stt_provider() -> STTProvider:
    """Build STT provider from settings; falls back to mock if creds missing."""
    return get_stt_provider(
        settings.STT_PROVIDER,
        api_key=settings.STT_API_KEY,
        model=settings.STT_MODEL or None,
    )


def _init_tts_provider() -> TTSProvider:
    """Build TTS provider from settings; falls back to mock if creds missing."""
    return get_tts_provider(
        settings.TTS_PROVIDER,
        api_key=settings.TTS_API_KEY,
        voice_id=settings.TTS_VOICE_ID or None,
        model=settings.TTS_MODEL or None,
    )


_stt_provider: STTProvider = _init_stt_provider()
_tts_provider: TTSProvider = _init_tts_provider()
# Keep mock instances for automatic fallback when real providers fail at runtime.
_stt_fallback: STTProvider = MockSTTProvider()
_tts_fallback: TTSProvider = MockTTSProvider()


def _get_circuit_breaker(role: str):
    """Return the circuit breaker for the given role."""
    if role == "stt":
        return stt_circuit_breaker
    return tts_circuit_breaker


def _persist_tts_artifact(
    session_id: str, response_text: str, tts_result: object,
) -> str | None:
    """Store TTS audio artifact locally if real audio bytes are available.

    Returns the artifact URL/path or None if nothing was persisted (e.g. mock
    provider that produces no actual audio).
    """
    # Real providers could attach audio bytes to TTS result in future.
    # For now, only persist when audio_url is set (not None) or we synthesised
    # real bytes.  The mock provider returns audio_url=None — skip silently.
    if tts_result.audio_url is not None:
        # Already has a URL (e.g. from a cloud provider) — return as-is.
        return tts_result.audio_url
    # Placeholder: when real TTS providers return raw bytes (future),
    # we can persist them via tts_artifact_store.store_and_get_url().
    return None


def _record_provider_outcome(
    role: str, outcome: ProviderOutcome, rid: str, session_id: str,
) -> None:
    """Emit structured log + metrics for a provider call outcome, update circuit breaker."""
    cb = _get_circuit_breaker(role)

    if outcome.fallback_used:
        cb.record_failure()
        metrics.inc(f"provider_{role}_fallback")
        _slog.warning(
            f"provider_{role}_fallback",
            request_id=rid,
            session_id=session_id,
            error_kind=outcome.error_kind.value if outcome.error_kind else None,
            error_detail=outcome.error_detail,
            cb_state=cb.state.value,
        )
    elif outcome.error_kind and not outcome.success:
        cb.record_failure()
        metrics.inc(f"provider_{role}_error")
        metrics.inc(f"provider_{role}_{outcome.error_kind.value}")
        _slog.error(
            f"provider_{role}_error",
            request_id=rid,
            session_id=session_id,
            error_kind=outcome.error_kind.value,
            error_detail=outcome.error_detail,
            cb_state=cb.state.value,
        )
    else:
        cb.record_success()


def _collect_provider_errors(
    *outcomes: tuple[str, ProviderOutcome],
) -> list[dict] | None:
    """Gather non-success outcomes into a serialisable list (or None)."""
    errors = []
    for role, o in outcomes:
        if o.error_kind is not None:
            errors.append({
                "role": role,
                "error_kind": o.error_kind.value,
                "error_detail": o.error_detail,
                "fallback_used": o.fallback_used,
            })
    return errors or None


# ── Session helpers (DB-backed + in-memory cache) ────────────

async def _resolve_session(
    db: AsyncSession,
    session_id: str | None,
    tenant_id: int = 0,
    client_name: str | None = None,
    client_phone: str | None = None,
    channel: str = "phone",
    *,
    auto_create: bool = False,
) -> ConversationState:
    """
    Load a session from DB (source of truth), falling back to the in-memory
    cache for backward compat.  Optionally auto-create if session_id is None.
    """
    if session_id:
        # Try DB first
        state = await db_load_session(db, session_id, tenant_id=tenant_id or None)
        if state is not None:
            # Carry over volatile in-memory attributes (e.g. fallback counter)
            old = conversation_manager._sessions.get(session_id)
            if old is not None:
                state._consecutive_fallbacks = getattr(  # type: ignore[attr-defined]
                    old, "_consecutive_fallbacks", 0
                )
            # Sync into in-memory cache
            conversation_manager._sessions[session_id] = state
            return state
        # Fallback: maybe it only exists in memory (legacy)
        state = conversation_manager.get_session(session_id)
        if state is not None:
            return state
        raise HTTPException(
            status_code=404,
            detail=f"Session '{session_id}' introuvable.",
        )

    if not auto_create:
        raise HTTPException(status_code=422, detail="session_id is required.")

    # Create in DB and mirror to in-memory cache
    state = await db_create_session(db, tenant_id, client_name, client_phone, channel)
    conversation_manager._sessions[state.session_id] = state
    return state


async def _persist_state(db: AsyncSession, state: ConversationState) -> None:
    """Save session state to DB and keep in-memory cache in sync."""
    conversation_manager._sessions[state.session_id] = state
    await db_save_session(db, state)


# ── Endpoints ────────────────────────────────────────────────

@router.post("/sessions/start", response_model=SessionStartResponse, status_code=201)
async def start_session(
    payload: SessionStartRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> SessionStartResponse:
    """Open a new voice conversation session."""
    rid = new_request_id()
    state = await _resolve_session(
        db,
        session_id=None,
        tenant_id=tenant.id,
        client_name=payload.client_name,
        client_phone=payload.client_phone,
        channel=payload.channel,
        auto_create=True,
    )
    await db.commit()
    metrics.inc("sessions_started")
    _slog.info(
        "session_started",
        request_id=rid,
        session_id=state.session_id,
        channel=payload.channel,
    )
    tenant_settings = get_tenant_settings(tenant.id)
    return SessionStartResponse(
        session_id=state.session_id,
        status=state.status,
        greeting=tenant_settings.GREETING_TEXT,
        created_at=state.created_at,
    )


@router.post("/sessions/message", response_model=UserMessageResponse)
async def process_message(
    payload: UserMessageRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> UserMessageResponse:
    """Process a transcribed user utterance through intent detection and fulfillment."""
    import time as _time

    rid = new_request_id()
    t0 = _time.monotonic()

    state = await _resolve_session(db, payload.session_id, tenant_id=tenant.id)
    if state.status != SessionStatus.active:
        raise HTTPException(status_code=409, detail="Cette session est déjà terminée.")

    state.increment_turn()

    # Extract intent and entities (LLM-first when configured, else rule-based)
    result = await extract_intent_async(payload.text)
    intent = result.intent
    entities = result.entities

    # Update session intent (new intent overrides, unless unknown)
    if intent != VoiceIntent.unknown:
        state.current_intent = intent

    # Merge extracted entities into booking draft
    _merge_entities_to_draft(state, entities)

    # Route to appropriate handler
    handler = _INTENT_HANDLERS.get(state.current_intent or VoiceIntent.unknown, _handle_unknown)
    response_text, action_taken, data = await handler(state, entities, db)

    # Persist state + transcript event
    await _persist_state(db, state)
    await append_transcript_event(
        db,
        session_id=state.session_id,
        turn_number=state.turns,
        user_text=payload.text,
        intent=(state.current_intent or VoiceIntent.unknown).value,
        confidence=result.confidence,
        response_text=response_text,
        action_taken=action_taken,
        data=data,
    )
    await db.commit()

    latency_ms = round((_time.monotonic() - t0) * 1000, 2)
    resolved_intent = (state.current_intent or VoiceIntent.unknown).value
    metrics.inc("voice_turns")
    metrics.inc(f"intent_{resolved_intent}")
    metrics.record_latency("voice_turn_ms", latency_ms)
    _slog.info(
        "message_processed",
        request_id=rid,
        session_id=state.session_id,
        intent=resolved_intent,
        outcome=action_taken,
        latency_ms=latency_ms,
    )

    return UserMessageResponse(
        session_id=state.session_id,
        intent=state.current_intent or VoiceIntent.unknown,
        response_text=response_text,
        booking_draft=state.booking_draft,
        action_taken=action_taken,
        data=data,
    )


@router.post("/sessions/end", response_model=SessionEndResponse)
async def end_session(
    payload: SessionEndRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> SessionEndResponse:
    """Close a voice conversation session."""
    rid = new_request_id()
    state = await _resolve_session(db, payload.session_id, tenant_id=tenant.id)

    state.status = SessionStatus.completed
    state.touch()

    # Persist + sync
    conversation_manager.end_session(payload.session_id)
    await _persist_state(db, state)
    await db.commit()

    metrics.inc("sessions_completed")
    _slog.info(
        "session_ended",
        request_id=rid,
        session_id=state.session_id,
        turns=state.turns,
        duration_s=round(state.duration_seconds, 2),
    )

    tenant_settings = get_tenant_settings(tenant.id)
    return SessionEndResponse(
        session_id=state.session_id,
        status=SessionStatus.completed,
        message=tenant_settings.GOODBYE_TEXT,
        turns=state.turns,
        duration_seconds=state.duration_seconds,
    )


# ── Phase 3: Voice Turn Orchestration ───────────────────────


@router.post("/turn", response_model=VoiceTurnResponse)
async def voice_turn(
    payload: VoiceTurnRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> VoiceTurnResponse:
    """
    Unified voice turn endpoint — full STT → Intent → Handler → TTS loop.

    Accepts either pre-transcribed text or mock transcript payload.
    Creates a session automatically if session_id is not provided.
    Returns assistant reply with intent metadata and TTS audio metadata.
    """
    import time as _time

    rid = new_request_id()
    t0 = _time.monotonic()

    # 1. Resolve or create session
    state: ConversationState
    if payload.session_id:
        state = await _resolve_session(db, payload.session_id, tenant_id=tenant.id)
        if state.status != SessionStatus.active:
            raise HTTPException(
                status_code=409,
                detail="Cette session est déjà terminée.",
            )
    else:
        # Auto-create session for convenience
        state = await _resolve_session(
            db,
            session_id=None,
            tenant_id=tenant.id,
            client_name=payload.client_name,
            client_phone=payload.client_phone,
            channel=payload.channel,
            auto_create=True,
        )

    # 2. Resolve input text — three modes:
    #    a) text or mock_transcript provided → skip STT (text-only / test mode)
    #    b) audio_base64 provided → decode and send through real STT pipeline
    #    c) none of the above → 422
    import base64 as _b64

    user_text = payload.text or payload.mock_transcript
    audio_bytes: bytes | None = None
    audio_fmt_str = payload.audio_format or "wav"

    # Only decode audio_base64 when no text input is provided (text takes priority).
    if not user_text and payload.audio_base64 is not None:
        # Decode base64 audio payload
        try:
            audio_bytes = _b64.b64decode(payload.audio_base64, validate=True)
        except Exception:
            raise HTTPException(
                status_code=422,
                detail="audio_base64 is not valid base64.",
            )
        if len(audio_bytes) == 0:
            raise HTTPException(
                status_code=422,
                detail="audio_base64 decoded to empty bytes.",
            )

    if not user_text and audio_bytes is None:
        raise HTTPException(
            status_code=422,
            detail="Provide 'text', 'mock_transcript', or 'audio_base64'.",
        )

    # 3. STT step (with circuit-breaker + error classification + auto-fallback)
    #    When audio_bytes are present, route through the real STT provider.
    #    When only text is provided, pass encoded text (mock-friendly path).
    from app.providers import AudioFormat

    stt_input_bytes = audio_bytes if audio_bytes is not None else user_text.encode("utf-8")
    stt_audio_format = (
        AudioFormat(audio_fmt_str) if audio_fmt_str in AudioFormat.__members__ else AudioFormat.wav
    )
    stt_sample_rate = payload.audio_sample_rate or 16000

    stt_cb = _get_circuit_breaker("stt")
    if stt_cb.should_allow_request():
        stt_result, stt_outcome = await safe_transcribe(
            _stt_provider,
            audio_bytes=stt_input_bytes,
            audio_format=stt_audio_format,
            language="fr",
            fallback=_stt_fallback,
        )
    else:
        # Circuit breaker is open → skip real provider, go straight to fallback
        stt_result = await _stt_fallback.transcribe(
            stt_input_bytes, audio_format=stt_audio_format, language="fr",
        )
        stt_outcome = ProviderOutcome(
            success=True,
            error_kind=ProviderErrorKind.fallback_used,
            error_detail=f"circuit_breaker_open (cooldown {stt_cb.snapshot()['current_cooldown_s']}s)",  # noqa: E501
            fallback_used=True,
        )
        metrics.inc("cb_stt_short_circuit")
    _record_provider_outcome("stt", stt_outcome, rid, state.session_id)
    stt_meta = AudioMeta(
        format=audio_fmt_str,
        duration_ms=stt_result.duration_ms,
        sample_rate=stt_sample_rate,
        provider=stt_result.provider,
    )

    # When audio_base64 was provided, the STT transcript is the resolved text.
    # Override user_text with STT result if we came through the audio path.
    if audio_bytes is not None:
        user_text = stt_result.transcript

    # 4. Intent extraction (LLM-first when configured, else rule-based)
    state.increment_turn()
    intent_result = await extract_intent_async(user_text)
    intent = intent_result.intent
    confidence = intent_result.confidence
    entities = intent_result.entities

    # 5. Deterministic fallback strategy
    has_active_intent = (
        state.current_intent is not None and state.current_intent != VoiceIntent.unknown
    )
    if (confidence < FALLBACK_CONFIDENCE_THRESHOLD or intent == VoiceIntent.unknown) \
            and not has_active_intent:
        consecutive = getattr(state, "_consecutive_fallbacks", 0) + 1
        state._consecutive_fallbacks = consecutive  # type: ignore[attr-defined]

        if consecutive >= MAX_CONSECUTIVE_FALLBACKS:
            response_text = _human_transfer_msg()
            action_taken = "human_transfer_offered"
        else:
            idx = (consecutive - 1) % len(_FALLBACK_RESPONSES)
            response_text = _FALLBACK_RESPONSES[idx]
            action_taken = "fallback"

        # Generate TTS for fallback (with circuit-breaker + error classification)
        tts_cb = _get_circuit_breaker("tts")
        if tts_cb.should_allow_request():
            tts_result, tts_outcome = await safe_synthesize(
                _tts_provider, response_text, language="fr", fallback=_tts_fallback,
            )
        else:
            tts_result = await _tts_fallback.synthesize(response_text, language="fr")
            tts_outcome = ProviderOutcome(
                success=True,
                error_kind=ProviderErrorKind.fallback_used,
                error_detail=f"circuit_breaker_open (cooldown {tts_cb.snapshot()['current_cooldown_s']}s)",  # noqa: E501
                fallback_used=True,
            )
            metrics.inc("cb_tts_short_circuit")
        _record_provider_outcome("tts", tts_outcome, rid, state.session_id)
        tts_meta = AudioMeta(
            format=tts_result.audio_format.value,
            duration_ms=tts_result.duration_ms,
            sample_rate=tts_result.sample_rate,
            provider=tts_result.provider,
        )

        # Persist state + transcript event
        await _persist_state(db, state)
        await append_transcript_event(
            db,
            session_id=state.session_id,
            turn_number=state.turns,
            user_text=user_text,
            intent=VoiceIntent.unknown.value,
            confidence=confidence,
            response_text=response_text,
            action_taken=action_taken,
            is_fallback=True,
        )
        await db.commit()

        latency_ms = round((_time.monotonic() - t0) * 1000, 2)
        metrics.inc("voice_turns")
        metrics.inc("voice_fallbacks")
        metrics.record_latency("voice_turn_ms", latency_ms)
        _slog.info(
            "voice_turn_fallback",
            request_id=rid,
            session_id=state.session_id,
            intent="unknown",
            outcome=action_taken,
            latency_ms=latency_ms,
            consecutive_fallbacks=consecutive,
        )

        tts_audio_url = _persist_tts_artifact(state.session_id, response_text, tts_result)

        return VoiceTurnResponse(
            session_id=state.session_id,
            turn_number=state.turns,
            intent=VoiceIntent.unknown,
            confidence=confidence,
            response_text=response_text,
            is_fallback=True,
            booking_draft=state.booking_draft,
            action_taken=action_taken,
            data=None,
            stt_meta=stt_meta,
            tts_meta=tts_meta,
            tts_audio_url=tts_audio_url,
            provider_errors=_collect_provider_errors(
                ("stt", stt_outcome), ("tts", tts_outcome),
            ),
        )

    # Reset consecutive fallback counter on successful intent
    state._consecutive_fallbacks = 0  # type: ignore[attr-defined]

    # 6. Update session intent (new intent overrides, unless unknown)
    if intent != VoiceIntent.unknown:
        state.current_intent = intent

    # 7. Merge extracted entities into booking draft
    _merge_entities_to_draft(state, entities)

    # 8. Route to appropriate handler
    handler = _INTENT_HANDLERS.get(state.current_intent or VoiceIntent.unknown, _handle_unknown)
    response_text, action_taken, data = await handler(state, entities, db)

    # 9. TTS synthesis (with circuit-breaker + error classification + auto-fallback)
    tts_cb = _get_circuit_breaker("tts")
    if tts_cb.should_allow_request():
        tts_result, tts_outcome = await safe_synthesize(
            _tts_provider, response_text, language="fr", fallback=_tts_fallback,
        )
    else:
        tts_result = await _tts_fallback.synthesize(response_text, language="fr")
        tts_outcome = ProviderOutcome(
            success=True,
            error_kind=ProviderErrorKind.fallback_used,
            error_detail=f"circuit_breaker_open (cooldown {tts_cb.snapshot()['current_cooldown_s']}s)",  # noqa: E501
            fallback_used=True,
        )
        metrics.inc("cb_tts_short_circuit")
    _record_provider_outcome("tts", tts_outcome, rid, state.session_id)
    tts_meta = AudioMeta(
        format=tts_result.audio_format.value,
        duration_ms=tts_result.duration_ms,
        sample_rate=tts_result.sample_rate,
        provider=tts_result.provider,
    )

    # 10. Persist state + transcript event
    await _persist_state(db, state)
    await append_transcript_event(
        db,
        session_id=state.session_id,
        turn_number=state.turns,
        user_text=user_text,
        intent=(state.current_intent or VoiceIntent.unknown).value,
        confidence=confidence,
        response_text=response_text,
        action_taken=action_taken,
        is_fallback=False,
        data=data,
    )
    await db.commit()

    latency_ms = round((_time.monotonic() - t0) * 1000, 2)
    resolved_intent = (state.current_intent or VoiceIntent.unknown).value
    metrics.inc("voice_turns")
    metrics.inc(f"intent_{resolved_intent}")
    metrics.record_latency("voice_turn_ms", latency_ms)
    _slog.info(
        "voice_turn_processed",
        request_id=rid,
        session_id=state.session_id,
        intent=resolved_intent,
        outcome=action_taken,
        latency_ms=latency_ms,
    )

    tts_audio_url = _persist_tts_artifact(state.session_id, response_text, tts_result)

    return VoiceTurnResponse(
        session_id=state.session_id,
        turn_number=state.turns,
        intent=state.current_intent or VoiceIntent.unknown,
        confidence=confidence,
        response_text=response_text,
        is_fallback=False,
        booking_draft=state.booking_draft,
        action_taken=action_taken,
        data=data,
        stt_meta=stt_meta,
        tts_meta=tts_meta,
        tts_audio_url=tts_audio_url,
        provider_errors=_collect_provider_errors(
            ("stt", stt_outcome), ("tts", tts_outcome),
        ),
    )


# ── Intent handlers ──────────────────────────────────────────
# Each returns (response_text, action_taken, data)

async def _handle_book(
    state: ConversationState, entities: dict, db: AsyncSession
) -> tuple[str, str | None, dict | None]:
    """Handle booking intent — collect fields, search slots, or create booking."""
    missing = state.missing_booking_fields()

    # If we have a service category but no exact service_id, try to resolve it
    if "service_id" in missing and entities.get("service_category"):
        resolved = await _resolve_service(
            db,
            entities["service_category"],
            entities.get("genre"),
            entities.get("longueur"),
        )
        if resolved:
            state.update_draft(service_id=resolved.id, service_label=resolved.label)
            missing = state.missing_booking_fields()

    # Still missing fields → ask for them
    if missing:
        return _prompt_for_missing(missing), "collecting_info", None

    # All required fields present → search availability
    draft = state.booking_draft

    # Resolve employee name → employee_id if a preference was expressed
    if draft.employee_name and not draft.employee_id:
        emp = await _resolve_employee(db, draft.employee_name)
        if emp is None:
            return (
                f"Je ne connais pas de coiffeur(se) nommé(e) '{draft.employee_name}'. "
                "Nos coiffeurs sont : Sophie, Karim, Léa, Hugo et Amira. "
                "Souhaitez-vous continuer sans préférence ?",
                "employee_not_found",
                None,
            )
        # Verify this employee can perform the requested service
        comp_result = await db.execute(
            select(EmployeeCompetency)
            .where(EmployeeCompetency.employee_id == emp.id)
            .where(EmployeeCompetency.service_id == draft.service_id)
        )
        if comp_result.scalars().first() is None:
            return (
                f"{emp.prenom} ne propose pas ce service. "
                "Souhaitez-vous qu'un autre coiffeur s'en charge, "
                "ou préférez-vous choisir un autre service ?",
                "employee_not_competent",
                None,
            )
        state.update_draft(employee_id=emp.id)

    try:
        target_date = date.fromisoformat(draft.date)
    except (ValueError, TypeError):
        return (
            "Je n'ai pas compris la date. Pouvez-vous la répéter au format jour/mois/année ?",
            "date_invalid",
            None,
        )

    avail = await find_available_slots(
        session=db,
        service_id=draft.service_id,
        target_date=target_date,
        preferred_employee_id=draft.employee_id,
        tenant_id=state.tenant_id or None,
    )

    if not avail["slots"]:
        alt_text = ""
        if avail["alternatives"]:
            alt_slots = avail["alternatives"][:3]
            alt_text = " Alternatives disponibles : " + ", ".join(
                f"{s['start']}" for s in alt_slots
            )
        return (
            f"Désolé, aucun créneau disponible le {draft.date} pour ce service.{alt_text}",
            "no_slots",
            {"alternatives": avail["alternatives"][:3]},
        )

    # Try to match requested time
    requested_time = draft.time
    matched_slot = None
    for slot in avail["slots"]:
        slot_time = slot["start"].split("T")[1][:5] if "T" in slot["start"] else slot["start"][:5]
        if slot_time == requested_time:
            matched_slot = slot
            break

    if not matched_slot:
        top_slots = avail["slots"][:3]
        slots_text = ", ".join(
            s["start"].split("T")[1][:5] if "T" in s["start"] else s["start"]
            for s in top_slots
        )
        return (
            f"Le créneau de {requested_time} n'est pas disponible. "
            f"Créneaux disponibles : {slots_text}. Lequel préférez-vous ?",
            "slots_offered",
            {"available_slots": top_slots},
        )

    # Slot matched — attempt to create booking
    employee_id = matched_slot["employee"]["id"]
    employee_name = f"{matched_slot['employee']['prenom']} {matched_slot['employee']['nom']}"
    start_dt = datetime.fromisoformat(matched_slot["start"])

    ok, message, end_time = await validate_booking_request(
        db, draft.service_id, employee_id, start_dt, tenant_id=state.tenant_id or None
    )
    if not ok:
        return f"Impossible de réserver : {message}", "validation_failed", None

    # Create the booking
    booking = Booking(
        tenant_id=state.tenant_id,
        client_name=draft.client_name or state.client_name or "Client vocal",
        client_phone=draft.client_phone or state.client_phone,
        service_id=draft.service_id,
        employee_id=employee_id,
        start_time=start_dt,
        end_time=end_time,
        status=BookingStatus.confirmed,
    )
    db.add(booking)
    await db.commit()
    await db.refresh(booking)

    state.update_draft(employee_id=employee_id, employee_name=employee_name)
    metrics.inc("bookings_created")

    return (
        f"Parfait ! Votre rendez-vous est confirmé : {draft.service_label or draft.service_id} "
        f"le {draft.date} à {draft.time} avec {employee_name}. "
        f"Numéro de réservation : #{booking.id}.",
        "booking_created",
        {"booking_id": booking.id, "employee": employee_name, "start": matched_slot["start"]},
    )


async def _handle_reschedule(
    state: ConversationState, entities: dict, db: AsyncSession
) -> tuple[str, str | None, dict | None]:
    """Handle reschedule intent — find existing booking and move it."""
    booking_id = entities.get("booking_id")
    if not booking_id:
        return (
            "Pour modifier votre rendez-vous, j'ai besoin de votre numéro de réservation. "
            "Quel est-il ?",
            "need_booking_id",
            None,
        )

    # Load booking
    booking_conditions = [Booking.id == booking_id]
    if state.tenant_id:
        booking_conditions.append(Booking.tenant_id == state.tenant_id)
    result = await db.execute(
        select(Booking)
        .options(selectinload(Booking.service), selectinload(Booking.employee))
        .where(*booking_conditions)
    )
    booking = result.scalars().first()
    if not booking:
        return f"Réservation #{booking_id} introuvable.", "booking_not_found", None
    if booking.status != BookingStatus.confirmed:
        return (
            f"Impossible de modifier la réservation #{booking_id} "
            f"(statut : "
            f"{booking.status.value if hasattr(booking.status, 'value') else booking.status}).",
            "booking_not_modifiable",
            None,
        )

    # Check if we have a new date/time
    new_date = entities.get("date") or state.booking_draft.date
    new_time = entities.get("time") or state.booking_draft.time
    if not new_date or not new_time:
        return (
            f"Votre rendez-vous #{booking_id} est actuellement le "
            f"{booking.start_time.strftime('%Y-%m-%d')} à {booking.start_time.strftime('%H:%M')}. "
            "Quelle nouvelle date et heure souhaitez-vous ?",
            "need_new_datetime",
            {"current_start": booking.start_time.isoformat()},
        )

    # Validate new time
    try:
        target_date = date.fromisoformat(new_date)
        h, m = new_time.split(":")
        new_start = datetime.combine(
            target_date, datetime.min.time().replace(hour=int(h), minute=int(m))
        )
    except (ValueError, TypeError):
        return (
            "Je n'ai pas compris la date ou l'heure. Pouvez-vous répéter ?",
            "datetime_invalid",
            None,
        )

    employee_id = booking.employee_id
    ok, message, end_time = await validate_booking_request(
        db, booking.service_id, employee_id, new_start, exclude_booking_id=booking_id
    )
    if not ok:
        return f"Ce créneau n'est pas disponible : {message}", "reschedule_conflict", None

    booking.start_time = new_start
    booking.end_time = end_time
    await db.commit()

    return (
        f"Rendez-vous #{booking_id} déplacé au {new_date} à {new_time}. C'est confirmé !",
        "booking_rescheduled",
        {"booking_id": booking_id, "new_start": new_start.isoformat()},
    )


async def _handle_cancel(
    state: ConversationState, entities: dict, db: AsyncSession
) -> tuple[str, str | None, dict | None]:
    """Handle cancel intent — find and cancel a booking."""
    booking_id = entities.get("booking_id")
    if not booking_id:
        return (
            "Pour annuler votre rendez-vous, j'ai besoin de votre numéro de réservation.",
            "need_booking_id",
            None,
        )

    cancel_conditions = [Booking.id == booking_id]
    if state.tenant_id:
        cancel_conditions.append(Booking.tenant_id == state.tenant_id)
    cancel_result = await db.execute(select(Booking).where(*cancel_conditions))
    booking = cancel_result.scalars().first()
    if not booking:
        return f"Réservation #{booking_id} introuvable.", "booking_not_found", None
    if booking.status == BookingStatus.cancelled:
        return f"La réservation #{booking_id} est déjà annulée.", "already_cancelled", None

    booking.status = BookingStatus.cancelled
    await db.commit()
    metrics.inc("bookings_cancelled")

    return (
        f"Votre réservation #{booking_id} a été annulée. "
        "Souhaitez-vous prendre un nouveau rendez-vous ?",
        "booking_cancelled",
        {"booking_id": booking_id},
    )


async def _handle_check_availability(
    state: ConversationState, entities: dict, db: AsyncSession
) -> tuple[str, str | None, dict | None]:
    """Handle availability check — search slots without booking."""
    service_category = entities.get("service_category")
    if not service_category:
        return (
            "Pour quel type de prestation souhaitez-vous vérifier les disponibilités ? "
            "Par exemple : coupe, couleur, balayage, brushing…",
            "need_service",
            None,
        )

    # Resolve service
    resolved = await _resolve_service(
        db, service_category, entities.get("genre"), entities.get("longueur")
    )
    if not resolved:
        return (
            f"Je n'ai pas trouvé de service correspondant à '{service_category}'. "
            "Pouvez-vous préciser ?",
            "service_not_found",
            None,
        )

    target_date_str = entities.get("date") or state.booking_draft.date
    if not target_date_str:
        return (
            f"J'ai trouvé le service : {resolved.label} ({resolved.prix_eur}€, "
            f"{resolved.duree_min} min). "
            "Pour quelle date souhaitez-vous vérifier les disponibilités ?",
            "need_date",
            {"service": {"id": resolved.id, "label": resolved.label}},
        )

    try:
        target_date = date.fromisoformat(target_date_str)
    except (ValueError, TypeError):
        return (
            "Format de date non reconnu. Merci d'utiliser le format AAAA-MM-JJ.",
            "date_invalid",
            None,
        )

    avail = await find_available_slots(
        session=db,
        service_id=resolved.id,
        target_date=target_date,
        tenant_id=state.tenant_id or None,
    )

    if not avail["slots"]:
        alt_info = ""
        if avail["alternatives"]:
            alt_info = " Voici des alternatives : " + ", ".join(
                s["start"] for s in avail["alternatives"][:3]
            )
        return (
            f"Aucun créneau disponible le {target_date_str} pour {resolved.label}.{alt_info}",
            "no_slots",
            {"alternatives": avail["alternatives"][:3]},
        )

    top_slots = avail["slots"][:5]
    slots_text = ", ".join(
        s["start"].split("T")[1][:5] if "T" in s["start"] else s["start"]
        for s in top_slots
    )
    return (
        f"{len(avail['slots'])} créneau(x) disponible(s) le {target_date_str} "
        f"pour {resolved.label}. "
        f"Premiers créneaux : {slots_text}. Souhaitez-vous réserver ?",
        "slots_found",
        {
            "service_id": resolved.id,
            "slots_count": len(avail["slots"]),
            "top_slots": top_slots,
        },
    )


async def _handle_get_info(
    state: ConversationState, entities: dict, db: AsyncSession
) -> tuple[str, str | None, dict | None]:
    """Handle get_info intent — answer questions about the salon."""
    from app.salon_info import get_info_response

    info_topic = entities.get("info_topic")
    # Pass the raw user text via state if topic wasn't resolved by entity extractor
    response = get_info_response(info_topic)
    return response, "info_provided", {"info_topic": info_topic}


async def _handle_unknown(
    state: ConversationState, entities: dict, db: AsyncSession
) -> tuple[str, str | None, dict | None]:
    """Fallback handler for unrecognised intents."""
    return (
        "Je n'ai pas bien compris. Je peux vous aider à :\n"
        "• Prendre un rendez-vous\n"
        "• Modifier ou annuler un rendez-vous\n"
        "• Vérifier les disponibilités\n"
        "• Répondre à vos questions sur le salon (adresse, horaires, tarifs…)\n"
        "Que souhaitez-vous faire ?",
        None,
        None,
    )


# ── Phase 4.2→4.3: Session transcript / state review ─────────


@router.get("/sessions/{session_id}/transcript")
async def get_session_transcript(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> dict:
    """
    Fetch the current state of a voice session for demo review.

    Returns session metadata, booking draft, lifecycle info, and the full
    transcript event log.  Since Phase 4.3 the transcript is persisted in
    the database, so it survives process restarts.
    """
    # Try DB first, fall back to in-memory
    state = await db_load_session(db, session_id, tenant_id=tenant.id)
    if state is None:
        state = conversation_manager.get_session(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' introuvable.")

    # Load transcript events from DB
    events = await get_transcript_events(db, session_id)

    return {
        "session_id": state.session_id,
        "status": state.status.value,
        "current_intent": state.current_intent.value if state.current_intent else None,
        "turns": state.turns,
        "booking_draft": state.booking_draft.model_dump(),
        "client_name": state.client_name,
        "client_phone": state.client_phone,
        "channel": state.channel,
        "created_at": state.created_at.isoformat(),
        "last_activity": state.last_activity.isoformat(),
        "duration_seconds": state.duration_seconds,
        "transcript": events,
    }


# ── Handler dispatch table ───────────────────────────────────

_INTENT_HANDLERS = {
    VoiceIntent.book: _handle_book,
    VoiceIntent.reschedule: _handle_reschedule,
    VoiceIntent.cancel: _handle_cancel,
    VoiceIntent.check_availability: _handle_check_availability,
    VoiceIntent.get_info: _handle_get_info,
    VoiceIntent.unknown: _handle_unknown,
}


# ── Helpers ──────────────────────────────────────────────────

def _merge_entities_to_draft(state: ConversationState, entities: dict) -> None:
    """Merge extracted entities into the session's booking draft."""
    mapping = {
        "date": "date",
        "time": "time",
        "employee_name": "employee_name",
        "service_keyword": None,  # handled separately
        "service_category": None,
    }
    for entity_key, draft_key in mapping.items():
        if draft_key and entity_key in entities:
            state.update_draft(**{draft_key: entities[entity_key]})


async def _resolve_employee(db: AsyncSession, name: str) -> Employee | None:
    """Find an employee by first name (case-insensitive)."""
    result = await db.execute(
        select(Employee).where(func.lower(Employee.prenom) == name.lower())
    )
    return result.scalars().first()


async def _resolve_service(
    db: AsyncSession,
    category: str,
    genre: str | None = None,
    longueur: str | None = None,
) -> Service | None:
    """Find the best-matching service from a category keyword."""
    query = select(Service).where(Service.category_id == category)
    if genre:
        query = query.where(Service.genre.in_([genre, "mixte"]))
    if longueur:
        query = query.where(Service.longueur.in_([longueur, "tout"]))
    # Prefer shorter/cheaper as default
    query = query.order_by(Service.duree_min.asc()).limit(1)
    result = await db.execute(query)
    return result.scalars().first()


def _prompt_for_missing(missing: list[str]) -> str:
    """Generate a natural prompt asking for missing booking fields."""
    prompts = {
        "service_id": "Quelle prestation souhaitez-vous ? (coupe, couleur, balayage, brushing…)",
        "date": "Pour quelle date souhaitez-vous votre rendez-vous ?",
        "time": "À quelle heure souhaitez-vous votre rendez-vous ?",
    }
    # Ask for the first missing field
    field = missing[0]
    return prompts.get(field, f"J'ai besoin de l'information suivante : {field}")
