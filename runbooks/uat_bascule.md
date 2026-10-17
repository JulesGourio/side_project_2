# Bascule UAT — checklist

Une seule liste, cochable, pour suivre la bascule de l'UAT de l'ancienne version vers la nouvelle. Cocher une case seulement après confirmation. L'ancienne app reste en marche jusqu'à la phase S.

Profil CLI : `UAT`. Les déploiements se font par les pipelines Bitbucket (SP UAT), pas avec ton profil personnel.

## Phase P — préparation (ancienne app en marche, aucune modification de l'app)

- [ ] **P1. Clones des tables** (éditeur SQL UAT, un bloc à la fois)

```sql
CREATE SCHEMA IF NOT EXISTS uat_proj.qualibot;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parsing_checkpoint DEEP CLONE uat_landingzone.qualibot._pipeline_checkpoint_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parsed_documents DEEP CLONE uat_landingzone.qualibot.processed_files_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.image_metadata DEEP CLONE uat_landingzone.qualibot.image_metadata_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parse_manifest DEEP CLONE uat_landingzone.qualibot.parse_manifest_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.category_reference DEEP CLONE uat_landingzone.qualibot.category_reference_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.parsing_run_health DEEP CLONE uat_landingzone.qualibot.parsing_run_health_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.document_change_log DEEP CLONE uat_landingzone.qualibot.document_change_log_v1;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.eval_feedback_failures DEEP CLONE uat_landingzone.qualibot.feedback_failure_cases;
CREATE TABLE IF NOT EXISTS uat_proj.qualibot.chunks DEEP CLONE uat_landingzone.qualibot.chunks_v1;
ALTER TABLE uat_proj.qualibot.chunks SET TBLPROPERTIES (
  'delta.enableChangeDataFeed' = true,
  'delta.deletedFileRetentionDuration' = 'interval 60 days',
  'delta.logRetentionDuration' = 'interval 60 days'
);
```

- [ ] **P2. Contrôle des comptes**

```sql
SELECT 'chunks' AS t, (SELECT COUNT(*) FROM uat_landingzone.qualibot.chunks_v1) AS source, (SELECT COUNT(*) FROM uat_proj.qualibot.chunks) AS copie;
SELECT 'parsed_documents' AS t, (SELECT COUNT(*) FROM uat_landingzone.qualibot.processed_files_v1) AS source, (SELECT COUNT(*) FROM uat_proj.qualibot.parsed_documents) AS copie;
```

Attendu : `source` = `copie` sur les deux lignes.

- [ ] **P3. Renommage des colonnes de `parsed_documents`**

```sql
SHOW COLUMNS IN uat_proj.qualibot.parsed_documents;
```

Si `include_in_rag`, `filtered_by_date` et `chunks_truncated` sont présentes :

```sql
ALTER TABLE uat_proj.qualibot.parsed_documents SET TBLPROPERTIES (
  'delta.columnMapping.mode' = 'name',
  'delta.minReaderVersion' = '2',
  'delta.minWriterVersion' = '5'
);
ALTER TABLE uat_proj.qualibot.parsed_documents RENAME COLUMN include_in_rag TO is_in_rag;
ALTER TABLE uat_proj.qualibot.parsed_documents RENAME COLUMN filtered_by_date TO is_filtered_by_date;
ALTER TABLE uat_proj.qualibot.parsed_documents RENAME COLUMN chunks_truncated TO is_chunks_truncated;
SHOW COLUMNS IN uat_proj.qualibot.parsed_documents;
```

Attendu : `is_in_rag`, `is_filtered_by_date`, `is_chunks_truncated` présentes, anciens noms absents.

- [ ] **P4. Volumes** (PowerShell, profil `UAT`)

