# Vocal-Salon Operations Runbook

Local-first operational guide for the Maison Éclat voice-booking backend.
Covers startup, smoke testing, log/metrics inspection, and common failure recovery.

---

## 1. Starting the Service

```bash
# Install dependencies (first time / after updates)
pip install -e ".[dev]"

# Start with hot-reload (development)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Start without reload (closer to production)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

**Environment configuration** — copy `.env.example` to `.env` and adjust:

| Variable              | Default       | Purpose                                    |
|-----------------------|---------------|--------------------------------------------|
| `DATABASE_URL`        | SQLite file   | Switch to PostgreSQL for production         |
| `VOICE_API_KEY`       | *(empty)*     | Set to enable API key auth on voice routes  |
| `RATE_LIMIT_PER_MINUTE`| 60          | 0 to disable; adjust for expected load      |
| `STT_PROVIDER`        | `mock`        | `deepgram` for real STT (needs API key)     |
| `TTS_PROVIDER`        | `mock`        | `elevenlabs` for real TTS (needs API key)   |
| `TTS_ARTIFACT_DIR`    | `data/tts_artifacts` | Local directory for persisted TTS audio files |

On startup the app will:
1. Create database tables (idempotent)
2. Seed reference data (services, employees, competencies)
3. Log `Seed complete: ...` with counts

---

## 2. Smoke Test

After startup, verify the service is healthy:

```bash
# Health check (should return status: ok, database: ok)
curl -s http://localhost:8000/health | python -m json.tool

# List services (should return count > 0)
curl -s http://localhost:8000/api/v1/services | python -m json.tool

# Start a voice session
curl -s -X POST http://localhost:8000/api/v1/voice/sessions/start \
  -H "Content-Type: application/json" \
  -d '{"channel": "test"}' | python -m json.tool

# Send a voice turn (booking intent)
curl -s -X POST http://localhost:8000/api/v1/voice/turn \
  -H "Content-Type: application/json" \
  -d '{"text": "Je voudrais réserver une coupe femme"}' | python -m json.tool
```

**Expected outcomes:**
- `/health` → `{"status": "ok", "database": "ok", "voice_turns": 0, ...}`
- Voice turn → returns `intent: "book"`, `action_taken: "collecting_info"`

---

## 3. Inspecting Logs

Logs are emitted as structured key-value pairs to stdout (INFO level):

```
INFO app.routers.voice | session_started | request_id=a1b2c3 session_id=abc123 channel=phone
INFO app.routers.voice | voice_turn_processed | request_id=d4e5f6 session_id=abc123 intent=book outcome=collecting_info latency_ms=12.5
INFO app.routers.voice | session_ended | request_id=g7h8i9 session_id=abc123 turns=3 duration_s=45.2
```

**Key log events:**
| Event                        | Meaning                                      |
|------------------------------|----------------------------------------------|
| `session_started`            | New voice session created                     |
| `message_processed`          | User message handled (via /sessions/message)  |
| `voice_turn_processed`       | Full voice turn completed successfully        |
| `voice_turn_fallback`        | Turn fell back to unknown-intent handler      |
| `session_ended`              | Session closed                                |
| `circuit_breaker_tripped`    | Provider circuit breaker opened (Phase 5.2)   |
| `circuit_breaker_closed`     | Provider circuit breaker recovered (Phase 5.2)|
| `provider_stt_fallback`      | STT provider failed, fell back to mock        |
| `provider_tts_fallback`      | TTS provider failed, fell back to mock        |
| `tts_artifact_stored`        | TTS audio file persisted to local store       |

**Filtering tips:**
```bash
# Watch voice logs only
uvicorn app.main:app 2>&1 | grep "app.routers.voice"

# Watch fallbacks
uvicorn app.main:app 2>&1 | grep "fallback"

