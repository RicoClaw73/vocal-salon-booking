# Analyse Vidéo — "Créer Son Premier Agent IA Vocal en 48 minutes"

> **Reverse engineering détaillé** pour le projet vocal-salon-portfolio
> Date d'analyse : 2026-03-10

---

## Métadonnées

| Champ | Valeur |
|---|---|
| **Titre** | Créer Son Premier Agent IA Vocal en 48 minutes |
| **Chaîne** | Yassine Sdiri |
| **Date de publication** | 2026-03-08 (il y a 2 jours) |
| **Durée** | 48:08 |
| **Vues** | ~6 600 |
| **Likes** | 329 |
| **Commentaires** | 22 |
| **URL** | https://www.youtube.com/watch?v=04P0GIKNNa4 |

### Méthode d'extraction

- **Transcript** : Auto-generated FR via `youtube-transcript-api` (1742 snippets). Qualité : bonne pour du auto-sub, quelques erreurs de transcription sur les noms propres (Valéo/Valio/Valet, Zdiri/Zidiri/Zri). Pas de sous-titres officiels disponibles.
- **Commentaires** : Extraction via `yt-dlp --write-comments` (22 commentaires, 20 root + 2 replies).
- **Métadonnées** : `yt-dlp --dump-json`.

---

## Structure de la Vidéo (Chapitres)

| Timestamp | Chapitre | Contenu résumé |
|---|---|---|
| 00:00–04:49 | **Intro** | Démo live d'un appel au salon Valéo + présentation de Yas et Chatflow AI + explication du concept d'agent IA vocal |
| 04:50–23:39 | **Étape 1 : Création de l'agent** | Configuration ElevenLabs (voix, LLM, prompt système, knowledge base) + premier test |
| 23:40–37:27 | **Étape 2 : Fonctionnalités avancées** | Intégration n8n + Google Calendar (check availability + book appointment via webhooks) + test live |
| 37:28–44:25 | **Étape 3 : Numéro de téléphone** | Configuration Twilio + connexion à ElevenLabs + appel réel |
| 44:26–48:08 | **Derniers conseils** | 4 conseils de production + promotion Académie |

---

## Stack Technique Complète (OBSERVÉ)

| Composant | Outil utilisé | Rôle |
|---|---|---|
| Plateforme agent vocal | **ElevenLabs Agents** | Héberge l'agent, gère STT + LLM + TTS en un |
| LLM (cerveau) | **Gemini 2.5 Flash** (par défaut, puis changé) | Raisonnement et génération de réponses |
| TTS (voix) | **ElevenLabs** (voix "Lucy", accent français, catégorie "support agent") | Synthèse vocale |
| Orchestration/automatisation | **n8n** | Pont entre ElevenLabs et Google Calendar via webhooks |
| Calendrier | **Google Calendar** | Stockage des RDV réels |
| Téléphonie | **Twilio** | Numéro de téléphone réel (+33 ou +41) |
| Knowledge Base | **ElevenLabs KB** (URL scraping du site salon) | Base de connaissances pour FAQ |

### Alternatives mentionnées (non utilisées)

- Vapi, Rounded, Retell AI (plateformes agent vocal alternatives)
- Zapier, Make (alternatives n8n)
- MCP servers (alternative aux webhooks API classiques)
- cal.com (alternative Google Calendar)

---

## Architecture Détaillée du Workflow n8n (OBSERVÉ)

