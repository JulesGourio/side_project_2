# Opérations manuelles — copie DEV de l'UAT (Jules)

Même règle que `OPERATIONS.md` : Claude n'a pas accès à Databricks, chaque
étape à faire à la main est listée ici, prête à copier-coller. Cocher et dater
une fois faite. Ce fichier ne couvre **que** la mise en place de l'environnement
DEV ; `OPERATIONS.md` reste la référence pour UAT / uat-test / PROD.

Branche : **`feature/chat-vsi-merged-on-impact-search`** (= `claude/hopeful-bardeen-57zeur`).
Pas de PR ; le zip à déployer est celui de cette branche.

Mis à jour le 2026-10-08 : **commencer par le bloc S** (DEV propre : un seul chatbot, un seul index,
plus aucun nom versionné, plus de Knowledge Assistant). Les blocs G, K, L, P, A et C3 décrivent l'état
d'avant (KA, tables `_v1`) : remplacés par S, gardés pour l'historique.

## Décisions prises (2026-10-05)

| Sujet | Choix |
|---|---|
| Workspace | DEV — `https://dbc-c623749d-731b.cloud.databricks.com`, profil CLI `DEV` |
| Cible bundle | `dev` (réécrite en miroir de `qualibot-uat`) |
| App | `qualibot` (même nom qu'en UAT, workspace différent) |
| Catalog / schema | `dev_landingzone.qualibot` (écriture), `dev_proj.qualibot` (projet) |
| Suffixe tables / index | aucun depuis le 2026-10-08 (bloc S) ; `_v1` avant |
| Identité des jobs (`run_as`) | SP DEV `fde6ff28-739f-4a41-b61e-604a298c8478` |
| Lakebase | projet `qualibot` neuf, base `doccompare` vide (l'app crée ses tables) |
| Corpus | copie UAT → DEV via le volume `uat_landingzone.qualibot.staging` (format Delta) : chunks + état du pipeline. **Aucun run complet du pipeline de parsing** |
| Phase archive avant 2018 | ignorée en DEV (plafond 0, fiches hors RAG, pas de `chunks_full`) |
| Export côté UAT | run ponctuel `databricks jobs submit` — la cible `qualibot-uat` n'est pas modifiée |
| Vector Search | endpoint `qualibot`, un seul index `chunks_index` (filtre de division), embeddings `databricks-qwen3-embedding-0-6b` |
| Knowledge Assistants | retirés le 2026-10-08 (bloc S7) |
| Contrôle par groupe (can_chat / can_compare) | **désactivé temporairement en DEV seulement** (`CAPS_BYPASS=true`) |
| Accès à l'app (ACL) | mêmes groupes qu'en UAT + `jules.gourio.external@latecoere.aero` + `mehdi.lamrani@databricks.com` + SP DEV (app existante rattachée ; `users` CAN_MANAGE retiré) |
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
| R | Reliquat de l'ancien Qualibot DEV : app rattachée, vieux index/tables facultatifs | DEV | non |
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

## Récapitulatif — ce qui est déployé en DEV (2026-10-05)

Workspace DEV : https://dbc-c623749d-731b.cloud.databricks.com (id `2865348338307293`). Liens construits à partir des
noms/ids connus ; si un chemin d'UI a bougé, la commande CLI de la ligne donne
l'objet.

| Quoi | Lien | CLI |
|---|---|---|
| **App `qualibot`** (utilisateurs) | https://qualibot-2865348338307293.aws.databricksapps.com | |
| App — gestion (déploiements, logs, permissions) | https://dbc-c623749d-731b.cloud.databricks.com/apps/qualibot | `databricks apps get qualibot --profile DEV` |
| **Catalog** — schema `dev_landingzone.qualibot` | https://dbc-c623749d-731b.cloud.databricks.com/explore/data/dev_landingzone/qualibot | `databricks tables list dev_landingzone qualibot --profile DEV` |
| Tables corpus : `chunks_v1`, `src_chunks_as_v1`, `src_chunks_is_v1` | https://dbc-c623749d-731b.cloud.databricks.com/explore/data/dev_landingzone/qualibot/chunks_v1 | |
| État pipeline : `_pipeline_checkpoint_v1`, `processed_files_v1`, `image_metadata_v1`, `parse_manifest_v1`, `category_reference_v1` | https://dbc-c623749d-731b.cloud.databricks.com/explore/data/dev_landingzone/qualibot | |
| Volumes `doc_compare`, `test`, `images`, `staging` (+ `docling_models`, existant) | https://dbc-c623749d-731b.cloud.databricks.com/explore/data/volumes/dev_landingzone/qualibot/doc_compare | `databricks volumes list dev_landingzone qualibot --profile DEV` |
| Schema projet `dev_proj.qualibot` | https://dbc-c623749d-731b.cloud.databricks.com/explore/data/dev_proj/qualibot | |
| **Vector Search** — endpoint `qualibot` | https://dbc-c623749d-731b.cloud.databricks.com/compute/vector-search/qualibot | `databricks vector-search-indexes list-indexes qualibot --profile DEV` |
| Index `chunks_index_v1` (ALL, impact search), `chunks_as_index_v1`, `chunks_is_index_v1` | https://dbc-c623749d-731b.cloud.databricks.com/explore/data/dev_landingzone/qualibot/chunks_index_v1 | `databricks vector-search-indexes get-index dev_landingzone.qualibot.chunks_index_v1 --profile DEV` |
| **Agents** `qualibot_ALL_v2` → `ka-4d15cb32-endpoint` | https://dbc-c623749d-731b.cloud.databricks.com/ml/endpoints/ka-4d15cb32-endpoint | `databricks knowledge-assistants list-knowledge-assistants --profile DEV` |
| `qualibot_AS_v2` → `ka-2ef8a9ac-endpoint` | https://dbc-c623749d-731b.cloud.databricks.com/ml/endpoints/ka-2ef8a9ac-endpoint | |
| `qualibot_IS_v2` → `ka-710526e7-endpoint` | https://dbc-c623749d-731b.cloud.databricks.com/ml/endpoints/ka-710526e7-endpoint | |
| **Lakebase** — projet `qualibot` (« Qualibot History », uid `e9185a28-…`), branche `production`, base `doccompare` | UI : menu Lakebase (ou SQL Warehouses ▸ Lakebase) ▸ « Qualibot History » | `databricks postgres get-project projects/qualibot --profile DEV` |
| Rôles Postgres : app SP, SP DEV, `leap-qualibot-service-accounts`, CoreDev, Mehdi | | `databricks postgres list-roles projects/qualibot/branches/production --profile DEV` |
| Bundle (fichiers, état) | https://dbc-c623749d-731b.cloud.databricks.com/browse/folders/Workspace/Shared/.bundle/qualibot/dev | `databricks bundle summary -t dev --profile DEV` |

Jobs (planning PAUSED sauf mention) — liste : https://dbc-c623749d-731b.cloud.databricks.com/jobs?filter=qualibot

| Job | Lien / déclenchement |
|---|---|
| `qualibot-copy-uat-to-dev` (manuel) | https://dbc-c623749d-731b.cloud.databricks.com/jobs/685542214168730 |
| `D_1_qualibot-parsing-pipeline-dev` (PAUSED) | https://dbc-c623749d-731b.cloud.databricks.com/jobs/362367007936662 |
| `qualibot-lakebase-import-uat-to-dev` (**actif**, 2h30/14h30) | https://dbc-c623749d-731b.cloud.databricks.com/jobs/728257090536196 |
| `qualibot-score-production-qa` (**actif**, 2h45/14h45) | https://dbc-c623749d-731b.cloud.databricks.com/jobs/715033309841102 |
| `qualibot-grant-app-access-dev` (manuel) | `databricks bundle run grant_app_access_dev -t dev --profile DEV` |
| `qualibot-provision-knowledge-assistant-dev` (manuel) | `databricks bundle run provision_knowledge_assistant_dev -t dev --profile DEV` |
| `qualibot-migrate-lakebase-dev` (manuel) | `databricks bundle run migrate_lakebase_dev -t dev --profile DEV` |
| `qualibot-lakebase-export-dev-to-volume-dev` (PAUSED) | |
| `apps-stop-nightly-dev`, `qualibot-stop-weekend-dev`, `qualibot-start-weekend-dev` (PAUSED) | |

Identités : app SP `8e411164-a7e8-46ff-8013-8c56af2c3656` ; SP des jobs
`job-runner-sa-dev` (`fde6ff28-739f-4a41-b61e-604a298c8478`). Contrôle par
groupe désactivé (`CAPS_BYPASS=true`, DEV seulement).

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

- [ ] **P3. Droits Unity Catalog du SP DEV.** Un premier essai de GRANT a
  échoué le 2026-10-05 (« User does not have MANAGE on Catalog
  'dev_landingzone' ») : vous n'avez pas MANAGE sur le catalog. Vérifier
  d'abord ce que le SP a **déjà** (il est peut-être déjà couvert via un groupe) :

  ```powershell
  $sp = 'fde6ff28-739f-4a41-b61e-604a298c8478'
  "=== dev_landingzone";          databricks grants get-effective catalog dev_landingzone --principal $sp --profile DEV
  "=== dev_landingzone.qualibot"; databricks grants get-effective schema dev_landingzone.qualibot --principal $sp --profile DEV
  "=== dev_proj";                 databricks grants get-effective catalog dev_proj --principal $sp --profile DEV
  "=== uat_landingzone.qualibot"; databricks grants get-effective schema uat_landingzone.qualibot --principal $sp --profile DEV
  "=== prod_bronze.intraqual";    databricks grants get-effective schema prod_bronze.intraqual --principal $sp --profile DEV
  "=== prod_landingzone.intraqual"; databricks grants get-effective schema prod_landingzone.intraqual --principal $sp --profile DEV
  ```

  Ce qu'il lui faut : `USE CATALOG` sur `dev_landingzone`, `dev_proj`,
  `uat_landingzone`, `prod_bronze`, `prod_landingzone` ; sur
  `dev_landingzone.qualibot` : `USE SCHEMA`, `CREATE TABLE`, `CREATE VOLUME`,
  `SELECT`, `MODIFY`, `READ VOLUME`, `WRITE VOLUME` ; `READ VOLUME` sur
  `uat_landingzone.qualibot.staging` ; `SELECT` sur `prod_bronze.intraqual` et
  `prod_landingzone.intraqual`, `READ VOLUME` sur
  `prod_landingzone.intraqual.intraqual_documents`.

  Ce qui manque au niveau **schema `dev_landingzone.qualibot`**, vous pouvez le
  donner vous-même (vous en êtes owner) :

  ```sql
  GRANT USE SCHEMA, CREATE TABLE, CREATE VOLUME, SELECT, MODIFY, READ VOLUME, WRITE VOLUME
    ON SCHEMA dev_landingzone.qualibot TO `fde6ff28-739f-4a41-b61e-604a298c8478`;
  ```

  Le reste (`USE CATALOG`, droits sur `uat_landingzone` / `prod_*`) est à
  demander à un admin des catalogs, uniquement pour ce qui manque.

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
  modifiée le 2026-10-05 par Mehdi → **rattachée au bundle en I1** ; endpoint
  Vector Search `qualibot` supprimé depuis, index orphelins → R)_ :

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
  leap-qualibot-service-accounts, Mehdi et les deux groupes
  End-users-Qualibot-* présents — ces derniers n'apparaissaient pas dans la
  1re page de `groups list`, mais l'ACL de l'app les référence)_ : `Role-Project-LEAP-CoreAdmin`,
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