# Watch rate limit warnings
uvicorn app.main:app 2>&1 | grep "Rate limit"
```

---

## 4. Inspecting Metrics

### Live metrics endpoint
```bash
curl -s http://localhost:8000/api/v1/ops/metrics | python -m json.tool
```

**Response structure:**
```json
{
  "uptime_seconds": 3600.0,
  "started_at": "2025-01-15T10:00:00+00:00",
  "counters": {
    "auth_failures": 0,
    "bookings_cancelled": 1,
    "bookings_created": 5,
    "intent_book": 12,
    "intent_cancel": 2,
    "rate_limit_hits": 0,
    "sessions_completed": 8,
    "sessions_started": 10,
    "voice_fallbacks": 3,
    "voice_turns": 25
  },
  "latencies": {
    "voice_turn_ms": {
      "count": 25,
      "avg_ms": 15.4,
      "min_ms": 3.2,
      "max_ms": 120.5
    }
  }
}
```

**Key metrics to watch:**
- `voice_fallbacks` / `voice_turns` → fallback rate (target: < 20%)
- `auth_failures` → potential unauthorized access attempts
- `rate_limit_hits` → client overloading
- `voice_turn_ms.avg_ms` → response latency (target: < 500ms local)
- `cb_stt_tripped` / `cb_tts_tripped` → circuit breaker trips (provider instability)
- `cb_stt_short_circuit` / `cb_tts_short_circuit` → requests bypassed due to open breaker
- `provider_stt_fallback` / `provider_tts_fallback` → fallback-to-mock events

### Recent sessions
```bash
curl -s "http://localhost:8000/api/v1/ops/sessions/recent?limit=5" | python -m json.tool
```

### Session diagnostics
```bash
curl -s "http://localhost:8000/api/v1/ops/sessions/{session_id}/diag" | python -m json.tool
```

### Failure summary (last 24h)
```bash
curl -s "http://localhost:8000/api/v1/ops/failures/summary?hours=24" | python -m json.tool
```

---

## 5. Common Failure Recovery

### 5.1 Database locked (SQLite)

**Symptom:** `sqlite3.OperationalError: database is locked`

**Cause:** Multiple workers writing to SQLite simultaneously.

**Fix:** Run with a single worker (`--workers 1`) or switch to PostgreSQL.

### 5.2 High fallback rate

**Symptom:** `voice_fallbacks` counter growing rapidly; fallback_rate > 30%.

**Diagnosis:**
```bash
curl -s "http://localhost:8000/api/v1/ops/failures/summary?hours=1" | python -m json.tool
```

**Possible causes:**
- Users speaking outside the supported intent vocabulary
- STT producing garbled transcriptions (check provider config)
- New service types not in the intent keyword list

**Fix:** Review high-fallback sessions via `/ops/sessions/{id}/diag` and update
intent keywords in `app/intent.py` if patterns emerge.

### 5.3 Auth failures spike

**Symptom:** `auth_failures` counter rising.

**Diagnosis:** Check if `VOICE_API_KEY` was rotated without updating clients.

**Fix:** Verify the key in `.env` matches what clients are sending in `X-API-Key`.

### 5.4 Rate limiting too aggressive

**Symptom:** Legitimate requests getting 429 responses.

**Fix:** Increase `RATE_LIMIT_PER_MINUTE` in `.env`, or set to `0` to disable.

### 5.5 Process restart / metrics reset

**Note:** In-memory metrics reset on process restart. This is by design for
local-first deployment. If you need persistent metrics, export the
`/ops/metrics` snapshot to a file periodically:

```bash
# Cron job: snapshot metrics every 5 minutes
*/5 * * * * curl -s http://localhost:8000/api/v1/ops/metrics >> /var/log/salon-metrics.jsonl
```

### 5.6 Session not found after restart

**Symptom:** 404 on session endpoints after process restart.

**Cause:** In-memory session cache lost, but DB state is preserved.

**Fix:** The system automatically loads from DB on next request for that session.
If sessions are still missing, check the database file exists and is not corrupted.

### 5.7 Provider circuit breaker tripped (Phase 5.2)

**Symptom:** `cb_stt_tripped` or `cb_tts_tripped` counters rising; `cb_stt_short_circuit`
/ `cb_tts_short_circuit` growing (requests bypassing the real provider).

**Diagnosis:**
```bash
# Check circuit breaker state
curl -s http://localhost:8000/api/v1/ops/providers/status | python -m json.tool
```

Look at `circuit_breakers.stt.state` and `circuit_breakers.tts.state`:
- `closed` → normal
- `open` → provider is being bypassed; requests go to mock fallback
- `half_open` → probe in progress (one request allowed through to test recovery)

**How it works:**

| Parameter            | Default  | Meaning                                              |
|----------------------|----------|------------------------------------------------------|
| `failure_threshold`  | 3        | Consecutive failures before tripping                 |
| `base_cooldown_s`    | 10s      | Wait before first half-open probe                    |
| `max_cooldown_s`     | 120s     | Cap for exponential backoff                          |
| `backoff_multiplier` | 2.0      | Cooldown grows: 10s → 20s → 40s → 80s → 120s (cap)  |
| `success_threshold`  | 1        | Successes in half-open to close the breaker          |

**Recovery:** The breaker self-heals. Once the cooldown elapses, a single probe
request is sent to the real provider. If it succeeds, the breaker closes and
normal operation resumes. No manual intervention needed unless the underlying
provider is permanently down.

**Manual check:** Run the smoke test to verify provider health:
```bash
curl -s -X POST http://localhost:8000/api/v1/ops/providers/smoke-test | python -m json.tool
```

### 5.8 Provider falling back to mock unexpectedly

**Symptom:** Responses show `"provider": "mock"` in `stt_meta`/`tts_meta` even
though a real provider is configured.

**Possible causes:**
1. API key missing or invalid → check `.env` for `STT_API_KEY` / `TTS_API_KEY`
2. Circuit breaker is open (provider recently failed) → check `/ops/providers/status`
3. Provider quota exhausted → check provider dashboard

---

## 6. Audio Path Usage (Phase 5.2)

### Sending real audio through the voice turn endpoint

The `/voice/turn` endpoint accepts three input modes:

1. **Text-only** (backward compat): `{"text": "Je voudrais réserver..."}`
2. **Mock transcript**: `{"mock_transcript": "..."}`
3. **Real audio** (Phase 5.2): send base64-encoded audio bytes

**Audio mode example:**
```bash
# Encode a WAV file to base64
AUDIO_B64=$(base64 -w0 recording.wav)

