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

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio_store import synthesize_to_file
from app.auth import get_tenant_from_slug
from app.config import settings
from app.conversation import conversation_manager
from app.database import async_session as db_factory
from app.database import get_db
from app.intent import extract_intent_async
from app.models import CallbackRequest, CallbackRequestStatus, Tenant
from app.observability import StructuredLogger, metrics
from app.settings_service import get_tenant_settings
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

_VOICEMAIL_GOODBYE = (
    "Votre message a bien été enregistré. "
    "Le salon vous rappellera dans les meilleurs délais. "
    "À bientôt !"
)

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
        text=settings.GREETING_TEXT,
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


def invalidate_greeting_cache() -> None:
    """Reset the pre-cached greeting filename so the next call regenerates it."""
    global _cached_greeting_filename
    _cached_greeting_filename = None


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


def _consent_url(request: Request) -> str:
    return f"{_base_url(request)}/api/v1/twilio/consent"


async def _greeting_audio_url(
    request: Request,
    tenant_settings: object,
    call_sid: str,
) -> str | None:
    """Return the greeting audio URL (pre-cached or freshly synthesised)."""
    if _cached_greeting_filename:
        return f"{_base_url(request)}/audio/{_cached_greeting_filename}"
    return await _tts(tenant_settings.GREETING_TEXT, request, call_sid, 0)  # type: ignore[attr-defined]


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


async def _transcribe_recording(recording_url: str) -> str | None:
    """
    Download a Twilio recording and transcribe it with OpenAI Whisper.

    Returns transcription text, or None on any failure.
    Requires OPENAI_API_KEY and Twilio credentials.
    """
    if not settings.OPENAI_API_KEY:
        return None

    try:
        # Download audio (Twilio recordings may require auth)
        auth = (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        async with httpx.AsyncClient(timeout=20.0) as client:
            dl = await client.get(recording_url, auth=auth if auth[0] else None)
        if dl.status_code != 200:
            logger.warning("Recording download HTTP %d for %s", dl.status_code, recording_url)
            return None

        audio_bytes = dl.content
        if not audio_bytes:
            return None

        # Transcribe with Whisper
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                files={"file": ("recording.mp3", audio_bytes, "audio/mpeg")},
                data={"model": "whisper-1", "language": "fr"},
            )
        if resp.status_code != 200:
            logger.warning("Whisper transcription HTTP %d", resp.status_code)
            return None
        return resp.json().get("text")
    except Exception as exc:
        logger.warning("_transcribe_recording error: %s", exc)
        return None


async def _process_recording_background(
    callback_id: int,
    recording_url: str | None,
    caller_phone: str | None,
) -> None:
    """
    Background task: transcribe recording with Whisper, update DB, send email.

    Runs after the HTTP response is already sent to Twilio so the caller
    is not kept waiting.
    """
    transcription = None
    if recording_url:
        transcription = await _transcribe_recording(recording_url)

    # Update DB with transcription
    async with db_factory() as db:
        cb = await db.get(CallbackRequest, callback_id)
        if cb:
            cb.transcription = transcription
            await db.commit()

    # Send email notification
    from app.email_sender import send_callback_notification
    await send_callback_notification(
        caller_phone=caller_phone,
        recording_url=recording_url,
        transcription=transcription,
        callback_id=callback_id,
        created_at=datetime.now(),
    )


# ── Voice endpoints ──────────────────────────────────────────