PowerShell, depuis la racine du projet (branche `feature/chat-vsi-merged-on-impact-search`)
sur la machine de déploiement. Toujours commencer la session par :

```powershell
Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
# autorise les scripts .ps1 du projet pour cette fenêtre seulement
# (sinon « n'est pas signé numériquement »)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### S. DEV propre : un chatbot, un index, aucun nom versionné (2026-10-08)

Ce que le code attend désormais en DEV : la table `chunks` (découpage retenu, aujourd'hui dans
`chunks_v2b`) et son index `chunks_index` ; les tables d'état du pipeline sans suffixe ; plus de
`src_chunks_as` / `src_chunks_is`, plus d'index par division, plus de KA. Rien ne se perd :
`chunks` est une copie de `chunks_v2b`, l'ancien est supprimé seulement à la fin (S8), après test.
Durée : surtout l'embedding du nouvel index (30 à 60 min). Le reste prend quelques minutes.

- [x] **S0. Inventaire** _(2026-10-08 : tables `_v1` de la copie UAT + `chunks_v2a/v2b` et leurs index ; pas de `v2c`, pas d'autre table d'état `_v1` que les 5 de S3)_ (éditeur SQL DEV) — à garder sous les yeux pour S2 à S11 :

  ```sql
  SHOW TABLES IN dev_landingzone.qualibot;
  ```

  ```powershell
  databricks vector-search-indexes list-indexes qualibot --profile DEV
  ```

- [x] **S1. Code et jobs** _(2026-10-08)_ : extraire le zip dans un **dossier vide** (beaucoup de fichiers ont été
  déplacés vers `archive/` ou renommés : extrait par-dessus l'ancien dossier, les anciens fichiers
  resteraient et seraient redéployés avec le reste). Vérifier ensuite que le fichier de config de
  l'app est le bon — la première ligne doit être `"""Config endpoint — exposes app configuration to the frontend."""` :

  ```powershell
  Get-Content server\routers\config.py -TotalCount 1
  ```

  Puis, depuis ce dossier :

  ```powershell
  databricks bundle deploy -t dev --profile DEV
  ```

  Ce déploiement supprime le job `qualibot-provision-knowledge-assistant-dev` (pas les KA eux-mêmes,
  voir S7) et passe les jobs DEV sur les noms sans suffixe. Les plannings restent en pause.

- [x] **S2. Restes de l'ancien Qualibot DEV** _(2026-10-08 : inventaire S0 → aucune table sans suffixe ni index `chunks_all/as/is` : rien à faire)_ (bloc R4, s'il n'a pas été fait). Seulement si S0
  liste ces tables **sans** suffixe (ce sont celles de juillet, jamais relues depuis) :

  ```sql
  DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks;
  DROP TABLE IF EXISTS dev_landingzone.qualibot._pipeline_checkpoint;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.processed_files;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.intraqual_docs;
  ```

  Index orphelins de juillet (bloc R3 ; si « endpoint not found », les laisser) :

  ```powershell
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_all --profile DEV
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_as  --profile DEV
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_is  --profile DEV
  ```

- [x] **S3. Tables d'état du pipeline** _(2026-10-08)_ : retirer le suffixe** (renommage instantané, aucune copie) :

  ```sql
  ALTER TABLE dev_landingzone.qualibot._pipeline_checkpoint_v1 RENAME TO dev_landingzone.qualibot._pipeline_checkpoint;
  ALTER TABLE dev_landingzone.qualibot.processed_files_v1      RENAME TO dev_landingzone.qualibot.processed_files;
  ALTER TABLE dev_landingzone.qualibot.image_metadata_v1       RENAME TO dev_landingzone.qualibot.image_metadata;
  ALTER TABLE dev_landingzone.qualibot.parse_manifest_v1       RENAME TO dev_landingzone.qualibot.parse_manifest;
  ALTER TABLE dev_landingzone.qualibot.category_reference_v1   RENAME TO dev_landingzone.qualibot.category_reference;
  ```

  Toute autre table `…_v1` de S0 qui n'est **pas** une table de passages (`chunks_v1`,
  `src_chunks_as_v1`, `src_chunks_is_v1`) : même commande (ex. `parsing_run_health_v1`,
  `document_change_log_v1`, `audit_files_unified_v1`).

- [x] **S4. La table `chunks`** _(2026-10-08)_ = le découpage retenu** (copie de `chunks_v2b`, avec les propriétés
  qu'exige un index : Change Data Feed et 60 jours d'historique) :

  ```sql
  CREATE TABLE dev_landingzone.qualibot.chunks
  TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.deletedFileRetentionDuration' = 'interval 60 days',
    'delta.logRetentionDuration' = 'interval 60 days')
  AS SELECT * FROM dev_landingzone.qualibot.chunks_v2b;

  SELECT (SELECT count(*) FROM dev_landingzone.qualibot.chunks)     AS chunks,
         (SELECT count(*) FROM dev_landingzone.qualibot.chunks_v2b) AS chunks_v2b;   -- identiques
  ```

- [ ] **S5. L'index `chunks_index`** _(créé 2026-10-08, embedding en cours : ~10 % indexé à la dernière vérification)_, créé sous votre identité (propriétaire de l'endpoint `qualibot`
  et de la table `chunks`) ; même spécification que les index UAT. La création lance la première
  synchronisation (embedding de tout `chunks`).

  ```powershell
  $json = '{"name": "dev_landingzone.qualibot.chunks_index", "endpoint_name": "qualibot", "primary_key": "chunk_id", "index_type": "DELTA_SYNC", "delta_sync_index_spec": {"source_table": "dev_landingzone.qualibot.chunks", "pipeline_type": "TRIGGERED", "embedding_source_columns": [{"name": "chunk_text", "embedding_model_endpoint_name": "databricks-qwen3-embedding-0-6b"}]}}'
  [IO.File]::WriteAllText("$PWD\chunks_index.json", $json)
  databricks vector-search-indexes create-index --json "@chunks_index.json" --profile DEV
  Remove-Item chunks_index.json
  ```

  Attendre `ONLINE` dans l'UI Vector Search (endpoint `qualibot`), avec autant de lignes indexées que
  `chunks` (30 à 60 min).

- [ ] **S6. Questions d'évaluation en DEV** (copies des deux tables UAT que lit `retrieval_eval`) :

  ```sql
  CREATE TABLE dev_landingzone.qualibot.synthetic_retrieval_questions
  AS SELECT * FROM uat_landingzone.qualibot.synthetic_retrieval_questions_v2;
  CREATE TABLE dev_landingzone.qualibot.feedback_failure_cases
  AS SELECT * FROM uat_landingzone.qualibot.feedback_failure_cases;
  ```

- [ ] **S7. Supprimer les 3 Knowledge Assistants DEV** (`qualibot_ALL_v2`, `qualibot_AS_v2`,
  `qualibot_IS_v2`, endpoints `ka-4d15cb32-endpoint`, `ka-2ef8a9ac-endpoint`, `ka-710526e7-endpoint`).
  UI DEV : **Agents** → chaque KA → menu ⋮ → **Delete**. Si l'UI refuse (ils ont été créés par le
  SP DEV), même chose dans une cellule Python d'un notebook DEV serverless :

  ```python
  from databricks.sdk import WorkspaceClient
  w = WorkspaceClient()
  for ka in w.knowledge_assistants.list_knowledge_assistants():
      print(ka.name, ka.display_name, ka.endpoint_name)
      if ka.display_name in ('qualibot_ALL_v2', 'qualibot_AS_v2', 'qualibot_IS_v2'):
          w.knowledge_assistants.delete_knowledge_assistant(name=ka.name)
          print('  deleted')
  ```

  Si `PERMISSION_DENIED` : la lancer en job sous le SP DEV
  (`fde6ff28-739f-4a41-b61e-604a298c8478`), ou me renvoyer l'erreur. Si la méthode
  `delete_knowledge_assistant` n'existe pas dans la version du SDK du notebook :
  `%pip install -U databricks-sdk` puis `dbutils.library.restartPython()`.

- [ ] **S8. Droits du SP de l'app, catalogue des documents, puis l'app** _(2026-10-08 : droits + tâche 6 faits, `doc_catalog 0 -> 4937 documents` ; reste le déploiement de l'app une fois l'index ONLINE)_ (une fois `chunks_index`
  `ONLINE`). Le catalogue (REF, titre, lien de chaque document) vient désormais de la table
  Lakebase `doc_catalog`, écrite par la dernière tâche du pipeline depuis `parse_manifest` et
  `chunks`. Cette tâche tourne sous le SP DEV : le job de droits lui donne la lecture de ces
  tables (créées par vous en S3/S4), puis on la lance seule (rien n'est parsé) :

  ```powershell
  databricks bundle run grant_app_access_dev -t dev --profile DEV
  databricks bundle run parsing_pipeline -t dev --profile DEV --only 6_update_kb_metadata
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
  ```

  Sortie attendue de la tâche 6 : `doccompare: doc_catalog 0 -> N documents` (N ≈ le nombre de
  documents en périmètre). Dans les logs de l'app après démarrage :
  `doc_catalog: loaded N documents from Lakebase`. Si la tâche 6 échoue (droits Lakebase du SP),
  **m'envoyer l'erreur** : l'app marche quand même, avec l'ancien instantané `doc_catalog.json`
  (log `using the bundled snapshot`).

  Sortie attendue du job de droits : une ligne `OK:` par droit, dont une pour `chunks_index` et
  une `SELECT, MODIFY` par table du pipeline pour le SP DEV.

- [ ] **S9. Tester** :
  - un seul onglet **Chat** (plus de « Chat KA » / « Chat VSI ») ;
  - une question en ALL, une en AS, une en IS : les documents cités sont bien de la division
    (filtre) ;
  - une question en espagnol ou tchèque, une hors sujet (→ refus), « donne-moi le lien de l'OPEX
    Sharepoint » (→ l'URL de INAQ-742) ;
  - Compare : « Judge Impacted Docs » sur deux révisions connues (index `chunks_index`).

- [ ] **S10. Mesure de contrôle** (≈ 1 €, ≈ 10 min) : notebook
  `utils/databricks_ops/evaluation/retrieval_eval.py`, Run all avec les défauts (`indexes = chat`).
  Attendu : environ 83 % des documents attendus trouvés (le score de `idx-v2b-all`). Envoyer le
  dernier tableau.

- [ ] **S11. Supprimer l'ancien** (après S9 et S10 réussis). D'abord les index, puis leurs tables :

  ```powershell
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_index_v1    --profile DEV
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_as_index_v1 --profile DEV
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_is_index_v1 --profile DEV
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_index_v2a   --profile DEV
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_index_v2b   --profile DEV
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_index_v2c   --profile DEV
  ```

  (Ignorer « does not exist » pour un index qui n'a pas été construit, ex. `v2c`.)

  ```sql
  DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks_v1;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.src_chunks_as_v1;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.src_chunks_is_v1;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks_v2a;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks_v2b;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks_v2c;
  ```

  `UNDROP TABLE` reste possible 7 jours.

  À **garder** dans `dev_landingzone.qualibot` (inventaire du 2026-10-08) :
  - écrites chaque jour par `lakebase_import_uat_to_dev` (copie de la base de l'app UAT) :
    `chat_feedbacks`, `chat_messages`, `chat_sessions`, `errors`, `feedbacks`, `impact_cache`,
    `impact_document_results`, `impact_requests`, `knowledge_base_metadata`, `llm_requests`,
    `messages`, `summary_cache`, `users` ;
  - écrites par `score_production_qa` : `chat_quality_scores`, `chat_quality_scoring_runs` ;
  - application Translator (autre projet) : `dnt_rules`, `glossary_terms` ;
  - évaluation : `qualibot_eval_golden` (jeu golden), `eval_retrieval_runs`, `eval_pairwise_runs`,
    `eval_pairwise_runs_cache` (écrites par les deux notebooks gardés) ;
  - pipeline : `chunks`, `chunks_index`, `_pipeline_checkpoint`, `processed_files`,
    `image_metadata`, `parse_manifest`, `category_reference`.

  Facultatif : `eval_golden_runs` n'est plus écrite (notebook KA contre VSI archivé) ; ses
  résultats sont dans `docs/chat_vsi_tests.md` : `DROP TABLE IF EXISTS dev_landingzone.qualibot.eval_golden_runs;` Ensuite `SHOW TABLES IN dev_landingzone.qualibot` ne doit
  plus montrer aucun nom en `_v1` / `_v2…` (les tables `eval_*` gardent les anciens noms d'essai
  **dans leurs lignes** : c'est l'historique des mesures, `docs/chat_vsi_tests.md` § B).

Trop long ou bloqué en DEV ? Le seul pas lent est S5 (embedding). Si un pas bloque (droits sur les
KA, index orphelins), le laisser et me renvoyer l'erreur : le reste ne dépend pas de lui, et l'UAT
se fera proprement de toute façon (`OPERATIONS.md`, D5).

### T. Combien de passages garder ? (après S, 2026-10-08)

Aujourd'hui : 3 requêtes × (12 passages reclassés + 10 bruts), fusionnés sans plafond (≈ 35
passages et 11 k tokens en moyenne, 66 au plus avant les REF et titres). Les passages bruts
viennent d'un test fait avec les anciens passages de 4 000 caractères, que le reranker lisait à
moitié : à remesurer sur le découpage actuel. Les défauts du code ne changent pas tant que ce
test n'a pas tranché.

- [ ] **T1. Recherche seule** (≈ 4 €, ≈ 30 min) : notebook
  `utils/databricks_ops/evaluation/retrieval_eval.py`, widget `indexes` =

  ```text
  chat,rerank-only|raw=0,raw5|raw=5,cap40|cap=40,cap25|cap=25,rerank8-only|rerank=8|raw=0
  ```

  puis Run all. **M'envoyer les deux derniers tableaux** (par source et global : documents
  trouvés, dans les 5 premiers, tokens de contexte).

- [ ] **T2. Réponses** (≈ 2 €, ≈ 20 min, après T1, sur la meilleure configuration plus courte) :
  notebook `pairwise_answers.py`, widgets `indexes` = la même liste que T1, `reference` =
  `databricks-gpt-6-luna@chat`, `contenders` = `databricks-gpt-6-luna@<label retenu en T1>`,
  `eval_id` = `passages-<label>`. **M'envoyer les trois tableaux.**

### U. Test de charge du chat (après S, 2026-10-08)

Combien de questions simultanées l'app DEV tient avant de casser, et pourquoi elle casse
(quota du modèle, Vector Search, instance de l'app). Notebook
`utils/databricks_ops/evaluation/load_test_chat.py` : paliers de 5, 10, 20, 40, 80 questions en
cours en même temps, 4 questions par « utilisateur » et par palier, questions réelles (golden +
vraies questions DEV). ≈ 620 questions, ≈ 2 €, 30 à 60 min. S'arrête au premier palier où plus
de la moitié des questions échouent.

- [ ] **U1. Lancer** : app DEV démarrée, notebook en serverless, Run all (défauts). La cellule
  « Mode » dit si le test passe par l'app (`MODE = app`) ou, si l'app refuse le jeton du
  notebook, par le moteur appelé dans le notebook (`MODE = engine` : mêmes modèles et mêmes
  limites, sans l'instance de l'app).
- [ ] **U2. M'envoyer** les deux tableaux de la dernière cellule et la ligne `MODE = …`, plus,
  dans les logs de l'app pendant le test, les lignes `chat_vsi_llm:` (bascule vers le modèle de
  secours, relances).

### V2. Test de charge de Vector Search seul (2026-10-08)

Le test de charge du chat (bloc U, run du 2026-10-08) : 100 % de réussite jusqu'à 10 questions
simultanées, puis `Vector Search returned 429` dès 20 (9 % d'échecs), 40 (16 %) et 80 (24 %) ; débit
plafonné vers 50 questions/min. Toutes les erreurs viennent de Vector Search, aucune des modèles.
Notebook `utils/databricks_ops/evaluation/load_test_vector_search.py` : seulement des requêtes
Vector Search (aucun appel LLM), sans relance, par paliers de 1 à 64 requêtes simultanées,
20 s par palier, pour chaque type de requête (brute, avec reranker, reranker sur le texte seul,
ANN, avec filtre, mélange du chat). ≈ 20 min.

- [ ] **V2.1. Lancer** : notebook en serverless, Run all (défauts).
- [ ] **V2.2. M'envoyer** la sortie de la cellule « endpoint and index » et les trois tableaux de la
  dernière cellule.

### E. Export du corpus UAT (workspace UAT, run ponctuel)

Lecture seule sur les tables UAT ; écrit uniquement dans
`/Volumes/uat_landingzone/qualibot/staging/dev_copy/`. La cible `qualibot-uat`
du bundle n'est pas touchée. Tables exportées (`_v1`) : `chunks`,
`src_chunks_as`, `src_chunks_is`, `_pipeline_checkpoint`, `processed_files`,
`image_metadata`, `parse_manifest`, `category_reference` + l'archive
LibreOffice. Rien de la phase archive avant 2018.

- [x] **E1. Importer le notebook dans votre dossier et lancer le run** _(2026-10-05 : chunks 75 086, as 59 897, is 11 241, checkpoint 7 097, processed_files 18 610, image_metadata 49 342, parse_manifest 4 942, category_reference 18 587)_

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

### R. Reliquat de l'ancien Qualibot DEV (2026-10-05)

Constat : l'app `qualibot` DEV existe hors du bundle (créée à la main le
2026-07-09, jamais déployée, SP `8e411164-a7e8-46ff-8013-8c56af2c3656`) ;
l'endpoint Vector Search `qualibot` (id `0571f7cf-…`) a été supprimé, ses 3
index `chunks_all` / `chunks_as` / `chunks_is` restent orphelins dans
`dev_landingzone.qualibot` avec leurs `*_writeback_table`, plus d'anciennes
tables de pipeline.

**L'app n'est pas supprimée** : elle est rattachée au bundle (I1) et garde son
SP, son URL et les droits déjà donnés à ce SP. Le bundle remplace en revanche
sa liste de permissions par celle de `databricks.yml` (R2 pour comparer avant).

**Ne pas toucher** : les tables des jobs DEV conservés
(`lakebase_import_uat_to_dev` : `chat_feedbacks`, `chat_messages`,
`chat_sessions`, `errors`, `feedbacks`, `impact_*`, `knowledge_base_metadata`,
`llm_requests`, `messages`, `summary_cache`, `users` ; `score_production_qa` :
`chat_quality_*`), le volume `docling_models`, `glossary_terms` / `dnt_rules`
(Translator).

- [x] **R1. Vector Search** _(2026-10-05 : `get-index` → « endpoint
  0571f7cf-… not found » : l'endpoint n'existe plus, index orphelins)_

- [x] **R2. Permissions actuelles de l'app** _(2026-10-05 : users CAN_MANAGE,
  CoreAdmin/CoreDev CAN_MANAGE, End-users DocCompare/ChatBot CAN_USE, Jules
  CAN_MANAGE, admins hérité. Le déploiement garde tout sauf **`users` CAN_MANAGE,
  retiré** ; ajoute Mehdi CAN_USE et le SP DEV CAN_MANAGE. L'app « custom » de
  Mehdi est une autre app, non concernée)_

- [ ] **R3. Index orphelins** (facultatif, aucun conflit de nom avec les index
  `_v1`). Essayer ; si l'erreur « endpoint not found » revient, les laisser :

  ```powershell
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_all --profile DEV
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_as  --profile DEV
  databricks vector-search-indexes delete-index dev_landingzone.qualibot.chunks_is  --profile DEV
  ```

- [ ] **R4. Anciennes tables du pipeline** (facultatif, aucun conflit de nom ;
  SQL editor DEV, `UNDROP TABLE` possible 7 jours) :

  ```sql
  DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks;
  DROP TABLE IF EXISTS dev_landingzone.qualibot._pipeline_checkpoint;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.processed_files;
  DROP TABLE IF EXISTS dev_landingzone.qualibot.intraqual_docs;
  ```

### I. Infra DEV (`bundle deploy -t dev`)

Crée l'app `qualibot`, le projet Lakebase `qualibot` (base `doccompare`), le
schema `dev_proj.qualibot`, les volumes, les rôles Postgres et les jobs ;
met à jour le job de parsing DEV existant (il écrit désormais dans
`dev_landingzone.qualibot`, tables `_v1`, planning PAUSED).

- [x] **I0. État du bundle** _(2026-10-05 : app, schemas, volumes, Lakebase et
  nouveaux jobs « not deployed » ; `parsing_pipeline`, `lakebase_import_uat_to_dev`,
  `score_production_qa` déjà déployés → mis à jour sur place)_

- [x] **I1. Binder le schema** _(2026-10-05 : `dev_landingzone.qualibot` bindé.
  L'app : « Resource already managed by Terraform » — elle est déjà dans l'état
  du bundle `dev` (créée par un ancien `bundle deploy -t dev` le 2026-07-09),
  rien à binder)_

- [x] **I1b. Prévisualiser le déploiement, sans rien modifier** _(2026-10-05 : 25 create, 5 update, 0 delete)_ :

  ```powershell
  python utils/deploy/render_target_config_env.py dev target_config.env
  databricks bundle validate -t dev --profile DEV
  databricks bundle plan -t dev --profile DEV
  ```

  Me coller la sortie de `plan` : chaque ressource doit être `create` ou
  `update`. Rien en `delete` / `recreate`, sauf accord explicite.

- [x] **I2. Déployer l'infra, puis le code de l'app** _(2026-10-05 : `bundle deploy` OK au 3e essai ; code déployé 11:03 UTC, « App started successfully »)_ — sous votre identité
  (profil `DEV`). Vous devenez owner de ce qui est créé ; le SP DEV a CAN_MANAGE
  sur l'app, le projet Lakebase et chaque job propre à DEV, pour que la
  pipeline Bitbucket (qui déploie en tant que SP) puisse les mettre à jour
  ensuite. Exception : `D_1_qualibot-parsing-pipeline-dev` (définition partagée
  avec UAT/PROD) — à régler au bloc B.

  ```powershell
  databricks bundle deploy -t dev --profile DEV
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
  ```

  Erreurs possibles au `bundle deploy` (le déploiement s'arrête, relançable
  après correction) :
  - `run_as` refusé → il vous faut le rôle « Service principal: User » sur
    `job-runner-sa-dev` (Settings ▸ Identity and access ▸ Service principals ▸
    job-runner-sa-dev ▸ Permissions) ;
  - création de `dev_proj.qualibot` refusée → CREATE SCHEMA sur `dev_proj`
    (owner `leap-core-service_accounts-dev`) ;
  - création du projet Lakebase refusée → droit de création Lakebase.

  **1er essai (2026-10-05) — échec partiel** : volumes, projet Lakebase
  (id `e9185a28-13c6-42da-9ccc-0dbcf6b97b60`) et jobs créés ; échecs sur l'app
  (« Volume … doc_compare does not exist ») et les rôles Postgres (« Project
  projects/qualibot not found », puis « not authorized … Can Manage for
  Database project »). Causes corrigées dans `databricks.yml` : l'app et les
  rôles référencent maintenant le volume / le projet (ils attendent leur
  création), et vous êtes déclaré en CAN_MANAGE du projet Lakebase. Vérifié
  ensuite : le projet existe (uid `e9185a28-…`), vous en avez Can Manage, le
  rôle `app-doc-compare-sp` est créé — le « not authorized » venait de la même
  course, aucun admin nécessaire.

  Puis relancer `git pull` + `databricks bundle deploy -t dev --profile DEV`.

  **2e essai (2026-10-05)** : tout passe sauf l'app — « all account users lack
  USE CATALOG permission on catalog dev_landingzone, and the user does not
  have MANAGE ». Rattacher un volume à une app oblige le déployeur à avoir
  MANAGE sur le catalog. Corrigé : plus de rattachement de volumes sur l'app
  DEV (elle n'en a pas besoin, elle lit `COMPARE_VOLUME_PATH`) ; les droits de
  son SP passent par le job `qualibot-grant-app-access-dev` (I2b).

- [x] **I2b. Droits du SP de l'app sur ses volumes** _(OK 2026-10-05)_ — job manuel, sous le SP
  DEV. Il ne marche que si le SP DEV peut accorder des droits sur
  `dev_landingzone` (owner du catalog = son groupe `leap-core-service_accounts-dev`,
  ou MANAGE). Vérifier l'owner, puis lancer :

  ```powershell
  (databricks catalogs get dev_landingzone --profile DEV -o json | ConvertFrom-Json).owner
  databricks bundle run grant_app_access_dev -t dev --profile DEV
  ```

  En cas d'échec « run_as lacks GRANT rights » : faire passer par un admin du
  catalog, en SQL :

  ```sql
  GRANT USE CATALOG ON CATALOG dev_landingzone TO `8e411164-a7e8-46ff-8013-8c56af2c3656`;
  GRANT USE SCHEMA ON SCHEMA dev_landingzone.qualibot TO `8e411164-a7e8-46ff-8013-8c56af2c3656`;
  GRANT READ VOLUME, WRITE VOLUME ON VOLUME dev_landingzone.qualibot.doc_compare TO `8e411164-a7e8-46ff-8013-8c56af2c3656`;
  GRANT READ VOLUME, WRITE VOLUME ON VOLUME dev_landingzone.qualibot.test TO `8e411164-a7e8-46ff-8013-8c56af2c3656`;
  ```

  À ce stade : Compare marche ; impact search et chat non (pas encore d'index
  ni de KA) — normal.

- [ ] **I3. Vérifier que tous les nouveaux plannings sont en PAUSED** (UI
  Jobs DEV, filtre `qualibot`) : `D_1_qualibot-parsing-pipeline-dev`,
  `qualibot-lakebase-export-dev-to-volume-dev`, `apps-stop-nightly-dev`,
  `qualibot-stop-weekend-dev`, `qualibot-start-weekend-dev` = PAUSED.
  `qualibot-lakebase-import-uat-to-dev` et `qualibot-score-production-qa`
  restent actifs, comme avant.

- [x] **I4. Client id du SP de l'app DEV** _(`8e411164-a7e8-46ff-8013-8c56af2c3656`, app existante rattachée, figé dans `databricks.yml`)_

- [ ] **I5. Tests sans index ni KA** (pendant la copie) _(logs OK 2026-10-05 : production mode, `doccompare` créée, Lakebase ready ; tests UI à faire)_ — app :
  https://qualibot-2865348338307293.aws.databricksapps.com

  ```powershell
  databricks apps logs qualibot --profile DEV
  ```

  Dans les logs : `Starting in production mode`, `Database "doccompare" created`
  (1er démarrage seulement), `Lakebase schema ready`, `Lakebase ready` ; pas de
  `history feature disabled` ni de trace d'erreur.

  - Mehdi (hors groupes End-users) ouvre l'app : Compare et Chat visibles,
    pas d'« Access denied » (`CAPS_BYPASS`) ;
  - Compare : deux révisions d'un document → Change Summary, Change Table,
    exports Excel et PDF ;
  - « History » : la comparaison apparaît, la recharger (Lakebase OK) ;
  - aperçu « Exact (PDF) » d'un DOCX — seulement une fois la tâche
    `1_import_tables` de la copie finie (elle copie l'archive LibreOffice) ;
  - attendu en échec à ce stade : « Judge Impacted Docs » (index pas prêt) et
    le Chat (pas de KA).

### C. Copie du corpus + index Vector Search (job DEV)

Job `qualibot-copy-uat-to-dev`, déclenchement manuel, sous l'identité de qui
le lance (il faut READ VOLUME sur `uat_landingzone.qualibot.staging`), serverless :
`1_import_tables` (snapshot → `dev_landingzone.qualibot.*_v1`, rétention
60 jours, Change Data Feed sur les 3 tables de chunks, archive LibreOffice
→ volume `doc_compare`) → `2_vector_search_endpoint` (crée l'endpoint
`qualibot` s'il manque) → `3_sync_indexes` (crée les 3 index `_v1` et les
synchronise — embedding de tout le corpus, peut être long). **Le pipeline de
parsing n'est pas lancé.**

- [ ] **C1. Lancer la copie** _(1er run 2026-10-05 13:00 en échec : « User does
  not have READ VOLUME on Volume staging » — le SP DEV ne lit pas le volume
  UAT. 2e run idem : retirer `run_as` avait laissé le SP en place. Le job a
  maintenant `run_as` = **votre** utilisateur, explicitement, et tourne en
  serverless (plus les ~7 min de démarrage de cluster) : `git pull`,
  `databricks bundle deploy -t dev --profile DEV`, puis relancer)_

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

- [x] **C3a. Spec des index alignée sur l'UAT** _(2026-10-05 : UAT `chunks_index_v1` =
  `databricks-qwen3-embedding-0-6b` sur `chunk_text`, TRIGGERED, pas de
  `columns_to_sync` → identique à ce que crée `3_sync_indexes`)_

- [ ] **C3. Vérifier les index** (UI Vector Search DEV, endpoint `qualibot`) :
  `chunks_index_v1`, `chunks_as_index_v1`, `chunks_is_index_v1` ONLINE, nombre
  de lignes indexées = nombre de lignes des tables. Si le job s'arrête avant la
  fin de l'embedding (« Still running after 120 min »), ce n'est pas une erreur :
  la sync continue côté serveur.

### K. Knowledge Assistants DEV

- [x] **K1. Créer les 3 KA** _(2026-10-05)_ (`qualibot_ALL_v2` / `qualibot_AS_v2` /
  `qualibot_IS_v2`, sur les index `_v1` DEV, CAN_QUERY pour le SP de l'app) :

  ```powershell
  databricks bundle run provision_knowledge_assistant_dev -t dev --profile DEV
  ```

- [ ] **K1b. Vous donner Can Manage sur les 3 KA** (créés par le SP : « User does
  not have permission 'View' on Endpoint ka-4d15cb32-endpoint », 2026-10-05).
  Le job accepte maintenant `extra_manager_users` (défaut en DEV : vous). Il est
  idempotent : il ne recrée rien, il complète les droits. 1er essai : droits
  posés sur les KA mais toujours « no permission 'View' » sur l'endpoint → le
  job accorde maintenant aussi CAN_MANAGE directement sur les 3 endpoints de
  serving (ligne `CAN_MANAGE on endpoint … for [...]` dans la sortie).

  ```powershell
  git pull
  databricks bundle deploy -t dev --profile DEV
  databricks bundle run provision_knowledge_assistant_dev -t dev --profile DEV
  ```

- [x] **K2. Me donner les 3 `endpoint_name`** _(2026-10-05 : ALL `ka-4d15cb32-endpoint`, AS `ka-2ef8a9ac-endpoint`, IS `ka-710526e7-endpoint` — reportés dans `target_env.json`)_ affichés dans le résumé du run
  (`[ALL] … -> endpoint_name=ka-…`, idem AS et IS). Je les mets dans le bloc
  `dev` de `utils/deploy/target_env.json` (`CHAT_ENDPOINT` = `CHAT_ENDPOINT_ALL`
  = ALL, `CHAT_ENDPOINT_AS`, `CHAT_ENDPOINT_IS`), puis bloc A.

### D. Droits du SP de l'app (`8e411164-…`) — audit du 2026-10-05

L'app appelle tout avec le token de son SP (l'impact search retente avec le
token de l'utilisateur sur un 403). Constat : chat en échec de permission.

| Appel | Droit | Accordé par |
|---|---|---|
| Chat → 3 endpoints KA | CAN_QUERY sur chaque endpoint de serving | `provision_knowledge_assistant_dev` (`GRANT_ON_ENDPOINTS=true`) |
| Impact search → `chunks_index_v1` (+ AS, IS) | USE CATALOG/SCHEMA + SELECT | `qualibot-grant-app-access-dev` |
| Volumes `doc_compare`, `test` | READ + WRITE VOLUME | `qualibot-grant-app-access-dev` |
| Lakebase | rôle Postgres `app-doc-compare-sp` | bundle (OK, logs du 2026-10-05) |
| LLM `databricks-claude-sonnet-4-6`, `databricks-gpt-5-6-luna` | CAN_QUERY | en général ouverts à tous — D3 |

- [ ] **D1. Rejouer les droits**

  ```powershell
  git pull
  databricks bundle deploy -t dev --profile DEV
  databricks bundle run grant_app_access_dev -t dev --profile DEV
  databricks bundle run provision_knowledge_assistant_dev -t dev --profile DEV
  ```

  Sortie attendue du 2e job : 3 lignes `[ALL|AS|IS] endpoint ka-…: CAN_MANAGE [...], CAN_QUERY app SP 8e411164-…`.

- [ ] **D2. Vérifier sur un endpoint KA**

  ```powershell
  $id = (databricks serving-endpoints get ka-4d15cb32-endpoint --profile DEV -o json | ConvertFrom-Json).id
  databricks serving-endpoints get-permissions $id --profile DEV
  ```

- [ ] **D3. Endpoints LLM** : si Compare ou le judge d'impact renvoie un 403,
  regarder qui peut les interroger :

  ```powershell
  foreach ($e in 'databricks-claude-sonnet-4-6','databricks-gpt-5-6-luna') {
    $id = (databricks serving-endpoints get $e --profile DEV -o json | ConvertFrom-Json).id
    "== $e"; databricks serving-endpoints get-permissions $id --profile DEV
  }
  ```

### V. Chat VSI — 403 et droits du SP de l'app (2026-10-07)

Le Chat VSI appelle tout avec le token du SP de l'app (`8e411164-a7e8-46ff-8013-8c56af2c3656`) :
Vector Search sur `dev_landingzone.qualibot.chunks_index_v1` / `chunks_as_index_v1` /
`chunks_is_index_v1` (SELECT), puis `databricks-claude-sonnet-4-6` (CAN_QUERY) pour la
reformulation et la réponse. Le message d'erreur dit lequel répond 403 :
« Document search failed (Vector Search returned 403) » → index ; « Chat failed … endpoint
returned 403 » → endpoint LLM.

- [ ] **V0. Erreur `dev: no such target. Available targets: default`** (+ `unknown field: nclude` /
  `ariables`) : le `databricks.yml` local n'est pas celui de la branche (début de fichier abîmé).
  Le remplacer par celui de `feature/chat-vsi-merged-on-impact-search`, puis vérifier :

  ```powershell
  databricks bundle validate -t dev --profile DEV
  ```

- [ ] **V1. Rejouer les droits par le job** — il accorde maintenant, en un seul run : USE
  CATALOG/SCHEMA, volumes, SELECT sur les 3 index, et CAN_QUERY sur
  `databricks-claude-sonnet-4-6` et `databricks-gpt-5-6-luna`. Pour changer ce qui est
  accordé : les 3 listes `volumes` / `indexes` / `serving_endpoints` du job
  `grant_app_access_dev` dans `databricks.yml` (`base_parameters`), rien d'autre.

  ```powershell
  databricks bundle deploy -t dev --profile DEV
  databricks bundle run grant_app_access_dev -t dev --profile DEV
  ```

  Sortie attendue : une ligne `OK:` par droit, puis `All 9 grants applied.`

- [ ] **V2. Si une ligne `FAILED: CAN_QUERY on serving endpoint …`** : le SP des jobs n'a pas
  CAN_MANAGE sur cet endpoint LLM (souvent réservé à un admin du workspace). Vérifier, puis
  faire accorder le droit par un admin :

  ```powershell
  $id = (databricks serving-endpoints get databricks-claude-sonnet-4-6 --profile DEV -o json | ConvertFrom-Json).id
  databricks serving-endpoints get-permissions $id --profile DEV
  databricks serving-endpoints update-permissions $id --profile DEV --json '{\"access_control_list\":[{\"service_principal_name\":\"8e411164-a7e8-46ff-8013-8c56af2c3656\",\"permission_level\":\"CAN_QUERY\"}]}'
  ```

- [ ] **V3. Retester** une question en ALL, AS et IS dans l'onglet Chat VSI ; en cas
  d'erreur, `databricks apps logs qualibot --profile DEV` montre la ligne
  `chat_vsi: Vector Search … returned 403` ou `… returned 403` sur l'endpoint.

### G. Évaluation golden — Chat KA vs Chat VSI (DEV, 2026-10-07)

Notebook `utils/databricks_ops/evaluation/golden_eval_ka_vs_vsi.py` : chaque cas de
`dev_landingzone.qualibot.qualibot_eval_golden` passe par le code de l'app déployée, mêmes
étapes qu'un tour de chat (historique, traduction, date, moteur, citations, sources,
retraduction), avec la config de l'app DEV (`app.yaml` + `target_config.env`). Un run MLflow
par moteur : `Correctness`, `ExpectationsGuidelines`, `golden_doc_recall`, `latency_s`.

- [ ] **G1. Déployer le code en DEV** (le notebook lit le code et la config déployés) :

  ```powershell
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
  ```

- [ ] **G2. Lancer le notebook** dans le workspace DEV, en serverless :
  `/Workspace/Shared/.bundle/qualibot/dev/files/utils/databricks_ops/evaluation/golden_eval_ka_vs_vsi`
  → Run all. Il enchaîne tous les essais de la cellule *Plan* qui ne sont pas encore dans
  `eval_golden_runs` (les essais déjà enregistrés sont sautés) ; chaque essai est enregistré
  dès qu'il finit. Nouvel essai = une ligne de plus dans *Plan*, puis Run all.

- [ ] **G2b. Juger à l'œil, sans run complet** : notebook
  `utils/databricks_ops/evaluation/replay_compare.py` — widgets `configs` (noms de la cellule
  CONFIGS), `question_filter` (morceaux de questions golden séparés par `|`), `extra_question`,
  `stored_evals` (réponses déjà enregistrées, sans relance). Run all → une carte par question,
  une colonne par configuration. Rien n'est enregistré.

- [ ] **G2c. Évaluer la recherche seule (sans réponse, sans juge)** : notebook
  `utils/databricks_ops/evaluation/retrieval_eval.py` → Run all. Questions golden + synthétiques
  (UAT) + retours négatifs (UAT) qui ont un document attendu ; mesure si ces documents sont
  dans le contexte envoyé au LLM, pour chaque configuration de recherche (cellule CONFIGS).
  Résultats dans `dev_landingzone.qualibot.eval_retrieval_runs`. Seules les questions pas encore
  réussies pour une configuration sont lancées (les erreurs sont relancées, 3 essais chacune) ;
  les tableaux ne comptent que les questions réussies par toutes les configurations.
  Vague 3 (configs `u-*`, 2026-10-07) : recherche par titre du catalogue, réécriture FR + EN
  (dont `u-bi-luna`, réécriture par GPT-5.6 Luna), une langue par document, 20 passages
  plafonnés à 3 par document, puis tout ensemble (`u-all`).
  Colonne `recall_top5_pct` : documents attendus parmi les 5 premiers du contexte, pour comparer
  à taille égale.

- [ ] **G2d. Requêtes de diagnostic de l'index** : `docs/chat_vsi_tests.md` § 7, Q1 à Q8,
  dans l'éditeur SQL DEV (lecture seule). Envoyer les résultats : ils chiffrent les constats
  de l'audit du parsing (part d'images, tables des matières, documents attendus absents de
  l'index…).

- [ ] **R. Tester le nouveau découpage sur des index de test** (étapes 1 à 3 faites le 2026-10-08 :
  **v2b retenu**, 80.9 % contre 75.8 % avec deux fois moins de contexte ; audit § 5.6) (code du 2026-10-08, audit
  `docs/chat_vsi_tests.md` § 5 ; ne touche ni `chunks_v1` ni `chunks_index_v1`).
  1. `.\utils\deploy\deploy_qualibot.ps1 -AppEnv dev -SyncOnly`.
  2. Notebook `utils/databricks_ops/evaluation/rechunk_experiment.py`, serverless, Run all, trois
     fois avec ces widgets (le reste par défaut) :
     - `variant=v2a` : découpage corrigé, tailles actuelles (250 / 500 / 1 000 tokens, 4 000 caractères) ;
     - `variant=v2b`, `min_tokens=150`, `target_tokens=300`, `max_tokens=450`, `max_chars=1600` :
       passages courts (tiennent dans la fenêtre du reranker) ;
     - `variant=v2c`, `embed_prefix=false` : comme v2a sans le préfixe `[Source: …]`.
     Chaque run crée `chunks_<variant>` et lance l'index `chunks_index_<variant>` (30 à 60 min
     d'embedding avant `ONLINE`).
  3. Quand les index sont `ONLINE` : `retrieval_eval`, widgets `index_variants=v2a,v2b` (`,v2c` si
     construit) et `configs=u-all-luna6` (pas vide : sinon toutes les anciennes configs restent dans la
     liste ; celles déjà mesurées sont sautées, mais autant ne lancer que les nouvelles). Run all.
     Configs lancées : `idx-v1` et `idx-v1-all` (index actuel), puis pour chaque variante `idx-v2a`
     (union-ctx), `idx-v2a-clean` (sommaires, cartouches et textes répétés écartés), `idx-v2a-all`
     (`u-all`, la config du chat). Tout est réécrit par GPT-6 Luna (widget `rewrite_model`, comme le
     chat) ; `u-all-luna6` se compare à l'ancienne ligne `u-all` (réécriture Sonnet 4.6) et dit ce que le
     passage à GPT-6 Luna coûte en recherche. Envoyer les tableaux.
  3b. _(fait 2026-10-08 : k20 moins bon que 12, réponses v2b au moins aussi bonnes et −39 % de coût ;
     audit § 5.6)_ Deux vérifications sur v2b avant le re-découpage UAT (copier le nouveau zip, `-SyncOnly`) :
     - `retrieval_eval`, widgets `index_variants=v2b`, `configs` = `idx-v2b-all-k20` (une seule ligne
       nouvelle : 20 passages reclassés au lieu de 12 ; ≈ 10 min). Si elle dépasse nettement 80.9 %
       sans repasser au-dessus de ≈ 20k tokens, on met `CHAT_VSI_RERANK_TOP_K=20` dans le chat.
     - `pairwise_answers`, Run all avec les **nouveaux défauts** (supprimer les widgets si le notebook
       garde les anciens) : `eval_id=luna6-v2b`, référence `databricks-gpt-6-luna@u-all-v1+v3+lang`,
       concurrent `databricks-gpt-6-luna@u-all-v2b+v3+lang`, juge GPT-5.6 Luna. Même modèle, seul
       l'index change : dit si les réponses sont au moins aussi bonnes avec des passages courts.
       ≈ 2 €, ≈ 20 min. Envoyer les trois tableaux.
  4. Plus tard, si ça vaut la dépense : `variant=v2d`, `doc_cards=true` (fiche par document,
     ≈ 15 €), puis `variant=v2e`, `doc_cards=true`, `chunk_context=true` (≈ 65 € au total).
     `enrich_max_docs=50` pour un essai à quelques euros d'abord.

- [ ] **P. Comparer des versions de réponse côte à côte** (2026-10-08) : notebook
  `utils/databricks_ops/evaluation/pairwise_answers.py`, serverless, Run all. Référence Sonnet 5.5 +
  recherche `union-ctx`, contre GPT-6 Luna avec `union-ctx`, `u-title`, `u-bi`, `u-all`, et les réponses
  **déjà stockées** du KA. 40 questions (les 21 du golden + 19 vraies questions DEV auxquelles le KA a
  répondu). Juge GPT-5.6 Luna, dans les deux ordres. Vérifier d'abord le nom exact des endpoints dans
  Serving (widgets `reference`, `contenders`, `judge`). Résultats dans
  `dev_landingzone.qualibot.eval_pairwise_runs` ; une autre série = un autre `eval_id`.
  Fait le 2026-10-08 (`luna6-versions`), résultats dans `docs/chat_vsi_tests.md` § 2.6.

- [x] **P2. Rerun prompt de GPT-6 Luna** _(fait 2026-10-08, `luna6-prompt`)_ : `u-all+v3+lang` retenu
  (19 gagnés / 16 perdus contre Sonnet 5.5, 0.63 invention contre 1.59, juge constant 88 %). Détail :
  `docs/chat_vsi_tests.md` § 2.6.

- [ ] **L. Passer le Chat VSI DEV sur GPT-6 Luna + GPT-5.6 Luna en secours, sans aucun modèle Claude**
  (décision 2026-10-08 ; récap `docs/chat_vsi_robustesse_2026-10.md`). Tout est déjà dans le code :
  `app.yaml` (réponse GPT-6 Luna, secours GPT-5.6 Luna, pour toutes les cibles) et le bloc `"dev"` de
  `utils/deploy/target_env.json` (variante `rerank` + `u-all`, réécriture GPT-6 Luna avec secours GPT-5.6
  Luna, `CHAT_VSI_INSTRUCTIONS=v3`, `CHAT_VSI_LANGUAGE_REMINDER=on`). Le pont de traduction était déjà sur
  GPT-5.6 Luna (secours GPT-6 Luna). À faire, dans l'ordre :
  1. Serving DEV : vérifier que `databricks-gpt-6-luna` et `databricks-gpt-5-6-luna` existent.
  2. Copier le zip, puis donner au SP de l'app le droit d'interroger GPT-6 Luna (ajouté au job) :
     ```powershell
     databricks bundle deploy -t dev --profile DEV
     databricks bundle run grant_app_access_dev -t dev --profile DEV
     ```
     Sortie attendue : une ligne `OK:` par droit, dont `CAN_QUERY on serving endpoint databricks-gpt-6-luna`.
     Si `FAILED` sur cet endpoint : bloc V2 avec `databricks-gpt-6-luna`.
  3. `.\utils\deploy\deploy_qualibot.ps1 -AppEnv dev` (**sans** `-SyncOnly` : l'app doit redémarrer).
  4. Test dans l'onglet Chat VSI : une question en français, une en espagnol ou tchèque, une hors sujet
     (« recette de riz au thon » → refus), « donne-moi le lien de l'OPEX Sharepoint » (→ l'URL de INAQ-742),
     une question de suivi courte (« et pour IS ? ») pour voir la réécriture.
  5. Logs de l'app : `chat_vsi_llm` (aucune ligne = aucun incident ; une ligne « answered by fallback »
     = GPT-6 Luna indisponible), `chat turn done`.
  Notebooks d'éval : ils reprennent les index et le modèle de réponse de l'app ; `retrieval_eval` et
  `pairwise_answers` réécrivent désormais avec GPT-6 Luna (widget `rewrite_model`). UAT : bloc `"uat"` de
  `target_env.json` + droits du SP UAT, seulement avec ton accord (déploiement de la vraie app).

- [ ] **G3. Lire le résultat** : tableau des moyennes par moteur, puis le détail cas par cas ;
  les runs sont dans l'expérience MLflow `/Users/<toi>/qualibot-golden-ka-vs-vsi`.

- [ ] **G3b. Comparer les essais en SQL** : chaque essai est ajouté à
  `dev_landingzone.qualibot.eval_golden_runs` sous son nom (widgets `ka_eval_id`, défaut `ka`,
  et `vsi_eval_id`, défaut `baseline` ; `notes` pour décrire ce qui a changé). Coller tout
  `utils/databricks_ops/evaluation/golden_eval_queries.sql` dans l'éditeur SQL DEV, renseigner
  `eval_a`, `eval_b`, `question_like`, Run all.

- [ ] **G3c. Variante `rerank`** (2026-10-07, `server/services/chat_vsi_rerank.py`, baseline
  `chat_vsi.py` inchangé) : une fois, ajouter les nouvelles colonnes à la table existante :

  ```sql
  ALTER TABLE dev_landingzone.qualibot.eval_golden_runs ADD COLUMNS (
    vsi_variant string, vsi_settings string, app_code_hash string,
    retrieved_refs array<string>, search string);
  ```

  puis déployer, et lancer le notebook avec `engines=vsi`, `vsi_variant=rerank` (eval id
  `rerank` par défaut). Vérifier dans la requête 1 que `settings` montre `databricks_reranker`
  et dans la table que `search` vaut `vector_search+rerank` (sinon le reranker a été refusé
  par l'index et la baseline a répondu).

- [ ] **G4. Comparer un autre modèle pour VSI** : `engines=vsi`, `vsi_llm_endpoint=<endpoint>`,
  `vsi_eval_id=<nom>` (ex. `databricks-gpt-5-6-luna`). Pour `databricks-claude-sonnet-5-5`, la
  température forcée doit d'abord être retirée dans `streaming.py` (voir le rapport Chat VSI).

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
  En cas de 403 sur l'index (USE CATALOG vient de I2b ; le reste, vous pouvez
  l'accorder comme owner du schema) :

  ```sql
  GRANT USE CATALOG ON CATALOG dev_landingzone TO `8e411164-a7e8-46ff-8013-8c56af2c3656`;
  GRANT USE SCHEMA ON SCHEMA dev_landingzone.qualibot TO `8e411164-a7e8-46ff-8013-8c56af2c3656`;
  GRANT SELECT ON TABLE dev_landingzone.qualibot.chunks_index_v1 TO `8e411164-a7e8-46ff-8013-8c56af2c3656`;
  ```

- [ ] **A5. Date « documents as of »** : vide dans une base Lakebase neuve.
  Pour la remplir **sans** lancer la chaîne de parsing, uniquement la dernière
  tâche du job :

  ```powershell
  databricks bundle run parsing_pipeline -t dev --profile DEV --only 6_update_kb_metadata
  ```

### M. Accès développeur pour Mehdi (`mehdi.lamrani@databricks.com`, 2026-10-05)

Deux familles d'objets :
- **gérés par le bundle** (app, projet Lakebase, rôles Postgres, jobs DEV) :
  liste de permissions = état complet, un droit ajouté à la main dans l'UI
  est **effacé au prochain `bundle deploy`** → Mehdi est déclaré dans
  `databricks.yml` (M1) ;
- **hors bundle** (Unity Catalog, endpoint Vector Search, KA) : à la main,
  ça reste (M2–M4).

- [ ] **M1. Bundle** : app CAN_MANAGE, projet Lakebase CAN_MANAGE, rôle Postgres
  `mehdi-lamrani` (SUPERUSER sur la base `doccompare`), CAN_MANAGE sur les 10
  jobs DEV ; puis le job KA, qui l'ajoute en CAN_MANAGE sur les 3 KA et leurs
  endpoints.

  ```powershell
  git pull
  databricks bundle plan -t dev --profile DEV     # permissions en update, create postgres_roles.mehdi, 0 to delete
  databricks bundle deploy -t dev --profile DEV
  databricks bundle run provision_knowledge_assistant_dev -t dev --profile DEV
  ```

- [ ] **M2. Unity Catalog — catalogs, volumes, index** (USE CATALOG demande des
  droits que vous n'avez pas : on passe par le job de droits, qui tourne sous le
  SP DEV, avec Mehdi comme destinataire) :

  ```powershell
  databricks bundle run grant_app_access_dev -t dev --profile DEV --params app_service_principal=mehdi.lamrani@databricks.com
  ```

  → USE CATALOG `dev_landingzone`, USE SCHEMA `qualibot`, READ+WRITE sur
  `doc_compare`/`test`, SELECT sur les 3 index.

- [ ] **M3. Unity Catalog — tables** (SQL editor DEV ; vous êtes owner de
  `dev_landingzone.qualibot` et de `dev_proj.qualibot`). **Attention** :
  `dev_landingzone.qualibot` contient aussi la copie des conversations UAT
  réelles (`chat_messages`, `chat_sessions`, `users`, `feedbacks`… écrites par
  `lakebase_import_uat_to_dev`). Choisir :

  ```sql
  -- (a) tout le schema, données de chat UAT comprises
  GRANT ALL PRIVILEGES ON SCHEMA dev_landingzone.qualibot TO `mehdi.lamrani@databricks.com`;

  -- (b) seulement le corpus et l'état du pipeline
  GRANT CREATE TABLE ON SCHEMA dev_landingzone.qualibot TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot.chunks_v1               TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot.src_chunks_as_v1        TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot.src_chunks_is_v1        TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot._pipeline_checkpoint_v1 TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot.processed_files_v1      TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot.image_metadata_v1       TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot.parse_manifest_v1       TO `mehdi.lamrani@databricks.com`;
  GRANT SELECT, MODIFY ON TABLE dev_landingzone.qualibot.category_reference_v1   TO `mehdi.lamrani@databricks.com`;

  -- schema projet (MLflow, traces) : vide pour l'instant
  GRANT ALL PRIVILEGES ON SCHEMA dev_proj.qualibot TO `mehdi.lamrani@databricks.com`;
  ```

  Pour `dev_proj`, USE CATALOG : propriétaire `leap-core-service_accounts-dev`
  (demander, ou me dire et j'étends le job de M2).

- [ ] **M4. Endpoint Vector Search `qualibot`** (créé sous votre identité) — UI :
  Compute ▸ Vector Search ▸ `qualibot` ▸ Permissions ▸ Mehdi « Can manage ».
  Ou :

  ```powershell
  $ep = (databricks vector-search-endpoints get-endpoint qualibot --profile DEV -o json | ConvertFrom-Json).id
  databricks permissions update vector-search-endpoints $ep --profile DEV `
    --json '{\"access_control_list\":[{\"user_name\":\"mehdi.lamrani@databricks.com\",\"permission_level\":\"CAN_MANAGE\"}]}'
  ```

