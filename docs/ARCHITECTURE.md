# Architecture — Agent Vocal de Prise de RDV

## Vue d'ensemble

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CLIENT (téléphone)                          │
│                     Appel entrant / sortant                        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ SIP/WebRTC
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     COUCHE TÉLÉPHONIE                                │
│                                                                      │
│  Twilio / Vonage / Vapi                                              │
│  - Réception appel                                                   │
│  - Gestion session audio bidirectionnelle                            │
│  - Détection silence / barge-in                                      │
│  - Webhook événements (call.started, call.ended)                     │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ Audio stream (WebSocket)
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     PIPELINE VOCAL                                   │
│                                                                      │
│  ┌─────────┐    ┌─────────────┐    ┌─────────┐                      │
│  │  STT    │───▶│  LLM Agent  │───▶│  TTS    │                      │
│  │         │    │             │    │         │                      │
│  │Deepgram │    │ Claude /    │    │ElevenLabs│                      │
│  │Whisper  │    │ GPT-4o     │    │PlayHT   │                      │
│  │Google   │    │            │    │Google   │                      │
│  └─────────┘    └──────┬──────┘    └─────────┘                      │
│                        │                                             │
│  Latence cible : < 800ms bout-en-bout (STT+LLM+TTS)                │
└────────────────────────┼─────────────────────────────────────────────┘
                         │ Function calls / Tool use
                         ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     MOTEUR DE RÉSERVATION (API)                      │
│                                                                      │
│  FastAPI / Express.js                                                │
│                                                                      │
│  Endpoints :                                                         │
│  ┌────────────────────────────────────────────────────────────┐       │
│  │ POST /api/v1/availability/search                          │       │
│  │   → Cherche créneaux dispo selon service + date + prefs   │       │
│  │                                                            │       │
│  │ POST /api/v1/bookings                                     │       │
│  │   → Crée un RDV (verrouillage optimiste)                  │       │
│  │                                                            │       │
│  │ GET  /api/v1/bookings/:id                                 │       │
│  │   → Détail d'un RDV existant                              │       │
│  │                                                            │       │
│  │ PATCH /api/v1/bookings/:id                                │       │
│  │   → Modification (date, service, annulation)              │       │
│  │                                                            │       │
│  │ GET  /api/v1/services                                     │       │
│  │   → Catalogue des prestations                             │       │
│  │                                                            │       │
│  │ GET  /api/v1/employees                                    │       │
│  │   → Liste coiffeurs + compétences                         │       │
│  └────────────────────────────────────────────────────────────┘       │
│                                                                      │
│  Logique métier :                                                    │
│  - Matching service → employés compétents                            │
│  - Calcul durée (service + longueur + modificateurs)                 │
│  - Recherche créneaux libres (employee.horaires ∩ salon.ouverture)   │
│  - Application buffers inter-RDV                                     │
│  - Gestion conflits (double-booking prevention via lock)             │
│  - Fallback si aucun créneau                                        │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     COUCHE DONNÉES                                   │
│                                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │  PostgreSQL  │  │    Redis     │  │   S3/Minio   │               │
│  │              │  │              │  │              │               │
│  │ - bookings   │  │ - locks RDV  │  │ - logs audio │               │
│  │ - employees  │  │ - sessions   │  │ - transcripts│               │
│  │ - services   │  │ - cache dispo│  │              │               │
│  │ - clients    │  │              │  │              │               │
│  │ - audit_log  │  │              │  │              │               │
│  └──────────────┘  └──────────────┘  └──────────────┘               │
└──────────────────────────────────────────────────────────────────────┘
```

## Flux détaillé d'un appel

### 1. Accueil et identification

```
Client appelle → Twilio décroche → TTS : "Bonjour, Maison Éclat, comment puis-je vous aider ?"
                                   STT écoute la réponse
```

### 2. Compréhension de l'intention

Le LLM analyse la transcription et identifie :
- **Intent** : `book_appointment` | `modify_appointment` | `cancel_appointment` | `get_info` | `transfer_human`
- **Entities** : service demandé, date/heure souhaitée, coiffeur préféré, longueur cheveux

### 3. Recherche de disponibilité

```
LLM → function_call: search_availability({
  service_id: "balayage_mi_long",
  date_souhaitee: "2026-03-14",
  heure_pref: "14:00",
  employee_pref: "emp_03"  // optionnel
})

API → Algorithme :
  1. Filtrer employés compétents pour "balayage_mi_long"
     → [emp_01 (Sophie), emp_03 (Léa)]
  2. Calculer durée = 150min (base) + 0 (pas de modificateur)
  3. Ajouter buffer = 15min (prestation chimique)
  4. Chercher slots libres de 165min dans planning de chaque employé
  5. Retourner top 3 créneaux les plus proches
