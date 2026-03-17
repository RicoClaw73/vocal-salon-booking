"""
Local telephony event simulator (Phase 5.1).

Maps standard telephony call events to ``/voice/turn`` API payloads.
No external telephony account required — this module generates the
HTTP requests that a real telephony bridge (Twilio, Vonage, Vapi, etc.)
would produce.

Usage::

    # Programmatic
    from app.telephony_simulator import TelephonySimulator
    sim = TelephonySimulator(base_url="http://localhost:8000")
    events = sim.scenario_booking_flow()
    results = await sim.run(events)

    # CLI
    python -m app.telephony_simulator --scenario booking

Events follow a simplified telephony lifecycle:
  call.started → utterance (×N) → call.ended

Each event is translated to one or more ``/api/v1/voice/*`` requests.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TelephonyEvent(str, Enum):
    """Telephony call lifecycle events."""
    call_started = "call.started"
    utterance = "utterance"
    dtmf = "dtmf"
    silence_timeout = "silence_timeout"
    call_ended = "call.ended"


@dataclass
class CallEvent:
    """A single telephony event in a simulated call flow."""
    event: TelephonyEvent
    payload: dict = field(default_factory=dict)
    description: str = ""


# ── Pre-built scenarios ────────────────────────────────────────


def scenario_booking_flow() -> list[CallEvent]:
    """Full booking scenario: call → greet → book a coupe → confirm → hang up."""
    return [
        CallEvent(
            event=TelephonyEvent.call_started,
            payload={
                "caller_number": "+33612345678",
                "caller_name": "Marie Dupont",
                "channel": "phone",
            },
            description="Incoming call from Marie Dupont",
        ),
        CallEvent(
            event=TelephonyEvent.utterance,
            payload={"transcript": "Bonjour, je voudrais prendre rendez-vous pour une coupe femme"},
            description="Caller requests a haircut",
        ),
        CallEvent(
            event=TelephonyEvent.utterance,
            payload={"transcript": "Le 2025-06-15 à 10h00 s'il vous plaît"},
            description="Caller provides date and time",
        ),
        CallEvent(
            event=TelephonyEvent.call_ended,
            payload={"reason": "user_hangup"},
            description="Caller hangs up after confirmation",
        ),
    ]


def scenario_cancel_flow() -> list[CallEvent]:
    """Cancel scenario: call → cancel booking #1 → confirm → hang up."""
    return [
        CallEvent(
            event=TelephonyEvent.call_started,
            payload={"caller_number": "+33698765432", "channel": "phone"},
            description="Incoming call to cancel",
        ),
        CallEvent(
            event=TelephonyEvent.utterance,
            payload={"transcript": "Je voudrais annuler ma réservation numéro 1"},
            description="Caller requests cancellation",
        ),
        CallEvent(
            event=TelephonyEvent.call_ended,
            payload={"reason": "user_hangup"},
            description="Caller hangs up",
        ),
    ]


def scenario_fallback_flow() -> list[CallEvent]:
    """Fallback scenario: unintelligible input → fallback → human transfer."""
    return [
        CallEvent(
            event=TelephonyEvent.call_started,
            payload={"channel": "phone"},
            description="Incoming call (no caller ID)",
        ),
        CallEvent(
            event=TelephonyEvent.utterance,
            payload={"transcript": "mmm ahhh eee"},
            description="Unintelligible utterance 1",
        ),
        CallEvent(
            event=TelephonyEvent.utterance,
            payload={"transcript": "blah blah blah"},
            description="Unintelligible utterance 2",
        ),
        CallEvent(
            event=TelephonyEvent.utterance,
            payload={"transcript": "xyz abc 123"},
            description="Unintelligible utterance 3 → should trigger human transfer",
        ),
        CallEvent(
            event=TelephonyEvent.call_ended,
            payload={"reason": "transfer_to_human"},
            description="Session ends after transfer offer",
        ),
    ]


SCENARIOS = {
    "booking": scenario_booking_flow,
    "cancel": scenario_cancel_flow,
    "fallback": scenario_fallback_flow,
}


# ── Event → API payload mapper ────────────────────────────────


