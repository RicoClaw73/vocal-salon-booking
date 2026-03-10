"""Tests for /api/v1/bookings endpoints."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from httpx import AsyncClient

PREFIX = "/api/v1/bookings"


def _next_tuesday() -> date:
    today = date.today()
    days_ahead = (1 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def _booking_payload(hour: int = 10, minute: int = 0) -> dict:
    """Return a valid booking payload for coupe_homme with emp_02 on next Tuesday."""
    tuesday = _next_tuesday()
    start = datetime(tuesday.year, tuesday.month, tuesday.day, hour, minute)
    return {
        "client_name": "Jean Dupont",
        "client_phone": "+33612345678",
        "service_id": "coupe_homme",
        "employee_id": "emp_02",
        "start_time": start.isoformat(),
        "notes": "Test booking",
    }


@pytest.mark.asyncio
async def test_create_booking(client: AsyncClient):
    resp = await client.post(PREFIX, json=_booking_payload())
    assert resp.status_code == 201
    data = resp.json()
    assert data["client_name"] == "Jean Dupont"
    assert data["service_id"] == "coupe_homme"
    assert data["employee_id"] == "emp_02"
    assert data["status"] == "confirmed"
    assert data["id"] >= 1


@pytest.mark.asyncio
async def test_create_booking_conflict(client: AsyncClient):
    """Second booking at same time should fail."""
    payload = _booking_payload()
    resp1 = await client.post(PREFIX, json=payload)
    assert resp1.status_code == 201
    resp2 = await client.post(PREFIX, json=payload)
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_get_booking(client: AsyncClient):
    # Create first
    resp = await client.post(PREFIX, json=_booking_payload())
    booking_id = resp.json()["id"]

    resp = await client.get(f"{PREFIX}/{booking_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == booking_id


@pytest.mark.asyncio
async def test_get_booking_not_found(client: AsyncClient):
    resp = await client.get(f"{PREFIX}/99999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reschedule_booking(client: AsyncClient):
    # Create booking at 10:00
    resp = await client.post(PREFIX, json=_booking_payload(hour=10))
    assert resp.status_code == 201
    booking_id = resp.json()["id"]

    # Reschedule to 15:00
    tuesday = _next_tuesday()
    new_start = datetime(tuesday.year, tuesday.month, tuesday.day, 15, 0)
    resp = await client.patch(
        f"{PREFIX}/{booking_id}",
        json={"new_start_time": new_start.isoformat()},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "15:00" in data["start_time"]


@pytest.mark.asyncio
async def test_cancel_booking(client: AsyncClient):
    resp = await client.post(PREFIX, json=_booking_payload())
    booking_id = resp.json()["id"]

    resp = await client.delete(f"{PREFIX}/{booking_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancelled"

    # Verify it's actually cancelled
    resp = await client.get(f"{PREFIX}/{booking_id}")
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_already_cancelled(client: AsyncClient):
    resp = await client.post(PREFIX, json=_booking_payload())
    booking_id = resp.json()["id"]
    await client.delete(f"{PREFIX}/{booking_id}")

    # Try to cancel again
    resp = await client.delete(f"{PREFIX}/{booking_id}")
    assert resp.status_code == 409
