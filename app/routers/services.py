"""
Services catalogue endpoints.

GET    /services          – list all services (with optional filters)
GET    /services/{id}     – single service detail
POST   /services          – create service
PATCH  /services/{id}     – update service
DELETE /services/{id}     – delete service
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_tenant_from_slug
from app.database import get_db
from app.models import Service, Tenant
from app.schemas import ServiceCreate, ServiceListOut, ServiceOut, ServiceUpdate

router = APIRouter(prefix="/services", tags=["services"])


@router.get("", response_model=ServiceListOut)
async def list_services(
    category: str | None = Query(None, description="Filter by category_id"),
    genre: str | None = Query(None, description="Filter by genre (F/M/mixte)"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> ServiceListOut:
    """Return the full service catalogue, optionally filtered."""
    query = (
        select(Service)
        .where(Service.tenant_id == tenant.id)
        .order_by(Service.category_id, Service.label)
    )
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
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> ServiceOut:
    """Return a single service by ID."""
    result = await db.execute(
        select(Service).where(Service.id == service_id, Service.tenant_id == tenant.id)
    )
    service = result.scalars().first()
    if not service:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' introuvable.")
    return ServiceOut.model_validate(service)


@router.post("", response_model=ServiceOut, status_code=201)
async def create_service(
    data: ServiceCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> ServiceOut:
    """Create a new service."""
    service = Service(
        id=uuid4().hex[:12],
        tenant_id=tenant.id,
        **data.model_dump(),
    )
    db.add(service)
    await db.commit()
    await db.refresh(service)
    return ServiceOut.model_validate(service)


@router.patch("/{service_id}", response_model=ServiceOut)
async def update_service(
    service_id: str,
    data: ServiceUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> ServiceOut:
    """Update a service's fields."""
    result = await db.execute(
        select(Service).where(Service.id == service_id, Service.tenant_id == tenant.id)
    )
    service = result.scalars().first()
    if not service:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' introuvable.")
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(service, field, value)
    await db.commit()
    await db.refresh(service)
    return ServiceOut.model_validate(service)


@router.delete("/{service_id}")
async def delete_service(
    service_id: str,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_tenant_from_slug),
) -> dict:
    """Delete a service."""
    result = await db.execute(
        select(Service).where(Service.id == service_id, Service.tenant_id == tenant.id)
    )
    service = result.scalars().first()
    if not service:
        raise HTTPException(status_code=404, detail=f"Service '{service_id}' introuvable.")
    await db.delete(service)
    await db.commit()
    return {"deleted": service_id}
