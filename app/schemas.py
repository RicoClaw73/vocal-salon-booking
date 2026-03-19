"""
Pydantic v2 schemas for request / response payloads.

Designed to stay stable for n8n integration: field names match the
tool-call payloads described in docs/N8N_WORKFLOW_REDESIGN.md.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# ── Service ──────────────────────────────────────────────────

class ServiceOut(BaseModel):
    id: str
    category_id: str
    category_label: str
    label: str
    description: str
    prix_eur: float
    duree_min: int
    genre: str
    longueur: str
    is_chemical: bool

    model_config = {"from_attributes": True}


class ServiceListOut(BaseModel):
    count: int
    services: list[ServiceOut]


# ── Employee (minimal, for slot results) ─────────────────────

class EmployeeSlim(BaseModel):
    id: str
    prenom: str
    nom: str
    role: str
    niveau: str
    horaires_json: str | None = None

    model_config = {"from_attributes": True}


class EmployeeCreate(BaseModel):
    prenom: str
    nom: str
    role: str = ""
    horaires_json: str = "{}"
    notes: str = ""


class EmployeeUpdate(BaseModel):
    prenom: str | None = None
    nom: str | None = None
    role: str | None = None
    horaires_json: str | None = None
    notes: str | None = None


class ServiceCreate(BaseModel):
    label: str
    category_label: str = "Prestation"
    category_id: str = "general"
    prix_eur: float = 0.0
    duree_min: int = 30
    genre: str = "mixte"
    description: str = ""
    longueur: str = "tout"
    is_chemical: bool = False


class ServiceUpdate(BaseModel):
    label: str | None = None
    category_label: str | None = None
    category_id: str | None = None
    prix_eur: float | None = None
    duree_min: int | None = None
    genre: str | None = None
    description: str | None = None
    longueur: str | None = None
    is_chemical: bool | None = None


# ── Availability ─────────────────────────────────────────────

class AvailabilityQuery(BaseModel):
    """Query params for GET /availability/search (used as query model)."""
    service_id: str = Field(..., description="Service ID from catalogue")
    date: str = Field(..., description="Date YYYY-MM-DD to search")
    employee_id: str | None = Field(None, description="Preferred employee (optional)")


class SlotOut(BaseModel):
    start: str  # ISO datetime
    end: str
    employee: EmployeeSlim


class AvailabilityOut(BaseModel):
    service_id: str
    date: str
    slots: list[SlotOut]
    alternatives: list[SlotOut] = Field(
        default_factory=list,
        description="Up to 3 alternative slots on nearby dates if requested date has none",
    )
    message: str | None = None


# ── Booking ──────────────────────────────────────────────────

class BookingCreate(BaseModel):
    client_name: str = Field(..., min_length=1, max_length=120)
    client_phone: str | None = Field(None, max_length=30)
    service_id: str
    employee_id: str
    start_time: datetime = Field(..., description="ISO 8601 datetime")
    notes: str | None = None


class BookingOut(BaseModel):
    id: int
    client_name: str
    client_phone: str | None
    service_id: str
    service_label: str
    employee_id: str
    employee_name: str
    start_time: datetime
    end_time: datetime
    status: str
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BookingReschedule(BaseModel):
    new_start_time: datetime = Field(..., description="New start ISO 8601 datetime")
    employee_id: str | None = Field(
        None, description="Change employee (optional, keeps current if null)"
    )


class BookingCancelOut(BaseModel):
    id: int
    status: str
    message: str


# ── Health ───────────────────────────────────────────────────

class HealthOut(BaseModel):
    status: str
    version: str
    database: str
