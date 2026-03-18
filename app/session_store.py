"""
Persistent session store for voice conversations (Phase 4.3).

Provides the same logical interface as ConversationManager but backs
state to the database via VoiceSession / TranscriptEvent models.
The in-memory ConversationState dataclass is still the "hot" working
object inside a single request; this module handles load/save to DB.

Usage in endpoints::

    state = await load_or_create_session(db, session_id=None, ...)
    # ... mutate state ...
    await save_session(db, state)
    await append_transcript_event(db, state.session_id, ...)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation import ConversationState
from app.models import TranscriptEvent, VoiceSession
from app.voice_schemas import BookingDraft, SessionStatus, VoiceIntent

logger = logging.getLogger(__name__)


# ── Conversion helpers ───────────────────────────────────────


def _ensure_aware(dt: datetime | None) -> datetime:
    """Ensure a datetime is timezone-aware (UTC).  SQLite returns naive datetimes."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_state(row: VoiceSession) -> ConversationState:
    """Convert a VoiceSession DB row into an in-memory ConversationState."""
    draft_data = json.loads(row.booking_draft_json) if row.booking_draft_json else {}
    draft = BookingDraft(**draft_data)

    intent = None
    if row.current_intent:
        try:
            intent = VoiceIntent(row.current_intent)
        except ValueError:
            intent = None

    try:
        status = SessionStatus(row.status)
    except ValueError:
        status = SessionStatus.active

    messages: list = []
    if row.messages_json:
        try:
            messages = json.loads(row.messages_json)
        except (json.JSONDecodeError, TypeError):
            messages = []

    state = ConversationState(
        session_id=row.session_id,
        status=status,
        current_intent=intent,
        booking_draft=draft,
        turns=row.turns,
        client_name=row.client_name,
        client_phone=row.client_phone,
        channel=row.channel,
        created_at=_ensure_aware(row.created_at),
        last_activity=_ensure_aware(row.last_activity),
        messages=messages,
    )
    return state


def _state_to_row_dict(state: ConversationState) -> dict:
    """Serialise ConversationState fields into a dict suitable for VoiceSession."""
    return {
        "session_id": state.session_id,
        "status": state.status.value if hasattr(state.status, "value") else str(state.status),
        "current_intent": state.current_intent.value if state.current_intent else None,
        "booking_draft_json": state.booking_draft.model_dump_json(),
        "turns": state.turns,
        "client_name": state.client_name,
        "client_phone": state.client_phone,
        "channel": state.channel,
        "last_activity": state.last_activity,
        "messages_json": json.dumps(state.messages) if state.messages else "[]",
    }


# ── Public API ───────────────────────────────────────────────


async def load_session(
    db: AsyncSession,
    session_id: str,
) -> ConversationState | None:
    """Load a session from the database. Returns None if not found."""
    result = await db.execute(
        select(VoiceSession).where(VoiceSession.session_id == session_id)
    )
    row = result.scalars().first()
    if row is None:
        return None
    return _row_to_state(row)


async def create_session(
    db: AsyncSession,
    client_name: str | None = None,
    client_phone: str | None = None,
    channel: str = "phone",
    session_id: str | None = None,
) -> ConversationState:
    """Create a new voice session and persist it to the database.

    Pass ``session_id`` to use a specific ID (e.g. Twilio CallSid) instead
    of the auto-generated one.
    """
    session_id = session_id or uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    row = VoiceSession(
        session_id=session_id,
        status="active",
        client_name=client_name,
        client_phone=client_phone,
        channel=channel,
        booking_draft_json="{}",
        turns=0,
        created_at=now,
        last_activity=now,
        messages_json="[]",
    )
    db.add(row)
    await db.flush()  # get defaults populated

    state = ConversationState(
        session_id=session_id,
        client_name=client_name,
        client_phone=client_phone,
        channel=channel,
        created_at=now,
        last_activity=now,
    )
    return state


async def save_session(db: AsyncSession, state: ConversationState) -> None:
    """Persist the current in-memory state back to the database."""
    result = await db.execute(
        select(VoiceSession).where(VoiceSession.session_id == state.session_id)
    )
    row = result.scalars().first()
    if row is None:
        logger.warning("save_session: session %s not found in DB", state.session_id)
        return

    updates = _state_to_row_dict(state)
    for key, value in updates.items():
        if key != "session_id":  # don't overwrite PK
            setattr(row, key, value)
    await db.flush()


async def append_transcript_event(
    db: AsyncSession,
    session_id: str,
    turn_number: int,
    user_text: str = "",
    intent: str | None = None,
    confidence: float | None = None,
    response_text: str = "",
    action_taken: str | None = None,
    is_fallback: bool = False,
    data: dict | None = None,
) -> TranscriptEvent:
    """Append a turn event to the session transcript."""
    event = TranscriptEvent(
        session_id=session_id,
        turn_number=turn_number,
        user_text=user_text,
        intent=intent,
        confidence=confidence,
        response_text=response_text,
        action_taken=action_taken,
        is_fallback=is_fallback,
        data_json=json.dumps(data) if data is not None else None,
    )
    db.add(event)
    await db.flush()
    return event


async def get_transcript_events(
    db: AsyncSession,
    session_id: str,
) -> list[dict]:
    """Return all transcript events for a session, ordered by turn number."""
    result = await db.execute(
        select(TranscriptEvent)
        .where(TranscriptEvent.session_id == session_id)
        .order_by(TranscriptEvent.turn_number)
    )
    rows = result.scalars().all()
    events = []
    for row in rows:
        events.append({
            "turn_number": row.turn_number,
            "user_text": row.user_text,
            "intent": row.intent,
            "confidence": row.confidence,
            "response_text": row.response_text,
            "action_taken": row.action_taken,
            "is_fallback": row.is_fallback,
            "data": json.loads(row.data_json) if row.data_json else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
    return events


async def load_or_create_session(
    db: AsyncSession,
    session_id: str | None = None,
    client_name: str | None = None,
    client_phone: str | None = None,
    channel: str = "phone",
) -> ConversationState:
    """Load an existing session or create a new one.

    Raises ValueError if session_id is provided but not found.
    """
    if session_id:
        state = await load_session(db, session_id)
        if state is None:
            raise ValueError(f"Session '{session_id}' introuvable.")
        return state
    return await create_session(db, client_name, client_phone, channel)
