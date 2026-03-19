"""
Admin dashboard API.

GET  /api/v1/admin/bookings   — upcoming bookings (tenant-scoped, API key required)
GET  /api/v1/admin/settings   — read runtime settings
PATCH /api/v1/admin/settings  — update runtime settings
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_tenant
from app.database import get_db
from app.models import Booking, BookingStatus, CallbackRequest, CallbackRequestStatus, Employee, Service, Tenant, VoiceSession
from app.routers.bookings import _booking_to_out
from app.schemas import BookingOut
from app.session_store import get_transcript_events
from app.settings_service import get_settings_with_values, update_settings


# ── Callback schemas ──────────────────────────────────────────

class CallbackOut(BaseModel):
    id: int
    caller_phone: str | None
    recording_url: str | None
    recording_duration: int | None
    transcription: str | None
    status: str
    notes: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CallbackPatch(BaseModel):
    status: str | None = None
    notes: str | None = None


class VoiceSessionOut(BaseModel):
    session_id: str
    status: str
    turns: int
    client_name: str | None
    client_phone: str | None
    channel: str
    current_intent: str | None
    created_at: datetime
    last_activity: datetime

    model_config = {"from_attributes": True}

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/callbacks", response_model=list[CallbackOut])
async def list_callbacks(
    status: str | None = Query(None, description="Filter by status: pending|called_back|resolved"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> list[CallbackOut]:
    """Return callback requests, newest first. Optionally filter by status."""
    query = (
        select(CallbackRequest)
        .where(CallbackRequest.tenant_id == tenant.id)
        .order_by(CallbackRequest.created_at.desc())
        .limit(100)
    )
    if status:
        try:
            query = query.where(CallbackRequest.status == CallbackRequestStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    result = await db.execute(query)
    rows = result.scalars().all()
    return [CallbackOut.model_validate(r) for r in rows]


@router.patch("/callbacks/{callback_id}")
async def update_callback(
    callback_id: int,
    body: CallbackPatch,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> dict:
    """Update status and/or notes on a callback request."""
    result = await db.execute(
        select(CallbackRequest).where(
            CallbackRequest.id == callback_id,
            CallbackRequest.tenant_id == tenant.id,
        )
    )
    cb = result.scalars().first()
    if cb is None:
        raise HTTPException(status_code=404, detail="Callback request not found.")

    if body.status is not None:
        try:
            cb.status = CallbackRequestStatus(body.status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {body.status}")
    if body.notes is not None:
        cb.notes = body.notes

    await db.commit()
    return {"id": callback_id, "status": cb.status.value}


@router.get("/services")
async def list_services(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> list[dict]:
    """Return all services for the create-booking form."""
    result = await db.execute(
        select(Service)
        .where(Service.tenant_id == tenant.id)
        .order_by(Service.category_label, Service.label)
    )
    svcs = result.scalars().all()
    return [
        {
            "id": s.id,
            "label": s.label,
            "category_label": s.category_label,
            "prix_eur": s.prix_eur,
            "duree_min": s.duree_min,
        }
        for s in svcs
    ]


@router.get("/employees")
async def list_employees(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> list[dict]:
    """Return all employees for the create-booking form."""
    result = await db.execute(
        select(Employee)
        .where(Employee.tenant_id == tenant.id)
        .order_by(Employee.prenom)
    )
    emps = result.scalars().all()
    return [
        {"id": e.id, "prenom": e.prenom, "nom": e.nom, "role": e.role or "", "horaires_json": e.horaires_json or "{}"}
        for e in emps
    ]


@router.get("/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> dict:
    """
    Return aggregated stats for the current calendar month:
      - rdv_count / revenue_eur for the month
      - upcoming_count (from now)
      - pending_callbacks count
      - top_services  (top 5 by booking count)
      - by_employee   (all employees, sorted by count desc)
    """
    now = datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_end = (month_start + timedelta(days=32)).replace(day=1)

    # Month bookings (confirmed + completed only)
    result = await db.execute(
        select(Booking)
        .options(selectinload(Booking.service), selectinload(Booking.employee))
        .where(
            and_(
                Booking.tenant_id == tenant.id,
                Booking.start_time >= month_start,
                Booking.start_time < month_end,
                Booking.status.in_([BookingStatus.confirmed, BookingStatus.completed]),
            )
        )
    )
    month_bkgs = result.scalars().all()

    # Upcoming (from now, not cancelled)
    result2 = await db.execute(
        select(Booking)
        .where(
            and_(
                Booking.tenant_id == tenant.id,
                Booking.start_time >= now,
                Booking.status != BookingStatus.cancelled,
            )
        )
    )
    upcoming_count = len(result2.scalars().all())

    # Pending callbacks
    cb_result = await db.execute(
        select(CallbackRequest).where(
            CallbackRequest.tenant_id == tenant.id,
            CallbackRequest.status == CallbackRequestStatus.pending,
        )
    )
    pending_callbacks = len(cb_result.scalars().all())

    # Aggregate
    rdv_count = len(month_bkgs)
    revenue_eur = sum(b.service.prix_eur for b in month_bkgs if b.service)

    svc_counts: dict[str, dict] = {}
    for b in month_bkgs:
        if not b.service:
            continue
        key = b.service.label
        if key not in svc_counts:
            svc_counts[key] = {"label": key, "count": 0, "revenue": 0.0}
        svc_counts[key]["count"] += 1
        svc_counts[key]["revenue"] += b.service.prix_eur
    top_services = sorted(svc_counts.values(), key=lambda x: x["count"], reverse=True)[:5]

    emp_counts: dict[str, dict] = {}
    for b in month_bkgs:
        if not b.employee:
            continue
        name = f"{b.employee.prenom} {b.employee.nom}"
        if name not in emp_counts:
            emp_counts[name] = {"name": name, "count": 0, "revenue": 0.0}
        emp_counts[name]["count"] += 1
        emp_counts[name]["revenue"] += b.service.prix_eur if b.service else 0.0
    by_employee = sorted(emp_counts.values(), key=lambda x: x["count"], reverse=True)

    return {
        "period_label": month_start.strftime("%B %Y"),
        "rdv_count": rdv_count,
        "revenue_eur": round(revenue_eur, 2),
        "upcoming_count": upcoming_count,
        "pending_callbacks": pending_callbacks,
        "top_services": top_services,
        "by_employee": by_employee,
    }


@router.get("/bookings", response_model=list[BookingOut])
async def list_bookings(
    days: int = Query(30, ge=1, le=365),
    past: bool = Query(False, description="If true, return past bookings instead of upcoming"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> list[BookingOut]:
    """
    Return bookings for the admin dashboard.
    - past=false (default): next `days` days, non-cancelled, oldest first.
    - past=true:            last `days` days, all statuses, newest first.
    """
    now = datetime.now()

    if past:
        since = now - timedelta(days=days)
        result = await db.execute(
            select(Booking)
            .options(selectinload(Booking.service), selectinload(Booking.employee))
            .where(Booking.tenant_id == tenant.id)
            .where(Booking.start_time >= since)
            .where(Booking.start_time < now)
            .order_by(Booking.start_time.desc())
        )
    else:
        until = now + timedelta(days=days)
        result = await db.execute(
            select(Booking)
            .options(selectinload(Booking.service), selectinload(Booking.employee))
            .where(Booking.tenant_id == tenant.id)
            .where(Booking.start_time >= now)
            .where(Booking.start_time <= until)
            .where(Booking.status != BookingStatus.cancelled)
            .order_by(Booking.start_time)
        )

    bookings = result.scalars().all()
    return [_booking_to_out(b) for b in bookings]


# ── Settings ──────────────────────────────────────────────────

@router.get("/settings")
async def get_settings(
    tenant: Tenant = Depends(get_current_tenant),
) -> list[dict]:
    """Return all editable runtime settings with their current values (sensitive fields masked)."""
    return get_settings_with_values(tenant.id)


@router.patch("/settings")
async def patch_settings(
    body: dict[str, str],
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> list[dict]:
    """Update one or more runtime settings. Changes take effect immediately."""
    if not body:
        raise HTTPException(status_code=400, detail="Aucun paramètre fourni.")
    await update_settings(db, tenant.id, body)
    return get_settings_with_values(tenant.id)


# ── Voice Sessions ─────────────────────────────────────────────

@router.get("/sessions", response_model=list[VoiceSessionOut])
async def list_sessions(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> list[VoiceSessionOut]:
    """Return the 50 most recent voice call sessions, newest first."""
    result = await db.execute(
        select(VoiceSession)
        .where(VoiceSession.tenant_id == tenant.id)
        .order_by(VoiceSession.last_activity.desc())
        .limit(50)
    )
    sessions = result.scalars().all()
    return [VoiceSessionOut.model_validate(s) for s in sessions]


@router.get("/sessions/{session_id}")
async def get_session_detail(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> dict:
    """Return a voice session with its full turn-by-turn transcript."""
    result = await db.execute(
        select(VoiceSession).where(
            VoiceSession.session_id == session_id,
            VoiceSession.tenant_id == tenant.id,
        )
    )
    session = result.scalars().first()
    if session is None:
        raise HTTPException(status_code=404, detail="Session introuvable.")
    transcript = await get_transcript_events(db, session_id)
    out = VoiceSessionOut.model_validate(session).model_dump()
    out["transcript"] = transcript
    return out
