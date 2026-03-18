"""
Twilio webhook endpoints (Phase 5 — real telephony integration).

Handles real Twilio Voice and SMS webhooks.  Returns TwiML XML.

Endpoints:
    POST /twilio/voice    — Incoming call (initial TwiML greeting + Gather)
    POST /twilio/gather   — Speech result from <Gather> → voice pipeline → TwiML
    POST /twilio/status   — Call status callback (completed, failed, etc.)
    POST /twilio/sms      — Inbound SMS → voice pipeline → TwiML <Message>

Key design decisions:
    - Twilio sends application/x-www-form-urlencoded (not JSON).
    - Twilio expects application/xml (TwiML) in response.
    - Signature is in X-Twilio-Signature header (HMAC-SHA1 of URL + sorted params).
    - CallSid is used as session_id (persists across all turns of a call).
    - SMS sessions use MessageSid as session_id.
    - Voice pipeline reuses existing intent/handlers from app.routers.voice.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio_store import synthesize_to_file
from app.config import settings
from app.conversation import conversation_manager
from app.database import get_db
from app.intent import extract_intent_async
from app.observability import StructuredLogger, metrics
from app.session_store import (
    append_transcript_event,
    create_session as db_create_session,
    load_session as db_load_session,
    save_session as db_save_session,
)
from app.telephony_adapter import TwilioAdapter
from app.twiml import TwiML
from app.voice_schemas import SessionStatus, VoiceIntent

logger = logging.getLogger(__name__)
_slog = StructuredLogger(__name__)

router = APIRouter(prefix="/twilio", tags=["twilio"])

# Pre-cached greeting filename (set at startup by warm_greeting_cache)
_cached_greeting_filename: str | None = None

# ── Constants ────────────────────────────────────────────────

_GREETING = (
    "Bonjour et bienvenue chez Maison Éclat ! "
    "Je peux vous aider à prendre rendez-vous, modifier ou annuler une réservation. "
    "Comment puis-je vous aider ?"
)

_GOODBYE = "Merci d'avoir appelé Maison Éclat. À bientôt !"

_SILENCE_PROMPT = "Je n'ai pas entendu votre réponse. Pouvez-vous répéter ?"

_VOICE = "alice"
_LANG = "fr-FR"


# ── Helpers ──────────────────────────────────────────────────


async def warm_greeting_cache(
    audio_dir: Path,
    api_key: str,
    voice_id: str = "",
    model: str = "",
) -> None:
    """
    Pre-generate the greeting MP3 at startup and cache the filename.

    Saves to audio_dir/greeting.mp3 (fixed name, excluded from TTL cleanup).
    Falls back gracefully if ElevenLabs is not configured or unavailable.
    """
    global _cached_greeting_filename

    if not api_key:
        return

    fname = await synthesize_to_file(
        text=_GREETING,
        audio_dir=audio_dir,
        api_key=api_key,
        session_id="greeting",
        turn=0,
        voice_id=voice_id,
        model=model,
        filename="greeting.mp3",
    )
    if fname:
        _cached_greeting_filename = fname
        logger.info("Greeting pre-cached: %s", fname)
    else:
        logger.warning("Greeting pre-cache failed — will generate on first call")


def _get_adapter() -> TwilioAdapter:
    """Return a TwilioAdapter configured from settings."""
    return TwilioAdapter(
        webhook_secret=settings.TWILIO_AUTH_TOKEN,
        webhook_url="",  # URL is derived per-request for ngrok compatibility
    )


def _base_url(request: Request) -> str:
    """
    Derive the public base URL from the incoming request.

    Works correctly with ngrok (which sets the correct Host header and
    forwards X-Forwarded-Proto).  Returns e.g. https://xxxx.ngrok-free.app
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


def _gather_action(request: Request) -> str:
    return f"{_base_url(request)}/api/v1/twilio/gather"


def _voice_url(request: Request) -> str:
    return f"{_base_url(request)}/api/v1/twilio/voice"


