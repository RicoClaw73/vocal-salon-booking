# Phase 3 — Minimal Viable Voice Loop

Local-first voice orchestration layer for Maison Éclat.
**No paid providers or external credentials required.**

## Architecture

```
┌─────────────┐     ┌──────────┐     ┌───────────┐     ┌──────────┐     ┌──────────┐
│  User Input  │────▶│ Mock STT │────▶│  Intent   │────▶│ Handler  │────▶│ Mock TTS │
│ (text/mock)  │     │ Provider │     │ Extractor │     │ Dispatch │     │ Provider │
└─────────────┘     └──────────┘     └───────────┘     └──────────┘     └──────────┘
                         │                                    │               │
                         ▼                                    ▼               ▼
                    STT metadata                     Booking/Avail/       TTS metadata
                                                     Cancel logic
```

### Endpoint

```
POST /api/v1/voice/turn
```

Single unified endpoint that runs the full voice loop:
1. **Session management** — auto-creates or reuses an existing session
2. **STT** — mock provider (accepts pre-transcribed text)
3. **Intent extraction** — deterministic regex-based NLU
4. **Fallback strategy** — rotating responses for low-confidence/unknown intents
5. **Handler dispatch** — routes to book/reschedule/cancel/availability handlers
6. **TTS** — mock provider returns audio metadata (duration, format, sample rate)

### Provider Abstraction

```python
# app/providers.py

class STTProvider(ABC):
    async def transcribe(audio_bytes, audio_format, language) -> STTResult

class TTSProvider(ABC):
    async def synthesize(text, language, voice_id) -> TTSResult

# Mock implementations included — swap for real providers later:
# - STT: Whisper, Deepgram, Google STT
# - TTS: ElevenLabs, Google TTS, PlayHT
```

### Fallback Strategy

When intent confidence < 0.5 or intent is `unknown`:

| Consecutive unknowns | Behavior |
|---------------------|----------|
| 1st | Fallback message #1 — lists available actions |
| 2nd | Fallback message #2 — provides example utterances |
| 3rd+ | Offers human transfer with phone number |

Counter resets when a valid intent is detected.

## Running Locally

```bash
# Install dependencies
pip install -e ".[dev]"

# Start server
uvicorn app.main:app --reload

# Run tests
pytest tests/ -v
```

## Example curl Requests

### 1. Simple voice turn (auto-creates session)

```bash
curl -X POST http://localhost:8000/api/v1/voice/turn \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Bonjour, je voudrais réserver une coupe",
    "client_name": "Marie Curie",
    "channel": "test"
  }'
```

Response:
```json
{
  "session_id": "a1b2c3d4e5f6",
  "turn_number": 1,
  "intent": "book",
  "confidence": 1.0,
  "response_text": "Quelle prestation souhaitez-vous ? (coupe, couleur, balayage, brushing…)",
  "is_fallback": false,
  "booking_draft": {"service_id": null, "date": null, "time": null, ...},
  "action_taken": "collecting_info",
  "stt_meta": {"format": "wav", "duration_ms": 100, "sample_rate": 16000, "provider": "mock"},
  "tts_meta": {"format": "wav", "duration_ms": 1350, "sample_rate": 22050, "provider": "mock"}
}
```

### 2. Full booking in one turn

```bash
# Replace YYYY-MM-DD with a future Tuesday
curl -X POST http://localhost:8000/api/v1/voice/turn \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Je voudrais réserver une coupe homme le 2025-04-08 à 10h00",
    "client_name": "Jean Dupont",
    "client_phone": "+33612345678"
  }'
```

### 3. Multi-turn conversation

```bash
# Turn 1: start with intent
curl -X POST http://localhost:8000/api/v1/voice/turn \
  -H "Content-Type: application/json" \
  -d '{"text": "Je voudrais prendre rendez-vous"}'
# → returns session_id, asks for service

# Turn 2: provide service (use session_id from turn 1)
curl -X POST http://localhost:8000/api/v1/voice/turn \
  -H "Content-Type: application/json" \
  -d '{"session_id": "SESSION_ID_HERE", "text": "Une coupe pour homme"}'
# → asks for date

# Turn 3: provide date and time
curl -X POST http://localhost:8000/api/v1/voice/turn \
  -H "Content-Type: application/json" \
  -d '{"session_id": "SESSION_ID_HERE", "text": "Le 2025-04-08 à 10h00"}'
# → creates booking or offers slots
```

### 4. Cancel via voice

```bash
curl -X POST http://localhost:8000/api/v1/voice/turn \
  -H "Content-Type: application/json" \
  -d '{"text": "Annuler la réservation #1"}'
```

### 5. Unknown intent → fallback

```bash
curl -X POST http://localhost:8000/api/v1/voice/turn \
  -H "Content-Type: application/json" \
  -d '{"text": "Quel temps fait-il dehors ?"}'
```

Response includes `"is_fallback": true` and `"action_taken": "fallback"`.

### 6. Using mock_transcript (equivalent to text)

```bash
curl -X POST http://localhost:8000/api/v1/voice/turn \
  -H "Content-Type: application/json" \
  -d '{"mock_transcript": "Je veux vérifier les disponibilités pour un balayage"}'
```

### 7. Check availability

```bash
curl -X POST http://localhost:8000/api/v1/voice/turn \
  -H "Content-Type: application/json" \
  -d '{"text": "Quand êtes-vous disponible pour une coupe le 2025-04-08 ?"}'
```

## Files Added/Modified (Phase 3)

| File | Change |
|------|--------|
| `app/providers.py` | **NEW** — STT/TTS provider interfaces + mock implementations |
| `app/voice_schemas.py` | **MODIFIED** — Added `AudioMeta`, `VoiceTurnRequest`, `VoiceTurnResponse` |
| `app/routers/voice.py` | **MODIFIED** — Added `/voice/turn` endpoint + fallback strategy |
| `app/routers/__init__.py` | **MODIFIED** — Added `voice` to exports |
| `tests/test_voice_turn.py` | **NEW** — E2E tests for voice turn orchestration |
| `tests/test_providers.py` | **NEW** — Unit tests for provider abstractions |
| `docs/PHASE3_VOICE_LOOP.md` | **NEW** — This documentation |

## Known Limitations

- **Mock-only providers**: STT/TTS don't process real audio; designed for swap-in
- **In-memory sessions**: Not suitable for multi-process deployments (use Redis)
- **No real audio I/O**: Audio bytes are accepted but not processed by mock STT
- **Single-language**: Hardcoded French responses (i18n ready via `settings.DEFAULT_LANG`)
- **No streaming**: Request/response only; no WebSocket streaming for real-time voice
- **Deterministic NLU**: Regex-based intent extraction (swap for LLM classifier in Phase 4)
