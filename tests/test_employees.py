"""Tests for /api/v1/employees endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

PREFIX = "/api/v1/employees"


@pytest.mark.asyncio
async def test_list_employees(client: AsyncClient):
    resp = await client.get(PREFIX)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 5  # 5 employees in seed data
    # Check required fields
    for emp in data:
        assert "id" in emp
        assert "prenom" in emp
        assert "nom" in emp
        assert "role" in emp
        assert "niveau" in emp


@pytest.mark.asyncio
async def test_get_employee_found(client: AsyncClient):
    resp = await client.get(f"{PREFIX}/emp_01")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "emp_01"
    assert data["prenom"] == "Sophie"


@pytest.mark.asyncio
async def test_get_employee_not_found(client: AsyncClient):
    resp = await client.get(f"{PREFIX}/emp_99")
    assert resp.status_code == 404
