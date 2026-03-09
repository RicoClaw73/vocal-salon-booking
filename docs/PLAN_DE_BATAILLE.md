# Plan de bataille — Agent Vocal Salon de Coiffure

## Vue d'ensemble

| Phase | Nom | Durée estimée | Statut |
|---|---|---|---|
| 0 | Fondations & Data | 1 semaine | ✅ FAIT |
| 1 | API Réservation | 2 semaines | 🔜 À faire |
| 2 | Pipeline Vocal | 2 semaines | 🔜 À faire |
| 3 | Intégration & Tests | 1 semaine | 🔜 À faire |
| 4 | Polish & Démo | 1 semaine | 🔜 À faire |

**Durée totale estimée** : 7 semaines

---

## Phase 0 — Fondations & Data ✅

### Milestone 0.1 : Benchmark marché
- [x] Recherche tarifs salons parisiens (6+ sources)
- [x] Extraction brute en CSV
- [x] Documentation des sources

### Milestone 0.2 : Données normalisées
- [x] Catalogue services (services.json)
- [x] Matrice de durées (durations-matrix.json)
- [x] Profils employés (employees.json)
- [x] Règles de scheduling (scheduling-rules.json)

### Milestone 0.3 : Documentation projet
- [x] Architecture technique (ARCHITECTURE.md)
- [x] Script de démo (DEMO_SCRIPT.md)
- [x] Business case (BUSINESS_CASE.md)
- [x] README projet

**Critère d'acceptation** : Toutes les données sont cohérentes entre elles (les service_id dans employees.json correspondent à ceux de services.json). Les durées de la matrice sont utilisées dans le catalogue.

---

## Phase 1 — API Réservation (Backend)

### Milestone 1.1 : Setup projet backend
- [ ] Init projet FastAPI + structure
- [ ] Modèle de données PostgreSQL (migrations Alembic)
- [ ] Seed database avec les JSON de la phase 0
- [ ] Docker Compose (API + Postgres + Redis)

**Critère d'acceptation** : `docker compose up` lance l'ensemble. La base contient les 5 employés et le catalogue complet.

### Milestone 1.2 : Endpoints CRUD
- [ ] `GET /api/v1/services` — catalogue filtrable
- [ ] `GET /api/v1/employees` — liste + compétences
- [ ] `GET /api/v1/employees/:id/schedule` — planning d'un coiffeur
- [ ] `POST /api/v1/bookings` — création RDV
- [ ] `GET /api/v1/bookings/:id` — détail RDV
- [ ] `PATCH /api/v1/bookings/:id` — modification/annulation
- [ ] Tests unitaires pour chaque endpoint

**Critère d'acceptation** : Tous les endpoints retournent des réponses conformes au schéma OpenAPI. Couverture tests > 80%.

### Milestone 1.3 : Moteur de disponibilité
- [ ] Algorithme de recherche de créneaux (service × employé × plage horaire)
- [ ] Application des règles de scheduling-rules.json
- [ ] Gestion des buffers inter-RDV
- [ ] Gestion des conflits (exclusion constraint PostgreSQL)
- [ ] `POST /api/v1/availability/search` — recherche multi-critères
- [ ] Tests avec scénarios de conflit

**Critère d'acceptation** : L'algorithme ne propose jamais un créneau qui viole une règle de scheduling. Test de charge : < 200ms pour une recherche sur 1 semaine avec 5 employés.

### Milestone 1.4 : Fonctions appelables par le LLM
- [ ] Définir les tool schemas (JSON Schema / OpenAPI)
- [ ] `search_availability(service_id, date, heure_pref, employee_pref?)`
- [ ] `create_booking(client_phone, client_name, employee_id, service_id, start_at)`
- [ ] `get_booking(client_phone)` — lookup par téléphone
- [ ] `modify_booking(booking_id, new_date?, new_service?)`
- [ ] `cancel_booking(booking_id)`
- [ ] `list_services(category?)`

**Critère d'acceptation** : Chaque fonction est testable en isolation via curl. Les schémas JSON sont validés.

---

## Phase 2 — Pipeline Vocal

