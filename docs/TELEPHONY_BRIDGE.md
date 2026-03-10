# Telephony Bridge Contract (Phase 5.1)

## Overview

This document defines the integration contract between an external telephony
provider (Twilio, Vonage, Vapi, etc.) and the Maison Éclat voice API.

**No external telephony account is required for development.** The local event
simulator (`app/telephony_simulator.py`) generates the same payloads a real
bridge would produce.

---

## Call Lifecycle

```
Telephony Provider              Maison Éclat API
────────────────────            ────────────────
Incoming call ───────────────▶  POST /voice/sessions/start
                                  ◀──── session_id + greeting
Caller speaks ───────────────▶  POST /voice/turn
  (STT transcript)                ◀──── response_text + intent + TTS meta
Caller speaks ───────────────▶  POST /voice/turn
                                  ◀──── response_text + action_taken
  ... (repeat) ...
Call ends ───────────────────▶  POST /voice/sessions/end
                                  ◀──── summary (turns, duration)
```

## Event Mapping

| Telephony Event      | API Endpoint                     | Payload                                  |
|---------------------|----------------------------------|------------------------------------------|
| `call.started`      | `POST /api/v1/voice/sessions/start` | `{client_name, client_phone, channel}`  |
| `utterance`         | `POST /api/v1/voice/turn`        | `{session_id, text}`                     |
| `dtmf`              | `POST /api/v1/voice/turn`        | `{session_id, text: "[DTMF: 123]"}`     |
| `silence_timeout`   | `POST /api/v1/voice/turn`        | `{session_id, text: "..."}`             |
| `call.ended`        | `POST /api/v1/voice/sessions/end`| `{session_id, reason}`                   |

## Request / Response Schemas

### POST /api/v1/voice/sessions/start

**Request:**
```json
{
  "client_name": "Marie Dupont",
  "client_phone": "+33612345678",
  "channel": "phone"
}
```

**Response (201):**
```json
{
  "session_id": "a1b2c3d4e5f6...",
  "status": "active",
  "greeting": "Bonjour et bienvenue chez Maison Éclat ! ...",
  "created_at": "2025-06-15T09:00:00"
}
```

### POST /api/v1/voice/turn

**Request:**
```json
{
  "session_id": "a1b2c3d4e5f6...",
  "text": "Je voudrais prendre rendez-vous pour une coupe"
}
```

**Response (200):**
```json
{
  "session_id": "a1b2c3d4e5f6...",
  "turn_number": 1,
  "intent": "book",
  "confidence": 1.0,
  "response_text": "Pour quelle date souhaitez-vous votre rendez-vous ?",
  "is_fallback": false,
  "booking_draft": {"service_id": "coupe-femme-court", ...},
  "action_taken": "collecting_info",
  "stt_meta": {"format": "wav", "provider": "mock", ...},
  "tts_meta": {"format": "wav", "provider": "mock", ...},
  "provider_errors": null
}
```

### POST /api/v1/voice/sessions/end

**Request:**
```json
{
  "session_id": "a1b2c3d4e5f6...",
  "reason": "user_hangup"
}
```

**Response (200):**
```json
{
  "session_id": "a1b2c3d4e5f6...",
  "status": "completed",
  "message": "Merci d'avoir appelé Maison Éclat. À bientôt !",
  "turns": 3,
  "duration_seconds": 45.2
}
```

## Provider Error Classification (Phase 5.1)

The `provider_errors` field in `/voice/turn` responses reports provider issues:

| Error Kind           | Meaning                                      |
|---------------------|----------------------------------------------|
| `config_missing`    | Credentials absent / provider misconfigured  |
| `provider_timeout`  | Network timeout from real provider           |
| `provider_http_error` | Non-2xx HTTP response from provider        |
| `provider_error`    | Other runtime error from provider            |
| `fallback_used`     | Fell back to mock provider successfully      |

Example:
```json
{
  "provider_errors": [
    {
      "role": "stt",
      "error_kind": "provider_timeout",
      "error_detail": "ReadTimeout: ...",
      "fallback_used": true
    }
  ]
}
```

## Operational Endpoints

| Endpoint                        | Purpose                              |
|---------------------------------|--------------------------------------|
| `GET /api/v1/ops/providers/status` | Provider readiness check          |
| `POST /api/v1/ops/providers/smoke-test` | Smoke test real providers   |
| `GET /api/v1/ops/metrics`       | Counter/latency snapshots            |
| `GET /health`                   | Liveness/readiness probe             |

## Local Simulator

```bash
# Run booking scenario against local server
python -m app.telephony_simulator --scenario booking

# Available scenarios: booking, cancel, fallback
python -m app.telephony_simulator --scenario fallback
```

In tests, use `TelephonySimulator` with the FastAPI test client:
```python
from app.telephony_simulator import TelephonySimulator, scenario_booking_flow

sim = TelephonySimulator(client=async_test_client)
results = await sim.run(scenario_booking_flow())
```

## Bridge Implementation Notes

When building a real telephony bridge:

1. **Session management**: Store `session_id` for the duration of the call.
2. **Audio format**: Send WAV (16kHz, 16-bit, mono) for STT compatibility.
3. **TTS playback**: The API returns `response_text`; bridge must send it
   to TTS or use the returned `tts_meta` for pre-synthesised audio.
4. **Timeouts**: Set a silence timeout (e.g. 10s) and send `silence_timeout`
   events to trigger a prompt.
5. **DTMF**: Forward DTMF digits as `[DTMF: 123]` text in `/voice/turn`.
6. **Error handling**: Check `provider_errors` in responses; log and alert
   if `fallback_used` is `true` in production.
