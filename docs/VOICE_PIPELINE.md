# Voice Pipeline Integration Layer

## Overview

Phase 2 adds a local voice-pipeline-ready integration layer on top of the existing
salon booking API. It provides webhook-style endpoints that sit between a local
STT (Speech-to-Text) / TTS (Text-to-Speech) pipeline and the booking backend.

**No external paid services required.** Everything runs locally.

## Architecture

```
┌─────────┐     ┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Phone  │────▶│  STT Engine  │────▶│  Voice Pipeline  │────▶│  TTS Engine  │
│  Call   │◀────│  (Whisper)   │     │  (this layer)    │◀────│  (Piper)     │
└─────────┘     └──────────────┘     └──────────────────┘     └──────────────┘
                                            │
                                            ▼
                                     ┌──────────────┐
                                     │ Booking API  │
                                     │ (existing)   │
                                     └──────────────┘
```

## Endpoints

All endpoints are under `/api/v1/voice/`.

### 1. Start Session

Opens a new voice conversation.

```
POST /api/v1/voice/sessions/start
```

**Request:**
```json
{
  "client_name": "Marie Curie",
  "client_phone": "+33612345678",
  "channel": "phone"
}
```

**Response (201):**
```json
{
  "session_id": "a1b2c3d4e5f6",
  "status": "active",
  "greeting": "Bonjour et bienvenue chez Maison Éclat ! ...",
  "created_at": "2025-03-10T10:00:00Z"
}
```

### 2. Process User Message

Sends a transcribed user utterance for intent detection and fulfillment.

```
POST /api/v1/voice/sessions/message
```

**Request:**
```json
{
  "session_id": "a1b2c3d4e5f6",
  "text": "Je voudrais réserver une coupe homme le 2025-04-10 à 14h30"
}
```

**Response (200):**
```json
{
  "session_id": "a1b2c3d4e5f6",
  "intent": "book",
  "response_text": "Parfait ! Votre rendez-vous est confirmé : ...",
  "booking_draft": {
    "service_id": "coupe_homme",
    "service_label": "Coupe homme",
    "employee_id": "emp_02",
    "employee_name": "Karim Benali",
    "date": "2025-04-10",
    "time": "14:30",
    "client_name": "Marie Curie",
    "client_phone": "+33612345678"
  },
  "action_taken": "booking_created",
  "data": {
    "booking_id": 1,
    "employee": "Karim Benali",
    "start": "2025-04-10T14:30:00"
  }
}
```

### 3. End Session

Closes a voice conversation.

```
POST /api/v1/voice/sessions/end
```

**Request:**
```json
{
  "session_id": "a1b2c3d4e5f6",
  "reason": "user_hangup"
}
```

**Response (200):**
```json
{
  "session_id": "a1b2c3d4e5f6",
  "status": "completed",
  "message": "Merci d'avoir appelé Maison Éclat. À bientôt !",
  "turns": 3,
  "duration_seconds": 45.2
}
```

## Intent Detection

The system uses deterministic keyword matching to classify user utterances:

| Intent | Priority | Keywords (FR/EN) |
|--------|----------|------------------|
| `cancel` | 1 (highest) | annuler, supprimer, cancel, delete |
| `reschedule` | 2 | déplacer, reporter, changer, modifier, reschedule, move |
| `book` | 3 | réserver, prendre, rendez-vous, appointment, book |
| `check_availability` | 4 | disponible, libre, créneau, quand, horaire |
| `unknown` | 5 (fallback) | No match |

### Entity Extraction

Entities are extracted from user speech alongside intent:

| Entity | Patterns | Example |
|--------|----------|---------|
| `date` | ISO (2025-03-15), EU (15/03/2025) | "le 2025-04-10" |
| `time` | French (14h30, 9h), standard (14:30) | "à 14h30" |
| `booking_id` | "réservation #42", "rendez-vous 7" | "#42" |
| `service_category` | coupe, couleur, balayage, brushing, soin... | "une coupe" |
| `genre` | homme/femme/man/woman | "pour homme" |
| `longueur` | court/mi-long/long/short/medium | "cheveux courts" |

## Conversation Flow Examples

### Example 1: Full Booking

```
STT → "Bonjour, je voudrais prendre rendez-vous"
  → intent: book, action: collecting_info
  → TTS: "Quelle prestation souhaitez-vous ?"

STT → "Une coupe pour homme"
  → intent: book, action: collecting_info (need date)
  → TTS: "Pour quelle date souhaitez-vous votre rendez-vous ?"

STT → "Mardi prochain à 10h"
  → intent: book, action: booking_created
  → TTS: "Parfait ! Votre rendez-vous est confirmé..."
```

### Example 2: Cancel Booking

```
STT → "Je voudrais annuler ma réservation"
  → intent: cancel, action: need_booking_id
  → TTS: "Pour annuler votre rendez-vous, j'ai besoin de votre numéro de réservation."

STT → "Réservation #3"
  → intent: cancel, action: booking_cancelled
  → TTS: "Votre réservation #3 a été annulée."
```

### Example 3: Check Availability

```
STT → "Quand êtes-vous disponible pour une coupe le 2025-04-15 ?"
  → intent: check_availability, action: slots_found
  → TTS: "12 créneaux disponibles le 2025-04-15. Premiers créneaux : 09:00, 09:15, 09:30..."
```

## Conversation State

Each voice session maintains:

- **session_id**: Unique 12-char hex identifier
- **current_intent**: Active intent being fulfilled
- **booking_draft**: Fields collected so far (service, date, time, employee...)
- **turns**: Number of user messages processed
- **timestamps**: Created and last activity

State is stored **in-memory** (Python dict). For multi-process deployments,
replace with Redis or similar shared store.

## New Files

| File | Purpose |
|------|---------|
| `app/voice_schemas.py` | Pydantic v2 request/response models |
| `app/intent.py` | Deterministic intent extraction + entity parsing |
| `app/conversation.py` | In-memory conversation state manager |
| `app/routers/voice.py` | FastAPI webhook-style endpoints |
| `tests/test_intent.py` | Intent classification + entity extraction tests |
| `tests/test_conversation.py` | State manager unit tests |
| `tests/test_voice.py` | Endpoint integration tests |

## Design Decisions

1. **Deterministic intent detection** over ML — reliable, no dependencies, easy to debug.
   Designed to be swapped for an LLM classifier later (same `IntentResult` interface).

2. **In-memory state** — simplest possible for single-process local deployment.
   The `ConversationManager` class is a clean abstraction that can be backed by Redis.

3. **Webhook-style endpoints** — session events map naturally to voice pipeline events
   (call start, speech recognized, call end). Compatible with any STT/TTS provider.

4. **Connected to existing services** — booking creation, availability search, and
   cancellation flow through the same `slot_engine` and `Booking` model as the REST API.
