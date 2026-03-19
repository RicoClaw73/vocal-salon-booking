"""
RGPD auto-purge background loop.

Runs every 24 h and deletes data older than configured retention periods:
  - voice_sessions (+ cascade transcript_events) older than SESSION_RETENTION_DAYS
  - callback_requests in terminal states (called_back / resolved) older than
    CALLBACK_RETENTION_DAYS

Design decisions:
- Purge runs at a fixed hour (PURGE_HOUR) to avoid hitting the DB at peak time.
- Sessions are identified by last_activity so a long-running session isn't
  prematurely deleted.
- Bookings are NOT purged here — they are business records with legal retention
  requirements (French law: 5–10 years for commercial data).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, delete

from app.config import settings
from app.database import async_session
from app.models import CallbackRequest, CallbackRequestStatus, TranscriptEvent, VoiceSession

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 3600  # check every hour
_last_purge_date: "date | None" = None  # noqa: F821 — forward ref for type hint


async def _purge_old_data() -> dict[str, int]:
    """Delete stale sessions and resolved callbacks. Returns counts per table."""
    from datetime import date

    now = datetime.now()
    session_cutoff = now - timedelta(days=settings.SESSION_RETENTION_DAYS)
    callback_cutoff = now - timedelta(days=settings.CALLBACK_RETENTION_DAYS)

    counts: dict[str, int] = {}

    async with async_session() as db:
        # 1. Transcript events for old sessions (manual cascade for SQLite compatibility)
        old_session_ids_result = await db.execute(
            __import__("sqlalchemy", fromlist=["select"])
            .select(VoiceSession.session_id)
            .where(VoiceSession.last_activity < session_cutoff)
        )
        old_ids = [r[0] for r in old_session_ids_result.all()]

        if old_ids:
            te_result = await db.execute(
                delete(TranscriptEvent).where(TranscriptEvent.session_id.in_(old_ids))
            )
            counts["transcript_events"] = te_result.rowcount

            vs_result = await db.execute(
                delete(VoiceSession).where(VoiceSession.session_id.in_(old_ids))
            )
            counts["voice_sessions"] = vs_result.rowcount
        else:
            counts["transcript_events"] = 0
            counts["voice_sessions"] = 0

        # 2. Resolved / called-back callback requests
        cb_result = await db.execute(
            delete(CallbackRequest).where(
                and_(
                    CallbackRequest.created_at < callback_cutoff,
                    CallbackRequest.status.in_(
                        [CallbackRequestStatus.called_back, CallbackRequestStatus.resolved]
                    ),
                )
            )
        )
        counts["callback_requests"] = cb_result.rowcount

        await db.commit()

    return counts


async def purge_loop() -> None:
    """
    Background asyncio task — runs once per day at PURGE_HOUR (local time).
    Always active (no enable flag — RGPD purge is not optional).
    """
    global _last_purge_date
    from datetime import date

    logger.info(
        "Purge loop started (session_retention=%dd, callback_retention=%dd, hour=%dh)",
        settings.SESSION_RETENTION_DAYS,
        settings.CALLBACK_RETENTION_DAYS,
        settings.PURGE_HOUR,
    )

    while True:
        await asyncio.sleep(_CHECK_INTERVAL)

        now = datetime.now()
        today = now.date()

        if now.hour == settings.PURGE_HOUR and _last_purge_date != today:
            _last_purge_date = today
            try:
                counts = await _purge_old_data()
                logger.info(
                    "RGPD purge complete: %d sessions, %d events, %d callbacks deleted",
                    counts.get("voice_sessions", 0),
                    counts.get("transcript_events", 0),
                    counts.get("callback_requests", 0),
                )
            except Exception as exc:
                logger.error("RGPD purge error: %s", exc)
