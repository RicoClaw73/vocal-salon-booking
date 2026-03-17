"""
Availability search endpoint.

GET /availability/search  – find available slots for a service on a date
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import AvailabilityOut
from app.slot_engine import find_available_slots

router = APIRouter(prefix="/availability", tags=["availability"])


@router.get("/search", response_model=AvailabilityOut)
async def search_availability(
    service_id: str = Query(..., description="Service ID from catalogue"),
    date_str: str = Query(..., alias="date", description="Date YYYY-MM-DD"),
    employee_id: str | None = Query(None, description="Preferred employee (optional)"),
    db: AsyncSession = Depends(get_db),
) -> AvailabilityOut:
    """Search available time slots for a service on a given date."""
    # Parse and validate date
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Format de date invalide: '{date_str}'. Attendu: YYYY-MM-DD",
        )

    result = await find_available_slots(
        session=db,
        service_id=service_id,
        target_date=target_date,
        preferred_employee_id=employee_id,
    )

    return AvailabilityOut(
        service_id=service_id,
        date=date_str,
        slots=result["slots"],
        alternatives=result["alternatives"],
        message=result["message"],
    )
