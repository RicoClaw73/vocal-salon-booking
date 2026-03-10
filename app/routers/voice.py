"""
Voice pipeline webhook-style endpoints.

POST /voice/sessions/start    – open a new voice conversation
POST /voice/sessions/message  – process a transcribed user utterance
POST /voice/sessions/end      – close a voice session

These endpoints form the integration layer between a local STT/TTS pipeline
and the existing salon booking API.  No external services required.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.conversation import ConversationState, conversation_manager
from app.database import get_db
from app.intent import extract_intent
from app.models import Booking, BookingStatus, Service
from app.slot_engine import find_available_slots, validate_booking_request
from app.voice_schemas import (
    BookingDraft,
    SessionEndRequest,
    SessionEndResponse,
    SessionStartRequest,
    SessionStartResponse,
    SessionStatus,
    UserMessageRequest,
    UserMessageResponse,
    VoiceIntent,
)

router = APIRouter(prefix="/voice", tags=["voice"])

# ── Greeting templates ───────────────────────────────────────

_GREETING = (
    "Bonjour et bienvenue chez Maison Éclat ! "
    "Je peux vous aider à prendre rendez-vous, modifier ou annuler une réservation. "
    "Comment puis-je vous aider ?"
)

_GOODBYE = "Merci d'avoir appelé Maison Éclat. À bientôt !"


# ── Endpoints ────────────────────────────────────────────────

@router.post("/sessions/start", response_model=SessionStartResponse, status_code=201)
async def start_session(payload: SessionStartRequest) -> SessionStartResponse:
    """Open a new voice conversation session."""
    state = conversation_manager.create_session(
        client_name=payload.client_name,
        client_phone=payload.client_phone,
        channel=payload.channel,
    )
    return SessionStartResponse(
        session_id=state.session_id,
        status=state.status,
        greeting=_GREETING,
        created_at=state.created_at,
    )


@router.post("/sessions/message", response_model=UserMessageResponse)
async def process_message(
    payload: UserMessageRequest,
    db: AsyncSession = Depends(get_db),
) -> UserMessageResponse:
    """Process a transcribed user utterance through intent detection and fulfillment."""
    state = conversation_manager.get_session(payload.session_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Session '{payload.session_id}' introuvable.")
    if state.status != SessionStatus.active:
        raise HTTPException(status_code=409, detail="Cette session est déjà terminée.")

    state.increment_turn()

    # Extract intent and entities
    result = extract_intent(payload.text)
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

    return UserMessageResponse(
        session_id=state.session_id,
        intent=state.current_intent or VoiceIntent.unknown,
        response_text=response_text,
        booking_draft=state.booking_draft,
        action_taken=action_taken,
        data=data,
    )


@router.post("/sessions/end", response_model=SessionEndResponse)
async def end_session(payload: SessionEndRequest) -> SessionEndResponse:
    """Close a voice conversation session."""
    state = conversation_manager.end_session(payload.session_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Session '{payload.session_id}' introuvable.")

    return SessionEndResponse(
        session_id=state.session_id,
        status=SessionStatus.completed,
        message=_GOODBYE,
        turns=state.turns,
        duration_seconds=state.duration_seconds,
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
    try:
        target_date = date.fromisoformat(draft.date)
    except (ValueError, TypeError):
        return "Je n'ai pas compris la date. Pouvez-vous la répéter au format jour/mois/année ?", "date_invalid", None

    avail = await find_available_slots(
        session=db,
        service_id=draft.service_id,
        target_date=target_date,
        preferred_employee_id=draft.employee_id,
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
        # Offer first 3 available slots
        top_slots = avail["slots"][:3]
        slots_text = ", ".join(s["start"].split("T")[1][:5] if "T" in s["start"] else s["start"] for s in top_slots)
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
        db, draft.service_id, employee_id, start_dt
    )
    if not ok:
        return f"Impossible de réserver : {message}", "validation_failed", None

    # Create the booking
    booking = Booking(
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
    result = await db.execute(
        select(Booking)
        .options(selectinload(Booking.service), selectinload(Booking.employee))
        .where(Booking.id == booking_id)
    )
    booking = result.scalars().first()
    if not booking:
        return f"Réservation #{booking_id} introuvable.", "booking_not_found", None
    if booking.status != BookingStatus.confirmed:
        return (
            f"Impossible de modifier la réservation #{booking_id} "
            f"(statut : {booking.status.value if hasattr(booking.status, 'value') else booking.status}).",
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
        new_start = datetime.combine(target_date, datetime.min.time().replace(hour=int(h), minute=int(m)))
    except (ValueError, TypeError):
        return "Je n'ai pas compris la date ou l'heure. Pouvez-vous répéter ?", "datetime_invalid", None

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

    booking = await db.get(Booking, booking_id)
    if not booking:
        return f"Réservation #{booking_id} introuvable.", "booking_not_found", None
    if booking.status == BookingStatus.cancelled:
        return f"La réservation #{booking_id} est déjà annulée.", "already_cancelled", None

    booking.status = BookingStatus.cancelled
    await db.commit()

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
            f"J'ai trouvé le service : {resolved.label} ({resolved.prix_eur}€, {resolved.duree_min} min). "
            "Pour quelle date souhaitez-vous vérifier les disponibilités ?",
            "need_date",
            {"service": {"id": resolved.id, "label": resolved.label}},
        )

    try:
        target_date = date.fromisoformat(target_date_str)
    except (ValueError, TypeError):
        return "Format de date non reconnu. Merci d'utiliser le format AAAA-MM-JJ.", "date_invalid", None

    avail = await find_available_slots(
        session=db,
        service_id=resolved.id,
        target_date=target_date,
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
        f"{len(avail['slots'])} créneau(x) disponible(s) le {target_date_str} pour {resolved.label}. "
        f"Premiers créneaux : {slots_text}. Souhaitez-vous réserver ?",
        "slots_found",
        {"service_id": resolved.id, "slots_count": len(avail["slots"]), "top_slots": top_slots},
    )


async def _handle_unknown(
    state: ConversationState, entities: dict, db: AsyncSession
) -> tuple[str, str | None, dict | None]:
    """Fallback handler for unrecognised intents."""
    return (
        "Je n'ai pas bien compris. Je peux vous aider à :\n"
        "• Prendre un rendez-vous\n"
        "• Modifier un rendez-vous existant\n"
        "• Annuler un rendez-vous\n"
        "• Vérifier les disponibilités\n"
        "Que souhaitez-vous faire ?",
        None,
        None,
    )


# ── Handler dispatch table ───────────────────────────────────

_INTENT_HANDLERS = {
    VoiceIntent.book: _handle_book,
    VoiceIntent.reschedule: _handle_reschedule,
    VoiceIntent.cancel: _handle_cancel,
    VoiceIntent.check_availability: _handle_check_availability,
    VoiceIntent.unknown: _handle_unknown,
}


# ── Helpers ──────────────────────────────────────────────────

def _merge_entities_to_draft(state: ConversationState, entities: dict) -> None:
    """Merge extracted entities into the session's booking draft."""
    mapping = {
        "date": "date",
        "time": "time",
        "service_keyword": None,  # handled separately
        "service_category": None,
    }
    for entity_key, draft_key in mapping.items():
        if draft_key and entity_key in entities:
            state.update_draft(**{draft_key: entities[entity_key]})


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
