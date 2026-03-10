# 🎙️ Agent Vocal de Prise de RDV — Salon de Coiffure

> Projet portfolio : agent vocal IA pour la gestion automatisée des rendez-vous d'un salon de coiffure parisien fictif ("Maison Éclat").

## Vision

Démontrer qu'un agent conversationnel vocal peut remplacer la prise de RDV téléphonique dans un salon de coiffure multi-employés, en gérant :

- La compréhension de la demande en langage naturel
- Le matching service → coiffeur compétent → créneau disponible
- La réservation, modification et annulation de RDV
- Les règles métier complexes (compétences, durées variables, buffers)
- Le fallback vers un humain quand nécessaire

## Salon fictif — Maison Éclat

| Info | Détail |
|---|---|
| Nom | Maison Éclat |
| Adresse | 42 rue des Petits-Champs, 75002 Paris |
| Positionnement | Milieu-haut de gamme |
| Employés | 5 (2 seniors, 1 coloriste, 1 junior, 1 apprentie) |
| Jours d'ouverture | Mardi-Samedi |
| Services | 35+ prestations (coupes, colorations, soins, barbe) |

## Structure du projet

```
projects/vocal-salon-portfolio/
├── README.md                              ← Ce fichier
├── docs/
│   ├── ARCHITECTURE.md                    ← Flux STT→LLM→TTS + API + BDD
│   ├── BUSINESS_CASE.md                   ← Positionnement commercial
│   ├── DEMO_SCRIPT.md                     ← 3 scénarios d'appel de démo
│   └── PLAN_DE_BATAILLE.md                ← Phases, milestones, critères
├── data/
│   ├── benchmark/
│   │   ├── sources.md                     ← 12 sources web consultées
│   │   └── services_raw.csv               ← 85 lignes, 7 salons parisiens
│   └── normalized/
│       ├── services.json                  ← Catalogue 35+ services normalisés
│       ├── durations-matrix.json          ← Durées par service × longueur
│       ├── employees.json                 ← 5 profils, compétences, horaires
│       └── scheduling-rules.json          ← Règles d'affectation et conflits
├── app/
│   ├── config.py                          ← Settings (pydantic-settings)
│   ├── database.py                        ← Async SQLAlchemy engine & session
│   ├── models.py                          ← ORM models (Service, Employee, Booking)
│   ├── schemas.py                         ← Pydantic v2 request/response
│   ├── voice_schemas.py                   ← Voice pipeline Pydantic models
│   ├── intent.py                          ← Deterministic intent extraction
│   ├── conversation.py                    ← In-memory voice session state
│   ├── providers.py                       ← STT/TTS provider abstractions (Phase 4.1)
│   ├── slot_engine.py                     ← Availability & conflict logic
│   ├── seed.py                            ← DB seeder from JSON data
│   ├── main.py                            ← FastAPI app entrypoint
│   ├── demo/                              ← Phase 4.2: E2E demo flow package
│   │   ├── scenarios.py                   ← 3 scenario fixtures + loaders
│   │   ├── orchestrator.py                ← Demo runner + artifact generation
│   │   └── __main__.py                    ← CLI: python -m app.demo
│   └── routers/
│       ├── services.py                    ← GET /services
│       ├── employees.py                   ← GET /employees
│       ├── availability.py                ← GET /availability/search
│       ├── bookings.py                    ← POST/GET/PATCH/DELETE /bookings
│       └── voice.py                       ← Voice pipeline + transcript endpoint
└── tests/                                 ← pytest async test suite (164 tests)
```

## Stack technique prévue

| Couche | Technologie |
|---|---|
| Téléphonie | Vapi ou Twilio |
| Speech-to-Text | Deepgram Nova-2 |
| LLM | Claude 3.5 Sonnet (tool use) |
| Text-to-Speech | ElevenLabs Turbo v2.5 |
| Backend API | FastAPI (Python) |
| Base de données | PostgreSQL 16 |
| Cache | Redis |

## Données de benchmark

Le catalogue est construit à partir de **12 sources web** couvrant 7 salons parisiens de segments variés (accessible à premium). Voir [`data/benchmark/sources.md`](data/benchmark/sources.md).

### Fourchettes de prix observées (Paris 2025-2026)

| Prestation | Entrée de gamme | Milieu de gamme | Haut de gamme |
|---|---|---|---|
| Coupe femme | 29-38€ | 46-75€ | 120-200€ |
| Coupe homme | 19-27€ | 33-38€ | 80€ |
| Coloration | 40-49€ | 73-100€ | 160-280€ |
| Balayage | 45-72€ | 90-120€ | 175-330€ |

