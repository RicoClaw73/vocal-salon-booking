# CLAUDE.md — Instructions projet (Claude Code)

Ce fichier cadre le comportement de Claude Code dans ce repo.

## Scope

Repo: `projects/vocal-salon-portfolio`

## Règle n8n (obligatoire)

Quand la tâche concerne un **workflow n8n** (création, édition, validation, debug, architecture):

1. Utiliser en priorité les ressources locales suivantes:
   - `tools/n8n-skills/`
   - `tools/n8n-mcp/`
   - `tools/n8n-mcp-cc-buildier/`
2. Lire d’abord les guides pertinents (SKILL.md, guides de validation/config/patterns) avant de proposer une implémentation.
3. Appliquer les conventions n8n-mcp (formats de nodeType, validation profiles, update partiel itératif, etc.).
4. Préférer une approche outillée et reproductible (scripts/docs/steps claires) plutôt qu’une réponse abstraite.

## Règle navigateur / debug UI (complémentaire)

Quand la tâche implique un navigateur réel (debug console/network, validation UI, test E2E web), utiliser en complément:

- `tools/chrome-devtools-mcp/`

Ce MCP est secondaire par rapport aux outils n8n pour les tâches purement workflow/backend, mais prioritaire dès qu’un diagnostic navigateur est requis.

## Priorité d’usage

- Si une info existe déjà dans `tools/n8n-*`, elle est prioritaire sur des hypothèses génériques.
- En cas de doute ou conflit entre sources, expliciter le conflit et proposer l’option la plus sûre.

## Sortie attendue

Pour chaque tâche n8n, fournir:

- décisions techniques prises;
- étapes d’exécution;
- points de validation;
- risques/limites éventuels.
