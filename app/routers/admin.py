"""
Admin dashboard API.

GET /api/v1/admin/bookings  — upcoming bookings (token-protected)
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import Booking, BookingStatus
from app.routers.bookings import _booking_to_out
from app.schemas import BookingOut

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_token(token: str | None = Query(None)) -> None:
    expected = settings.VOICE_API_KEY
    if not expected:
        return  # dev mode — no auth
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="Token invalide.")


@router.get("/bookings", response_model=list[BookingOut])
async def list_upcoming_bookings(
    token: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> list[BookingOut]:
    """Return upcoming non-cancelled bookings for the next `days` days."""
    _require_token(token)

    now = datetime.now()
    until = now + timedelta(days=days)

    result = await db.execute(
        select(Booking)
        .options(selectinload(Booking.service), selectinload(Booking.employee))
        .where(Booking.start_time >= now)
        .where(Booking.start_time <= until)
        .where(Booking.status != BookingStatus.cancelled)
        .order_by(Booking.start_time)
    )
    bookings = result.scalars().all()
    return [_booking_to_out(b) for b in bookings]