@router.post("/voice")
async def twilio_voice(
    request: Request,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
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

    # Idempotent session check (Twilio may replay the webhook)
    state = await db_load_session(db, call_sid, tenant_id=tenant.id)
    is_new_session = state is None

    tenant_settings = get_tenant_settings(tenant.id)

    # New call + RGPD consent enabled → play consent message before creating session
    if is_new_session and tenant_settings.CONSENT_ENABLED:
        metrics.inc("telephony_calls_started")
        audio_url: str | None = await _tts(tenant_settings.CONSENT_TEXT, request, call_sid, 0)
        twiml = TwiML()
        gather = twiml.gather(
            action=_consent_url(request),
            input="dtmf",
            timeout="8",
            num_digits="1",
        )
        if audio_url:
            gather.play(audio_url)
        else:
            gather.say(tenant_settings.CONSENT_TEXT, voice=_VOICE, language=_LANG)
        # Redirect handles timeout (no DTMF = implied consent)
        twiml.redirect(_consent_url(request))
        return twiml.response()

    # New call, consent disabled → create session immediately
    if is_new_session:
        state = await db_create_session(
            db,
            tenant.id,
            client_name=caller_name or None,
            client_phone=caller_number or None,
            channel="twilio",
            session_id=call_sid,
        )
        conversation_manager._sessions[call_sid] = state
        await db.commit()
        metrics.inc("telephony_calls_started")
        metrics.inc("sessions_started")

    # First call (no consent) or silence timeout re-entry
    if is_new_session:
        audio_url = await _greeting_audio_url(request, tenant_settings, call_sid)
        prompt = tenant_settings.GREETING_TEXT
    else:
        audio_url = await _tts(_SILENCE_PROMPT, request, call_sid, state.turns)
        prompt = _SILENCE_PROMPT

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
        gather.say(prompt, voice=_VOICE, language=_LANG)
    twiml.redirect(_voice_url(request))

    return twiml.response()


@router.post("/consent")
async def twilio_consent(
    request: Request,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> Response:
    """
    Handles RGPD consent DTMF result.

    Called after <Gather input="dtmf"> from /twilio/voice:
    - Digits == "1" → caller refuses recording → hangup cleanly (no session created)
    - Digits empty (timeout = redirect fallthrough) → implied consent → create session + greeting
    """
    params = dict(await request.form())
    await _verify_signature(request, params)

    call_sid = params.get("CallSid", "")
    caller_number = params.get("From", "")
    caller_name = params.get("CallerName", "")
    digits = params.get("Digits", "").strip()

    if not call_sid:
        raise HTTPException(status_code=400, detail="Missing CallSid.")

    tenant_settings = get_tenant_settings(tenant.id)

    if digits == "1":
        # Caller refuses recording — hang up politely without creating a session
        _slog.warning("consent_refused", call_sid=call_sid[:12], caller=caller_number)
        metrics.inc("consent_refused")
        refusal_text = tenant_settings.CONSENT_REFUSAL_TEXT
        audio_url: str | None = await _tts(refusal_text, request, call_sid, 0)
        twiml = TwiML()
        if audio_url:
            twiml.play(audio_url)
        else:
            twiml.say(refusal_text, voice=_VOICE, language=_LANG)
        twiml.hangup()
        return twiml.response()

    if digits:
        # Unexpected DTMF key — re-play the consent message
        _slog.info("consent_unexpected_digit", call_sid=call_sid[:12], digit=digits)
        return TwiML().redirect(_voice_url(request)).response()

    # No DTMF (timeout) = implied consent — create session and play greeting
    _slog.info("consent_accepted", call_sid=call_sid[:12], caller=caller_number)
    metrics.inc("consent_accepted")

    state = await db_load_session(db, call_sid, tenant_id=tenant.id)
    if state is None:
        state = await db_create_session(
            db,
            tenant.id,
            client_name=caller_name or None,
            client_phone=caller_number or None,
            channel="twilio",
            session_id=call_sid,
        )
        state.consent_given = True
        state.consent_at = datetime.now(timezone.utc)
        conversation_manager._sessions[call_sid] = state
        await db_save_session(db, state)
        await db.commit()
        metrics.inc("sessions_started")

    # Play greeting
    audio_url = await _greeting_audio_url(request, tenant_settings, call_sid)

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
        gather.say(tenant_settings.GREETING_TEXT, voice=_VOICE, language=_LANG)
    twiml.redirect(_voice_url(request))

    return twiml.response()


@router.post("/gather")
async def twilio_gather(
    request: Request,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
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
    state = await db_load_session(db, call_sid, tenant_id=tenant.id)
    if state is None:
        state = conversation_manager.get_session(call_sid)
    if state is None:
        # Session lost (restart) — redirect to /voice to re-create
        _slog.warning("twilio_gather_session_lost", call_sid=call_sid[:12])
        return TwiML().redirect(_voice_url(request)).response()

    if state.status != SessionStatus.active:
        tenant_settings = get_tenant_settings(tenant.id)
        return TwiML().say(tenant_settings.GOODBYE_TEXT, voice=_VOICE, language=_LANG).hangup().response()

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
                state.messages, user_text, db,
                client_phone=state.client_phone,
                client_name=state.client_name,
                tenant_id=state.tenant_id,
            )
            state.messages = new_messages
            action_taken = action_taken or "llm_response"
            # Detect natural farewell (in bot response OR user input) → hang up gracefully
            _FAREWELL = (
                "au revoir", "bonne journée", "bonsoir", "bonne soirée",
                "à bientôt", "merci, au revoir", "c'est tout", "raccrocher",
            )
            if any(kw in response_text.lower() for kw in _FAREWELL) \
                    or any(kw in user_text.lower() for kw in _FAREWELL):
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
            _human_transfer_msg,
            _INTENT_HANDLERS,
            _merge_entities_to_draft,
        )

        # Fast-path: detect explicit goodbye before intent extraction
        _GOODBYE_FALLBACK = (
            "au revoir", "bonne journée", "bonsoir", "bonne soirée",
            "à bientôt", "c'est tout", "raccrocher", "merci beaucoup",
        )
        _is_goodbye = any(kw in user_text.lower() for kw in _GOODBYE_FALLBACK)
        if _is_goodbye:
            response_text = "Au revoir et à bientôt !"
            action_taken = "session_ended"
            intent_str = "goodbye"
            confidence = 1.0
        else:
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
                    response_text = _human_transfer_msg()
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
        "request_voicemail",
    )

    twiml = TwiML()

    if action_taken == "request_voicemail":
        # Play the bot's response ("Laissez un message après le bip...") then record
        recording_action = f"{_base_url(request)}/api/v1/twilio/recording"
        if audio_url:
            twiml.play(audio_url)
        else:
            twiml.say(response_text, voice=_VOICE, language=_LANG)
        twiml.record(action=recording_action, max_length=120, timeout=10)
    elif action_taken == "human_transfer_offered" and settings.TWILIO_TRANSFER_NUMBER:
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
    tenant: Tenant = Depends(get_tenant_from_slug),
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
        state = await db_load_session(db, call_sid, tenant_id=tenant.id)
        if state is None:
            state = conversation_manager.get_session(call_sid)
        if state is not None:
            state.status = SessionStatus.completed
            state.touch()
            conversation_manager.end_session(call_sid)
            await db_save_session(db, state)
            await db.commit()
            metrics.inc("sessions_completed")

        metrics.inc("telephony_calls_ended")

    return {"status": "ok", "call_sid": call_sid, "call_status": call_status}