def map_event_to_requests(
    event: CallEvent,
    session_id: str | None = None,
) -> list[dict]:
    """
    Convert a ``CallEvent`` into a list of API request specs.

    Each spec is a dict with keys: ``method``, ``path``, ``json``.

    Returns:
        List of request specs (usually one, but call_started returns
        a session-start request).
    """
    if event.event == TelephonyEvent.call_started:
        return [{
            "method": "POST",
            "path": "/api/v1/voice/sessions/start",
            "json": {
                "client_name": event.payload.get("caller_name"),
                "client_phone": event.payload.get("caller_number"),
                "channel": event.payload.get("channel", "phone"),
            },
        }]

    if event.event == TelephonyEvent.utterance:
        return [{
            "method": "POST",
            "path": "/api/v1/voice/turn",
            "json": {
                "session_id": session_id,
                "text": event.payload.get("transcript", ""),
            },
        }]

    if event.event == TelephonyEvent.call_ended:
        if session_id:
            return [{
                "method": "POST",
                "path": "/api/v1/voice/sessions/end",
                "json": {
                    "session_id": session_id,
                    "reason": event.payload.get("reason", "user_hangup"),
                },
            }]
        return []

    if event.event == TelephonyEvent.silence_timeout:
        # Map silence to a "no input" fallback utterance
        return [{
            "method": "POST",
            "path": "/api/v1/voice/turn",
            "json": {
                "session_id": session_id,
                "text": "...",
            },
        }]

    if event.event == TelephonyEvent.dtmf:
        # DTMF digits → text representation
        digits = event.payload.get("digits", "")
        return [{
            "method": "POST",
            "path": "/api/v1/voice/turn",
            "json": {
                "session_id": session_id,
                "text": f"[DTMF: {digits}]",
            },
        }]

    return []


# ── Simulator runner ───────────────────────────────────────────


class TelephonySimulator:
    """
    Runs a sequence of ``CallEvent`` objects against the API.

    Can use either an httpx ``AsyncClient`` (for real HTTP) or a
    FastAPI ``TestClient`` (for in-process testing).
    """

    def __init__(self, client: Any = None, base_url: str = "http://localhost:8000"):
        self._client = client
        self._base_url = base_url

    async def run(self, events: list[CallEvent]) -> list[dict]:
        """Execute the event sequence and return all API responses."""
        results: list[dict] = []
        session_id: str | None = None

        client = self._client
        should_close = False
        if client is None:
            import httpx
            client = httpx.AsyncClient(base_url=self._base_url)
            should_close = True

        try:
            for event in events:
                requests = map_event_to_requests(event, session_id)
                for req_spec in requests:
                    resp = await client.request(
                        method=req_spec["method"],
                        url=req_spec["path"],
                        json=req_spec["json"],
                    )
                    body = resp.json() if resp.status_code < 500 else {"error": resp.text}
                    result_entry = {
                        "event": event.event.value,
                        "description": event.description,
                        "status_code": resp.status_code,
                        "response": body,
                    }
                    results.append(result_entry)

                    # Capture session_id from start response
                    if event.event == TelephonyEvent.call_started and resp.status_code == 201:
                        session_id = body.get("session_id")
        finally:
            if should_close:
                await client.aclose()

        return results


# ── CLI entry point ────────────────────────────────────────────


async def _cli_main(scenario_name: str, base_url: str) -> None:
    factory = SCENARIOS.get(scenario_name)
    if not factory:
        print(f"Unknown scenario: {scenario_name}")
        print(f"Available: {', '.join(SCENARIOS)}")
        raise SystemExit(1)

    events = factory()
    sim = TelephonySimulator(base_url=base_url)
    results = await sim.run(events)
    print(json.dumps(results, indent=2, default=str))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Telephony event simulator")
    parser.add_argument(
        "--scenario", default="booking", choices=list(SCENARIOS),
        help="Scenario to simulate (default: booking)",
    )
    parser.add_argument(
        "--base-url", default="http://localhost:8000",
        help="API base URL (default: http://localhost:8000)",
    )
    args = parser.parse_args()
    asyncio.run(_cli_main(args.scenario, args.base_url))


if __name__ == "__main__":
    main()
