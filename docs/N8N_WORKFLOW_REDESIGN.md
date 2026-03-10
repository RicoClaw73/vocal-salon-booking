# Redesign Workflow n8n — Agent Vocal Salon

> Basé sur le reverse engineering de la vidéo Yassine Sdiri (2026-03-08)
> Objectif : passer de la démo vidéo à une architecture production pour salon de coiffure

---

## Comparaison : Vidéo vs Notre Design

| Aspect | Vidéo (OBSERVÉ) | Notre redesign |
|---|---|---|
| Tools | 2 (check_availability, book_appointment) | 7 tools complets |
| Backend | Google Calendar direct | FastAPI + PostgreSQL + Google Calendar sync |
| Webhook | 1 webhook unique, branching IF | 1 webhook par tool (ou router dédié) |
| Employés | 1 calendrier, pas de choix coiffeur | Multi-employés avec compétences |
| Validation | Aucune | Validation téléphone, date, nom |
| Confirmation | Non implémenté | SMS/WhatsApp via n8n |
| Annulation | Non implémenté | Tool cancel_booking |
| Modification | Non implémenté | Tool modify_booking |
| Concurrence | Non gérée | PostgreSQL exclusion + Redis lock |
| Monitoring | Console ElevenLabs | n8n executions + logs API |

---

## Architecture Cible n8n

### Vue d'ensemble

```
Twilio ──→ ElevenLabs Agent ──→ n8n (orchestrateur) ──→ FastAPI Backend
                                       │                      │
                                       ├──→ SMS (Twilio)      ├──→ PostgreSQL
                                       └──→ WhatsApp          └──→ Redis (locks)
                                            (optionnel)
```

### Workflow 1 : Router Principal

```
┌──────────────┐
│  Webhook     │ POST /webhook/vocal-agent
│  (entrée)    │ Header: x-tool-name
└──────┬───────┘
       │
┌──────▼───────┐
│   Switch     │ sur header "x-tool-name"
│   (router)   │
└──┬──┬──┬──┬──┘
   │  │  │  │
   ▼  ▼  ▼  ▼   (7 branches → sub-workflows ou nœuds)
```

### Workflow 2 : search_availability

```
Webhook ──→ Validate Input ──→ HTTP Request (GET /api/v1/availability/search)
                                    │
                                    ▼
                              Format Response ──→ Webhook Response
                              (slots lisibles)
```

**Paramètres d'entrée (depuis ElevenLabs tool)** :
```json
{
  "tool": "search_availability",
  "service_type": "coupe_homme",
  "preferred_date": "2026-03-05",
  "preferred_time": "11:00",
  "employee_preference": null
}
```

**Réponse vers ElevenLabs** :
```json
{
  "available_slots": [
    {"date": "2026-03-05", "time": "10:00", "employee": "Sophie", "duration": 30},
    {"date": "2026-03-05", "time": "14:00", "employee": "Marc", "duration": 30}
  ],
  "message": "2 créneaux disponibles le mercredi 5 mars"
}
```

### Workflow 3 : create_booking

```
Webhook ──→ Validate Input ──→ HTTP Request (POST /api/v1/bookings)
                                    │
                               ┌────▼────┐
                               │ Success? │
                               └──┬───┬──┘
                                  │   │
                              ┌───▼┐ ┌▼────────┐
                              │ OK │ │ Conflict │
                              └─┬──┘ └────┬────┘
                                │         │
                      ┌─────────▼──┐  ┌───▼──────────┐
                      │Send SMS    │  │Return error   │
                      │confirmation│  │"créneau pris" │
                      └─────────┬──┘  └──────────────┘
                                │
                      Webhook Response
```

**Paramètres d'entrée** :
```json
{
  "tool": "create_booking",
  "client_name": "Yassine Sdiri",
  "client_phone": "+33678901234",
  "service_id": "coupe_homme",
  "employee_id": null,
  "start_at": "2026-03-05T10:00:00+01:00"
}
```

### Workflow 4 : cancel_booking

```
Webhook ──→ Lookup booking (GET /api/v1/bookings?phone=X)
                │
                ▼
         Confirm found ──→ PATCH /api/v1/bookings/:id (status=cancelled)
                                │
                                ▼
                          Send SMS annulation ──→ Webhook Response
```

### Workflow 5 : modify_booking

```
Webhook ──→ Lookup booking ──→ Check new slot available
                                    │
                               ┌────▼────┐
                               │ Dispo?  │
                               └──┬───┬──┘
                              ┌───▼┐ ┌▼───┐
                              │Oui │ │Non │
                              └─┬──┘ └─┬──┘
                                │      │
                    PATCH booking   Return alternatives
                         │
                    Send SMS modif ──→ Webhook Response
```

### Workflow 6 : list_services

```
Webhook ──→ GET /api/v1/services ──→ Format lisible ──→ Webhook Response
```

### Workflow 7 : get_booking_info

```
Webhook ──→ GET /api/v1/bookings?phone=X ──→ Format lisible ──→ Webhook Response
```

### Workflow 8 : transfer_to_human

```
Webhook ──→ Log reason ──→ Return transfer instruction ──→ Webhook Response
                │
                ▼
          Notification SMS/email au gérant
          "Client demande un humain, raison: ..."
```

---

## Configuration ElevenLabs Tools (spécification)

