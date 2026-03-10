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
├── src/                                   ← (Phase 1 — à implémenter)
└── tests/                                 ← (Phase 1 — à implémenter)
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

- **Pas de code fonctionnel** : Phase 0 (données + architecture) uniquement
- **Durées estimées** : Quand non publiées par les salons, les durées sont des hypothèses métier documentées
- **Pas de validation terrain** : Les données sont issues du web, pas d'entretien avec un gérant de salon
- **Modèle économique théorique** : Les projections de ROI sont à valider avec des pilotes réels
- **Contention ressources matérielles** : La gestion des bacs/casques partagés est prévue en v2

## Roadmap

| Phase | Description | Statut |
|---|---|---|
| **Phase 0** | Fondations, benchmark, architecture | ✅ Fait |
| **Phase 1** | API de réservation (FastAPI + PostgreSQL) | 🔜 |
| **Phase 2** | Pipeline vocal (STT + LLM + TTS) | 🔜 |
| **Phase 3** | Intégration et tests end-to-end | 🔜 |
| **Phase 4** | Polish, vidéo démo, landing page | 🔜 |

Voir [`docs/PLAN_DE_BATAILLE.md`](docs/PLAN_DE_BATAILLE.md) pour le détail.

## Lancer le projet (futur)

```bash
# Phase 1+ : quand le backend sera implémenté
docker compose up -d
# L'API sera disponible sur http://localhost:8000
# La doc OpenAPI sur http://localhost:8000/docs
```

## Outils n8n intégrés pour agents de code

Le repo inclut des submodules dédiés à n8n dans `tools/` :

- `tools/n8n-mcp`
- `tools/n8n-skills`
- `tools/n8n-mcp-cc-buildier`
- `tools/chrome-devtools-mcp` (debug navigateur / UI / E2E)

Le fichier `CLAUDE.md` impose leur usage prioritaire pour toute tâche liée aux workflows n8n, avec `chrome-devtools-mcp` comme complément pour les diagnostics navigateur.

## Licence

Projet portfolio — usage démonstration uniquement.