- [ ] **M5. Pour qu'il puisse déployer lui-même** (`bundle deploy -t dev`) :
  - rôle « Service principal: User » sur `job-runner-sa-dev` (Settings ▸
    Identity and access ▸ Service principals ▸ job-runner-sa-dev ▸
    Permissions) — sinon le `run_as` des jobs est refusé ;
  - accès au repo Bitbucket (hors Databricks) ;
  - le dossier du bundle `/Workspace/Shared/.bundle/qualibot/dev` est déjà
    accessible à tous.

- [ ] **M6. Job de parsing `D_1_qualibot-parsing-pipeline-dev`** : sa
  définition est partagée avec UAT/PROD, Mehdi n'y est pas déclaré. Un droit
  ajouté à la main dans l'UI tient jusqu'au prochain `bundle deploy`. Solution
  durable et la plus simple pour tout : l'ajouter au groupe
  `Role-Project-LEAP-CoreDev` (CAN_MANAGE partout) — décision / action d'un admin.

- [ ] **A6. Impact search — « Judgment failed: judge returned no JSON object: '' »**
  (2 documents sur une douzaine, 2026-10-05). Cause : le juge
  (`databricks-gpt-5-6-luna`, modèle à raisonnement) épuise ses 1 500 tokens
  en raisonnement sur les documents à nombreux passages et ne répond rien. Corrigé
  dans le code : la relance se fait avec 3× le budget (plafond 8 000), et l'erreur
  dit maintenant `ran out of tokens` si ça arrive encore. Redéployer l'app puis
  relancer la même impact search (les résultats en erreur ne sont jamais mis en
  cache) :

  ```powershell
  git pull
  .\utils\deploy\deploy_qualibot.ps1 -AppEnv dev -SkipBuild
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
- Si le pipeline de parsing DEV est un jour réactivé (sous le SP DEV) : vérifier
  que le SP peut synchroniser les index et utiliser l'endpoint `qualibot`
  créés sous votre identité par `copy_uat_to_dev` (CAN_MANAGE sur l'endpoint
  au besoin).
- Copier aussi les images du volume `uat_landingzone.qualibot.images` (pas
  nécessaire au chat ni à l'impact search ; seulement si un run de parsing
  DEV doit retravailler des images déjà extraites).

## Fait

_(rien de confirmé pour l'instant)_
