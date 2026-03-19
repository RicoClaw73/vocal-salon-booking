"""
SQLAlchemy ORM models for Maison Éclat salon.

Tables:
  - services              Catalogue of salon offerings
  - employees             Staff with schedules and competencies
  - employee_competencies M2M link between employees and services
  - bookings              Client appointments
  - voice_sessions        Persistent voice conversation sessions (Phase 4.3)
  - transcript_events     Per-turn transcript log for voice sessions (Phase 4.3)
  - salon_settings        Runtime-editable key-value settings (overrides env vars)

Design choices
--------------
* IDs are VARCHAR based on the JSON seed IDs (e.g. "coupe_femme_court").
  This keeps the data human-readable and aligned with the n8n tool payloads.
* Booking uses an auto-increment integer PK for simplicity (clients
  reference a booking number, not a UUID).
* Schedules & pauses are stored as JSON columns – they are read-only
  reference data, not queried with WHERE clauses.
* Voice session state is stored as JSON columns for flexibility;
  the schema mirrors the in-memory ConversationState dataclass.
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


class CallbackRequestStatus(str, enum.Enum):
    pending = "pending"
    called_back = "called_back"
    resolved = "resolved"


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
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # relationships
    employee: Mapped["Employee"] = relationship(back_populates="bookings")
    service: Mapped["Service"] = relationship(back_populates="bookings")


# ── Voice Session (Phase 4.3) ──────────────────────────────

class VoiceSession(Base):
    """
    Persistent voice conversation session.

    Mirrors the fields of the in-memory ConversationState dataclass so that
    session state survives process restarts.  Mutable fields (status, turns,
    current_intent, booking_draft) are updated in-place on every turn.
    """
    __tablename__ = "voice_sessions"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    current_intent: Mapped[str | None] = mapped_column(String(30), nullable=True)
    booking_draft_json: Mapped[str] = mapped_column(Text, default="{}")
    turns: Mapped[int] = mapped_column(Integer, default=0)
    client_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    client_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    channel: Mapped[str] = mapped_column(String(20), default="phone")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_activity: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # LLM conversation history — JSON array of OpenAI messages dicts
    messages_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # relationships
    events: Mapped[list["TranscriptEvent"]] = relationship(
        back_populates="session",
        order_by="TranscriptEvent.turn_number",
        cascade="all, delete-orphan",
    )


# ── Callback Request (voicemail) ───────────────────────────

class CallbackRequest(Base):
    """
    Voice message left by a caller when the bot cannot resolve their request.

    The caller records a message; it is transcribed (Whisper) and shown in
    the admin dashboard so the salon can call them back.
    """
    __tablename__ = "callback_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    caller_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    recording_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    recording_duration: Mapped[int | None] = mapped_column(Integer, nullable=True)  # seconds
    transcription: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[CallbackRequestStatus] = mapped_column(
        Enum(CallbackRequestStatus), default=CallbackRequestStatus.pending
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class TranscriptEvent(Base):
    """
    Single turn in a voice conversation transcript.

    Stored per-turn so the full transcript can be reconstructed after a
    process restart.  Both user input and assistant response are captured.
    """
    __tablename__ = "transcript_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("voice_sessions.session_id"), index=True,
    )
    turn_number: Mapped[int] = mapped_column(Integer)

    # User side
    user_text: Mapped[str] = mapped_column(Text, default="")
    intent: Mapped[str | None] = mapped_column(String(30), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Assistant side
    response_text: Mapped[str] = mapped_column(Text, default="")
    action_taken: Mapped[str | None] = mapped_column(String(60), nullable=True)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # relationships
    session: Mapped["VoiceSession"] = relationship(back_populates="events")


# ── Runtime Settings ─────────────────────────────────────────

class SalonSetting(Base):
    """
    Key-value store for runtime-editable settings.
    Overrides env vars loaded at startup via settings_service.load_settings_from_db().
    """
    __tablename__ = "salon_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
