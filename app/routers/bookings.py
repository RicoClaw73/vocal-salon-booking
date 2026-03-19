"""
Booking CRUD endpoints.

POST   /bookings            – create a new booking
GET    /bookings/{id}       – get booking details
PATCH  /bookings/{id}       – reschedule a booking
DELETE /bookings/{id}       – cancel a booking
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Booking, BookingStatus
from app.schemas import BookingCancelOut, BookingCreate, BookingOut, BookingReschedule
from app.slot_engine import validate_booking_request
from app.sms_sender import send_owner_cancel_alert

router = APIRouter(prefix="/bookings", tags=["bookings"])


async def _get_booking_with_rels(db: AsyncSession, booking_id: int) -> Booking | None:
    """Fetch a booking with service & employee eagerly loaded."""
    result = await db.execute(
        select(Booking)
        .options(selectinload(Booking.service), selectinload(Booking.employee))
        .where(Booking.id == booking_id)
    )
    return result.scalars().first()


def _booking_to_out(booking: Booking) -> BookingOut:
    """Convert ORM Booking to BookingOut schema (with joined fields)."""
    return BookingOut(
        id=booking.id,
        client_name=booking.client_name,
        client_phone=booking.client_phone,
        service_id=booking.service_id,
        service_label=booking.service.label,
        employee_id=booking.employee_id,
        employee_name=f"{booking.employee.prenom} {booking.employee.nom}",
        start_time=booking.start_time,
        end_time=booking.end_time,
        status=booking.status.value if hasattr(booking.status, "value") else booking.status,
        notes=booking.notes,
        created_at=booking.created_at,
        updated_at=booking.updated_at,
    )


@router.post("", response_model=BookingOut, status_code=201)
async def create_booking(
    payload: BookingCreate,
    db: AsyncSession = Depends(get_db),
) -> BookingOut:
    """Create a new salon booking after validating against business rules."""
    ok, message, end_time = await validate_booking_request(
        db, payload.service_id, payload.employee_id, payload.start_time
    )
    if not ok:
        raise HTTPException(status_code=409, detail=message)

    booking = Booking(
        client_name=payload.client_name,
        client_phone=payload.client_phone,
        service_id=payload.service_id,
        employee_id=payload.employee_id,
        start_time=payload.start_time,
        end_time=end_time,
        status=BookingStatus.confirmed,
        notes=payload.notes,
    )
    db.add(booking)
    await db.commit()

    # Re-fetch with eager-loaded relationships
    loaded = await _get_booking_with_rels(db, booking.id)
    return _booking_to_out(loaded)


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
) -> BookingOut:
    """Retrieve a booking by ID."""
    booking = await _get_booking_with_rels(db, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail=f"Réservation #{booking_id} introuvable.")
    return _booking_to_out(booking)


@router.patch("/{booking_id}", response_model=BookingOut)
async def reschedule_booking(
    booking_id: int,
    payload: BookingReschedule,
    db: AsyncSession = Depends(get_db),
) -> BookingOut:
    """Reschedule a booking to a new time (and optionally new employee)."""
    booking = await _get_booking_with_rels(db, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail=f"Réservation #{booking_id} introuvable.")
    if booking.status != BookingStatus.confirmed:
        raise HTTPException(
            status_code=409,
            detail=f"Impossible de modifier une réservation avec statut '{booking.status.value}'.",
        )

    employee_id = payload.employee_id or booking.employee_id
    ok, message, end_time = await validate_booking_request(
        db, booking.service_id, employee_id, payload.new_start_time,
        exclude_booking_id=booking_id,
    )
    if not ok:
        raise HTTPException(status_code=409, detail=message)

    booking.start_time = payload.new_start_time
    booking.end_time = end_time
    booking.employee_id = employee_id
    await db.commit()

    # Re-fetch with eager-loaded relationships
    loaded = await _get_booking_with_rels(db, booking_id)
    return _booking_to_out(loaded)


@router.delete("/{booking_id}", response_model=BookingCancelOut)
async def cancel_booking(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
) -> BookingCancelOut:
    """Cancel a booking."""
    booking = await db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail=f"Réservation #{booking_id} introuvable.")
    if booking.status == BookingStatus.cancelled:
        raise HTTPException(status_code=409, detail="Cette réservation est déjà annulée.")

    booking.status = BookingStatus.cancelled
    await db.commit()

    asyncio.create_task(send_owner_cancel_alert(
        booking_id=booking.id,
        client_name=booking.client_name,
        client_phone=booking.client_phone,
        start_time=booking.start_time,
    ))

    return BookingCancelOut(
        id=booking.id,
        status="cancelled",
        message=f"Réservation #{booking_id} annulée avec succès.",
    )
