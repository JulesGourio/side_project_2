# Opérations manuelles — copie DEV de l'UAT (Jules)

Même règle que `OPERATIONS.md` : Claude n'a pas accès à Databricks, chaque
étape à faire à la main est listée ici, prête à copier-coller. Cocher et dater
une fois faite. Ce fichier ne couvre **que** la mise en place de l'environnement
DEV ; `OPERATIONS.md` reste la référence pour UAT / uat-test / PROD.

Branche : **`claude/adoring-cray-trexmn`** (= `audit/doc-compare` + copie DEV).
Pas de PR ; le zip à déployer est celui de cette branche.

Mis à jour le 2026-10-05. Rien de ce qui suit n'a encore été confirmé comme fait.

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
| R | Suppression du reliquat de l'ancien Qualibot DEV (app, vieux index et tables) | DEV | non |
| I | Infra DEV : bind + `bundle deploy -t dev` (app, Lakebase, volumes, jobs) | DEV | non |
| C | Copie : job `qualibot-copy-uat-to-dev` (tables + endpoint + 3 index) | DEV | lecture du volume staging |
| K | Knowledge Assistants DEV + report des endpoints dans `target_env.json` | DEV | non |
| A | Déploiement du code de l'app + tests | DEV | non |
| B | Bitbucket : environnement « Development » + pipelines `deploy-dev` | Bitbucket | non |

Ordre imposé : P → E → R → I → C → K → A. B peut se faire à tout moment, mais
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

- [x] **P1. Profil CLI `DEV`** _(OK 2026-10-05)_ sur la machine de déploiement

  ```powershell
  databricks auth login --host https://dbc-c623749d-731b.cloud.databricks.com --profile DEV
  databricks current-user me --profile DEV
  ```

- [x] **P2. Le SP DEV existe dans le workspace DEV** _(OK 2026-10-05 : `job-runner-sa-dev`, ACTIVE)_ et vous pouvez l'utiliser
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

- [x] **P4. Catalog `dev_proj`** _(existe, owner `leap-core-service_accounts-dev` ; schema `dev_proj.qualibot` absent → créé par le bundle, il faut CREATE SCHEMA sur `dev_proj`)_ : le bundle crée le schema `dev_proj.qualibot`
  mais pas le catalog.

  ```powershell
  databricks catalogs get dev_proj --profile DEV
  ```

  S'il n'existe pas : me le dire (je pointe le schema projet ailleurs) ou le
  faire créer par un admin.

- [x] **P5. Ce qui existe déjà dans `dev_landingzone.qualibot`** _(2026-10-05 : schema présent ;
  seul volume `docling_models` ; aucun projet Lakebase ; **app `qualibot` déjà
  créée** le 2026-07-09, jamais déployée, SP `8e411164-a7e8-46ff-8013-8c56af2c3656`,
  modifiée le 2026-10-05 par Mehdi → **supprimée en R2** ; endpoint Vector Search
  `qualibot` listé avec 3 index puis introuvable → R1/R3)_ :

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

- [x] **P6. Groupes de compte visibles en DEV** _(2026-10-05 : CoreAdmin, CoreDev,
  leap-qualibot-service-accounts et Mehdi présents ; **les deux groupes
  End-users-Qualibot-* n'existent pas en DEV** → retirés de la cible `dev`)_ : `Role-Project-LEAP-CoreAdmin`,
  `Role-Project-LEAP-CoreDev`, `Role-Project-LEAP-End-users-Qualibot-DocCompare`,
  `Role-Project-LEAP-End-users-Qualibot-ChatBot`, `leap-qualibot-service-accounts`,
  et l'utilisateur `mehdi.lamrani@databricks.com` (sinon le `bundle deploy`
  échoue sur la permission correspondante — me dire lequel manque, je le retire).

  ```powershell
  databricks groups list --profile DEV --filter "displayName sw 'Role-Project-LEAP'"
  databricks groups list --profile DEV --filter "displayName eq 'leap-qualibot-service-accounts'"
  databricks users list --profile DEV --filter "userName eq 'mehdi.lamrani@databricks.com'"
  ```

## À faire

PowerShell, depuis la racine du projet (branche `claude/adoring-cray-trexmn`)
sur la machine de déploiement. Toujours commencer la session par :

```powershell
Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
```

### E. Export du corpus UAT (workspace UAT, run ponctuel)

Lecture seule sur les tables UAT ; écrit uniquement dans
`/Volumes/uat_landingzone/qualibot/staging/dev_copy/`. La cible `qualibot-uat`
du bundle n'est pas touchée. Tables exportées (`_v1`) : `chunks`,
`src_chunks_as`, `src_chunks_is`, `_pipeline_checkpoint`, `processed_files`,
`image_metadata`, `parse_manifest`, `category_reference` + l'archive
LibreOffice. Rien de la phase archive avant 2018.