async def _tts(
    text: str,
    request: Request,
    session_id: str,
    turn: int,
) -> str | None:
    """
    Generate TTS audio via ElevenLabs and return its public URL.

    Returns None if ElevenLabs is not configured or generation fails.
    Caller should fall back to Twilio <Say> in that case.
    """
    if not settings.ELEVENLABS_API_KEY:
        return None

    audio_dir = Path(settings.AUDIO_DIR)
    filename = await synthesize_to_file(
        text=text,
        audio_dir=audio_dir,
        api_key=settings.ELEVENLABS_API_KEY,
        session_id=session_id,
        turn=turn,
        voice_id=settings.ELEVENLABS_VOICE_ID,
        model=settings.ELEVENLABS_MODEL,
    )
    if filename is None:
        return None

    return f"{_base_url(request)}/audio/{filename}"


async def _verify_signature(request: Request, params: dict[str, str]) -> None:
    """
    Verify Twilio webhook signature.  Skip if TWILIO_AUTH_TOKEN is not set.

    Raises HTTP 401 if signature is invalid.
    """
    if not settings.TWILIO_AUTH_TOKEN:
        return  # Dev mode — no verification

    signature = request.headers.get("X-Twilio-Signature", "")

    # Reconstruct the URL with the correct scheme (cloudflare/ngrok set
    # X-Forwarded-Proto: https but request.url.scheme may still be http).
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    url = f"{proto}://{host}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"

    adapter = _get_adapter()

    if not adapter.validate_signature(
        raw_body=b"",  # not used in real Twilio mode
        signature=signature,
        url=url,
        params=params,
    ):
        _slog.warning("twilio_signature_invalid", url=url)
        raise HTTPException(status_code=401, detail="Invalid Twilio signature.")


def _is_call_end(params: dict) -> bool:
    """Return True if this is a terminal call status (completed/failed/etc.)."""
    status = params.get("CallStatus", "").lower()
    return status in ("completed", "canceled", "busy", "no-answer", "failed")


# ── Voice endpoints ──────────────────────────────────────────