```powershell
databricks volumes create uat_proj qualibot doc_compare MANAGED --profile UAT
databricks fs cp -r dbfs:/Volumes/uat_landingzone/qualibot/doc_compare dbfs:/Volumes/uat_proj/qualibot/doc_compare --profile UAT
databricks volumes create uat_proj qualibot docling_models MANAGED --profile UAT
databricks fs cp -r dbfs:/Volumes/uat_landingzone/qualibot/docling_models dbfs:/Volumes/uat_proj/qualibot/docling_models --profile UAT
databricks volumes create uat_proj qualibot images MANAGED --profile UAT
databricks fs cp -r dbfs:/Volumes/uat_landingzone/qualibot/images dbfs:/Volumes/uat_proj/qualibot/images --profile UAT
databricks fs ls dbfs:/Volumes/uat_landingzone/qualibot/doc_compare --profile UAT
databricks fs ls dbfs:/Volumes/uat_proj/qualibot/doc_compare --profile UAT
```

Attendu : les dossiers de session sont identiques des deux côtés. `test` n'est pas copié.

- [ ] **P5. Rétention de 60 jours** (SQL UAT)

```sql
ALTER TABLE uat_proj.qualibot.parsing_checkpoint SET TBLPROPERTIES ('delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
ALTER TABLE uat_proj.qualibot.parsed_documents SET TBLPROPERTIES ('delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
ALTER TABLE uat_proj.qualibot.image_metadata SET TBLPROPERTIES ('delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
ALTER TABLE uat_proj.qualibot.parse_manifest SET TBLPROPERTIES ('delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
ALTER TABLE uat_proj.qualibot.category_reference SET TBLPROPERTIES ('delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
ALTER TABLE uat_proj.qualibot.parsing_run_health SET TBLPROPERTIES ('delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
ALTER TABLE uat_proj.qualibot.document_change_log SET TBLPROPERTIES ('delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days');
```

- [ ] **P6. Endpoint Sonnet 5.5 présent** (PowerShell)

```powershell
databricks serving-endpoints get databricks-claude-sonnet-5-5 --profile UAT
```

Attendu : `"name": "databricks-claude-sonnet-5-5"`.

- [ ] **P7. État de référence de l'app actuelle** (PowerShell) — à noter avant toute action

```powershell
databricks apps get qualibot --profile UAT
```

- [ ] **P8. Décision sur les `DELETE` et `DROP COLUMN` de `server/services/infra/lakebase/schema.py`** (doublons de `users`, colonnes). Décision à noter ici : ____________. Le code correspondant est modifié et poussé avant la phase S.

## Phase J — jobs (ancienne app intacte)

- [ ] **J0a. Détacher l'état du bundle UAT de `uat_landingzone`** (sans rien supprimer dans Databricks) : copier le git dans `latec-compare`, puis Bitbucket, pipeline `uat-state-fix`. Il détache `qualibot_schema`, `qualibot_test`, `qualibot_doc_compare`, `qualibot_images`, puis rattache `qualibot_doc_compare` et `qualibot_images` aux volumes `uat_proj` créés en P4. Attendu : vert.

- [ ] **J0b. Plan lecture seule UAT** : Bitbucket, pipeline `plan-uat`. Attendu : aucun `delete` ni `recreate` sur le schéma, les volumes ou l'app. Les `create` de nouveaux jobs sont normaux. Les `delete` de jobs (`apps_stop_nightly_uat`, `quality_scoring`, `score_production_qa_uat`, `sync_mlflow_scorer_assessments_uat`, `traces_migration`) sont à valider un par un dans l'UI avant J3.

- [ ] **J0c. Retirer les déclarations temporaires** `qualibot_schema` et `qualibot_test` de `databricks.yml` (cible `qualibot-uat`), commit et push. Les ressources de l'app (`doc-compare-volume` et `test-volume` vers `uat_landingzone`) restent temporaires jusqu'à S4.

- [ ] **J1. Pause du planning du job de parsing UAT** : UI UAT, Workflows, `D_1_Qualibot_Parsing_Pipeline`, *Pause* sur le planning.

