"""
Tests for Phase 4.2 demo flow utilities.

Covers:
  - Scenario loading & structure
  - Orchestrator execution (in-process, no external server)
  - Artifact generation (JSON transcript + Markdown summary)
  - Transcript endpoint
  - Edge cases (unknown scenario, empty steps)

No external credentials required — uses mock providers and in-memory DB.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.demo.scenarios import (
    Scenario,
    ScenarioStep,
    load_scenarios,
    get_scenario_by_id,
    _next_weekday,
    load_scenarios_from_file,
)
from app.demo.orchestrator import DemoOrchestrator, DemoRunResult, save_artifacts


# ── Scenario fixtures tests ───────────────────────────────────


class TestScenarios:
    """Tests for scenario definitions and loaders."""

    def test_load_scenarios_returns_three(self):
        scenarios = load_scenarios()
        assert len(scenarios) == 3

    def test_scenario_ids_are_unique(self):
        scenarios = load_scenarios()
        ids = [s.id for s in scenarios]
        assert len(ids) == len(set(ids))

    def test_all_scenarios_have_steps(self):
        for s in load_scenarios():
            assert len(s.steps) >= 2, f"Scenario '{s.id}' has < 2 steps"

    def test_get_scenario_by_id_found(self):
        s = get_scenario_by_id("happy_path_booking")
        assert s is not None
        assert s.id == "happy_path_booking"

    def test_get_scenario_by_id_not_found(self):
        assert get_scenario_by_id("nonexistent") is None

    def test_scenario_to_dict(self):
        s = load_scenarios()[0]
        d = s.to_dict()
        assert d["id"] == s.id
        assert isinstance(d["steps"], list)
        assert all("user_text" in step for step in d["steps"])

    def test_scenario_has_required_fields(self):
        for s in load_scenarios():
            assert s.title
            assert s.description
            assert s.persona
            assert s.tags

    def test_happy_path_has_booking_expectations(self):
        s = get_scenario_by_id("happy_path_booking")
        assert s is not None
        # Last step should expect booking_created
        assert s.steps[-1].expect_action == "booking_created"

    def test_clarification_starts_with_fallback(self):
        s = get_scenario_by_id("clarification_path")
        assert s is not None
        assert s.steps[0].expect_intent == "unknown"

    def test_cancellation_path_has_cancel_step(self):
        s = get_scenario_by_id("cancellation_path")
        assert s is not None
        cancel_steps = [st for st in s.steps if st.expect_action == "booking_cancelled"]
        assert len(cancel_steps) >= 1

    def test_next_weekday_returns_future_date(self):
        base = date(2026, 3, 10)  # Tuesday
        # Next Saturday from Tuesday
        sat = _next_weekday(5, base)
        assert sat == date(2026, 3, 14)
        assert sat.weekday() == 5

    def test_next_weekday_wraps_around(self):
        base = date(2026, 3, 14)  # Saturday
        # Next Saturday should be the following week
        sat = _next_weekday(5, base)
        assert sat == date(2026, 3, 21)

    def test_scenario_dates_are_in_future(self):
        today = date.today()
        for s in load_scenarios():
            for step in s.steps:
                # Check if step text contains a date and it's in the future
                import re
                dates = re.findall(r"\d{4}-\d{2}-\d{2}", step.user_text)
                for d_str in dates:
                    d = date.fromisoformat(d_str)
                    assert d > today, f"Date {d} in scenario '{s.id}' is not in the future"

    def test_load_scenarios_from_file(self, tmp_path: Path):
        data = [
            {
                "id": "custom_test",
                "title": "Custom",
                "description": "Test",
                "persona": "Test Person",
                "tags": ["test"],
                "steps": [
                    {"user_text": "Hello", "description": "greeting"},
                ],
            }
        ]
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps(data), encoding="utf-8")
        loaded = load_scenarios_from_file(fpath)
        assert len(loaded) == 1
        assert loaded[0].id == "custom_test"
        assert len(loaded[0].steps) == 1


# ── Orchestrator tests (in-process) ──────────────────────────


class TestOrchestrator:
    """
    Test the demo orchestrator running in-process against the ASGI app.
    Uses the same test DB fixtures as the rest of the test suite.
    """

    @pytest.fixture
    async def demo_client(self) -> AsyncClient:
        """Async client wired to test DB (same pattern as conftest)."""
        from app.main import app
        from app.database import get_db
        from tests.conftest import _override_get_db

        app.dependency_overrides[get_db] = _override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
        app.dependency_overrides.clear()

    @pytest.fixture
    def orchestrator(self, demo_client: AsyncClient) -> DemoOrchestrator:
        return DemoOrchestrator(http_client=demo_client)

    async def test_run_happy_path(self, orchestrator: DemoOrchestrator):
        result = await orchestrator.run_scenario("happy_path_booking")
        assert result.session_id is not None
        assert result.scenario_id == "happy_path_booking"
        assert len(result.turns) == 3
        assert result.greeting  # Session start returned a greeting
        assert result.goodbye_message  # Session end returned goodbye

    async def test_run_clarification_path(self, orchestrator: DemoOrchestrator):
        result = await orchestrator.run_scenario("clarification_path")
        assert result.session_id is not None
        assert len(result.turns) == 4
        # First turn should be a fallback
        assert result.turns[0].is_fallback is True

    async def test_run_cancellation_path(self, orchestrator: DemoOrchestrator):
        result = await orchestrator.run_scenario("cancellation_path")
        assert result.session_id is not None
        assert len(result.turns) == 2
        # First turn creates a booking
        assert result.turns[0].action_taken == "booking_created"
        # Second turn cancels it
        assert result.turns[1].action_taken == "booking_cancelled"

    async def test_run_unknown_scenario(self, orchestrator: DemoOrchestrator):
        result = await orchestrator.run_scenario("nonexistent_scenario")
        assert result.success is False
        assert "not found" in result.errors[0]

    async def test_run_all_scenarios(self, orchestrator: DemoOrchestrator):
        results = await orchestrator.run_all()
        assert len(results) == 3
        for r in results:
            assert r.session_id is not None
            assert len(r.turns) >= 2

    async def test_turn_records_have_latency(self, orchestrator: DemoOrchestrator):
        result = await orchestrator.run_scenario("happy_path_booking")
        for turn in result.turns:
            assert turn.latency_ms >= 0

    async def test_booking_id_injection(self, orchestrator: DemoOrchestrator):
        """Cancellation scenario injects booking_id from first turn into second."""
        result = await orchestrator.run_scenario("cancellation_path")
        if result.turns[0].action_taken == "booking_created":
            # The cancel step should have the real booking ID injected
            assert "{booking_id}" not in result.turns[1].user_text


# ── Artifact tests ────────────────────────────────────────────


class TestArtifacts:
    """Test JSON transcript and Markdown summary generation."""

    def _make_result(self) -> DemoRunResult:
        return DemoRunResult(
            scenario_id="test_scenario",
            scenario_title="Test Scenario",
            session_id="abc123",
            success=True,
            started_at="2026-03-10T10:00:00Z",
            finished_at="2026-03-10T10:00:05Z",
            total_duration_ms=5000.0,
            greeting="Bonjour !",
            goodbye_message="Au revoir !",
            goodbye_turns=2,
            assertions_passed=3,
            assertions_failed=0,
            turns=[],
        )

    def test_to_json_roundtrip(self):
        result = self._make_result()
        j = result.to_json()
        parsed = json.loads(j)
        assert parsed["scenario_id"] == "test_scenario"
        assert parsed["success"] is True
        assert parsed["total_duration_ms"] == 5000.0

    def test_to_summary_contains_key_info(self):
        result = self._make_result()
        summary = result.to_summary()
        assert "Test Scenario" in summary
        assert "abc123" in summary
        assert "PASS" in summary
        assert "Bonjour" in summary

    def test_to_summary_with_failure(self):
        result = self._make_result()
        result.success = False
        result.errors = ["Something went wrong"]
        summary = result.to_summary()
        assert "FAIL" in summary
        assert "Something went wrong" in summary

    def test_save_artifacts_creates_files(self):
        result = self._make_result()
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = save_artifacts(result, output_dir=tmpdir)
            assert paths["transcript"].exists()
            assert paths["summary"].exists()

            # Verify JSON is valid
            data = json.loads(paths["transcript"].read_text(encoding="utf-8"))
            assert data["scenario_id"] == "test_scenario"

            # Verify Markdown has content
            md = paths["summary"].read_text(encoding="utf-8")
            assert "# Demo Run:" in md


# ── Transcript endpoint tests ─────────────────────────────────


class TestTranscriptEndpoint:
    """Test the GET /voice/sessions/{id}/transcript endpoint."""

    async def test_transcript_after_turns(self, client):
        """Create a session, send turns, then fetch transcript."""
        # Start session
        resp = await client.post(
            "/api/v1/voice/sessions/start",
            json={"client_name": "Test User", "channel": "demo"},
        )
        assert resp.status_code == 201
        session_id = resp.json()["session_id"]

        # Send a turn
        await client.post(
            "/api/v1/voice/turn",
            json={"session_id": session_id, "text": "Je voudrais réserver une coupe homme"},
        )

        # Fetch transcript
        resp = await client.get(f"/api/v1/voice/sessions/{session_id}/transcript")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == session_id
        assert data["status"] == "active"
        assert data["turns"] == 1
        assert data["current_intent"] == "book"
        assert data["client_name"] == "Test User"
        assert data["booking_draft"] is not None

    async def test_transcript_not_found(self, client):
        resp = await client.get("/api/v1/voice/sessions/nonexistent/transcript")
        assert resp.status_code == 404

    async def test_transcript_completed_session(self, client):
        """Transcript is available even after session ends."""
        resp = await client.post(
            "/api/v1/voice/sessions/start",
            json={"channel": "demo"},
        )
        session_id = resp.json()["session_id"]

        # End the session
        await client.post(
            "/api/v1/voice/sessions/end",
            json={"session_id": session_id},
        )

        # Transcript should still work
        resp = await client.get(f"/api/v1/voice/sessions/{session_id}/transcript")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
