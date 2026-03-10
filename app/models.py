"""
SQLAlchemy ORM models for Maison Éclat salon.

Tables:
  - services        Catalogue of salon offerings
  - employees       Staff with schedules and competencies
  - employee_competencies  M2M link between employees and services
  - bookings        Client appointments

Design choices
--------------
* IDs are VARCHAR based on the JSON seed IDs (e.g. "coupe_femme_court").
  This keeps the data human-readable and aligned with the n8n tool payloads.
* Booking uses an auto-increment integer PK for simplicity (clients
  reference a booking number, not a UUID).
* Schedules & pauses are stored as JSON columns – they are read-only
  reference data, not queried with WHERE clauses.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── Enums ────────────────────────────────────────────────────

class BookingStatus(str, enum.Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"
    no_show = "no_show"


class EmployeeLevel(str, enum.Enum):
    expert = "expert"
    senior = "senior"
    confirme = "confirme"
    junior = "junior"
    apprenti = "apprenti"


# ── Service ──────────────────────────────────────────────────

class Service(Base):
    __tablename__ = "services"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    category_id: Mapped[str] = mapped_column(String(40))
    category_label: Mapped[str] = mapped_column(String(80))
    label: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    prix_eur: Mapped[float] = mapped_column(Float)
    duree_min: Mapped[int] = mapped_column(Integer)
    genre: Mapped[str] = mapped_column(String(10))  # F, M, mixte
    longueur: Mapped[str] = mapped_column(String(20))  # court, mi-long, long, tout
    is_chemical: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # relationships
    competent_employees: Mapped[list["EmployeeCompetency"]] = relationship(
        back_populates="service"
    )
    bookings: Mapped[list["Booking"]] = relationship(back_populates="service")


# ── Employee ─────────────────────────────────────────────────

class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    prenom: Mapped[str] = mapped_column(String(60))
    nom: Mapped[str] = mapped_column(String(60))
    role: Mapped[str] = mapped_column(String(120))
    anciennete_ans: Mapped[int] = mapped_column(Integer)
    niveau: Mapped[EmployeeLevel] = mapped_column(Enum(EmployeeLevel))
    # JSON-serialised schedule: {"mardi": {"debut": "09:00", ...}, ...}
    horaires_json: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # relationships
    competencies: Mapped[list["EmployeeCompetency"]] = relationship(
        back_populates="employee"
    )
    bookings: Mapped[list["Booking"]] = relationship(back_populates="employee")


class EmployeeCompetency(Base):
    """Many-to-many: which employees can perform which services."""
    __tablename__ = "employee_competencies"

    employee_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("employees.id"), primary_key=True
    )
    service_id: Mapped[str] = mapped_column(
        String(80), ForeignKey("services.id"), primary_key=True
    )

    employee: Mapped["Employee"] = relationship(back_populates="competencies")
    service: Mapped["Service"] = relationship(back_populates="competent_employees")


# ── Booking ──────────────────────────────────────────────────

class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_name: Mapped[str] = mapped_column(String(120))
    client_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)

    employee_id: Mapped[str] = mapped_column(String(20), ForeignKey("employees.id"))
    service_id: Mapped[str] = mapped_column(String(80), ForeignKey("services.id"))

    start_time: Mapped[datetime] = mapped_column(DateTime)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    # end_time = start_time + service duration (no buffer; buffer is between bookings)

    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus), default=BookingStatus.confirmed
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # relationships
    employee: Mapped["Employee"] = relationship(back_populates="bookings")
    service: Mapped["Service"] = relationship(back_populates="bookings")
