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

## Vue d'ensemble

| Bloc | Quoi | Où | Touche l'UAT ? |
|---|---|---|---|
| P | Prérequis : droits, profil CLI, vérifications | DEV + UAT | lecture seule |
| E | Export du corpus UAT vers le volume staging (run ponctuel) | UAT | écrit seulement dans `uat_landingzone.qualibot.staging/dev_copy` |
| I | Infra DEV : bind + `bundle deploy -t dev` (app, Lakebase, volumes, jobs) | DEV | non |
| C | Copie : job `qualibot-copy-uat-to-dev` (tables + endpoint + 3 index) | DEV | lecture du volume staging |
| K | Knowledge Assistants DEV + report des endpoints dans `target_env.json` | DEV | non |
| A | Déploiement du code de l'app + tests | DEV | non |
| B | Bitbucket : environnement « Development » + pipelines `deploy-dev` | Bitbucket | non |

Ordre imposé : P → E → I → C → K → A. B peut se faire à tout moment, mais
les pipelines `deploy-dev*` ne marcheront qu'après P.

Ce qui change dans le code (branche `claude/adoring-cray-trexmn`) :

- `databricks.yml`, cible `dev` : réécrite en copie de `qualibot-uat`
  (app `qualibot`, projet Lakebase, schemas/volumes, jobs). Les jobs
  `lakebase_import_uat_to_dev` et `score_production_qa` sont repris à
  l'identique (toujours UNPAUSED).
- `resources/parsing_pipeline.job.yml` : `run_as` = SP DEV pour la cible `dev`
  (UAT/PROD gardent le leur). Planning PAUSED.
- `server/services/user.py` + `app.yaml` : nouveau flag `CAPS_BYPASS`
  (défaut `false`) qui accorde chat + compare à tout visiteur.
- `utils/deploy/target_env.json` : bloc `dev` (volumes, index, `CAPS_BYPASS=true`,
  endpoints KA vides tant qu'ils ne sont pas créés).
- `tests/test_deploy_config.py` : garde-fou — `CAPS_BYPASS` ne peut être
  activé que pour `dev`.
- `utils/databricks_ops/dev_copy/` : notebooks de la copie (export UAT,
  import DEV, endpoint Vector Search) + JSON du `jobs submit` UAT.

## Prérequis

- [ ] **P1. Profil CLI `DEV`** sur la machine de déploiement

  ```powershell
  databricks auth login --host https://dbc-c623749d-731b.cloud.databricks.com --profile DEV
  databricks current-user me --profile DEV
  ```

- [ ] **P2. Le SP DEV existe dans le workspace DEV** et vous pouvez l'utiliser
  en `run_as` (rôle « Service principal: User » sur le SP, sinon `bundle deploy`
  refuse le `run_as`) :

  ```powershell
  databricks service-principals list --profile DEV --filter "applicationId eq 'fde6ff28-739f-4a41-b61e-604a298c8478'"
  ```

- [ ] **P3. Droits Unity Catalog du SP DEV** (SQL editor DEV, en tant
  qu'owner/admin des catalogs) :

  ```sql
  -- écriture du corpus et des volumes DEV
  GRANT USE CATALOG ON CATALOG dev_landingzone TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  GRANT USE SCHEMA, CREATE TABLE, CREATE VOLUME, SELECT, MODIFY, READ VOLUME, WRITE VOLUME
    ON SCHEMA dev_landingzone.qualibot TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  -- lecture du snapshot UAT (même volume que lakebase_import_uat_to_dev)
  GRANT USE CATALOG ON CATALOG uat_landingzone TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  GRANT USE SCHEMA ON SCHEMA uat_landingzone.qualibot TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  GRANT READ VOLUME ON VOLUME uat_landingzone.qualibot.staging TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  -- sources Intraqual du pipeline de parsing (comme job-runner-sa-uat)
  GRANT USE CATALOG ON CATALOG prod_bronze TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  GRANT USE SCHEMA, SELECT ON SCHEMA prod_bronze.intraqual TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  GRANT USE CATALOG ON CATALOG prod_landingzone TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  GRANT USE SCHEMA, SELECT ON SCHEMA prod_landingzone.intraqual TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  GRANT READ VOLUME ON VOLUME prod_landingzone.intraqual.intraqual_documents TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  ```

  Si `uat_landingzone` n'est pas visible depuis DEV pour le SP (le job
  `lakebase_import_uat_to_dev` tourne sous **votre** identité, pas sous un SP),
  le bloc C échouera au 1er task : me le dire, on fait tourner
  `copy_uat_to_dev` sous votre identité à la place.

- [ ] **P4. Catalog `dev_proj`** : le bundle crée le schema `dev_proj.qualibot`
  mais pas le catalog.

  ```powershell
  databricks catalogs get dev_proj --profile DEV
  ```

  S'il n'existe pas : me le dire (je pointe le schema projet ailleurs) ou le
  faire créer par un admin.

- [ ] **P5. Ce qui existe déjà dans `dev_landingzone.qualibot`** (à binder
  plutôt que créer, bloc I) :

  ```powershell
  databricks schemas get dev_landingzone.qualibot --profile DEV
  databricks volumes list dev_landingzone qualibot --profile DEV
  databricks postgres list-projects --profile DEV
  databricks apps get qualibot --profile DEV
  databricks vector-search-endpoints list-endpoints --profile DEV
  ```

  Noter : le schema (existe sûrement), les volumes `doc_compare`, `test`,
  `images`, `staging` (existent ou non), un projet Lakebase `qualibot`
  (normalement absent), une app `qualibot` (normalement absente), un
  endpoint `qualibot` (normalement absent).

- [ ] **P6. Groupes de compte visibles en DEV** : `Role-Project-LEAP-CoreAdmin`,
  `Role-Project-LEAP-CoreDev`, `Role-Project-LEAP-End-users-Qualibot-DocCompare`,
  `Role-Project-LEAP-End-users-Qualibot-ChatBot`, `leap-qualibot-service-accounts`,
  et l'utilisateur `mehdi.lamrani@databricks.com` (sinon le `bundle deploy`
  échoue sur la permission correspondante — me dire lequel manque, je le retire).

  ```powershell
  databricks groups list --profile DEV --filter "displayName sw 'Role-Project-LEAP'"
  databricks groups list --profile DEV --filter "displayName eq 'leap-qualibot-service-accounts'"
  databricks users list --profile DEV --filter "userName eq 'mehdi.lamrani@databricks.com'"
  ```

## À faire _(à venir)_

## Fait

_(rien de confirmé pour l'instant)_
