"""
Operations / observability endpoints (Phase 4.4).

Read-only endpoints for pilot monitoring and diagnostics.
No sensitive data (phone numbers, names) is exposed — only operational
metadata (session IDs, intents, outcomes, timestamps, counts).

Endpoints:
  GET /ops/metrics             – Counter and latency snapshots
  GET /ops/sessions/recent     – Recent session summaries
  GET /ops/sessions/{id}/diag  – Single-session failure diagnostics

These endpoints are designed for operators, not end users.
They sit behind the same API-key guard as voice endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_api_key
from app.circuit_breaker import stt_circuit_breaker, tts_circuit_breaker
from app.config import settings
from app.database import get_db
from app.models import TranscriptEvent, VoiceSession
from app.observability import metrics
from app.providers import check_provider_readiness
from app.smoke_test import run_smoke_test

router = APIRouter(
    prefix="/ops",
    tags=["ops"],
    dependencies=[Depends(require_api_key)],
)


# ── Metrics ─────────────────────────────────────────────────────


@router.get("/metrics")
async def get_metrics() -> dict:
    """Return a snapshot of in-memory operational metrics.

    Includes counters (voice_turns, fallbacks, bookings, auth failures, etc.)
    and latency statistics (min/max/avg voice turn processing time).
    """
    return metrics.snapshot()


# ── Provider Readiness (Phase 5.1) ─────────────────────────────


@router.get("/providers/status")
async def provider_status() -> dict:
    """Report STT/TTS provider readiness state with circuit-breaker info.

    Shows which providers are configured, whether credentials are present,
    and whether the system has fallen back to mock.  No secrets are exposed.
    Includes circuit-breaker state per provider role (Phase 5.2).
    """
    readiness = check_provider_readiness(
        stt_requested=settings.STT_PROVIDER,
        stt_api_key=settings.STT_API_KEY,
        tts_requested=settings.TTS_PROVIDER,
        tts_api_key=settings.TTS_API_KEY,
    )
    readiness["circuit_breakers"] = {
        "stt": stt_circuit_breaker.snapshot(),
        "tts": tts_circuit_breaker.snapshot(),
    }
    return readiness


@router.post("/providers/smoke-test")
async def provider_smoke_test() -> dict:
    """Run a lightweight smoke test against the configured providers.

    Sends a silence clip through STT and a short sentence through TTS,
    returning latency, success, and any error classification.
    Safe to call in production — uses tiny payloads.
    """
    return await run_smoke_test()


# ── Recent Sessions ─────────────────────────────────────────────


@router.get("/sessions/recent")
async def recent_sessions(
    limit: int = Query(20, ge=1, le=100, description="Max sessions to return"),
    status: str | None = Query(None, description="Filter by status: active, completed, expired"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List recent voice sessions with summary metadata.

    Returns operational data only — no client names or phone numbers.
    Ordered by last_activity descending (most recent first).
    """
    query = select(VoiceSession).order_by(VoiceSession.last_activity.desc())
    if status:
        query = query.where(VoiceSession.status == status)
    query = query.limit(limit)

    result = await db.execute(query)
    rows = result.scalars().all()

    sessions = []
    for row in rows:
        sessions.append({
            "session_id": row.session_id,
            "status": row.status,
            "current_intent": row.current_intent,
            "turns": row.turns,
            "channel": row.channel,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_activity": row.last_activity.isoformat() if row.last_activity else None,
        })

    return {"count": len(sessions), "sessions": sessions}


# ── Session Diagnostics ─────────────────────────────────────────


@router.get("/sessions/{session_id}/diag")
async def session_diagnostics(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Detailed diagnostics for a single voice session.

    Includes turn-by-turn intent/outcome timeline, fallback count,
    and action summary.  No sensitive client data exposed.
    """
    result = await db.execute(
        select(VoiceSession).where(VoiceSession.session_id == session_id)
    )
    row = result.scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")

    # Load transcript events
    ev_result = await db.execute(
        select(TranscriptEvent)
        .where(TranscriptEvent.session_id == session_id)
        .order_by(TranscriptEvent.turn_number)
    )
    events = ev_result.scalars().all()

    fallback_count = sum(1 for e in events if e.is_fallback)
    intents_seen = list(dict.fromkeys(e.intent for e in events if e.intent))
    actions_seen = list(dict.fromkeys(e.action_taken for e in events if e.action_taken))

    timeline = []
    for e in events:
        timeline.append({
            "turn": e.turn_number,
            "intent": e.intent,
            "confidence": e.confidence,
            "action": e.action_taken,
            "is_fallback": e.is_fallback,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        })

    duration_seconds = None
    if row.created_at and row.last_activity:
        created = row.created_at
        last = row.last_activity
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        duration_seconds = round((last - created).total_seconds(), 2)

    return {
        "session_id": row.session_id,
        "status": row.status,
        "channel": row.channel,
        "turns": row.turns,
        "fallback_count": fallback_count,
        "intents_seen": intents_seen,
        "actions_seen": actions_seen,
        "duration_seconds": duration_seconds,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_activity": row.last_activity.isoformat() if row.last_activity else None,
        "timeline": timeline,
    }


# ── Failure Summary ─────────────────────────────────────────────


@router.get("/failures/summary")
async def failure_summary(
    hours: int = Query(24, ge=1, le=168, description="Look-back window in hours"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Aggregate failure diagnostics over a time window.

    Returns counts of fallbacks, failed actions, and sessions with
    high fallback rates.  Useful for spotting systemic issues.
    """
    cutoff = datetime.now(timezone.utc)
    # We can't do timezone arithmetic in SQLite reliably, so load recent
    # events and filter in Python (acceptable for local-first pilot scale).
    ev_result = await db.execute(
        select(TranscriptEvent).order_by(TranscriptEvent.id.desc()).limit(5000)
    )
    all_events = ev_result.scalars().all()

    # Filter by time window
    events_in_window = []
    for e in all_events:
        if e.created_at:
            ts = e.created_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (cutoff - ts).total_seconds() <= hours * 3600:
                events_in_window.append(e)

    total_turns = len(events_in_window)
    fallback_turns = sum(1 for e in events_in_window if e.is_fallback)

    # Sessions with >= 2 fallbacks
    session_fallbacks: dict[str, int] = {}
    for e in events_in_window:
        if e.is_fallback:
            session_fallbacks[e.session_id] = session_fallbacks.get(e.session_id, 0) + 1

    high_fallback_sessions = [
        {"session_id": sid, "fallback_count": cnt}
        for sid, cnt in sorted(session_fallbacks.items(), key=lambda x: -x[1])
        if cnt >= 2
    ][:10]

    # Action distribution
    action_counts: dict[str, int] = {}
    for e in events_in_window:
        action = e.action_taken or "none"
        action_counts[action] = action_counts.get(action, 0) + 1

    return {
        "window_hours": hours,
        "total_turns": total_turns,
        "fallback_turns": fallback_turns,
        "fallback_rate": round(fallback_turns / total_turns, 3) if total_turns else 0.0,
        "high_fallback_sessions": high_fallback_sessions,
        "action_distribution": dict(sorted(action_counts.items(), key=lambda x: -x[1])),
    }