# ── Recording endpoint ───────────────────────────────────────


@router.post("/recording")
async def twilio_recording(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> Response:
    """
    Handles Twilio <Record> callback.

    Called by Twilio when a voicemail recording is complete.  Saves a
    CallbackRequest to DB immediately, then kicks off a background task
    to transcribe the audio and send an email notification to the salon.
    Returns a TwiML goodbye + <Hangup>.
    """
    params = dict(await request.form())

    call_sid = params.get("CallSid", "")
    recording_url = params.get("RecordingUrl", "")
    recording_duration = params.get("RecordingDuration", "0")
    recording_status = params.get("RecordingStatus", "completed")

    _slog.info(
        "twilio_recording_received",
        call_sid=call_sid[:12] if call_sid else "",
        duration_s=recording_duration,
        status=recording_status,
    )

    if recording_status != "completed":
        return TwiML().hangup().response()

    # Ensure .mp3 extension for playback
    if recording_url and not recording_url.endswith(".mp3"):
        recording_url += ".mp3"

    # Retrieve caller phone from the active session
    caller_phone: str | None = None
    if call_sid:
        state = await db_load_session(db, call_sid, tenant_id=tenant.id)
        if state is None:
            state = conversation_manager.get_session(call_sid)
        if state:
            caller_phone = state.client_phone

    # Persist callback request immediately (transcription filled in background)
    callback = CallbackRequest(
        tenant_id=tenant.id,
        caller_phone=caller_phone,
        recording_url=recording_url or None,
        recording_duration=int(recording_duration) if recording_duration else None,
        transcription=None,
        status=CallbackRequestStatus.pending,
    )
    db.add(callback)
    await db.commit()
    await db.refresh(callback)

    metrics.inc("voicemail_recorded")

    # Background: transcribe + send email (non-blocking for caller)
    background_tasks.add_task(
        _process_recording_background,
        callback.id,
        recording_url or None,
        caller_phone,
    )

    # Return goodbye immediately — caller hears it while background task runs
    twiml = TwiML()
    goodbye_audio = await _tts(_VOICEMAIL_GOODBYE, request, call_sid or "voicemail", 999)
    if goodbye_audio:
        twiml.play(goodbye_audio)
    else:
        twiml.say(_VOICEMAIL_GOODBYE, voice=_VOICE, language=_LANG)
    twiml.hangup()
    return twiml.response()


# ── SMS endpoint ─────────────────────────────────────────────


@router.post("/sms")
async def twilio_sms(
    request: Request,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> Response:
    """
    Handles inbound SMS from Twilio.

    Runs the user's message through the voice pipeline and responds with
    a TwiML <Message> reply.  Each SMS conversation is its own session
    (keyed by MessageSid), independent of voice sessions.
    """
    from app.routers.voice import (
        FALLBACK_CONFIDENCE_THRESHOLD,
        _FALLBACK_RESPONSES,
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
        tenant.id,
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
