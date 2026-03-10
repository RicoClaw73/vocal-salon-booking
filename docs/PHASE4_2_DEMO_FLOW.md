# Phase 4.2 — End-to-End Demo Flow

> Deterministic, reproducible demo scenarios for Maison Éclat voice assistant.

## Overview

Phase 4.2 adds a demo orchestration layer on top of the Phase 4.1 provider bridge.
It runs scripted conversation scenarios against the local API, producing structured
artifacts (JSON transcripts + human-readable summaries) for each run.

**Key properties:**
- **Local-first** — runs entirely against `localhost`, no external services
- **Deterministic** — same input → same output (mock STT/TTS, regex intent)
- **Reproducible** — dates are computed relative to "today" (always in the future)
- **Operator-friendly** — clean logs, Markdown summaries, assertion reporting

## Architecture

```
┌─────────────────────┐
│ CLI / Test Runner    │  python -m app.demo [scenario_id]
└──────────┬──────────┘
           │
  ┌────────▼────────────┐
  │  DemoOrchestrator    │  Runs scenarios against API
  │  (orchestrator.py)   │  Manages session lifecycle
  └────────┬────────────┘
           │ HTTP calls (in-process or live server)
  ┌────────▼────────────┐
  │  FastAPI App         │  /api/v1/voice/*
  │  + Mock Providers    │  STT → Intent → Handler → TTS
  └─────────────────────┘
           │
  ┌────────▼────────────┐
  │  Artifacts           │  JSON transcript + MD summary
  │  (demo_output/)      │  per scenario per run
  └─────────────────────┘
```

## Built-in Scenarios

| ID | Title | Steps | Tags |
|----|-------|-------|------|
| `happy_path_booking` | Réservation simple — Coupe homme | 3 | booking, happy-path, coupe |
| `clarification_path` | Parcours clarification — Demande ambiguë | 4 | clarification, fallback, booking |
| `cancellation_path` | Réservation puis annulation | 2 | booking, cancellation, lifecycle |

### Scenario 1: Happy Path Booking
Marc books a men's haircut (coupe homme) for next Saturday at 11h00.
Full flow: greeting → service → date → time → booking confirmed.

### Scenario 2: Clarification Path
Claire starts with a vague utterance (triggers fallback), then asks about brushing
availability, then books. Tests fallback recovery + intent switching.

### Scenario 3: Cancellation Path
Nadia books a haircut then immediately cancels it.
Tests booking creation + cancellation + booking_id injection.

## Local Run Commands

### Prerequisites

```bash
# From project root
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Run all demo scenarios (requires live server)

```bash
# Terminal 1: start API server
uvicorn app.main:app --reload

# Terminal 2: run all scenarios
python -m app.demo

# Run a specific scenario
python -m app.demo happy_path_booking

# List available scenarios
python -m app.demo --list

# Custom output directory
python -m app.demo --output ./my_demo_output
```

### Run demos via test suite (no server needed)

```bash
# Run only demo tests (in-process, self-contained)
pytest tests/test_demo.py -v

# Run full suite (164 tests)
pytest tests/ -v
```

### Fetch session transcript during/after a run

```bash
# While the server is running:
curl http://localhost:8000/api/v1/voice/sessions/{session_id}/transcript | python -m json.tool
```

## Expected Output

### Console output (python -m app.demo)

```
# Demo Run: Réservation simple — Coupe homme
**Status**: ✅ PASS
**Scenario**: `happy_path_booking`
**Session**: `a1b2c3d4e5f6`
**Duration**: 150ms
**Assertions**: 4 passed, 0 failed

## Conversation Turns

### Turn 1: Initial booking request
- **User**: Bonjour, je voudrais prendre rendez-vous pour une coupe homme
- **Agent**: Quelle prestation souhaitez-vous ? ...
- **Intent**: `book` (conf=1.0) ✅
...

==================================================
Demo run complete: 3/3 scenarios passed
  ✅ All scenarios passed!
Artifacts saved to: demo_output/
```

### Artifacts produced

Each run creates two files per scenario in `demo_output/`:

| File | Content |
|------|---------|
| `{scenario_id}_{timestamp}_transcript.json` | Full JSON transcript with every turn, latency, assertions |
| `{scenario_id}_{timestamp}_summary.md` | Human-readable Markdown report |

### JSON transcript structure

```json
{
  "scenario_id": "happy_path_booking",
  "scenario_title": "Réservation simple — Coupe homme",
  "session_id": "a1b2c3d4e5f6",
  "success": true,
  "total_duration_ms": 150.0,
  "assertions_passed": 4,
  "assertions_failed": 0,
  "turns": [
    {
      "turn_number": 1,
      "user_text": "...",
      "response_text": "...",
      "intent": "book",
      "confidence": 1.0,
      "action_taken": "collecting_info",
      "latency_ms": 45.2,
      "intent_match": true,
      "action_match": true
    }
  ]
}
```

## Transcript / State Review Endpoint

```
GET /api/v1/voice/sessions/{session_id}/transcript
```

Returns:
```json
{
  "session_id": "a1b2c3d4e5f6",
  "status": "active",
  "current_intent": "book",
  "turns": 3,
  "booking_draft": {
    "service_id": "coupe_homme",
    "date": "2026-03-14",
    "time": "11:00"
  },
  "client_name": "Marc Dupont",
  "duration_seconds": 12.5
}
```

## Test Coverage

28 new tests in `tests/test_demo.py`:
- 14 scenario fixture tests (loading, structure, dates, serialization)
- 7 orchestrator tests (all 3 scenarios + edge cases + booking_id injection)
- 4 artifact tests (JSON roundtrip, Markdown content, file persistence)
- 3 transcript endpoint tests (active, completed, 404)

## File Manifest

| File | Purpose |
|------|---------|
| `app/demo/__init__.py` | Package marker |
| `app/demo/scenarios.py` | 3 scenario fixtures + loaders + date helpers |
| `app/demo/orchestrator.py` | DemoOrchestrator engine + artifact persistence |
| `app/demo/__main__.py` | CLI entrypoint (`python -m app.demo`) |
| `app/routers/voice.py` | +transcript endpoint (`GET /sessions/{id}/transcript`) |
| `tests/test_demo.py` | 28 tests for all demo utilities |
| `docs/PHASE4_2_DEMO_FLOW.md` | This document |

## Known Limitations

1. **In-memory sessions** — transcript endpoint only works within a single process lifetime.
   Sessions are lost on restart. Future: Redis persistence.
2. **Mock audio** — STT/TTS providers return metadata only (no actual audio bytes).
   Real providers activate when credentials are set (`STT_PROVIDER=deepgram`, etc.).
3. **Single-language** — scenarios are French-only. Intent regex supports English but
   scenarios target the French demo persona.
4. **No concurrent scenario runs** — orchestrator is sequential. Parallel execution
   would require session isolation.
5. **Date determinism** — dates are relative to "today", so exact transcript content
   changes daily. The *structure* and *assertions* are stable.