### Tool 1 : search_availability
```
Name: search_availability
Description: Cherche les créneaux disponibles pour un service donné à une date/heure souhaitée. Utilise cet outil quand le client veut prendre rendez-vous ou connaître les disponibilités.
Method: POST
URL: https://<n8n-host>/webhook/vocal-agent
Headers: x-tool-name: search_availability
Body params:
  - service_type (string, required): type de service demandé
  - preferred_date (string, optional): date souhaitée format YYYY-MM-DD
  - preferred_time (string, optional): heure souhaitée format HH:MM
  - employee_preference (string, optional): nom du coiffeur souhaité
```

### Tool 2 : create_booking
```
Name: create_booking
Description: Crée une réservation confirmée. Utilise cet outil UNIQUEMENT après avoir vérifié la disponibilité ET obtenu confirmation du client.
Method: POST
URL: https://<n8n-host>/webhook/vocal-agent
Headers: x-tool-name: create_booking
Body params:
  - client_name (string, required): nom complet du client
  - client_phone (string, required): numéro de téléphone
  - service_id (string, required): identifiant du service
  - start_at (string, required): datetime ISO 8601
  - employee_id (string, optional): coiffeur choisi
```

### Tool 3 : cancel_booking
```
Name: cancel_booking
Description: Annule un rendez-vous existant. Demande confirmation au client avant d'annuler.
Body params:
  - client_phone (string, required): pour lookup du RDV
  - booking_id (string, optional): si connu
  - reason (string, optional): raison annulation
```

### Tool 4 : modify_booking
```
Name: modify_booking
Description: Modifie un rendez-vous existant (date, heure, service).
Body params:
  - client_phone (string, required)
  - booking_id (string, optional)
  - new_date (string, optional)
  - new_time (string, optional)
  - new_service (string, optional)
```

### Tool 5 : list_services
```
Name: list_services
Description: Liste les services disponibles au salon avec les tarifs.
Body params:
  - category (string, optional): filtrer par catégorie (homme, femme, coloration...)
```

### Tool 6 : get_booking_info
```
Name: get_booking_info
Description: Retrouve les informations d'un rendez-vous existant.
Body params:
  - client_phone (string, required)
```

### Tool 7 : transfer_to_human
```
Name: transfer_to_human
Description: Transfère l'appel à un membre de l'équipe. Utilise cet outil quand tu ne peux pas répondre à la demande ou que le client le demande explicitement.
Body params:
  - reason (string, required): raison du transfert
  - urgency (string, optional): low/medium/high
```

---

## Prompt Système Amélioré (Squelette)

```markdown
# Rôle
Tu es Marine, réceptionniste IA du salon [NOM_SALON], situé [ADRESSE].

# Tâche
Tu gères les appels entrants : prise de RDV, modifications, annulations, questions sur les services et le salon.

# Règles conversationnelles
- Parle en français, tutoie/vouvoie selon le client (vouvoyer par défaut)
- Réponses courtes (2-3 phrases max par tour)
- Ne coupe JAMAIS la parole
- Ne donne JAMAIS d'information incertaine — dis "je vais vérifier"
- Confirme TOUJOURS les infos critiques (date, heure, service) avant de booker
- Épelle le nom du client si doute sur la transcription

# Outils disponibles
- search_availability : chercher des créneaux libres
- create_booking : réserver un créneau confirmé
- cancel_booking : annuler un RDV
- modify_booking : modifier un RDV
- list_services : lister les services et tarifs
- get_booking_info : retrouver un RDV existant
- transfer_to_human : transférer à un humain

# Workflow type
1. Saluer → identifier le besoin
2. Si RDV : demander service + date/heure souhaitée
3. Utiliser search_availability
4. Proposer les créneaux (3 max)
5. Confirmer choix → demander nom + téléphone
6. Utiliser create_booking
7. Confirmer le récapitulatif

# Garde-fous
- Si le client est agressif ou demande un humain → transfer_to_human immédiat
- Si 3 tentatives échouent → proposer de rappeler ou transfer_to_human
- Mention obligatoire : "Cet appel est géré par un assistant IA et peut être enregistré"
- Ne jamais inventer de disponibilité sans avoir appelé search_availability
```

---

## Plan d'Implémentation n8n

| Phase | Tâche | Priorité | Dépendance |
|---|---|---|---|
| 1 | Setup n8n (self-hosted Docker) | P0 | - |
| 2 | Webhook router principal | P0 | Phase 1 |
| 3 | search_availability workflow | P0 | API backend Phase 1.3 |
| 4 | create_booking workflow | P0 | API backend Phase 1.2 |
| 5 | SMS confirmation (Twilio) | P1 | Workflow 4 |
| 6 | cancel_booking workflow | P1 | API backend Phase 1.2 |
| 7 | modify_booking workflow | P1 | API backend Phase 1.2 |
| 8 | list_services workflow | P2 | API backend Phase 1.2 |
| 9 | get_booking_info workflow | P2 | API backend Phase 1.2 |
| 10 | transfer_to_human workflow | P1 | Twilio call forwarding |
| 11 | Error handling global | P1 | Tous workflows |
| 12 | Monitoring/alerting | P2 | Tous workflows |

---

## Risques Spécifiques n8n

| Risque | Impact | Mitigation |
|---|---|---|
| n8n self-hosted tombe | Agent vocal sans backend | Docker restart policy + health check |
| Latence webhook > 3s | ElevenLabs timeout | Cache Redis pour services/dispo fréquents |
| Webhook Response manquant | Agent bloqué | Timeout + fallback "je n'ai pas pu vérifier" |
| Données sensibles dans n8n logs | RGPD | Configurer retention + anonymisation |
| Rate limiting Google Calendar API | Échecs silencieux | Notre API PostgreSQL est le master, pas GCal |
