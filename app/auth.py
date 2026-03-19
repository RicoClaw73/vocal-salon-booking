"""
Authentication and tenant resolution dependencies.

Multi-tenant behaviour:
  - get_current_tenant: resolves a Tenant from the X-API-Key header.
    If VOICE_API_KEY is empty (dev/CI), returns the default tenant without checking.
    Otherwise, looks up the tenant by api_key in the DB → 401 if not found.

  - get_tenant_from_slug: resolves a Tenant from the ?tenant=<slug> query param.
    Used by public endpoints (services, employees, availability) that need tenant
    context without requiring an API key. Falls back to the first active tenant
    when the param is absent.

  - require_api_key: backward-compat shim — calls get_current_tenant, returns None.

Usage::

    from app.auth import get_current_tenant, get_tenant_from_slug

    # Protected endpoint (API key required):
    @router.post("/voice/turn")
    async def voice_turn(tenant: Tenant = Depends(get_current_tenant)): ...

    # Public endpoint (tenant from query param):
    @router.get("/services")
    async def list_services(tenant: Tenant = Depends(get_tenant_from_slug)): ...
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Tenant
from app.observability import metrics
from app.tenant_service import (
    get_first_active_tenant,
    get_tenant_by_api_key,
    get_tenant_by_slug,
)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_current_tenant(
    api_key: str | None = Security(_api_key_header),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """FastAPI dependency – resolve and return the current Tenant.

    When VOICE_API_KEY is not configured, auth is in dev mode:
    the default tenant is returned without verifying the key.
    """
    from app.config import settings  # local import avoids circular dependency

    if not settings.VOICE_API_KEY:
        # Dev/CI mode — return default tenant (create if absent is handled at startup)
        tenant = await get_tenant_by_slug(db, "default")
        if tenant:
            return tenant
        # Fallback: any active tenant
        tenant = await get_first_active_tenant(db)
        if tenant:
            return tenant
        raise HTTPException(status_code=503, detail="No tenant configured.")

    if not api_key:
        metrics.inc("auth_failures")
        raise HTTPException(status_code=401, detail="Missing API key.")

    tenant = await get_tenant_by_api_key(db, api_key)
    if not tenant:
        metrics.inc("auth_failures")
        raise HTTPException(status_code=401, detail="Invalid API key.")

    return tenant


async def get_tenant_from_slug(
    tenant: str | None = Query(None, alias="tenant"),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """FastAPI dependency – resolve Tenant from ?tenant=<slug> query param.

    Falls back to the default tenant when the param is absent.
    Returns 404 if the specified slug does not exist or is inactive.
    """
    if tenant:
        obj = await get_tenant_by_slug(db, tenant)
        if not obj:
            raise HTTPException(status_code=404, detail=f"Tenant '{tenant}' not found.")
        return obj

    # No slug provided — use default or first active tenant
    obj = await get_tenant_by_slug(db, "default")
    if obj:
        return obj
    obj = await get_first_active_tenant(db)
    if obj:
        return obj
    raise HTTPException(status_code=503, detail="No tenant configured.")


async def require_api_key(
    api_key: str | None = Security(_api_key_header),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Backward-compat shim. Prefer get_current_tenant for new code."""
    await get_current_tenant(api_key=api_key, db=db)
