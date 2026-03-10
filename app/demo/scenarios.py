"""
Demo scenario definitions — deterministic conversation fixtures.

Each scenario is a sequence of user utterances that exercise a specific
end-to-end flow through the voice pipeline.  Scenarios use relative dates
(computed at runtime) so they stay valid regardless of when the demo runs.

The ``load_scenarios`` helper returns all built-in scenarios; custom ones
can be loaded from a JSON file via ``load_scenarios_from_file``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any


# ── Data model ────────────────────────────────────────────────

@dataclass(frozen=True)
class ScenarioStep:
    """A single user utterance within a demo scenario."""
    user_text: str
    description: str = ""
    expect_intent: str | None = None
    expect_action: str | None = None


@dataclass
class Scenario:
    """A complete demo conversation scenario."""
    id: str
    title: str
    description: str
    persona: str
    tags: list[str] = field(default_factory=list)
    client_name: str | None = None
    client_phone: str | None = None
    steps: list[ScenarioStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Date helpers (deterministic relative dates) ───────────────

def _next_weekday(weekday: int, base: date | None = None) -> date:
    """
    Return the next occurrence of *weekday* (0=Mon … 6=Sun) after *base*.
    Salon is open Tue(1)–Sat(5); this ensures fixtures land on open days.
    """
    base = base or date.today()
    days_ahead = weekday - base.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return base + timedelta(days=days_ahead)


def _next_tuesday(base: date | None = None) -> str:
    return _next_weekday(1, base).isoformat()


def _next_wednesday(base: date | None = None) -> str:
    return _next_weekday(2, base).isoformat()


def _next_thursday(base: date | None = None) -> str:
    return _next_weekday(3, base).isoformat()


def _next_friday(base: date | None = None) -> str:
    return _next_weekday(4, base).isoformat()


def _next_saturday(base: date | None = None) -> str:
    return _next_weekday(5, base).isoformat()


# ── Built-in scenarios ────────────────────────────────────────

def _scenario_happy_path_booking() -> Scenario:
    """
    Scenario 1 — Happy path: simple men's haircut booking.

    Marc books a coupe homme on the next Saturday at 11h00.
    Exercises: session start → book intent → service resolution →
    slot search → booking creation → session end.
    """
    target_date = _next_saturday()
    return Scenario(
        id="happy_path_booking",
        title="Réservation simple — Coupe homme",
        description=(
            "Client books a men's haircut on next Saturday at 11h00. "
            "Full happy path from greeting to booking confirmation."
        ),
        persona="Marc Dupont, 35 ans, client régulier",
        tags=["booking", "happy-path", "coupe"],
        client_name="Marc Dupont",
        client_phone="0612345678",
        steps=[
            ScenarioStep(
                user_text="Bonjour, je voudrais prendre rendez-vous pour une coupe homme",
                description="Initial booking request — triggers book intent + service resolution",
                expect_intent="book",
                expect_action="collecting_info",
            ),
            ScenarioStep(
                user_text=f"Samedi prochain, le {target_date}",
                description="Provide date — still missing time",
                expect_intent="book",
                expect_action="collecting_info",
            ),
            ScenarioStep(
                user_text="À 11h00 s'il vous plaît",
                description="Provide time — all fields present, triggers slot search + booking",
                expect_intent="book",
                expect_action="booking_created",
            ),
        ],
    )


def _scenario_clarification_path() -> Scenario:
    """
    Scenario 2 — Ambiguity / clarification: unknown intent then recovery.

    User starts with vague text, gets fallback, then clarifies into a
    booking with availability check.
    Exercises: fallback strategy → intent recovery → slot listing → booking.
    """
    target_date = _next_thursday()
    return Scenario(
        id="clarification_path",
        title="Parcours clarification — Demande ambiguë",
        description=(
            "User starts with vague text triggering fallback, "
            "then clarifies into a successful booking."
        ),
        persona="Claire Moreau, 28 ans, nouvelle cliente",
        tags=["clarification", "fallback", "booking"],
        client_name="Claire Moreau",
        client_phone="0698765432",
        steps=[
            ScenarioStep(
                user_text="Euh, bonjour, je sais pas trop…",
                description="Vague utterance — should trigger fallback",
                expect_intent="unknown",
                expect_action="fallback",
            ),
            ScenarioStep(
                user_text="Ah oui, je voudrais vérifier les disponibilités pour un brushing",
                description="Clarification — availability check with service keyword",
                expect_intent="check_availability",
                expect_action="need_date",
            ),
            ScenarioStep(
                user_text=f"Le {target_date} s'il vous plaît",
                description="Provide date — triggers slot search",
                expect_intent="check_availability",
            ),
            ScenarioStep(
                user_text=f"Oui, je voudrais réserver le brushing le {target_date} à 10h00",
                description="Switch to booking intent with full details",
                expect_intent="book",
                expect_action="booking_created",
            ),
        ],
    )


def _scenario_cancellation_path() -> Scenario:
    """
    Scenario 3 — Cancellation: book then cancel.

    User first books, then cancels in the same session.
    Exercises: booking → confirmation → cancel by booking ID → session end.
    """
    target_date = _next_friday()
    return Scenario(
        id="cancellation_path",
        title="Réservation puis annulation",
        description=(
            "User books a haircut then immediately cancels it. "
            "Tests the full book-then-cancel lifecycle."
        ),
        persona="Nadia Haddad, 42 ans, cliente régulière",
        tags=["booking", "cancellation", "lifecycle"],
        client_name="Nadia Haddad",
        client_phone="0655443322",
        steps=[
            ScenarioStep(
                user_text=f"Je voudrais réserver une coupe homme le {target_date} à 09h00",
                description="Full booking request with all details in one utterance",
                expect_intent="book",
                expect_action="booking_created",
            ),
            # The cancel step uses a placeholder — orchestrator replaces {booking_id}
            ScenarioStep(
                user_text="Finalement, je voudrais annuler ma réservation #{booking_id}",
                description="Cancel the just-created booking (ID injected by orchestrator)",
                expect_intent="cancel",
                expect_action="booking_cancelled",
            ),
        ],
    )


# ── Public loaders ────────────────────────────────────────────

def load_scenarios() -> list[Scenario]:
    """Return all built-in demo scenarios."""
    return [
        _scenario_happy_path_booking(),
        _scenario_clarification_path(),
        _scenario_cancellation_path(),
    ]


def load_scenarios_from_file(path: str | Path) -> list[Scenario]:
    """Load custom scenarios from a JSON file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    scenarios: list[Scenario] = []
    for item in data:
        steps = [ScenarioStep(**s) for s in item.pop("steps", [])]
        scenarios.append(Scenario(**item, steps=steps))
    return scenarios


def get_scenario_by_id(scenario_id: str) -> Scenario | None:
    """Find a built-in scenario by its ID."""
    for s in load_scenarios():
        if s.id == scenario_id:
            return s
    return None
