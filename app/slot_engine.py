"""
Slot engine – multi-employee availability with conflict detection.

Core algorithm:
1. Find employees competent for the requested service
2. For each employee, compute working windows on the target date
   (respecting personal schedule, lunch break)
3. Subtract existing bookings + buffers
4. Generate 15-min-granularity slots where the service fits
5. If no slots found, search adjacent dates for alternatives

All times are naive datetimes (salon is single-timezone: Europe/Paris).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Booking, BookingStatus, Employee, EmployeeCompetency, Service

logger = logging.getLogger(__name__)

# Day-name mapping (JSON uses French day names)
_WEEKDAY_FR = {
    0: "lundi",
    1: "mardi",
    2: "mercredi",
    3: "jeudi",
    4: "vendredi",
    5: "samedi",
    6: "dimanche",
}


def _parse_time(t: str) -> time:
    h, m = t.split(":")
    return time(int(h), int(m))


def _dt(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


# ── Public API ───────────────────────────────────────────────


async def find_available_slots(
    session: AsyncSession,
    service_id: str,
    target_date: date,
    preferred_employee_id: str | None = None,
    tenant_id: int | None = None,
) -> dict:
    """
    Return available slots for a service on a given date.

    Returns dict with keys:
      - slots: list of {start, end, employee} on the target date
      - alternatives: list of up to MAX_ALTERNATIVE_SLOTS on nearby dates
        (populated only when slots is empty)
      - message: human-readable status
    """
    # 1. Load service
    svc_query = select(Service).where(Service.id == service_id)
    if tenant_id is not None:
        svc_query = svc_query.where(Service.tenant_id == tenant_id)
    svc_result = await session.execute(svc_query)
    service = svc_result.scalars().first()
    if not service:
        return {"slots": [], "alternatives": [], "message": f"Service '{service_id}' introuvable."}

    duration = timedelta(minutes=service.duree_min)
    buffer = timedelta(
        minutes=settings.CHEMICAL_BUFFER_MIN if service.is_chemical else settings.DEFAULT_BUFFER_MIN
    )

    # 2. Find competent employees
    employees = await _get_competent_employees(session, service_id, preferred_employee_id, tenant_id)
    if not employees:
        return {
            "slots": [],
            "alternatives": [],
            "message": f"Aucun employé compétent pour '{service.label}'.",
        }

    # 3. Compute slots for target date
    slots = await _compute_slots_for_date(
        session, employees, target_date, duration, buffer, service_id
    )

    # 4. If preferred employee requested, sort their slots first
    if preferred_employee_id:
        slots.sort(key=lambda s: (s["employee"]["id"] != preferred_employee_id, s["start"]))
    else:
        slots.sort(key=lambda s: s["start"])

    # 5. If empty, find alternatives on nearby dates
    alternatives: list[dict] = []
    message = None
    if not slots:
        alternatives = await _find_alternative_slots(
            session, employees, target_date, duration, buffer, service_id
        )
        if alternatives:
            message = (
                f"Aucun créneau disponible le {target_date.isoformat()}. "
                f"Voici {len(alternatives)} alternative(s) sur les jours suivants."
            )
        else:
            message = (
                f"Aucun créneau disponible le {target_date.isoformat()} "
                "ni dans les 7 jours suivants."
            )
    else:
        message = f"{len(slots)} créneau(x) disponible(s) le {target_date.isoformat()}."

    return {"slots": slots, "alternatives": alternatives, "message": message}


async def check_booking_conflict(
    session: AsyncSession,
    employee_id: str,
    start: datetime,
    end: datetime,
    exclude_booking_id: int | None = None,
    tenant_id: int | None = None,
) -> bool:
    """Return True if the employee already has an overlapping booking."""
    query = select(Booking).where(
        and_(
            Booking.employee_id == employee_id,
            Booking.status == BookingStatus.confirmed,
            Booking.start_time < end,
            Booking.end_time > start,
        )
    )
    if tenant_id is not None:
        query = query.where(Booking.tenant_id == tenant_id)
    if exclude_booking_id is not None:
        query = query.where(Booking.id != exclude_booking_id)
    result = await session.execute(query)
    return result.scalars().first() is not None


async def validate_booking_request(
    session: AsyncSession,
    service_id: str,
    employee_id: str,
    start_time: datetime,
    exclude_booking_id: int | None = None,
    tenant_id: int | None = None,
) -> tuple[bool, str, datetime | None]:
    """
    Validate a booking request against all business rules.

    Returns (ok, message, end_time).
    """
    # Service exists?
    svc_q = select(Service).where(Service.id == service_id)
    if tenant_id is not None:
        svc_q = svc_q.where(Service.tenant_id == tenant_id)
    svc_r = await session.execute(svc_q)
    service = svc_r.scalars().first()
    if not service:
        return False, f"Service '{service_id}' introuvable.", None

    # Employee exists?
    emp_q = select(Employee).where(Employee.id == employee_id)
    if tenant_id is not None:
        emp_q = emp_q.where(Employee.tenant_id == tenant_id)
    emp_r = await session.execute(emp_q)
    employee = emp_r.scalars().first()
    if not employee:
        return False, f"Employé '{employee_id}' introuvable.", None

    # Competency check
    comp_conditions = [
        EmployeeCompetency.employee_id == employee_id,
        EmployeeCompetency.service_id == service_id,
    ]
    if tenant_id is not None:
        comp_conditions.append(EmployeeCompetency.tenant_id == tenant_id)
    comp = await session.execute(select(EmployeeCompetency).where(*comp_conditions))
    if not comp.scalars().first():
        return False, f"{employee.prenom} n'est pas compétent(e) pour '{service.label}'.", None

    end_time = start_time + timedelta(minutes=service.duree_min)

    # Schedule check
    horaires = json.loads(employee.horaires_json)
    day_name = _WEEKDAY_FR[start_time.weekday()]
    day_sched = horaires.get(day_name)
    if not day_sched:
        return False, f"{employee.prenom} ne travaille pas le {day_name}.", None

    work_start = _parse_time(day_sched["debut"])
    work_end = _parse_time(day_sched["fin"])
    if start_time.time() < work_start or end_time.time() > work_end:
        return (
            False,
            f"Hors des horaires de {employee.prenom} ({day_sched['debut']}-{day_sched['fin']}).",
            None,
        )

    # Lunch break check
    pause = day_sched.get("pause")
    if pause:
        pause_start = _dt(start_time.date(), _parse_time(pause["debut"]))
        pause_end = _dt(start_time.date(), _parse_time(pause["fin"]))
        if start_time < pause_end and end_time > pause_start:
            msg = (
                f"Le créneau chevauche la pause de {employee.prenom} "
                f"({pause['debut']}-{pause['fin']})."
            )
            return False, msg, None

    # Conflict check (with buffer)
    buffer_min = (
        settings.CHEMICAL_BUFFER_MIN if service.is_chemical else settings.DEFAULT_BUFFER_MIN
    )
    buffered_end = end_time + timedelta(minutes=buffer_min)
    has_conflict = await check_booking_conflict(
        session, employee_id, start_time, buffered_end, exclude_booking_id, tenant_id
    )
    if has_conflict:
        return False, f"{employee.prenom} a déjà un rendez-vous sur ce créneau.", None

    # Also check that nothing ends too close before our start
    buffer_before = timedelta(minutes=buffer_min)
    pre_conflict = await _check_pre_buffer_conflict(
        session, employee_id, start_time, buffer_before, exclude_booking_id, tenant_id
    )
    if pre_conflict:
        msg = f"Créneau trop proche d'un RDV précédent (buffer {buffer_min}min requis)."
        return False, msg, None

    return True, "OK", end_time


# ── Internal helpers ─────────────────────────────────────────


async def _get_competent_employees(
    session: AsyncSession,
    service_id: str,
    preferred_employee_id: str | None,
    tenant_id: int | None = None,
) -> list[Employee]:
    """Get employees who can perform the service, preferred first."""
    conditions = [EmployeeCompetency.service_id == service_id]
    if tenant_id is not None:
        conditions.append(EmployeeCompetency.tenant_id == tenant_id)
    query = (
        select(Employee)
        .join(EmployeeCompetency)
        .where(*conditions)
    )
    result = await session.execute(query)
    employees = list(result.scalars().all())

    if preferred_employee_id:
        employees.sort(key=lambda e: e.id != preferred_employee_id)

    return employees


async def _compute_slots_for_date(
    session: AsyncSession,
    employees: list[Employee],
    target_date: date,
    duration: timedelta,
    buffer: timedelta,
    service_id: str,
) -> list[dict]:
    """Generate all valid slots for the given date across all employees."""
    slots = []
    granularity = timedelta(minutes=settings.SLOT_GRANULARITY_MIN)

    for emp in employees:
        horaires = json.loads(emp.horaires_json)
        day_name = _WEEKDAY_FR[target_date.weekday()]
        day_sched = horaires.get(day_name)
        if not day_sched:
            continue  # Employee doesn't work this day

        work_start = _dt(target_date, _parse_time(day_sched["debut"]))
        work_end = _dt(target_date, _parse_time(day_sched["fin"]))

        # Get existing bookings for this employee on this date
        existing = await _get_bookings_for_date(session, emp.id, target_date)

        # Parse lunch break
        pause = day_sched.get("pause")
        pause_start = pause_end = None
        if pause:
            pause_start = _dt(target_date, _parse_time(pause["debut"]))
            pause_end = _dt(target_date, _parse_time(pause["fin"]))

        # Iterate through possible start times
        cursor = work_start
        while cursor + duration <= work_end:
            slot_end = cursor + duration

            # Skip if overlaps lunch
            if pause_start and pause_end:
                if cursor < pause_end and slot_end > pause_start:
                    # Jump to after lunch
                    cursor = pause_end
                    continue

            # Skip if conflicts with existing booking (including buffer)
            if _has_conflict_with_existing(cursor, slot_end, buffer, existing):
                cursor += granularity
                continue

            slots.append({
                "start": cursor.isoformat(),
                "end": slot_end.isoformat(),
                "employee": {
                    "id": emp.id,
                    "prenom": emp.prenom,
                    "nom": emp.nom,
                    "role": emp.role,
                    "niveau": emp.niveau.value if hasattr(emp.niveau, "value") else emp.niveau,
                },
            })
            cursor += granularity

    return slots


async def _find_alternative_slots(
    session: AsyncSession,
    employees: list[Employee],
    original_date: date,
    duration: timedelta,
    buffer: timedelta,
    service_id: str,
) -> list[dict]:
    """Search up to 7 days after original_date for alternatives."""
    alternatives = []
    for offset in range(1, 8):
        alt_date = original_date + timedelta(days=offset)
        # Skip closed days (Sunday=6, Monday=0)
        if alt_date.weekday() in (6, 0):
            continue
        day_slots = await _compute_slots_for_date(
            session, employees, alt_date, duration, buffer, service_id
        )
        for s in day_slots:
            alternatives.append(s)
            if len(alternatives) >= settings.MAX_ALTERNATIVE_SLOTS:
                return alternatives
    return alternatives


async def _get_bookings_for_date(
    session: AsyncSession,
    employee_id: str,
    target_date: date,
    tenant_id: int | None = None,
) -> list[Booking]:
    """Get all confirmed bookings for an employee on a specific date."""
    day_start = datetime.combine(target_date, time(0, 0))
    day_end = datetime.combine(target_date + timedelta(days=1), time(0, 0))
    conditions = and_(
        Booking.employee_id == employee_id,
        Booking.status == BookingStatus.confirmed,
        Booking.start_time >= day_start,
        Booking.start_time < day_end,
    )
    query = select(Booking).where(conditions)
    if tenant_id is not None:
        query = query.where(Booking.tenant_id == tenant_id)
    result = await session.execute(query)
    return list(result.scalars().all())


def _has_conflict_with_existing(
    slot_start: datetime,
    slot_end: datetime,
    buffer: timedelta,
    existing_bookings: list[Booking],
) -> bool:
    """Check if a proposed slot conflicts with any existing booking + buffer."""
    for bk in existing_bookings:
        # The existing booking occupies [bk.start_time, bk.end_time + buffer)
        # Our slot must not overlap with that, and also must respect buffer before
        bk_block_start = bk.start_time - buffer  # need buffer before too
        bk_block_end = bk.end_time + buffer
        if slot_start < bk_block_end and slot_end > bk_block_start:
            return True
    return False


async def _check_pre_buffer_conflict(
    session: AsyncSession,
    employee_id: str,
    start_time: datetime,
    buffer: timedelta,
    exclude_booking_id: int | None = None,
    tenant_id: int | None = None,
) -> bool:
    """Check if a booking ends too close before our proposed start."""
    buffer_window_start = start_time - buffer
    query = select(Booking).where(
        and_(
            Booking.employee_id == employee_id,
            Booking.status == BookingStatus.confirmed,
            Booking.end_time > buffer_window_start,
            Booking.end_time <= start_time,
        )
    )
    if tenant_id is not None:
        query = query.where(Booking.tenant_id == tenant_id)
    if exclude_booking_id is not None:
        query = query.where(Booking.id != exclude_booking_id)
    result = await session.execute(query)
    return result.scalars().first() is not None