```
┌─────────────────────────────────────────────┐
│           ElevenLabs Agent                  │
│  ┌───────────┐  ┌──────────┐  ┌──────────┐ │
│  │ STT (voix │→ │ LLM      │→ │ TTS      │ │
│  │ → texte)  │  │ (Gemini) │  │ (voix)   │ │
│  └───────────┘  └────┬─────┘  └──────────┘ │
│                      │ Tool call            │
│  Tools:              │                      │
│  • check_availability├──── webhook POST ──────┐
│  • book_appointment  │                      │ │
└─────────────────────────────────────────────┘ │
                                                │
┌───────────────── n8n Workflow ────────────────┘
│                                              │
│  ┌──────────┐                                │
│  │ Webhook  │ (POST, production URL)         │
│  │ (entrée) │                                │
│  └────┬─────┘                                │
│       │                                      │
│  ┌────▼────────────────────┐                 │
│  │ IF: quel tool appelé?   │                 │
│  │ "check_availability"    │                 │
│  │ ou "book_appointment"   │                 │
│  └────┬────────┬───────────┘                 │
│       │        │                             │
│  ┌────▼────┐ ┌─▼───────────┐                │
│  │ IF: a-t-│ │ Book:       │                │
│  │ il un   │ │ Create      │                │
│  │ jour    │ │ Calendar    │                │
│  │ désiré? │ │ Event       │                │
│  └──┬──┬───┘ └─────────────┘                │
│     │  │                                     │
│  ┌──▼──▼───────────┐                        │
│  │ Google Calendar  │                        │
│  │ - Get events     │                        │
│  │ - Check slots    │                        │
│  └────────┬────────┘                         │
│           │                                  │
│  ┌────────▼────────┐                         │
│  │ Webhook Response │ → retour à ElevenLabs  │
│  └─────────────────┘                         │
└──────────────────────────────────────────────┘
```

### Paramètres des Tools ElevenLabs → n8n (OBSERVÉ)

**Tool: check_availability**
- Type: Webhook (POST)
- URL: Production URL du webhook n8n
- Query param (string): `call = "check_availability"`
- Body params: `preferred_time` (créneau souhaité)

**Tool: book_appointment**
- Type: Webhook (POST)
- URL: Même webhook
- Query param (string): `call = "book_appointment"`
- Body params: `email`, `date_and_time`, `name`

### Logique du workflow n8n (OBSERVÉ)

1. **Webhook reçoit** la requête POST d'ElevenLabs
2. **Branchement IF** selon le paramètre `call` (check_availability vs book_appointment)
3. **Pour check_availability** :
   - Second IF : le client a-t-il un jour désiré ou veut-il voir la semaine entière ?
   - Nœud Google Calendar : récupère les événements existants
   - Un prompt/nœud ajoute la date actuelle en variable
   - Renvoie les créneaux disponibles via Webhook Response
4. **Pour book_appointment** :
   - Crée un événement Google Calendar avec : nom, email, date/heure
   - Timezone : Europe centrale (configurable)
   - Renvoie confirmation via Webhook Response

---

## Configuration de l'Agent ElevenLabs (OBSERVÉ)

### Paramètres clés

| Paramètre | Valeur observée |
|---|---|
| **Langue principale** | Français (French) |
| **Langue secondaire** | Anglais (pour touristes) |
| **Voix** | "Lucy" — catégorie "support agent", accent français, conversationnel |
| **LLM** | Gemini 2.5 Flash (par défaut), puis changé pour un modèle + rapide |
| **Emotional intelligence** | Activé (nouveau feature ElevenLabs) |
| **Premier message** | "Bonjour, je suis Marine, la réceptionniste IA du salon Valéo. Comment puis-je vous aider aujourd'hui ?" |
| **Knowledge Base** | URL du site web du salon (scraping auto) |

### Prompt Système

Le prompt a été **auto-généré par ElevenLabs** via le wizard "Business Agent" :
- Industrie : Professional Services
- Objectif : Réceptionniste salon de coiffure Paris
- Site web : celui du salon Valéo (5e arrondissement)
- Les tools (check_availability, book_appointment) sont référencés dans le prompt

**Observation importante** : Yassine note qu'il n'a PAS retouché le prompt auto-généré pour le premier test et que ça a quand même bien fonctionné. Il note même avoir OUBLIÉ d'ajouter book_appointment dans le prompt et que le LLM l'a utilisé quand même (raisonnement auto).

### Bonnes pratiques prompt vocal mentionnées

- Ne jamais couper la parole
- Ne jamais donner trop d'infos incertaines
- Ne jamais dire "trop tard"
- Réponses courtes et conversationnelles
- 6 composants d'un bon system prompt : rôle, tâche, spécificités, contexte, exemples, notes

---

## Démos Live et Résultats (OBSERVÉ)

### Démo 1 (sans n8n, ~20:00)
- Agent répond avec infos du site web (localisation, services)
- Propose une coupe homme
- Prononce correctement "Yassine Zdiri" (remarqué comme impressionnant)
- Émotion/expressivité dans les intonations
- **Coût** : ~2 min d'appel → coût LLM "dérisoire" (millièmes de centime)
- **Limite** : pas de vérification calendrier réelle, hallucine la disponibilité