### Milestone 2.1 : Choix et setup téléphonie
- [ ] Évaluation Vapi vs Twilio+assembly maison
- [ ] Création compte + numéro de téléphone de test
- [ ] Configuration webhook de base
- [ ] Test : appel entrant → réponse statique

**Critère d'acceptation** : Un appel au numéro de test déclenche un webhook et joue un message d'accueil.

### Milestone 2.2 : Pipeline STT → LLM → TTS
- [ ] Configuration STT (Deepgram ou Whisper)
- [ ] Prompt système pour l'agent LLM (Claude ou GPT-4o)
- [ ] Enregistrement des tools/functions du milestone 1.4
- [ ] Configuration TTS (ElevenLabs — voix française naturelle)
- [ ] Test bout-en-bout : appel → compréhension → réponse vocale

**Critère d'acceptation** : Latence bout-en-bout < 1.2s. L'agent comprend "je veux une coupe" et répond de manière cohérente.

### Milestone 2.3 : Prompt engineering conversationnel
- [ ] System prompt : personnalité, ton, règles salon
- [ ] Gestion du contexte multi-tour (mémoire de conversation)
- [ ] Détection d'intention robuste (book, modify, cancel, info, transfer)
- [ ] Gestion des cas limites (bruit, hésitation, interruption)
- [ ] Fallback vers humain (transfert d'appel)
- [ ] Tests avec les 3 scénarios de DEMO_SCRIPT.md

**Critère d'acceptation** : Les 3 scénarios de démo passent sans intervention manuelle. Le transfert humain fonctionne.

---

## Phase 3 — Intégration & Tests

### Milestone 3.1 : Tests end-to-end
- [ ] Scénario complet : appel → réservation → vérification en base
- [ ] Scénario modification + annulation
- [ ] Scénario conflit (créneau pris pendant l'appel d'un autre client)
- [ ] Scénario fallback (aucun créneau, coiffeur absent)
- [ ] Test avec accents et bruit de fond

**Critère d'acceptation** : 90% des scénarios passent sans erreur. Les cas d'échec sont gérés gracieusement.

### Milestone 3.2 : Dashboard admin (optionnel v1)
- [ ] Vue calendrier multi-employés (semaine)
- [ ] Liste des RDV du jour
- [ ] Indicateurs : taux de remplissage, appels traités, durée moyenne
- [ ] Interface simple (React ou Streamlit)

**Critère d'acceptation** : Le prospect peut visualiser en temps réel les RDV créés par l'agent vocal.

---

## Phase 4 — Polish & Démo

### Milestone 4.1 : Préparation démo
- [ ] Données de démo réalistes (semaine type avec RDV existants)
- [ ] Enregistrement vidéo des 3 scénarios
- [ ] Landing page portfolio (1 page)
- [ ] Deck de présentation (5-7 slides)

**Critère d'acceptation** : La démo tourne sans bug pendant 10 minutes. La vidéo est utilisable pour un prospect.

### Milestone 4.2 : Documentation déploiement
- [ ] Guide de déploiement (Docker)
- [ ] Variables d'environnement documentées
- [ ] Coût de fonctionnement estimé (par appel et par mois)
- [ ] Guide de personnalisation (adapter à un autre salon)

**Critère d'acceptation** : Un développeur peut déployer le projet en < 1h en suivant le guide.

---

## Risques identifiés

| Risque | Impact | Probabilité | Mitigation |
|---|---|---|---|
| Latence vocale > 1.5s | UX dégradée | Moyenne | Streaming STT+TTS, LLM rapide |
| STT mal français familier | Mauvaise compréhension | Haute | Prompt robuste + fallback humain |
| Coût LLM par appel élevé | Non rentable | Moyenne | Optimiser prompt, cacher résultats fréquents |
| Chevauchement RDV (race condition) | Double booking | Faible | Exclusion constraint PostgreSQL + Redis lock |
| Complexité règles métier réelles | Modèle trop simple | Haute | Itérer avec un vrai gérant de salon |

---

## Dépendances externes

- Compte Vapi ou Twilio (téléphonie)
- Clé API LLM (Anthropic ou OpenAI)
- Compte Deepgram (STT)
- Compte ElevenLabs (TTS)
- Serveur hébergement (Railway/Fly.io)
- Numéro de téléphone français (+33)
