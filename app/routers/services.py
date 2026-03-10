"""
Services catalogue endpoints.

GET /services          – list all services (with optional filters)
GET /services/{id}     – single service detail
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Service
from app.schemas import ServiceListOut, ServiceOut

router = APIRouter(prefix="/services", tags=["services"])


@router.get("", response_model=ServiceListOut)
async def list_services(
    category: str | None = Query(None, description="Filter by category_id"),
    genre: str | None = Query(None, description="Filter by genre (F/M/mixte)"),
    db: AsyncSession = Depends(get_db),
) -> ServiceListOut:
    """Return the full service catalogue, optionally filtered."""
    query = select(Service).order_by(Service.category_id, Service.label)
    if category:
        query = query.where(Service.category_id == category)
    if genre:
        query = query.where(Service.genre == genre)
    result = await db.execute(query)
    services = list(result.scalars().all())
    return ServiceListOut(
        count=len(services),
        services=[ServiceOut.model_validate(s) for s in services],
    )


@router.get("/{service_id}", response_model=ServiceOut)
async def get_service(
    service_id: str,
    db: AsyncSession = Depends(get_db),
) -> ServiceOut:
    """Return a single service by ID."""
    service = await db.get(Service, service_id)
    if not service:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' introuvable.")
    return ServiceOut.model_validate(service)