@router.post("/voice")
async def twilio_voice(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Handles initial Twilio call webhook.

    Twilio calls this URL when a call is received.  We create a voice session
    and return TwiML that greets the caller and opens a <Gather> to collect
    their first utterance.
    """
    params = dict(await request.form())
    await _verify_signature(request, params)

    call_sid = params.get("CallSid", "")
    caller_number = params.get("From", "")
    caller_name = params.get("CallerName", "")

    if not call_sid:
        raise HTTPException(status_code=400, detail="Missing CallSid.")

    _slog.info(
        "twilio_call_started",
        call_sid=call_sid[:12],
        caller=caller_number,
    )

    # Create session using CallSid as session_id (idempotent: check first)
    state = await db_load_session(db, call_sid)
    if state is None:
        state = await db_create_session(
            db,
            client_name=caller_name or None,
            client_phone=caller_number or None,
            channel="twilio",
            session_id=call_sid,
        )
        conversation_manager._sessions[call_sid] = state
        await db.commit()

    metrics.inc("telephony_calls_started")
    metrics.inc("sessions_started")

    # Use pre-cached greeting if available, else generate on-the-fly
    if _cached_greeting_filename:
        audio_url: str | None = f"{_base_url(request)}/audio/{_cached_greeting_filename}"
    else:
        audio_url = await _tts(_GREETING, request, call_sid, 0)

    twiml = TwiML()
    gather = twiml.gather(
        action=_gather_action(request),
        input="speech",
        language=_LANG,
        timeout="5",
        speech_timeout="auto",
    )
    if audio_url:
        gather.play(audio_url)
    else:
        gather.say(_GREETING, voice=_VOICE, language=_LANG)
    twiml.redirect(_voice_url(request))

    return twiml.response()


@router.post("/gather")
async def twilio_gather(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Handles Twilio <Gather> result.

    Called after the caller speaks (SpeechResult) or when gather times out
    (no SpeechResult → prompt to repeat).  Runs the voice pipeline and
    returns a new TwiML response.
    """
    params = dict(await request.form())
    await _verify_signature(request, params)

    call_sid = params.get("CallSid", "")
    speech_result = params.get("SpeechResult", "").strip()
    digits = params.get("Digits", "").strip()

    if not call_sid:
        raise HTTPException(status_code=400, detail="Missing CallSid.")

    # Resolve session
    state = await db_load_session(db, call_sid)
    if state is None:
        state = conversation_manager.get_session(call_sid)
    if state is None:
        # Session lost (restart) — redirect to /voice to re-create
        _slog.warning("twilio_gather_session_lost", call_sid=call_sid[:12])
        return TwiML().redirect(_voice_url(request)).response()

    if state.status != SessionStatus.active:
        return TwiML().say(_GOODBYE, voice=_VOICE, language=_LANG).hangup().response()

    # No speech received → prompt to repeat
    if not speech_result and not digits:
        twiml = TwiML()
        twiml.gather(
            action=_gather_action(request),
            input="speech",
            language=_LANG,
            timeout="5",
            speech_timeout="auto",
        ).say(_SILENCE_PROMPT, voice=_VOICE, language=_LANG)
        twiml.redirect(_voice_url(request))
        return twiml.response()

    # Build user text
    if digits:
        user_text = f"[DTMF: {digits}]"
    else:
        user_text = speech_result

    _slog.info(
        "twilio_gather_utterance",
        call_sid=call_sid[:12],
        text_preview=user_text[:60],
    )

    state.increment_turn()

    # ── Conversation engine ───────────────────────────────────
    from app.llm_conversation import is_available as llm_is_available
    from app.llm_conversation import llm_turn as llm_turn_fn

    is_fallback = False
    action_taken = "none"
    data = None
    intent_str = VoiceIntent.unknown.value
    confidence = 0.0

    _llm_ok = False
    if llm_is_available():
        try:
            response_text, new_messages, action_taken = await llm_turn_fn(
                state.messages, user_text, db
            )
            state.messages = new_messages
            action_taken = action_taken or "llm_response"
            # Detect natural farewell → hang up gracefully
            _FAREWELL = ("au revoir", "bonne journée", "bonsoir", "à bientôt", "merci, au revoir")
            if any(kw in response_text.lower() for kw in _FAREWELL):
                action_taken = "session_ended"
            intent_str = "llm_driven"
            confidence = 1.0
            _llm_ok = True
        except Exception as exc:
            logger.error("llm_turn failed, falling back to legacy pipeline: %s", exc)

    if not _llm_ok:
        # Legacy intent → handler pipeline (fallback when LLM not configured or fails)
        from app.routers.voice import (
            FALLBACK_CONFIDENCE_THRESHOLD,
            MAX_CONSECUTIVE_FALLBACKS,
            _FALLBACK_RESPONSES,
            _HUMAN_TRANSFER_MSG,
            _INTENT_HANDLERS,
            _merge_entities_to_draft,
        )

        intent_result = await extract_intent_async(user_text)
        intent = intent_result.intent
        confidence = intent_result.confidence
        entities = intent_result.entities
        intent_str = intent.value

        has_active_intent = (
            state.current_intent is not None
            and state.current_intent != VoiceIntent.unknown
        )

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

            handler = _INTENT_HANDLERS.get(state.current_intent or VoiceIntent.unknown)
            if handler is None:
                from app.routers.voice import _handle_unknown
                handler = _handle_unknown
            response_text, action_taken, data = await handler(state, entities, db)

    # Persist
    conversation_manager._sessions[call_sid] = state
    await db_save_session(db, state)
    await append_transcript_event(
        db,
        session_id=call_sid,
        turn_number=state.turns,
        user_text=user_text,
        intent=intent_str,
        confidence=confidence,
        response_text=response_text,
        action_taken=action_taken,
        is_fallback=is_fallback,
        data=data,
    )
    await db.commit()

    metrics.inc("voice_turns")
    metrics.inc("telephony_utterances_processed")

    # Try ElevenLabs TTS, fall back to Twilio <Say>
    audio_url = await _tts(response_text, request, call_sid, state.turns)

    # Build TwiML response
    should_end = action_taken in (
        "human_transfer_offered",
        "session_ended",
        "booking_confirmed",
    )

    twiml = TwiML()

    if action_taken == "human_transfer_offered" and settings.TWILIO_TRANSFER_NUMBER:
        if audio_url:
            twiml.play(audio_url)
        else:
            twiml.say(response_text, voice=_VOICE, language=_LANG)
        twiml.dial(settings.TWILIO_TRANSFER_NUMBER)
    elif should_end:
        if audio_url:
            twiml.play(audio_url)
        else:
            twiml.say(response_text, voice=_VOICE, language=_LANG)
        twiml.hangup()
    else:
        gather = twiml.gather(
            action=_gather_action(request),
            input="speech",
            language=_LANG,
            timeout="5",
            speech_timeout="auto",
        )
        if audio_url:
            gather.play(audio_url)
        else:
            gather.say(response_text, voice=_VOICE, language=_LANG)
        twiml.redirect(_voice_url(request))

    return twiml.response()


@router.post("/status")
async def twilio_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Handles Twilio call status callbacks.

    Twilio posts here when the call status changes (in-progress, completed,
    failed, etc.).  We close the session on terminal statuses.
    """
    params = dict(await request.form())
    await _verify_signature(request, params)

    call_sid = params.get("CallSid", "")
    call_status = params.get("CallStatus", "").lower()
    call_duration = params.get("CallDuration", "0")

    _slog.info(
        "twilio_status_callback",
        call_sid=call_sid[:12] if call_sid else "",
        status=call_status,
        duration_s=call_duration,
    )

    if call_sid and _is_call_end(params):
        state = await db_load_session(db, call_sid)
        if state is None:
            state = conversation_manager.get_session(call_sid)
        if state is not None:
            state.status = SessionStatus.completed
            state.touch()
            conversation_manager.end_session(call_sid)
            await db_save_session(db, state)
            await db.commit()

        metrics.inc("telephony_calls_ended")
        metrics.inc("sessions_completed")

    return {"status": "ok", "call_sid": call_sid, "call_status": call_status}


# ── SMS endpoint ─────────────────────────────────────────────


@router.post("/sms")
async def twilio_sms(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Handles inbound SMS from Twilio.

    Runs the user's message through the voice pipeline and responds with
    a TwiML <Message> reply.  Each SMS conversation is its own session
    (keyed by MessageSid), independent of voice sessions.
    """
    from app.routers.voice import (
        FALLBACK_CONFIDENCE_THRESHOLD,
        MAX_CONSECUTIVE_FALLBACKS,
        _FALLBACK_RESPONSES,
        _HUMAN_TRANSFER_MSG,
        _INTENT_HANDLERS,
        _merge_entities_to_draft,
    )

    params = dict(await request.form())
    await _verify_signature(request, params)

    message_sid = params.get("MessageSid", "")
    from_number = params.get("From", "")
    body = params.get("Body", "").strip()

    if not body:
        return TwiML().message("Désolé, je n'ai pas reçu votre message.").response()

    _slog.info(
        "twilio_sms_received",
        message_sid=message_sid[:12] if message_sid else "",
        from_number=from_number,
        text_preview=body[:60],
    )

    # SMS: create a fresh session per message (stateless SMS)
    state = await db_create_session(
        db,
        client_phone=from_number or None,
        channel="sms",
    )
    conversation_manager._sessions[state.session_id] = state

    state.increment_turn()

    # Intent extraction
    intent_result = await extract_intent_async(body)
    intent = intent_result.intent
    confidence = intent_result.confidence
    entities = intent_result.entities

    is_fallback = False
    action_taken = "none"
    data = None

    if confidence < FALLBACK_CONFIDENCE_THRESHOLD or intent == VoiceIntent.unknown:
        is_fallback = True
        response_text = _FALLBACK_RESPONSES[0]
        action_taken = "fallback"
        metrics.inc("voice_fallbacks")
    else:
        state.current_intent = intent
        _merge_entities_to_draft(state, entities)

        handler = _INTENT_HANDLERS.get(state.current_intent or VoiceIntent.unknown)
        if handler is None:
            from app.routers.voice import _handle_unknown
            handler = _handle_unknown
        response_text, action_taken, data = await handler(state, entities, db)

    # Persist session + transcript
    await db_save_session(db, state)
    await append_transcript_event(
        db,
        session_id=state.session_id,
        turn_number=state.turns,
        user_text=body,
        intent=(state.current_intent or VoiceIntent.unknown).value,
        confidence=confidence,
        response_text=response_text,
        action_taken=action_taken,
        is_fallback=is_fallback,
        data=data,
    )
    await db.commit()

    metrics.inc("voice_turns")

    return TwiML().message(response_text).response()