```

### 4. Proposition et confirmation

```
API retourne → [{employee: "Léa", date: "2026-03-14", heure: "14:00"},
                {employee: "Sophie", date: "2026-03-14", heure: "15:30"},
                {employee: "Léa", date: "2026-03-15", heure: "10:00"}]

LLM → TTS : "J'ai un créneau avec Léa le samedi 14 mars à 14h
              pour votre balayage. Ça dure environ 2h30.
              Est-ce que ça vous convient ?"
```

### 5. Confirmation et réservation

```
Client : "Oui c'est parfait"
STT → LLM détecte confirmation
LLM → function_call: create_booking({...})
API → Verrouille créneau + insère en base
LLM → TTS : "C'est réservé ! Léa vous attend le samedi 14 mars
              à 14h. Vous recevrez une confirmation par SMS.
              Bonne journée !"
```

## Modèle de données simplifié

```sql
-- Table bookings
CREATE TABLE bookings (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_phone  VARCHAR(20) NOT NULL,
  client_name   VARCHAR(100),
  employee_id   VARCHAR(10) REFERENCES employees(id),
  service_id    VARCHAR(50) NOT NULL,
  start_at      TIMESTAMPTZ NOT NULL,
  end_at        TIMESTAMPTZ NOT NULL,
  duration_min  INT NOT NULL,
  status        VARCHAR(20) DEFAULT 'confirmed',  -- confirmed|cancelled|completed|no_show
  source        VARCHAR(20) DEFAULT 'vocal_agent', -- vocal_agent|web|manual
  created_at    TIMESTAMPTZ DEFAULT now(),
  notes         TEXT,
  CONSTRAINT no_overlap EXCLUDE USING gist (
    employee_id WITH =,
    tstzrange(start_at, end_at) WITH &&
  )
);

-- Exclusion constraint empêche tout chevauchement pour un même employé
```

## Stack technique recommandée

| Composant | Option principale | Alternative |
|---|---|---|
| Téléphonie | Vapi (tout-en-un vocal) | Twilio + assembly maison |
| STT | Deepgram Nova-2 | Whisper large-v3 |
| LLM | Claude 3.5 Sonnet (tool use) | GPT-4o |
| TTS | ElevenLabs Turbo v2.5 | PlayHT 2.0 |
| Backend API | FastAPI (Python) | Express.js (Node) |
| Base de données | PostgreSQL 16 | - |
| Cache/Locks | Redis | - |
| Hébergement | Railway / Fly.io | AWS ECS |
| SMS confirmation | Twilio SMS | OVH SMS |
| Monitoring | Langfuse + Sentry | Datadog |

## Considérations de latence

| Étape | Cible | Technique |
|---|---|---|
| STT | < 300ms | Streaming Deepgram, endpointing agressif |
| LLM | < 400ms | Streaming, prompt court, tool use |
| TTS | < 200ms | Streaming ElevenLabs, voix pré-chargée |
| **Total** | **< 900ms** | **Conversation naturelle** |

## Sécurité et conformité

- **RGPD** : Consentement enregistrement vocal au début de l'appel. Droit à l'effacement des données vocales.
- **Données personnelles** : Téléphone et nom stockés chiffrés (AES-256). Pas de stockage carte bancaire.
- **Audit** : Chaque action de l'agent est loguée avec timestamp et transcription.
- **Fallback humain** : Transfert vers le salon à tout moment si le client dit "je veux parler à quelqu'un".

## Diagramme de séquence — Réservation standard

```
Client          Twilio/Vapi      STT         LLM          API           TTS
  │                │              │           │            │              │
  │── Appel ──────▶│              │           │            │              │
  │                │──audio──────▶│           │            │              │
  │                │              │──text────▶│            │              │
  │                │              │           │──intent───▶│              │
  │                │              │           │            │──search──┐   │
  │                │              │           │            │◀─slots───┘   │
  │                │              │           │◀─result────│              │
  │                │              │           │──speech───▶│          ───▶│
  │◀──────────────────────────────────────────────────────────── audio───│
  │                │              │           │            │              │
  │── "oui" ──────▶│──audio──────▶│──text────▶│            │              │
  │                │              │           │──book─────▶│              │
  │                │              │           │◀─confirm───│              │
  │                │              │           │──speech──────────────────▶│
  │◀──────────────────────────────────────────────────────────── audio───│
  │                │              │           │            │──SMS────────▶│
```