### Démo 2 (avec n8n, ~33:00)
- Check availability réel : mercredi 4 mars 2026 à 11h → non disponible
- Propose alternatives : 10h ou 12h
- Client choisit 10h → booking créé dans Google Calendar
- Exécution n8n visible en parallèle
- Nom légèrement mal prononcé ("Zidiri" au lieu de "Sdiri" — problème de S vs Z)
- **Limite observée** : email de confirmation mentionné mais pas configuré (email d'exemple)

### Démo 3 (appel téléphonique réel, ~40:00)
- Appel via numéro suisse (+41) Twilio
- Fonctionne, agent répond
- Son un peu différent (téléphonie vs navigateur)
- Démo tronquée pour économiser les tokens

### Démo 4 (voice cloning, ~43:00)
- Clone de la voix de Yassine en 30 secondes
- Résultat : "un petit peu robotique" mais "ça s'en rapproche vachement"

---

## Les 4 Conseils de Production (OBSERVÉ, timestamp 44:26)

1. **Commencer simple (MVP)** — Un agent avec un seul objectif clair. Ne pas tout automatiser d'un coup (RDV + SMS + email + CRM + qualification). Phase 1 = MVP, Phase 2 = V1 élaborée.

2. **Toujours prévoir le transfert vers un humain** — Non négociable. Pour : accents mal transcrits, mauvaise connexion, questions imprévues. Donne confiance aux équipes.

3. **Être transparent** — Mentionner que c'est un agent IA dès le début. La transparence n'a AUCUN impact négatif sur les résultats (selon l'expérience terrain).

4. **L'IA ne remplace pas l'humain, elle le libère** — L'agent gère le volume (appels répétitifs, spam, questions basiques). L'humain garde la vente, la relation client, l'empathie. **Ne jamais automatiser une tâche qu'aucun humain n'a faite manuellement avant.**

---

## Points Forts de la Vidéo

| # | Point fort | Détail |
|---|---|---|
| 1 | **Cas d'usage salon de coiffure** | Exactement notre domaine. Validé en conditions quasi-réelles |
| 2 | **Stack no-code complète** | ElevenLabs + n8n + Google Calendar + Twilio = 0 ligne de code |
| 3 | **Démo end-to-end convaincante** | De la création à l'appel téléphonique réel en 48 min |
| 4 | **Coût faible démontré** | Millièmes de centime par appel LLM |
| 5 | **Multilangue natif** | FR principal + EN secondaire (touristes) |
| 6 | **Emotional intelligence TTS** | Voix naturelle et expressive |
| 7 | **Conseils production solides** | Viennent d'expérience réelle (Chatflow AI, centaines de clients) |
| 8 | **Voice cloning** | Effet "wow" client démontré |
| 9 | **Approche MVP explicite** | Méthodologie claire pour itérer |

---

## Points Faibles / Fragilités Identifiées

| # | Fragilité | Type | Impact | Détail |
|---|---|---|---|---|
| 1 | **Pas de gestion multi-employés** | OBSERVÉ | Élevé | Un seul calendrier, pas de choix de coiffeur réel |
| 2 | **Pas de confirmation SMS/email réel** | OBSERVÉ | Moyen | L'agent dit "je vous envoie l'email" mais c'est pas configuré |
| 3 | **Webhook unique pour 2 tools** | OBSERVÉ | Moyen | Architecture fragile si + de tools |
| 4 | **Pas de gestion des annulations/modifications** | OBSERVÉ | Élevé | Seuls check + book. Pas de cancel/modify |
| 5 | **Pas de validation des données** | HYPOTHÈSE | Moyen | Nom, email, téléphone non validés avant booking |
| 6 | **Pas de gestion de concurrence** | HYPOTHÈSE | Élevé | Deux appels simultanés pourraient double-booker |
| 7 | **Dépendance Gemini Flash** | OBSERVÉ | Moyen | Si Gemini down, tout l'agent tombe |
| 8 | **Pas d'intégration logiciel métier** | OBSERVÉ | Élevé | Salons utilisent Planity, pas Google Calendar |
| 9 | **Timezone hardcodé** | OBSERVÉ | Faible | Europe centrale, problème si client dans un autre fuseau |
| 10 | **Knowledge base = URL scraping** | OBSERVÉ | Moyen | Pas de contrôle fin sur les infos retournées |
| 11 | **Transcription noms propres** | OBSERVÉ | Faible | "Sdiri" devient "Zidiri" → erreur dans le booking |
| 12 | **Pas de RGPD structuré** | OBSERVÉ | Élevé | Mentionné en passant mais pas implémenté |
| 13 | **Template n8n non fourni publiquement** | OBSERVÉ | Moyen | Commentaire demandant "comment avoir la template ?" |

---

## Mapping Décisionnel pour Notre Projet

### ✅ Ce qu'on GARDE de la vidéo

| Élément | Raison |
|---|---|
| **Cas d'usage salon de coiffure** | C'est exactement notre cible |
| **Architecture ElevenLabs comme front vocal** | Meilleure qualité voix FR du marché |
| **n8n comme orchestrateur** | Open source, flexible, coût maîtrisé |
| **Approche MVP → V1** | Méthodologie validée terrain |
| **Transfert humain obligatoire** | Non négociable, conseil production |
| **Transparence IA** | Bonne pratique UX et légale |
| **Multilangue FR + EN** | Paris = ville touristique |
| **Structure webhook pour tools** | Pattern simple et fonctionnel |

### 🔄 Ce qu'on REMPLACE / AMÉLIORE

| Élément vidéo | Notre amélioration | Raison |
|---|---|---|
| Google Calendar brut | **Notre API réservation (FastAPI + PostgreSQL)** | Multi-employés, règles métier, conflits, durées |
| 2 tools seulement | **7 tools** (search, book, modify, cancel, list_services, get_booking, transfer_human) | Couverture fonctionnelle complète |
| Pas de validation | **Validation stricte** des inputs (téléphone, dates, noms) | Éviter les bookings erronés |
| Webhook unique | **Endpoints dédiés** par action | Clarté, debug, monitoring |
| Pas de confirmation | **SMS/WhatsApp de confirmation** via n8n | UX professionnelle |
| URL scraping pour KB | **Document structuré** (FAQ réelle du salon) | Contrôle des réponses |
| Gemini Flash seul | **Claude/GPT-4o avec fallback** | Fiabilité + qualité raisonnement FR |
| Pas de dashboard | **Dashboard admin** (planning, analytics) | Valeur ajoutée pour le gérant |
| Pas d'intégration Planity | **Évaluer intégration Planity** (commentaire pertinent) | Les vrais salons utilisent Planity |
| Pas de race condition handling | **PostgreSQL exclusion constraint + Redis lock** | Pas de double-booking |
| RGPD non traité | **Consentement + mention enregistrement** | Obligation légale |

### ❌ Ce qu'on NE FAIT PAS (pour le MVP)

| Élément | Raison |
|---|---|
| Voice cloning | Effet wow mais pas prioritaire pour MVP |
| Appels sortants (prospection) | Hors scope salon — entrant uniquement |
| Agent texte (chat) | Focus vocal uniquement pour MVP |
| Intégration CRM | Pas pertinent pour un salon de coiffure |
| Workflow ElevenLabs (arbre de décision) | Le LLM gère suffisamment bien le routing |

---

## Coûts Estimés (basé sur les données vidéo)

| Poste | Coût estimé |
|---|---|
| ElevenLabs Agent | Plan à partir de ~$5/mois (free tier disponible pour tests) |
| n8n | Self-hosted gratuit (ou cloud ~$20/mois) |
| Twilio | ~$1/mois par numéro + ~$0.01/min |
| LLM (par appel ~2 min) | ~$0.005-0.02 selon modèle |
| **Total estimé/mois** | **~$25-50** (pour usage modéré ~100 appels/mois) |

---

## Lexique Technique (pour référence)

| Terme | Définition dans le contexte |
|---|---|
| STT | Speech-to-Text — transcription voix → texte |
| TTS | Text-to-Speech — synthèse texte → voix |
| LLM | Large Language Model — cerveau de l'agent |
| Webhook | Point d'entrée HTTP pour communication inter-services |
| Tool/Function | Capacité d'action du LLM sur un système externe |
| Knowledge Base | Base de données de connaissances pour le RAG |
| System Prompt | Instructions permanentes qui cadrent le comportement du LLM |
| MCP | Model Context Protocol — standard pour connecter des tools au LLM |
