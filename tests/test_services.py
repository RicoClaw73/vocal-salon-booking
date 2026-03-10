"""Tests for /api/v1/services endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

PREFIX = "/api/v1/services"


@pytest.mark.asyncio
async def test_list_services(client: AsyncClient):
    resp = await client.get(PREFIX)
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] > 0
    assert len(data["services"]) == data["count"]
    # Check a known service exists
    ids = [s["id"] for s in data["services"]]
    assert "coupe_femme_long" in ids


@pytest.mark.asyncio
async def test_list_services_filter_category(client: AsyncClient):
    resp = await client.get(PREFIX, params={"category": "coupe"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] > 0
    for s in data["services"]:
        assert s["category_id"] == "coupe"


@pytest.mark.asyncio
async def test_list_services_filter_genre(client: AsyncClient):
    resp = await client.get(PREFIX, params={"genre": "M"})
    assert resp.status_code == 200
    data = resp.json()
    for s in data["services"]:
        assert s["genre"] == "M"


@pytest.mark.asyncio
async def test_get_service_found(client: AsyncClient):
    resp = await client.get(f"{PREFIX}/coupe_homme")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "coupe_homme"
    assert data["duree_min"] > 0
    assert data["prix_eur"] > 0


@pytest.mark.asyncio
async def test_get_service_not_found(client: AsyncClient):
    resp = await client.get(f"{PREFIX}/service_inexistant")
    assert resp.status_code == 404
