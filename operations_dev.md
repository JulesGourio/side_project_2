# Opérations manuelles — copie DEV de l'UAT (Jules)

Même règle que `OPERATIONS.md` : Claude n'a pas accès à Databricks, chaque
étape à faire à la main est listée ici, prête à copier-coller. Cocher et dater
une fois faite. Ce fichier ne couvre **que** la mise en place de l'environnement
DEV ; `OPERATIONS.md` reste la référence pour UAT / uat-test / PROD.

Branche : **`claude/adoring-cray-trexmn`** (= `audit/doc-compare` + copie DEV).
Pas de PR ; le zip à déployer est celui de cette branche.

Rédigé progressivement le 2026-10-05 — les sections marquées _(à venir)_ ne
sont pas encore écrites.

## Décisions prises (2026-10-05)

| Sujet | Choix |
|---|---|
| Workspace | DEV — `https://dbc-c623749d-731b.cloud.databricks.com`, profil CLI `DEV` |
| Cible bundle | `dev` (réécrite en miroir de `qualibot-uat`) |
| App | `qualibot` (même nom qu'en UAT, workspace différent) |
| Catalog / schema | `dev_landingzone.qualibot` (écriture), `dev_proj.qualibot` (projet) |
| Suffixe tables / index | `_v1`, comme l'UAT |
| Identité des jobs (`run_as`) | SP DEV `fde6ff28-739f-4a41-b61e-604a298c8478` |
| Lakebase | projet `qualibot` neuf, base `doccompare` vide (l'app crée ses tables) |
| Corpus | copie UAT → DEV via le volume `uat_landingzone.qualibot.staging` (format Delta) : chunks + état du pipeline. **Aucun run complet du pipeline de parsing** |
| Phase archive avant 2018 | ignorée en DEV (plafond 0, fiches hors RAG, pas de `chunks_full`) |
| Export côté UAT | run ponctuel `databricks jobs submit` — la cible `qualibot-uat` n'est pas modifiée |
| Vector Search | endpoint `qualibot` créé en DEV, 3 index `_v1`, embeddings `databricks-qwen3-embedding-0-6b` |
| Knowledge Assistants | `qualibot_ALL_v2` / `qualibot_AS_v2` / `qualibot_IS_v2` (mêmes noms qu'en UAT) |
| Contrôle par groupe (can_chat / can_compare) | **désactivé temporairement en DEV seulement** (`CAPS_BYPASS=true`) |
| Accès à l'app (ACL) | mêmes groupes qu'en UAT + `jules.gourio.external@latecoere.aero` + `mehdi.lamrani@databricks.com` + SP DEV |
| Jobs répliqués | pipeline de parsing, export Lakebase, stop/start de l'app, provisioning KA, migrations Lakebase — **tous planifiés en PAUSED** |
| Jobs DEV existants | `lakebase_import_uat_to_dev` et `score_production_qa` gardés tels quels (UNPAUSED) |
| Bitbucket | pipelines manuelles `deploy-dev` / `deploy-dev-jobs`, environnement de déploiement Bitbucket **Development** |
| LibreOffice (aperçu PDF exact) | archive copiée depuis l'UAT par la copie |
| Modèles Docling | volume DEV existant `/Volumes/dev_landingzone/qualibot/docling_models/docling_models` |
| Endpoints LLM | identiques à l'UAT (disponibles en DEV) |

## Vue d'ensemble _(à venir)_

## Prérequis _(à venir)_

## À faire _(à venir)_

## Fait

_(rien de confirmé pour l'instant)_
