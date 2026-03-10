"""
Demo orchestrator — runs scenario fixtures against the local API.

Executes a full conversation scenario end-to-end:
  session_start → N × voice_turn → session_end

Produces structured artifacts per run:
  - JSON transcript (every request/response pair)
  - Human-readable summary (Markdown-formatted)

Usage (as a library):
    from app.demo.orchestrator import DemoOrchestrator
    orch = DemoOrchestrator(base_url="http://localhost:8000")
    result = await orch.run_scenario("happy_path_booking")

Usage (CLI):
    python -m app.demo.orchestrator                  # run all scenarios
    python -m app.demo.orchestrator happy_path_booking  # run one
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.demo.scenarios import Scenario, ScenarioStep, load_scenarios, get_scenario_by_id

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

# ── Result data models ────────────────────────────────────────


@dataclass
class TurnRecord:
    """Record of a single conversation turn."""
    turn_number: int
    user_text: str
    step_description: str
    response_text: str
    intent: str
    confidence: float
    is_fallback: bool
    action_taken: str | None
    booking_draft: dict | None
    data: dict | None
    latency_ms: float
    # Assertions
    expected_intent: str | None = None
    expected_action: str | None = None
    intent_match: bool | None = None
    action_match: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DemoRunResult:
    """Complete result of running a demo scenario."""
    scenario_id: str
    scenario_title: str
    session_id: str | None = None
    success: bool = True
    started_at: str = ""
    finished_at: str = ""
    total_duration_ms: float = 0.0
    turns: list[TurnRecord] = field(default_factory=list)
    greeting: str = ""
    goodbye_message: str = ""
    goodbye_turns: int = 0
    errors: list[str] = field(default_factory=list)
    assertions_passed: int = 0
    assertions_failed: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False, default=str)

    def to_summary(self) -> str:
        """Human-readable Markdown summary of the demo run."""
        lines: list[str] = []
        status = "✅ PASS" if self.success else "❌ FAIL"
        lines.append(f"# Demo Run: {self.scenario_title}")
        lines.append(f"**Status**: {status}")
        lines.append(f"**Scenario**: `{self.scenario_id}`")
        lines.append(f"**Session**: `{self.session_id}`")
        lines.append(f"**Duration**: {self.total_duration_ms:.0f}ms")
        lines.append(
            f"**Assertions**: {self.assertions_passed} passed, "
            f"{self.assertions_failed} failed"
        )
        lines.append("")

        if self.greeting:
            lines.append(f"## Greeting")
            lines.append(f"> {self.greeting}")
            lines.append("")

        lines.append("## Conversation Turns")
        lines.append("")
        for t in self.turns:
            intent_icon = "✅" if t.intent_match is not False else "❌"
            action_icon = "✅" if t.action_match is not False else "❌"
            lines.append(f"### Turn {t.turn_number}: {t.step_description}")
            lines.append(f"- **User**: {t.user_text}")
            lines.append(f"- **Agent**: {t.response_text[:200]}{'…' if len(t.response_text) > 200 else ''}")
            lines.append(f"- **Intent**: `{t.intent}` (conf={t.confidence:.1f}) {intent_icon}")
            if t.action_taken:
                lines.append(f"- **Action**: `{t.action_taken}` {action_icon}")
            lines.append(f"- **Latency**: {t.latency_ms:.0f}ms")
            if t.is_fallback:
                lines.append(f"- ⚠️ *Fallback triggered*")
            lines.append("")

        if self.goodbye_message:
            lines.append("## Session End")
            lines.append(f"> {self.goodbye_message}")
            lines.append(f"Total turns processed: {self.goodbye_turns}")
            lines.append("")

        if self.errors:
            lines.append("## Errors")
            for e in self.errors:
                lines.append(f"- ❌ {e}")
            lines.append("")

        return "\n".join(lines)


# ── Orchestrator ──────────────────────────────────────────────


class DemoOrchestrator:
    """
    Executes demo scenarios against a running (or in-process) API.

    Accepts either:
      - ``base_url`` for a live server (e.g. "http://localhost:8000")
      - ``http_client`` for in-process testing (httpx.AsyncClient with ASGI transport)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._external_client = http_client
        self._owns_client = http_client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._external_client:
            return self._external_client
        return httpx.AsyncClient(base_url=self._base_url, timeout=30.0)

    async def _close_client(self, client: httpx.AsyncClient) -> None:
        if self._owns_client:
            await client.aclose()

    # ── Public API ────────────────────────────────────────────

    async def run_scenario(self, scenario: str | Scenario) -> DemoRunResult:
        """
        Run a single scenario end-to-end.

        *scenario* can be a Scenario object or a scenario ID string.
        """
        if isinstance(scenario, str):
            sc = get_scenario_by_id(scenario)
            if not sc:
                result = DemoRunResult(scenario_id=scenario, scenario_title="Unknown")
                result.success = False
                result.errors.append(f"Scenario '{scenario}' not found")
                return result
        else:
            sc = scenario

        result = DemoRunResult(
            scenario_id=sc.id,
            scenario_title=sc.title,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        client = await self._get_client()
        try:
            await self._execute_scenario(client, sc, result)
        except Exception as exc:
            result.success = False
            result.errors.append(f"Unexpected error: {exc}")
            logger.exception("Demo scenario '%s' failed", sc.id)
        finally:
            result.finished_at = datetime.now(timezone.utc).isoformat()
            await self._close_client(client)

        return result

    async def run_all(self) -> list[DemoRunResult]:
        """Run all built-in scenarios and return results."""
        results: list[DemoRunResult] = []
        for sc in load_scenarios():
            result = await self.run_scenario(sc)
            results.append(result)
        return results

    # ── Internal execution ────────────────────────────────────

    async def _execute_scenario(
        self,
        client: httpx.AsyncClient,
        scenario: Scenario,
        result: DemoRunResult,
    ) -> None:
        t0 = time.monotonic()

        # 1. Start session
        start_resp = await client.post(
            f"{API_PREFIX}/voice/sessions/start",
            json={
                "client_name": scenario.client_name,
                "client_phone": scenario.client_phone,
                "channel": "demo",
            },
        )
        if start_resp.status_code != 201:
            result.success = False
            result.errors.append(
                f"Session start failed: {start_resp.status_code} — {start_resp.text}"
            )
            return

        start_data = start_resp.json()
        session_id = start_data["session_id"]
        result.session_id = session_id
        result.greeting = start_data.get("greeting", "")

        # Track booking_id for injection into later steps
        last_booking_id: int | None = None

        # 2. Execute turns
        for i, step in enumerate(scenario.steps):
            user_text = step.user_text
            # Inject booking_id if placeholder present
            if last_booking_id is not None:
                user_text = user_text.replace("{booking_id}", str(last_booking_id))

            t_turn = time.monotonic()
            turn_resp = await client.post(
                f"{API_PREFIX}/voice/turn",
                json={
                    "session_id": session_id,
                    "text": user_text,
                },
            )
            latency_ms = (time.monotonic() - t_turn) * 1000

            if turn_resp.status_code != 200:
                result.success = False
                result.errors.append(
                    f"Turn {i + 1} failed: {turn_resp.status_code} — {turn_resp.text}"
                )
                continue

            td = turn_resp.json()

            # Extract booking_id from response data
            if td.get("data") and td["data"].get("booking_id"):
                last_booking_id = td["data"]["booking_id"]

            # Build turn record
            record = TurnRecord(
                turn_number=td.get("turn_number", i + 1),
                user_text=user_text,
                step_description=step.description,
                response_text=td.get("response_text", ""),
                intent=td.get("intent", "unknown"),
                confidence=td.get("confidence", 0.0),
                is_fallback=td.get("is_fallback", False),
                action_taken=td.get("action_taken"),
                booking_draft=td.get("booking_draft"),
                data=td.get("data"),
                latency_ms=latency_ms,
                expected_intent=step.expect_intent,
                expected_action=step.expect_action,
            )

            # Check assertions
            if step.expect_intent is not None:
                record.intent_match = record.intent == step.expect_intent
                if record.intent_match:
                    result.assertions_passed += 1
                else:
                    result.assertions_failed += 1
                    result.errors.append(
                        f"Turn {i + 1}: expected intent '{step.expect_intent}', "
                        f"got '{record.intent}'"
                    )

            if step.expect_action is not None:
                record.action_match = record.action_taken == step.expect_action
                if record.action_match:
                    result.assertions_passed += 1
                else:
                    result.assertions_failed += 1
                    result.errors.append(
                        f"Turn {i + 1}: expected action '{step.expect_action}', "
                        f"got '{record.action_taken}'"
                    )

            result.turns.append(record)

        # 3. End session
        end_resp = await client.post(
            f"{API_PREFIX}/voice/sessions/end",
            json={"session_id": session_id, "reason": "demo_complete"},
        )
        if end_resp.status_code == 200:
            end_data = end_resp.json()
            result.goodbye_message = end_data.get("message", "")
            result.goodbye_turns = end_data.get("turns", 0)
        else:
            result.errors.append(
                f"Session end failed: {end_resp.status_code} — {end_resp.text}"
            )

        result.total_duration_ms = (time.monotonic() - t0) * 1000

        # Mark overall success based on assertion failures
        if result.assertions_failed > 0:
            result.success = False


# ── Artifact persistence ──────────────────────────────────────

def save_artifacts(result: DemoRunResult, output_dir: str | Path = "demo_output") -> dict[str, Path]:
    """
    Save JSON transcript + Markdown summary to *output_dir*.

    Returns dict with 'transcript' and 'summary' paths.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{result.scenario_id}_{ts}"

    transcript_path = out / f"{base}_transcript.json"
    transcript_path.write_text(result.to_json(), encoding="utf-8")

    summary_path = out / f"{base}_summary.md"
    summary_path.write_text(result.to_summary(), encoding="utf-8")

    return {"transcript": transcript_path, "summary": summary_path}