- [ ] **J2. Mode du rechunk** : `rechunk` = aucun parsing, `chunks` reconstruits depuis le checkpoint, `image_metadata` conservée telle quelle ; `full` = scanne tous les documents, parse ceux dont l'IDDOC n'a pas de ligne réutilisable pour les mêmes octets (au plus `parsing_max_parse_files`, 500 par défaut, sinon arrêt avant le parsing), reconstruit toutes les tables depuis le checkpoint, et copie le checkpoint dans `_pipeline_checkpoint_<horodatage>`. Le run UAT du 2026-10-16 a été lancé en `full` (ligne 456 de `databricks.yml`, cible `qualibot-uat`).

- [ ] **J3. Déploiement des jobs UAT** (seulement si J0b est conforme) : copier le git dans `latec-compare`, puis Bitbucket, pipeline `deploy-uat-jobs`. Attendu : vert.

- [ ] **J4. L'ancienne app tourne toujours** (PowerShell) :

```powershell
databricks apps get qualibot --profile UAT
```

Attendu : même état qu'en P7.

- [ ] **J5. Rechunk** : UI UAT, Workflows, `D_1_Qualibot_Parsing_Pipeline`, *Run now*, paramètres par défaut (`dry_run = true`). Attendre la fin.

- [ ] **J5b. Reprise après le run du 2026-10-16** (run `87f03f9a-b60f-40a7-a336-63561356d22a` : 20 h de parsing GPU de tout le corpus, puis échec `ModuleNotFoundError: great_expectations` dans les contrôles qualité de `3_parse`, rien écrit dans `chunks`). Le parsing est dans `parsing_checkpoint`, il n'est pas refait. Diagnostic du 2026-10-17 : 2 339 fichiers re-parsés, dont 2 273 aux mêmes octets qu'en v1 mais 0 au même chemin (dossier `D_` choisi au lieu de `Dm_`). Corrigé le 2026-10-17 : GX installé sur la tâche `3_parse` et importé dès la première cellule, reprise sur (IDDOC, hash du contenu) au lieu du chemin, arrêt avant tout parsing au-delà de `parsing_max_parse_files` (500) fichiers.
  1. Diagnostic (éditeur SQL UAT, lecture seule) : pourquoi les lignes copiées de `_pipeline_checkpoint_v1` n'ont pas été reconnues.

     ```sql
     WITH n AS (SELECT IDDOC, source_path, document_sha256 FROM uat_proj.qualibot.parsing_checkpoint
                WHERE ingestion_run_id = '87f03f9a-b60f-40a7-a336-63561356d22a'),
          o AS (SELECT IDDOC, source_path, document_sha256 FROM uat_proj.qualibot.parsing_checkpoint
                WHERE ingestion_run_id IS DISTINCT FROM '87f03f9a-b60f-40a7-a336-63561356d22a' AND parse_status = 'SUCCESS')
     SELECT count(DISTINCT n.IDDOC) AS parses_du_run,
            count(DISTINCT CASE WHEN o.IDDOC IS NOT NULL THEN n.IDDOC END) AS iddoc_deja_dans_v1,
            count(DISTINCT CASE WHEN o.document_sha256 = n.document_sha256 THEN n.IDDOC END) AS memes_octets,
            count(DISTINCT CASE WHEN o.source_path = n.source_path THEN n.IDDOC END) AS meme_chemin,
            max(n.source_path) AS exemple_chemin_nouveau, max(o.source_path) AS exemple_chemin_v1
     FROM n LEFT JOIN o ON n.IDDOC = o.IDDOC;
     ```
  2. Copier le git dans `latec-compare`, puis Bitbucket, pipeline `deploy-uat-jobs` (la cible reste en `full`).
  3. UI UAT, Workflows, `D_1_Qualibot_Parsing_Pipeline`, *Run now*. Attendu dans le log de `3_parse` : `[RESUME] ~4 900 of ~4 950 selected files reused`, `[BUDGET]` à quelques dizaines de fichiers, la tâche ne fait plus que le scan, le parsing de ces quelques fichiers et la reconstruction des tables. Si `[BUDGET]` arrête la tâche : rien n'est parsé, envoyer la ligne `[RESUME]`.

- [ ] **J6. Index `chunks_index` ONLINE** (PowerShell) :

