# Architecture — Agent Vocal de Prise de RDV (Maison Éclat)

> Dernière mise à jour : 2026-03-19. Ce document reflète l'état **implémenté et en production**.

---

## Vue d'ensemble

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CLIENT (téléphone)                           │
│                         Appel entrant                               │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ Webhook TwiML
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     TWILIO (téléphonie)                              │
│  - Réception appel                                                   │
│  - STT natif Twilio (transcription)                                  │
│  - Lecture audio (<Play> vers URL servée par le backend)             │
│  - Raccroché sur instruction TwiML                                   │
│  - SMS sortants (confirmation + rappels J-1)                         │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ HTTP webhook (POST /api/v1/twilio/*)
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     BACKEND FastAPI                                  │
│                                                                      │
│  app/routers/twilio_router.py   — Webhook Twilio, TwiML builder      │
│  app/routers/voice.py           — Sessions vocales, turns            │
│  app/routers/bookings.py        — CRUD réservations                  │
│  app/routers/availability.py    — Recherche créneaux dispo           │
│  app/routers/admin.py           — Dashboard API (token-protégé)      │
│  app/routers/ops.py             — Métriques, diagnostics             │
│  app/routers/telephony.py       — Pilote téléphonie abstrait         │
│                                                                      │
│  app/llm_conversation.py        — Moteur GPT-4o-mini (OpenAI)        │
│  app/tts_elevenlabs.py          — Synthèse ElevenLabs                │
│  app/audio_store.py             — Cache audio local (warm greeting)  │
│  app/sms_sender.py              — SMS via Twilio REST                │
│  app/reminder.py                — Boucle rappels J-1                 │
│  app/purge.py                   — Boucle purge RGPD nightly          │
│  app/session_store.py           — Persistence sessions en DB         │
│  app/settings_service.py        — Paramètres runtime DB-backed       │
│                                                                      │
└──────────────┬────────────────────────────┬─────────────────────────┘
               │ SQLAlchemy async            │ httpx
               ▼                             ▼
┌──────────────────────┐        ┌────────────────────────────┐
│  PostgreSQL (prod)   │        │  APIs externes             │
│  SQLite (dev)        │        │  - OpenAI (GPT-4o-mini)    │
│                      │        │  - ElevenLabs TTS          │
│  Tables :            │        │  - Twilio REST (SMS)       │
│  - services          │        │  - Resend (email)          │
│  - employees         │        └────────────────────────────┘
│  - employee_         │
│    competencies      │
│  - bookings          │
│  - voice_sessions    │
│  - transcript_events │
│  - callback_requests │
│  - salon_settings    │
└──────────────────────┘
```

---

## Stack technique (implémenté)

| Composant | Choix retenu |
|---|---|
| Téléphonie | **Twilio** (webhook TwiML) |
| STT | **Twilio natif** (transcription incluse dans le webhook) |
| LLM | **GPT-4o-mini** (OpenAI) avec function calling |
| TTS | **ElevenLabs** `eleven_flash_v2_5` — voix "Marine" |
| Backend | **FastAPI** (Python 3.12, uvicorn) |
| Base de données | **PostgreSQL** (prod) / SQLite (dev) via SQLAlchemy async |
| SMS | **Twilio REST** (via httpx, module `app/sms_sender.py`) |
| Email | **Resend** (notifications gérant) |
| Déploiement | VPS Linux, systemd, reverse proxy nginx |
| Dashboard | SPA vanilla JS + Tailwind CDN + Lucide icons |

---

## Flux d'un appel entrant

```
1. Client appelle le numéro Twilio
2. Twilio envoie POST /api/v1/twilio/voice (webhook)
3. Le backend répond TwiML <Gather> → Twilio écoute et transcrit
4. Twilio envoie POST /api/v1/twilio/gather avec le transcript
5. Le backend :
   a. Charge/crée la VoiceSession en base
   b. Passe le transcript à GPT-4o-mini (avec historique messages_json)
   c. Si function call : exécute (search_availability, create_booking, etc.)
   d. Génère la réponse TTS via ElevenLabs → fichier audio servi sous /audio/
   e. Répond TwiML <Play> (URL audio) + <Gather> (prochain tour)
6. Si create_booking réussi :
   a. Raccroche proprement (TwiML <Hangup>)
   b. Envoie SMS de confirmation au client (si opt-in)
   c. Envoie SMS/email au gérant (OWNER_PHONE, SALON_EMAIL)
```

---

## Dashboard admin (`/admin`)

SPA statique servie par FastAPI StaticFiles. Toutes les routes API sont prefixées `/api/v1/admin/` et protégées par token (`?token=VOICE_API_KEY`).

| Endpoint | Usage |
|---|---|
| `GET /admin/bookings` | Lister RDV à venir / historique |
| `POST /api/v1/bookings` | Créer RDV manuellement |
| `DELETE /api/v1/bookings/{id}` | Annuler un RDV |
| `GET /admin/callbacks` | Demandes de rappel vocal |
| `PATCH /admin/callbacks/{id}` | Mettre à jour statut/notes |
| `GET /admin/sessions` | Lister les sessions vocales |
| `GET /admin/sessions/{id}` | Détail + transcript d'une session |
| `GET /admin/stats` | Stats mensuelles agrégées |
| `GET /admin/settings` | Lire les paramètres runtime |
| `PATCH /admin/settings` | Modifier les paramètres runtime |
| `GET /admin/services` | Catalogue prestations (pour formulaires) |
| `GET /admin/employees` | Liste employés (pour formulaires) |

---

## Modèle de données

```sql
-- Réservations
bookings (id, client_name, client_phone, employee_id, service_id,
          start_time, end_time, status, notes, reminder_sent, created_at)

-- Sessions vocales
voice_sessions (session_id, status, turns, client_name, client_phone,
                channel, current_intent, booking_draft_json,
                messages_json, created_at, last_activity)

-- Transcripts (1 ligne = 1 tour)
transcript_events (id, session_id, turn_number, user_text, intent,
                   confidence, response_text, action_taken, is_fallback)

-- Demandes de rappel (voicemail)
callback_requests (id, caller_phone, recording_url, recording_duration,
                   transcription, status, notes, created_at)

-- Paramètres runtime (override env vars)
salon_settings (key, value, updated_at)
```

---

## Paramètres runtime

Les paramètres sont stockés dans `salon_settings` et chargés au démarrage via `settings_service.load_settings_from_db()`. Toute modification via `PATCH /admin/settings` est appliquée immédiatement en mémoire — pas de redémarrage requis.

| Groupe | Clés |
|---|---|
| Salon & Gérant | OWNER_PHONE, SALON_EMAIL, SALON_EMAIL_FROM |
| SMS | TWILIO_PHONE_NUMBER, TWILIO_TRANSFER_NUMBER |
| Rappels | REMINDER_ENABLED, REMINDER_HOUR |
| RGPD | SESSION_RETENTION_DAYS, CALLBACK_RETENTION_DAYS, PURGE_HOUR |
| Twilio (sensible) | TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN |
| Email (sensible) | RESEND_API_KEY |
| ElevenLabs (sensible) | ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID |
| Sécurité (sensible) | VOICE_API_KEY |

---

## Ce qui n'est PAS utilisé (décisions de simplification)

- **Redis** — pas de cache distribué ni de locks Redis. La prévention des double-bookings est gérée par les contraintes SQLAlchemy + logique applicative.
- **Deepgram / Whisper** — STT natif Twilio suffit pour le cas d'usage.
- **Vapi** — architecture Twilio directe retenue.
- **S3 / Minio** — les fichiers audio TTS sont stockés localement (`AUDIO_DIR`), purgés automatiquement.
- **Langfuse / Sentry** — métriques in-memory via `app/observability.py`, endpoint `/ops/metrics`.

---

## Prochaines évolutions envisagées

1. **Canal WhatsApp** — Twilio WhatsApp Business API ou migration Bird (EU-native)
2. **Multi-salon** — externaliser nom/adresse/horaires en config par client
3. **Annulation vocale** — ajouter function call `cancel_booking` dans le LLM
