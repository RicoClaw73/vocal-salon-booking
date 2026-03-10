"""Tests for /api/v1/availability/search and slot_engine behaviour."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Booking, BookingStatus
from app.slot_engine import find_available_slots, validate_booking_request

PREFIX = "/api/v1/availability/search"


def _next_tuesday() -> date:
    """Return the next Tuesday (salon is open Tue-Sat)."""
    today = date.today()
    days_ahead = (1 - today.weekday()) % 7  # 1 = Tuesday
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


# ── API endpoint tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_availability_search_basic(client: AsyncClient):
    """Searching for a simple service on a workday returns slots."""
    tuesday = _next_tuesday().isoformat()
    resp = await client.get(
        PREFIX,
        params={"service_id": "coupe_homme", "date": tuesday},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["service_id"] == "coupe_homme"
    assert data["date"] == tuesday
    assert len(data["slots"]) > 0
    # Each slot has start, end, employee
    slot = data["slots"][0]
    assert "start" in slot
    assert "end" in slot
    assert "employee" in slot
    assert "id" in slot["employee"]


@pytest.mark.asyncio
async def test_availability_search_with_preferred_employee(client: AsyncClient):
    """Preferred employee's slots appear first when available."""
    tuesday = _next_tuesday().isoformat()
    resp = await client.get(
        PREFIX,
        params={"service_id": "coupe_homme", "date": tuesday, "employee_id": "emp_02"},
    )
    assert resp.status_code == 200
    data = resp.json()
    if data["slots"]:
        # First slot should be from preferred employee
        assert data["slots"][0]["employee"]["id"] == "emp_02"


@pytest.mark.asyncio
async def test_availability_search_bad_date_format(client: AsyncClient):
    resp = await client.get(
        PREFIX,
        params={"service_id": "coupe_homme", "date": "not-a-date"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_availability_search_unknown_service(client: AsyncClient):
    tuesday = _next_tuesday().isoformat()
    resp = await client.get(
        PREFIX,
        params={"service_id": "nope", "date": tuesday},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["slots"]) == 0
    assert "introuvable" in data["message"]


@pytest.mark.asyncio
async def test_availability_sunday_returns_alternatives(client: AsyncClient):
    """Sunday (closed) should return 0 slots and potentially alternatives."""
    today = date.today()
    days_to_sunday = (6 - today.weekday()) % 7
    if days_to_sunday == 0:
        days_to_sunday = 7
    next_sunday = (today + timedelta(days=days_to_sunday)).isoformat()
    resp = await client.get(
        PREFIX,
        params={"service_id": "coupe_homme", "date": next_sunday},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["slots"]) == 0
    # Should have alternatives on upcoming workdays
    assert len(data["alternatives"]) > 0


# ── Slot engine unit tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_slot_engine_find_slots(db_session: AsyncSession):
    """Direct slot engine call returns valid slots."""
    tuesday = _next_tuesday()
    result = await find_available_slots(db_session, "coupe_homme", tuesday)
    assert len(result["slots"]) > 0
    # All slots should have correct structure
    for slot in result["slots"]:
        start = datetime.fromisoformat(slot["start"])
        end = datetime.fromisoformat(slot["end"])
        assert end > start
        assert (end - start).total_seconds() == 30 * 60  # 30-min service


@pytest.mark.asyncio
async def test_slot_engine_unknown_service(db_session: AsyncSession):
    """Unknown service returns empty with message."""
    result = await find_available_slots(db_session, "fake_service", date.today())
    assert result["slots"] == []
    assert "introuvable" in result["message"]


@pytest.mark.asyncio
async def test_validate_booking_request_ok(db_session: AsyncSession):
    """Valid booking request passes validation."""
    tuesday = _next_tuesday()
    # emp_02 (Karim) works Tuesdays, is competent for coupe_homme
    start = datetime(tuesday.year, tuesday.month, tuesday.day, 10, 0)
    ok, msg, end = await validate_booking_request(
        db_session, "coupe_homme", "emp_02", start
    )
    assert ok is True
    assert end is not None
    assert end > start


@pytest.mark.asyncio
async def test_validate_booking_request_incompetent(db_session: AsyncSession):
    """Employee not competent for service should fail."""
    tuesday = _next_tuesday()
    start = datetime(tuesday.year, tuesday.month, tuesday.day, 10, 0)
    # emp_05 (Amira, apprentice) might not be competent for complex services
    ok, msg, end = await validate_booking_request(
        db_session, "balayage_long", "emp_05", start
    )
    assert ok is False
    assert "compétent" in msg


@pytest.mark.asyncio
async def test_validate_booking_request_conflict(db_session: AsyncSession):
    """Overlapping booking should fail validation."""
    tuesday = _next_tuesday()
    start = datetime(tuesday.year, tuesday.month, tuesday.day, 10, 0)

    # Create an existing booking
    booking = Booking(
        client_name="Test Client",
        service_id="coupe_homme",
        employee_id="emp_02",
        start_time=start,
        end_time=start + timedelta(minutes=30),
        status=BookingStatus.confirmed,
    )
    db_session.add(booking)
    await db_session.commit()

    # Try to book same employee at same time
    ok, msg, _ = await validate_booking_request(
        db_session, "coupe_homme", "emp_02", start
    )
    assert ok is False