```powershell
databricks vector-search-indexes get-index uat_proj.qualibot.chunks_index --profile UAT -o json | ConvertFrom-Json | Select-Object name, status
```

Attendu : `ONLINE`. Peut prendre environ une heure.

- [ ] **J7. Retour au mode quotidien** : fait dans `databricks.yml` (ligne 456 repassée à `incremental`), commit et push. Puis Bitbucket, pipeline `deploy-uat-jobs`.

- [ ] **J8. Reprise du planning** : UI UAT, Workflows, `D_1_Qualibot_Parsing_Pipeline`, *Resume*.

- [ ] **Prompt du chat (sources en tableau, réponses plus complètes)** : après le déploiement de l'app, lancer le job `register_chat_prompts` de la cible, pour lier la nouvelle version du prompt.

## Phase S — bascule de l'app (accord écrit requis, moment calme recommandé)

- [ ] **S0. Accord écrit pour la bascule** : ____________.

- [ ] **S1. Identifiant du SP de l'app** (PowerShell) :

```powershell
databricks apps get qualibot --profile UAT -o json | ConvertFrom-Json | Select-Object service_principal_client_id
```

- [ ] **S2. Droits du SP de l'app** (éditeur SQL UAT, remplace `<APP_SP>` par la valeur de S1) :

```sql
GRANT USE CATALOG ON CATALOG uat_proj TO `<APP_SP>`;
GRANT USE SCHEMA ON SCHEMA uat_proj.qualibot TO `<APP_SP>`;
GRANT SELECT ON TABLE uat_proj.qualibot.chunks_index TO `<APP_SP>`;
```

- [ ] **S3. Can Query** (UI UAT, Serving) : donner à l'app `databricks-gpt-6-luna` et `databricks-gpt-5-6-luna`.

- [ ] **S4. Déploiement de l'app** : Bitbucket, pipeline `deploy-uat`.

- [ ] **S5. Journal de démarrage** (PowerShell) :

```powershell
databricks apps logs qualibot --profile UAT
```

Attendu : `Copied legacy rows messages -> compare_messages` et `Copied legacy rows errors -> app_errors`, sans erreur.

- [ ] **S6. Lakebase, base `doccompare`** (éditeur SQL Lakebase) :

```sql
SELECT COUNT(*) AS old_paths FROM compare_messages WHERE volume_session_path LIKE '%_landingzone%';
SELECT (SELECT COUNT(*) FROM messages) AS legacy, (SELECT COUNT(*) FROM compare_messages) AS current;
```

Attendu : `old_paths` = 0, `legacy` = `current`.

- [ ] **S7. Tests manuels de l'app** : ALL, AS, IS ; une question en anglais ; une question hors sujet ; un lien ; une recherche d'impact ; une comparaison avec export ; l'historique DocCompare.

- [ ] **S8. Journal de notation** : UI UAT, Workflows, `D_1_Qualibot_Lakebase_Export_To_Volume`, *Run now*. Puis :

```sql
SELECT COUNT(*) FROM uat_proj.qualibot.chat_retrieved_chunks;
```

- [ ] **S9. Catalogue des révisions** : UI UAT, Workflows, `D_1_Qualibot_Parsing_Pipeline`, *Run now* (mode quotidien). Puis redémarrer l'app : UI UAT, Apps, `qualibot`, *Stop*, puis *Start*.

- [ ] **S10. Alertes SQL** (UI UAT, SQL Editor) : les six requêtes de `utils/alerts/`, avec `dev_proj` remplacé par `uat_proj`, une par une. Le tableau nom, condition et planning est dans `utils/alerts/README.md`.

## Phase D — nettoyage (plusieurs jours après la validation, accord écrit)

- [ ] **D1. Anciens KA UAT** : UI UAT, Agents, ⋮, *Delete* sur `qualibot_ALL_v2`, `qualibot_AS_v2`, `qualibot_IS_v2`.

- [ ] **D2. Objets `_v1` et anciennes tables Lakebase** : liste à faire à ce moment-là, avec un accord écrit pour chaque suppression. Ne rien lancer avant.