## Limites actuelles

- **Durées estimées** : Quand non publiées par les salons, les durées sont des hypothèses métier documentées
- **Pas de validation terrain** : Les données sont issues du web, pas d'entretien avec un gérant de salon
- **Modèle économique théorique** : Les projections de ROI sont à valider avec des pilotes réels
- **Contention ressources matérielles** : La gestion des bacs/casques partagés est prévue en v2

## Roadmap

| Phase | Description | Statut |
|---|---|---|
| **Phase 0** | Fondations, benchmark, architecture | ✅ Fait |
| **Phase 1** | API de réservation (FastAPI + SQLite/PostgreSQL) | ✅ Fait |
| **Phase 2** | Pipeline vocal (intent + conversation + handlers) | ✅ Fait |
| **Phase 3** | Voice turn orchestration (STT → Intent → TTS loop) | ✅ Fait |
| **Phase 4.1** | Provider abstraction (mock ↔ real STT/TTS bridge) | ✅ Fait |
| **Phase 4.2** | E2E demo flow (orchestrator, fixtures, artifacts) | ✅ Fait |
| **Phase 5** | Polish, vidéo démo, landing page | 🔜 |

Voir [`docs/PLAN_DE_BATAILLE.md`](docs/PLAN_DE_BATAILLE.md) pour le détail.

## Quickstart

```bash
# 1. Create virtual environment & install
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Run the API (auto-creates SQLite DB + seeds data on first start)
uvicorn app.main:app --reload
# → http://localhost:8000/docs   (Swagger UI)
# → http://localhost:8000/health (health check)

# 3. Run the test suite
pytest tests/ -v
```

### API Endpoints (all under `/api/v1`)

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/api/v1/services` | List services (filter: `category`, `genre`) |
| `GET` | `/api/v1/services/{id}` | Service detail |
| `GET` | `/api/v1/employees` | List employees |
| `GET` | `/api/v1/employees/{id}` | Employee detail |
| `GET` | `/api/v1/availability/search` | Find available slots (`service_id`, `date`, `employee_id`) |
| `POST` | `/api/v1/bookings` | Create booking |
| `GET` | `/api/v1/bookings/{id}` | Get booking |
| `PATCH` | `/api/v1/bookings/{id}` | Reschedule booking |
| `DELETE` | `/api/v1/bookings/{id}` | Cancel booking |
| `POST` | `/api/v1/voice/sessions/start` | Open voice session |
| `POST` | `/api/v1/voice/sessions/message` | Process user utterance |
| `POST` | `/api/v1/voice/sessions/end` | Close voice session |
| `POST` | `/api/v1/voice/turn` | Full STT → Intent → TTS turn |
| `GET` | `/api/v1/voice/sessions/{id}/transcript` | Session state for demo review |

### Demo Flow (Phase 4.2)

```bash
# Run all 3 demo scenarios against a live server
python -m app.demo

# Run a specific scenario
python -m app.demo happy_path_booking

# List available scenarios
python -m app.demo --list

# Run demos via test suite (no server needed)
pytest tests/test_demo.py -v
```

See [`docs/PHASE4_2_DEMO_FLOW.md`](docs/PHASE4_2_DEMO_FLOW.md) for full details.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///salon.db` | Async DB URL (SQLite or PostgreSQL) |
| `DEBUG` | `true` | Enable SQL echo logging |
| `STT_PROVIDER` | `mock` | STT provider: `mock`, `deepgram` |
| `STT_API_KEY` | *(empty)* | Deepgram API key (mock fallback if empty) |
| `TTS_PROVIDER` | `mock` | TTS provider: `mock`, `elevenlabs` |
| `TTS_API_KEY` | *(empty)* | ElevenLabs API key (mock fallback if empty) |

## Outils n8n intégrés pour agents de code

Le repo inclut des submodules dédiés à n8n dans `tools/` :

- `tools/n8n-mcp`
- `tools/n8n-skills`
- `tools/n8n-mcp-cc-buildier`
- `tools/chrome-devtools-mcp` (debug navigateur / UI / E2E)

Le fichier `CLAUDE.md` impose leur usage prioritaire pour toute tâche liée aux workflows n8n, avec `chrome-devtools-mcp` comme complément pour les diagnostics navigateur.

## Licence

Projet portfolio — usage démonstration uniquement.
