"""
Maison Éclat – Salon Booking API.

FastAPI application entrypoint.
Initialises the database, seeds reference data, and wires all routers.

Usage:
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.audio_store import cleanup_loop, cleanup_old_files
from app.config import settings
from app.database import async_session, engine
from app.models import Base
from app.observability import metrics
from app.routers import availability, bookings, employees, ops, services, telephony, voice
from app.routers import twilio_router
from app.seed import seed_all

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Startup: create tables, seed data, init audio store. Shutdown: dispose engine."""
    logger.info("Creating database tables …")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Seeding reference data …")
    async with async_session() as session:
        summary = await seed_all(session)
        logger.info("Seed complete: %s", summary)

    # Audio store: create dir + initial cleanup
    audio_dir = Path(settings.AUDIO_DIR)
    audio_dir.mkdir(parents=True, exist_ok=True)
    deleted = cleanup_old_files(audio_dir, settings.AUDIO_MAX_AGE_HOURS)
    logger.info("Audio store ready: %s (cleaned %d old files)", audio_dir, deleted)

    # Background cleanup task (runs every hour)
    cleanup_task = asyncio.create_task(
        cleanup_loop(audio_dir, settings.AUDIO_MAX_AGE_HOURS)
    )

    yield  # ── app runs ──

    cleanup_task.cancel()
    await engine.dispose()


app = FastAPI(
    title=settings.APP_TITLE,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# ── Routers ─────────────────────────────────────────────────
app.include_router(services.router, prefix=API_PREFIX)
app.include_router(employees.router, prefix=API_PREFIX)
app.include_router(availability.router, prefix=API_PREFIX)
app.include_router(bookings.router, prefix=API_PREFIX)
app.include_router(voice.router, prefix=API_PREFIX)
app.include_router(ops.router, prefix=API_PREFIX)
app.include_router(telephony.router, prefix=API_PREFIX)
app.include_router(twilio_router.router, prefix=API_PREFIX)

# Serve generated TTS audio files for Twilio <Play>
_audio_dir = Path(settings.AUDIO_DIR)
_audio_dir.mkdir(parents=True, exist_ok=True)
app.mount("/audio", StaticFiles(directory=str(_audio_dir)), name="audio")


# ── Health ──────────────────────────────────────────────────
@app.get("/health", tags=["health"])
async def health() -> dict:
    """Liveness / readiness check with basic operational counters."""
    db_status = "ok"
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"
    snap = metrics.snapshot()
    return {
        "status": "ok",
        "version": settings.APP_VERSION,
        "database": db_status,
        "uptime_seconds": snap["uptime_seconds"],
        "voice_turns": snap["counters"].get("voice_turns", 0),
        "active_sessions": snap["counters"].get("sessions_started", 0)
        - snap["counters"].get("sessions_completed", 0),
    }
