"""
Tenant CRUD and resolution helpers.

Used at startup (ensure_default_tenant) and per-request (get_tenant_by_api_key,
get_tenant_by_slug) to identify and validate which salon a request belongs to.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tenant


async def get_tenant_by_api_key(db: AsyncSession, api_key: str) -> Tenant | None:
    result = await db.execute(
        select(Tenant).where(Tenant.api_key == api_key, Tenant.is_active.is_(True))
    )
    return result.scalar_one_or_none()


async def get_tenant_by_slug(db: AsyncSession, slug: str) -> Tenant | None:
    result = await db.execute(
        select(Tenant).where(Tenant.slug == slug, Tenant.is_active.is_(True))
    )
    return result.scalar_one_or_none()


async def get_first_active_tenant(db: AsyncSession) -> Tenant | None:
    result = await db.execute(
        select(Tenant).where(Tenant.is_active.is_(True)).limit(1)
    )
    return result.scalar_one_or_none()


async def create_tenant(
    db: AsyncSession,
    slug: str,
    name: str,
    api_key: str,
    config_path: str | None = None,
) -> Tenant:
    tenant = Tenant(slug=slug, name=name, api_key=api_key, config_path=config_path)
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


async def ensure_default_tenant(db: AsyncSession, default_api_key: str = "") -> Tenant:
    """
    Idempotent: return the existing default tenant or create one.
    Called at startup before seeding and settings load.
    """
    existing = await get_tenant_by_slug(db, "default")
    if existing:
        return existing

    api_key = default_api_key or str(uuid.uuid4())
    return await create_tenant(
        db,
        slug="default",
        name="Maison Éclat",
        api_key=api_key,
        config_path="config/tenants/default.yaml",
    )


async def list_tenants(db: AsyncSession) -> list[Tenant]:
    result = await db.execute(select(Tenant).where(Tenant.is_active.is_(True)))
    return list(result.scalars().all())