- [ ] **E1. Importer le notebook dans votre dossier et lancer le run**

  ```powershell
  databricks workspace mkdirs /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy --profile UAT
  databricks workspace import /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy/export_uat_to_staging `
    --file utils\databricks_ops\dev_copy\export_uat_to_staging.py --format SOURCE --language PYTHON --overwrite --profile UAT
  databricks jobs submit --json "@utils/databricks_ops/dev_copy/export_uat_submit.json" --profile UAT
  ```

  Compute serverless, sous votre identité. Si l'écriture Delta dans le volume
  est refusée, relancer en Parquet : ajouter dans le JSON, sous
  `notebook_task`, `"base_parameters": {"FORMAT": "parquet"}` (l'import DEV lit
  le format dans le manifeste, rien d'autre à changer).

- [ ] **E2. Vérifier le manifeste** (nombre de lignes par table), depuis UAT
  puis **depuis DEV** (preuve que DEV lit bien ce volume) :

  ```powershell
  databricks fs cat dbfs:/Volumes/uat_landingzone/qualibot/staging/dev_copy/manifest.json --profile UAT
  databricks fs ls  dbfs:/Volumes/uat_landingzone/qualibot/staging/dev_copy/tables --profile DEV
  ```

### R. Supprimer le reliquat de l'ancien Qualibot DEV (décidé le 2026-10-05)

Constat I0 (2026-10-05) : l'app `qualibot` DEV existe hors du bundle (créée à
la main le 2026-07-09, jamais déployée) ; `dev_landingzone.qualibot` contient
d'anciennes tables de chunks et d'anciens index Vector Search (`chunks_all`,
`chunks_as`, `chunks_is` + leurs `*_writeback_table`). Tout est supprimé puis
recréé par le bundle et `copy_uat_to_dev`.

**Ne pas toucher** : les tables écrites par les jobs DEV conservés
(`lakebase_import_uat_to_dev` : `chat_feedbacks`, `chat_messages`,
`chat_sessions`, `errors`, `feedbacks`, `impact_*`, `knowledge_base_metadata`,
`llm_requests`, `messages`, `summary_cache`, `users` ; `score_production_qa` :
`chat_quality_*`), le volume `docling_models`, et `glossary_terms` /
`dnt_rules` (Translator, autre projet).

- [ ] **R1. Identifier ce qui reste côté Vector Search**

  ```powershell
  databricks vector-search-endpoints list-endpoints --profile DEV -o json | ConvertFrom-Json | Select-Object name, num_indexes
  databricks vector-search-indexes get-index dev_landingzone.qualibot.chunks_all --profile DEV
  databricks vector-search-indexes get-index dev_landingzone.qualibot.chunks_as  --profile DEV
  databricks vector-search-indexes get-index dev_landingzone.qualibot.chunks_is  --profile DEV
  ```

- [ ] **R2. Supprimer l'ancienne app** (son SP part avec elle ; le bundle en
  recrée une avec un nouveau SP). Attendre que `apps get` réponde « not found »
  avant le bloc I :

  ```powershell
  databricks apps delete qualibot --profile DEV
  databricks apps get qualibot --profile DEV
  ```

- [ ] **R3. Supprimer les anciens index** (ceux que R1 a trouvés), puis
  l'endpoint `qualibot` s'il existe encore (il doit être vide ; `copy_uat_to_dev`
  le recrée) :

  ```powershell
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_all --profile DEV
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_as  --profile DEV
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_is  --profile DEV
  databricks vector-search-endpoints delete-endpoint qualibot --profile DEV
  ```

- [ ] **R4. Supprimer les anciennes tables du pipeline** (SQL editor DEV ;
  tables managées : `UNDROP TABLE` possible pendant 7 jours). Les
  `*_writeback_table` disparaissent normalement avec leur index (R3) ; les
  `DROP … IF EXISTS` couvrent le cas contraire :

  ```sql
  DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks_all_writeback_table;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks_as_writeback_table;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks_is_writeback_table;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks;
  DROP TABLE IF EXISTS dev_landingzone.qualibot._pipeline_checkpoint;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.processed_files;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.intraqual_docs;
  ```

  Vérifier qu'il ne reste que les tables « ne pas toucher » ci-dessus :

  ```powershell
  (databricks tables list dev_landingzone qualibot --profile DEV -o json | ConvertFrom-Json).name
  ```

### I. Infra DEV (`bundle deploy -t dev`)

Crée l'app `qualibot`, le projet Lakebase `qualibot` (base `doccompare`), le
schema `dev_proj.qualibot`, les volumes, les rôles Postgres et les jobs ;
met à jour le job de parsing DEV existant (il écrit désormais dans
`dev_landingzone.qualibot`, tables `_v1`, planning PAUSED).

- [x] **I0. État du bundle** _(2026-10-05 : app, schemas, volumes, Lakebase et
  nouveaux jobs « not deployed » ; `parsing_pipeline`, `lakebase_import_uat_to_dev`,
  `score_production_qa` déjà déployés → mis à jour sur place)_

- [ ] **I1. Binder le schema existant** (seul objet à binder ; l'app est
  supprimée en R2 et recréée) :

  ```powershell
  databricks bundle deployment bind qualibot_schema dev_landingzone.qualibot -t dev --profile DEV --auto-approve
  ```

- [ ] **I2. Déployer l'infra + l'app** (build du front, `bundle deploy`,
  démarrage de l'app, `apps deploy`) :

  ```powershell
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv dev -Infra
  ```

  `-Infra` accorde au SP de l'app l'accès aux volumes : il faut MANAGE sur
  `dev_landingzone` (ou être owner du schema). En cas de refus, le faire
  lancer par un admin du catalog, ou me le dire.

  À ce stade : la comparaison marche, l'impact search et le chat non (pas
  encore d'index ni de KA) — normal.

- [ ] **I3. Vérifier que tous les nouveaux plannings sont en PAUSED** (UI
  Jobs DEV, filtre `qualibot`) : `D_1_qualibot-parsing-pipeline-dev`,
  `qualibot-lakebase-export-dev-to-volume-dev`, `apps-stop-nightly-dev`,
  `qualibot-stop-weekend-dev`, `qualibot-start-weekend-dev` = PAUSED.
  `qualibot-lakebase-import-uat-to-dev` et `qualibot-score-production-qa`
  restent actifs, comme avant.

- [ ] **I4. Me donner le client id du SP de la nouvelle app DEV** (je le fige
  dans `databricks.yml`, comme en UAT) :

  ```powershell
  (databricks apps get qualibot --profile DEV -o json | ConvertFrom-Json).service_principal_client_id
  ```

### C. Copie du corpus + index Vector Search (job DEV)

Job `qualibot-copy-uat-to-dev`, déclenchement manuel, sous le SP DEV :
`1_import_tables` (snapshot → `dev_landingzone.qualibot.*_v1`, rétention
60 jours, Change Data Feed sur les 3 tables de chunks, archive LibreOffice
→ volume `doc_compare`) → `2_vector_search_endpoint` (crée l'endpoint
`qualibot` s'il manque) → `3_sync_indexes` (crée les 3 index `_v1` et les
synchronise — embedding de tout le corpus, peut être long). **Le pipeline de
parsing n'est pas lancé.**

- [ ] **C1. Lancer la copie**

  ```powershell
  databricks bundle run copy_uat_to_dev -t dev --profile DEV
  ```

  Une table déjà présente n'est pas écrasée (message « left as is »). Pour
  forcer : `--params overwrite=true`, puis supprimer l'index de chaque table
  de chunks remplacée (UI Vector Search) et relancer : `3_sync_indexes` le recrée.

- [ ] **C2. Vérifier les tables** (SQL editor DEV) — mêmes nombres que le
  manifeste E2 :

  ```sql
  SELECT 'chunks' AS t, count(*) FROM dev_landingzone.qualibot.chunks_v1
  UNION ALL SELECT 'as', count(*) FROM dev_landingzone.qualibot.src_chunks_as_v1
  UNION ALL SELECT 'is', count(*) FROM dev_landingzone.qualibot.src_chunks_is_v1
  UNION ALL SELECT 'checkpoint', count(*) FROM dev_landingzone.qualibot._pipeline_checkpoint_v1
  UNION ALL SELECT 'processed', count(*) FROM dev_landingzone.qualibot.processed_files_v1
  UNION ALL SELECT 'images', count(*) FROM dev_landingzone.qualibot.image_metadata_v1
  UNION ALL SELECT 'manifest', count(*) FROM dev_landingzone.qualibot.parse_manifest_v1
  UNION ALL SELECT 'categories', count(*) FROM dev_landingzone.qualibot.category_reference_v1;

  SHOW TBLPROPERTIES dev_landingzone.qualibot.chunks_v1;  -- enableChangeDataFeed = true, retentions = interval 60 days
  ```

- [ ] **C3. Vérifier les index** (UI Vector Search DEV, endpoint `qualibot`) :
  `chunks_index_v1`, `chunks_as_index_v1`, `chunks_is_index_v1` ONLINE, nombre
  de lignes indexées = nombre de lignes des tables. Si le job s'arrête avant la
  fin de l'embedding (« Still running after 120 min »), ce n'est pas une erreur :
  la sync continue côté serveur.

### K. Knowledge Assistants DEV

- [ ] **K1. Créer les 3 KA** (`qualibot_ALL_v2` / `qualibot_AS_v2` /
  `qualibot_IS_v2`, sur les index `_v1` DEV, CAN_QUERY pour le SP de l'app) :

  ```powershell
  databricks bundle run provision_knowledge_assistant_dev -t dev --profile DEV
  ```

- [ ] **K2. Me donner les 3 `endpoint_name`** affichés dans le résumé du run
  (`[ALL] … -> endpoint_name=ka-…`, idem AS et IS). Je les mets dans le bloc
  `dev` de `utils/deploy/target_env.json` (`CHAT_ENDPOINT` = `CHAT_ENDPOINT_ALL`
  = ALL, `CHAT_ENDPOINT_AS`, `CHAT_ENDPOINT_IS`), puis bloc A.

### A. Déploiement de l'app avec les endpoints KA + tests

- [ ] **A1. Redéployer le code** (après mon commit de K2) :

  ```powershell
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
  ```

- [ ] **A2. Contrôle par groupe désactivé** : ouvrir l'app avec un compte
  **hors** des groupes End-users (ex. `mehdi.lamrani@databricks.com`) — Chat
  et Compare accessibles, pas d'écran « Access denied ».

- [ ] **A3. Chat** : une question en ALL, AS et IS ; les citations et liens
  REF s'affichent.

- [ ] **A4. Compare** : deux révisions d'un document connu, Change Table,
  puis « Judge Impacted Docs » (index `dev_landingzone.qualibot.chunks_index_v1`) ;
  aperçu « Exact (PDF) » d'un DOCX (archive LibreOffice copiée en C1).
  En cas de 403 sur l'index, avec le client id de I4 :

  ```sql
  GRANT USE CATALOG ON CATALOG dev_landingzone TO `<client id I4>`;
  GRANT USE SCHEMA ON SCHEMA dev_landingzone.qualibot TO `<client id I4>`;
  GRANT SELECT ON TABLE dev_landingzone.qualibot.chunks_index_v1 TO `<client id I4>`;
  ```

- [ ] **A5. Date « documents as of »** : vide dans une base Lakebase neuve.
  Pour la remplir **sans** lancer la chaîne de parsing, uniquement la dernière
  tâche du job :

  ```powershell
  databricks bundle run parsing_pipeline -t dev --profile DEV --only 6_update_kb_metadata
  ```

### B. Bitbucket — pipelines DEV

- [ ] **B1. Créer l'environnement de déploiement « Development »**
  (Repository settings ▸ Deployments) avec la variable sécurisée
  `DATABRICKS_TOKEN` = PAT du SP DEV `fde6ff28-…` (optionnel :
  `DATABRICKS_HOST`, défaut `https://dbc-c623749d-731b.cloud.databricks.com`).
  Créer le PAT (en tant qu'admin du workspace DEV) :

  ```powershell
  databricks token-management create-obo-token fde6ff28-739f-4a41-b61e-604a298c8478 --lifetime-seconds 7776000 --comment "bitbucket qualibot dev" --profile DEV
  ```

  Le SP doit pouvoir déployer le bundle : CAN_MANAGE sur l'app (déclaré),
  sur le projet Lakebase (déclaré), droits UC de P3, et écriture sur
  `/Workspace/Shared/.bundle/qualibot/dev`.

- [ ] **B2. Premier essai** : Pipelines ▸ Run pipeline ▸ branche
  `claude/adoring-cray-trexmn` ▸ `deploy-dev-jobs` (ne touche pas l'app),
  puis `deploy-dev` (bundle + redéploiement de l'app).

### Plus tard — non fait, sur décision

- Réactiver le contrôle par groupe en DEV : passer `CAPS_BYPASS` à `false`
  (ou retirer la clé) dans le bloc `dev` de `target_env.json`, redéployer.
- Activer les plannings DEV (parsing quotidien, export Lakebase, stop/start
  de l'app) : me dire lesquels, je passe `pause_status` / `parsing_schedule_pause`
  à `UNPAUSED`.
- Copier aussi les images du volume `uat_landingzone.qualibot.images` (pas
  nécessaire au chat ni à l'impact search ; seulement si un run de parsing
  DEV doit retravailler des images déjà extraites).

## Fait

_(rien de confirmé pour l'instant)_
