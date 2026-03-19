"""
Unit tests for LLM tool executor functions (_exec_cancel_booking,
_exec_reschedule_booking, _exec_create_booking).

These test the DB-side logic directly, independent of OpenAI.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm_conversation import (
    _exec_cancel_booking,
    _exec_create_booking,
    _exec_reschedule_booking,
)
from app.models import Booking, BookingStatus


def _next_tuesday_10h() -> datetime:
    from datetime import date
    today = date.today()
    days_ahead = (1 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    tuesday = today + timedelta(days=days_ahead)
    return datetime(tuesday.year, tuesday.month, tuesday.day, 10, 0)


# ── _exec_cancel_booking ────────────────────────────────────────


class TestExecCancelBooking:

    @pytest.mark.asyncio
    async def test_cancel_missing_id(self, db_session: AsyncSession):
        result = await _exec_cancel_booking({}, db_session)
        assert "manquant" in result

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_booking(self, db_session: AsyncSession):
        result = await _exec_cancel_booking({"booking_id": 99999}, db_session)
        assert "introuvable" in result or "Aucun" in result

    @pytest.mark.asyncio
    async def test_cancel_booking_success(self, db_session: AsyncSession, default_tenant):
        start = _next_tuesday_10h()
        booking = Booking(
            tenant_id=default_tenant.id,
            client_name="Alice",
            service_id="coupe_homme",
            employee_id="emp_02",
            start_time=start,
            end_time=start + timedelta(minutes=30),
            status=BookingStatus.confirmed,
        )
        db_session.add(booking)
        await db_session.commit()
        await db_session.refresh(booking)

        with patch("app.llm_conversation.asyncio.create_task"):
            result = await _exec_cancel_booking(
                {"booking_id": booking.id}, db_session, tenant_id=default_tenant.id
            )

        assert "annulé" in result
        await db_session.refresh(booking)
        assert booking.status == BookingStatus.cancelled

    @pytest.mark.asyncio
    async def test_cancel_already_cancelled(self, db_session: AsyncSession, default_tenant):
        start = _next_tuesday_10h()
        booking = Booking(
            tenant_id=default_tenant.id,
            client_name="Bob",
            service_id="coupe_homme",
            employee_id="emp_02",
            start_time=start + timedelta(hours=2),
            end_time=start + timedelta(hours=2, minutes=30),
            status=BookingStatus.cancelled,
        )
        db_session.add(booking)
        await db_session.commit()
        await db_session.refresh(booking)

        result = await _exec_cancel_booking(
            {"booking_id": booking.id}, db_session, tenant_id=default_tenant.id
        )
        assert "déjà été annulé" in result

    @pytest.mark.asyncio
    async def test_cancel_tenant_isolation(self, db_session: AsyncSession, default_tenant):
        """Booking belonging to another tenant should not be cancellable."""
        start = _next_tuesday_10h()
        booking = Booking(
            tenant_id=default_tenant.id,
            client_name="Carol",
            service_id="coupe_homme",
            employee_id="emp_02",
            start_time=start + timedelta(hours=4),
            end_time=start + timedelta(hours=4, minutes=30),
            status=BookingStatus.confirmed,
        )
        db_session.add(booking)
        await db_session.commit()
        await db_session.refresh(booking)

        # Try to cancel with a different tenant_id
        result = await _exec_cancel_booking(
            {"booking_id": booking.id}, db_session, tenant_id=9999
        )
        assert "Aucun" in result or "introuvable" in result

        # Booking should still be confirmed
        await db_session.refresh(booking)
        assert booking.status == BookingStatus.confirmed


# ── _exec_reschedule_booking ────────────────────────────────────


class TestExecRescheduleBooking:

    @pytest.mark.asyncio
    async def test_reschedule_missing_id(self, db_session: AsyncSession):
        result = await _exec_reschedule_booking({}, db_session)
        assert "manquant" in result

    @pytest.mark.asyncio
    async def test_reschedule_nonexistent(self, db_session: AsyncSession):
        result = await _exec_reschedule_booking(
            {"booking_id": 99999, "new_date": "2099-01-01", "new_time": "10:00"},
            db_session,
        )
        assert "Aucun" in result or "introuvable" in result

    @pytest.mark.asyncio
    async def test_reschedule_invalid_date(self, db_session: AsyncSession, default_tenant):
        start = _next_tuesday_10h()
        booking = Booking(
            tenant_id=default_tenant.id,
            client_name="Dave",
            service_id="coupe_homme",
            employee_id="emp_02",
            start_time=start + timedelta(hours=1),
            end_time=start + timedelta(hours=1, minutes=30),
            status=BookingStatus.confirmed,
        )
        db_session.add(booking)
        await db_session.commit()
        await db_session.refresh(booking)

        result = await _exec_reschedule_booking(
            {"booking_id": booking.id, "new_date": "not-a-date", "new_time": "10:00"},
            db_session,
            tenant_id=default_tenant.id,
        )
        assert "invalide" in result

    @pytest.mark.asyncio
    async def test_reschedule_success(self, db_session: AsyncSession, default_tenant):
        from datetime import date, timedelta as td
        today = date.today()
        days_ahead = (1 - today.weekday()) % 7 or 7
        tuesday1 = today + td(days=days_ahead)
        tuesday2 = tuesday1 + td(days=7)  # next-next Tuesday

        start = datetime(tuesday1.year, tuesday1.month, tuesday1.day, 10, 0)
        new_start = datetime(tuesday2.year, tuesday2.month, tuesday2.day, 11, 0)

        booking = Booking(
            tenant_id=default_tenant.id,
            client_name="Eve",
            service_id="coupe_homme",
            employee_id="emp_02",
            start_time=start,
            end_time=start + td(minutes=30),
            status=BookingStatus.confirmed,
        )
        db_session.add(booking)
        await db_session.commit()
        await db_session.refresh(booking)

        with patch("app.llm_conversation.asyncio.create_task"):
            result = await _exec_reschedule_booking(
                {
                    "booking_id": booking.id,
                    "new_date": new_start.date().isoformat(),
                    "new_time": "11:00",
                },
                db_session,
                tenant_id=default_tenant.id,
            )

        assert "déplacé" in result or "confirmé" in result
        await db_session.refresh(booking)
        assert booking.start_time == new_start

    @pytest.mark.asyncio
    async def test_reschedule_tenant_isolation(self, db_session: AsyncSession, default_tenant):
        """Booking from another tenant must not be reschedulable."""
        from datetime import date, timedelta as td
        today = date.today()
        days_ahead = (1 - today.weekday()) % 7 or 7
        tuesday = today + td(days=days_ahead)
        start = datetime(tuesday.year, tuesday.month, tuesday.day, 14, 0)
        tuesday2 = tuesday + td(days=7)

        booking = Booking(
            tenant_id=default_tenant.id,
            client_name="Frank",
            service_id="coupe_homme",
            employee_id="emp_02",
            start_time=start,
            end_time=start + td(minutes=30),
            status=BookingStatus.confirmed,
        )
        db_session.add(booking)
        await db_session.commit()
        await db_session.refresh(booking)

        result = await _exec_reschedule_booking(
            {
                "booking_id": booking.id,
                "new_date": tuesday2.isoformat(),
                "new_time": "10:00",
            },
            db_session,
            tenant_id=9999,
        )
        assert "Aucun" in result or "introuvable" in result

        # Booking untouched
        await db_session.refresh(booking)
        assert booking.start_time == start


# ── _exec_create_booking tenant_id ────────────────────────────


class TestExecCreateBookingTenantId:

    @pytest.mark.asyncio
    async def test_create_booking_sets_tenant_id(self, db_session: AsyncSession, default_tenant):
        from datetime import date, timedelta as td
        today = date.today()
        days_ahead = (1 - today.weekday()) % 7 or 7
        tuesday = today + td(days=days_ahead)

        with patch("app.llm_conversation.asyncio.create_task"):
            result = await _exec_create_booking(
                {
                    "service_id": "coupe_homme",
                    "employee_id": "emp_02",
                    "date": tuesday.isoformat(),
                    "time": "10:00",
                    "client_name": "Grace",
                },
                db_session,
                tenant_id=default_tenant.id,
            )

        assert "confirmé" in result or "Rendez-vous" in result

        # Extract booking id from response text "#N"
        import re
        m = re.search(r"#(\d+)", result)
        if m:
            from sqlalchemy import select
            from app.models import Booking
            bk = (await db_session.execute(
                select(Booking).where(Booking.id == int(m.group(1)))
            )).scalars().first()
            assert bk is not None
            assert bk.tenant_id == default_tenant.id