curl -s -X POST http://localhost:8000/api/v1/voice/turn \
  -H "Content-Type: application/json" \
  -d "{
    \"audio_base64\": \"$AUDIO_B64\",
    \"audio_format\": \"wav\",
    \"audio_sample_rate\": 16000,
    \"audio_encoding\": \"linear16\"
  }" | python -m json.tool
```

**Supported audio parameters:**

| Field               | Values                                        | Default |
|---------------------|-----------------------------------------------|---------|
| `audio_format`      | wav, mp3, ogg, pcm                            | wav     |
| `audio_sample_rate` | 8000, 16000, 22050, 44100, 48000              | 16000   |
| `audio_encoding`    | linear16, mulaw, alaw, opus, mp3, ogg_vorbis  | —       |
| `audio_content_type`| MIME type (informational only)                 | —       |

**Priority:** When both `text` and `audio_base64` are provided, `text` takes
precedence (audio is ignored). This preserves backward compatibility.

### TTS audio artifact persistence

When a real TTS provider produces audio bytes, they are persisted locally:

```
data/tts_artifacts/
  <session_id>/
    <text_hash>.wav
    <text_hash>.mp3
```

The response includes `tts_audio_url` with a `file://` URI pointing to the
persisted artifact. In mock mode, `tts_audio_url` is `null` (no audio generated).

**Custom artifact directory:**
```bash
export TTS_ARTIFACT_DIR=/var/data/tts_audio
```

---

## 7. Running Tests

```bash
# Full test suite (275+ tests)
uv run python -m pytest tests/ -v

# Only observability / ops tests
uv run python -m pytest tests/test_observability.py tests/test_ops.py -v

# Circuit breaker tests
uv run python -m pytest tests/test_circuit_breaker.py -v

# Audio path tests
uv run python -m pytest tests/test_audio_path.py -v

# TTS artifact store tests
uv run python -m pytest tests/test_tts_artifact_store.py -v

# Quick health check
uv run python -m pytest tests/test_health.py -v
```

---

## 8. Monitoring Checklist (Daily Pilot)

- [ ] `/health` returns `status: ok`, `database: ok`
- [ ] Fallback rate < 20% (`/ops/metrics` → voice_fallbacks / voice_turns)
- [ ] No auth_failures spikes
- [ ] No rate_limit_hits (unless expected)
- [ ] Average latency < 500ms (`/ops/metrics` → latencies.voice_turn_ms.avg_ms)
- [ ] Review any high-fallback sessions (`/ops/failures/summary`)
- [ ] Circuit breakers closed (`/ops/providers/status` → state: "closed")
- [ ] No `cb_*_tripped` spikes in metrics
- [ ] TTS artifact directory not filling up excessively (clean up old sessions)
