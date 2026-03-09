# Business Case — Agent Vocal IA pour Salons de Coiffure

## Positionnement

### Le problème
Un salon de coiffure de 5 employés à Paris reçoit en moyenne 40-80 appels par jour. Les coiffeurs ne peuvent pas répondre au téléphone les mains dans les cheveux d'un client. Conséquences mesurables :

- **30-50% des appels sont manqués** en heure de pointe (source : retours terrain, à valider par enquête)
- Chaque appel manqué = potentiellement 50-150€ de CA perdu
- Un réceptionniste dédié coûte 2 000-2 500€/mois chargé (SMIC + charges Paris)
- Les solutions de réservation en ligne (Planity, Treatwell) ne captent que 40-60% de la clientèle — le reste appelle

### La solution
Un agent vocal IA qui :
- Répond à 100% des appels, 24h/24, 7j/7
- Comprend le langage naturel (pas de menu "tapez 1, tapez 2")
- Connaît le catalogue, les compétences de chaque coiffeur, et le planning en temps réel
- Réserve, modifie ou annule un RDV en < 2 minutes
- Envoie une confirmation SMS immédiate
- Transfère vers un humain quand nécessaire

## Marché cible

### TAM — Marché total adressable
- **86 000 salons de coiffure en France** (source : UNEC 2024)
- ~15 000 en Île-de-France
- Marché potentiel : 86 000 × 149€/mois = **153M€/an** (hypothèse basse)

### SAM — Marché serviceable
- Salons de 3+ coiffeurs (besoin réel de gestion d'appels) : ~30 000 salons
- Cible initiale : Paris + Île-de-France = ~5 000 salons

### SOM — Objectif réaliste 18 mois
- 50-100 salons abonnés
- MRR cible : 7 500 - 15 000€/mois

## Modèle économique

### Grille tarifaire proposée

| Offre | Prix/mois | Inclus | Cible |
|---|---|---|---|
| **Starter** | 149€ | 200 appels/mois, 1-3 coiffeurs, horaires simples | Petit salon |
| **Pro** | 249€ | 500 appels/mois, 4-7 coiffeurs, SMS + rappels | Salon moyen |
| **Premium** | 449€ | Appels illimités, multi-sites, dashboard analytics, support prioritaire | Groupe/franchise |

### Coût par appel (estimation)
| Composant | Coût unitaire | Durée moyenne 2min |
|---|---|---|
| Téléphonie (Twilio) | 0,02€/min | 0,04€ |
| STT (Deepgram) | 0,004€/min | 0,008€ |
| LLM (Claude Sonnet) | ~0,01€/appel | 0,01€ |
| TTS (ElevenLabs) | 0,02€/min audio généré | 0,02€ |
| **Total** | | **~0,08€/appel** |

→ À 200 appels/mois : coût infra = 16€. Marge brute > 85%.

### ROI client

| Métrique | Sans agent | Avec agent |
|---|---|---|
| Appels manqués | 30-50% | < 5% |
| CA récupéré (estimation) | - | 2 000-5 000€/mois |
| Coût réceptionniste | 2 500€/mois | 0€ (ou mi-temps) |
| Coût solution IA | 0€ | 149-249€/mois |
| **ROI net** | - | **10-20x le coût** |

## Argumentaire portfolio

### Pour le prospect salon
1. **"Vous ne ratez plus jamais un client"** — L'IA répond à chaque appel, même pendant les brushings.
2. **"Vos coiffeurs restent concentrés"** — Plus d'interruption téléphone en pleine coupe.
3. **"Ça coûte 10x moins qu'une réceptionniste"** — 149€/mois vs 2 500€/mois.
4. **"Vos clients adorent"** — Conversation naturelle, pas d'attente, SMS instantané.
5. **"Installation en 48h"** — On configure votre catalogue et vos horaires, et c'est parti.

### Pour le décideur technique (DSI franchise)
1. **Multi-sites natif** — Un seul système pour 10 ou 100 salons.
2. **API ouverte** — Intégration avec les logiciels de caisse existants.
3. **Dashboard centralisé** — Taux de remplissage, no-shows, tendances par salon.
4. **Scalable** — Infrastructure cloud, pas de matériel à installer.

### Différenciateurs vs concurrence

| Critère | Réservation en ligne (Planity) | Standard tél. classique | Notre agent vocal |
|---|---|---|---|
| Canal | Web/App uniquement | Téléphone humain | Téléphone IA |
| Disponibilité | 24/7 | Heures d'ouverture | 24/7 |
| Coût mensuel | 50-100€ | 2 500€ (réceptionniste) | 149-249€ |
| Compréhension client | Formulaire rigide | Excellente | Très bonne |
| Couverture clientèle | 40-60% (digitalisée) | 100% (qui appelle) | 100% (qui appelle) |
| Upsell intelligent | Non | Dépend de l'humain | Oui (forfaits, soins) |

## Stratégie de go-to-market

### Phase 1 — Preuve de concept (mois 1-2)
- Démo fonctionnelle avec salon fictif "Maison Éclat"
- 3-5 salons pilotes gratuits à Paris (contre testimonial)
- Itération sur le prompt et les edge cases réels

### Phase 2 — Lancement commercial (mois 3-6)
- Offre Starter à 149€/mois
- Acquisition : démarchage direct (porte-à-porte salons) + LinkedIn
- Objectif : 20 salons payants

### Phase 3 — Scale (mois 6-18)
- Offre Pro/Premium
- Partenariats avec fournisseurs de logiciel de caisse (Wavy, Shortcuts)
- Expansion géographique (Lyon, Marseille, Bordeaux)
- Objectif : 100 salons, MRR 20K€

## Hypothèses et limites

- Les estimations de CA récupéré sont des projections, pas des mesures. À valider avec les pilotes.
- Le coût par appel peut varier selon la durée et la complexité des conversations.
- Le taux d'acceptation de l'IA vocale par la clientèle senior (55+) est incertain.
- Le chiffre "73% préfèrent l'automatisé" dans le script de démo est une projection à valider.
- Ce business case suppose un positionnement milieu de gamme. Les salons low-cost (Quick Coiff etc.) ont des marges trop faibles.
