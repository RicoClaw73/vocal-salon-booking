"""
Maison Éclat – Salon Booking API.

FastAPI application entrypoint.
Initialises the database, seeds reference data, and wires all routers.

Usage:
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.config import settings
from app.database import async_session, engine
from app.models import Base
from app.routers import availability, bookings, employees, services, voice
from app.schemas import HealthOut
from app.seed import seed_all

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Startup: create tables & seed data. Shutdown: dispose engine."""
    logger.info("Creating database tables …")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Seeding reference data …")
    async with async_session() as session:
        summary = await seed_all(session)
        logger.info("Seed complete: %s", summary)

    yield  # ── app runs ──

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


# ── Health ──────────────────────────────────────────────────
@app.get("/health", response_model=HealthOut, tags=["health"])
async def health() -> HealthOut:
    """Liveness / readiness check."""
    db_status = "ok"
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"
    return HealthOut(
        status="ok",
        version=settings.APP_VERSION,
        database=db_status,
    )
