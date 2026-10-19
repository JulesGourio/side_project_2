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

- [ ] **M0. Copier l'état du bundle UAT hors de `/Workspace/Shared`** (aucun déploiement, l'état seulement) : Bitbucket, pipeline `migrate-uat-state` (SP `job-runner-sa-uat`). Il copie `/Workspace/Shared/.bundle/qualibot/qualibot-uat/state` vers `/Workspace/.bundle/qualibot/qualibot-uat/state`, sans écraser. Un second lancement échoue, c'est voulu. Doit passer **avant** J0a : `uat-state-fix` et `plan-uat` lisent l'état de la nouvelle racine. Si le SP n'a pas les droits sur `/Workspace/.bundle` : un admin lui donne `CAN_MANAGE` (commande dans le chat), puis on relance.

- [ ] **M1. Retirer l'ancien dossier de bundle UAT** (après M0, J0a, J0b, J3 et deploy-uat verts, et sur confirmation) : `/Workspace/Shared/.bundle/qualibot/qualibot-uat`. Rien ne doit rester sous `/Workspace/Shared/.bundle`, comme pour DEV et PROD.

- [ ] **J0a. Détacher l'état du bundle UAT de `uat_landingzone`** (sans rien supprimer dans Databricks, après M0) : copier le git dans `latec-compare`, puis Bitbucket, pipeline `uat-state-fix`. Il détache `qualibot_schema`, `qualibot_test`, `qualibot_doc_compare`, `qualibot_images`, puis rattache `qualibot_doc_compare` et `qualibot_images` aux volumes `uat_proj` créés en P4. Attendu : vert.

- [ ] **J0d. Couverture de `uat_proj.qualibot.chunks` par rapport à `uat_landingzone.qualibot.chunks_v1`** (SQL UAT, lecture seule). Attendu : tout à 0, sauf écarts expliqués par des écritures de l'ancienne app après le clone (voir la dernière requête).

```sql
-- 1. Totaux et doublons de clé
SELECT 'v1' AS src, COUNT(*) AS chunks, COUNT(DISTINCT chunk_id) AS distinct_chunk_ids, COUNT(DISTINCT IDDOC) AS docs
FROM uat_landingzone.qualibot.chunks_v1
UNION ALL
SELECT 'copie', COUNT(*), COUNT(DISTINCT chunk_id), COUNT(DISTINCT IDDOC)
FROM uat_proj.qualibot.chunks;

-- 2. Chunks présents d'un seul côté
SELECT 'absents de la copie' AS issue, COUNT(*) AS n
FROM (SELECT chunk_id FROM uat_landingzone.qualibot.chunks_v1 EXCEPT SELECT chunk_id FROM uat_proj.qualibot.chunks)
UNION ALL
SELECT 'absents de v1', COUNT(*)
FROM (SELECT chunk_id FROM uat_proj.qualibot.chunks EXCEPT SELECT chunk_id FROM uat_landingzone.qualibot.chunks_v1);

-- 3. Documents présents d'un seul côté, ou avec un nombre de chunks différent
WITH src AS (SELECT IDDOC, COUNT(*) AS n_v1 FROM uat_landingzone.qualibot.chunks_v1 GROUP BY IDDOC),
     cpy AS (SELECT IDDOC, COUNT(*) AS n_copie FROM uat_proj.qualibot.chunks GROUP BY IDDOC)
SELECT COALESCE(s.IDDOC, c.IDDOC) AS IDDOC, s.n_v1, c.n_copie,
       CASE WHEN c.IDDOC IS NULL THEN 'absent de la copie'
            WHEN s.IDDOC IS NULL THEN 'absent de v1'
            ELSE 'nombre différent' END AS issue
FROM src s
FULL OUTER JOIN cpy c ON s.IDDOC = c.IDDOC
WHERE s.IDDOC IS NULL OR c.IDDOC IS NULL OR s.n_v1 <> c.n_copie
ORDER BY issue, IDDOC;

-- 4. Schéma : colonnes absentes ou de type différent
WITH a AS (SELECT column_name, data_type FROM uat_landingzone.information_schema.columns
           WHERE table_schema = 'qualibot' AND table_name = 'chunks_v1'),
     b AS (SELECT column_name, data_type FROM uat_proj.information_schema.columns
           WHERE table_schema = 'qualibot' AND table_name = 'chunks')
SELECT COALESCE(a.column_name, b.column_name) AS column_name, a.data_type AS type_v1, b.data_type AS type_copie
FROM a FULL OUTER JOIN b ON a.column_name = b.column_name
WHERE a.column_name IS NULL OR b.column_name IS NULL OR a.data_type <> b.data_type;

-- 5. Dernière écriture de chaque table (si v1 a bougé après le clone, les écarts sont attendus)
DESCRIBE HISTORY uat_landingzone.qualibot.chunks_v1 LIMIT 1;
DESCRIBE HISTORY uat_proj.qualibot.chunks LIMIT 1;
```

Si une requête renvoie `PERMISSION_DENIED`, elle doit passer par le SP UAT (même mécanisme que `runbooks/scripts/grants/`), pas par le compte personnel.

- [ ] **J0e. Écarts de `uat_proj.qualibot.chunks` (analyse du 2026-10-19)**. La copie n'est plus un clone de v1 : le 18/10, la reprise du run du 16/10 a reconstruit la table (106 012 lignes) puis `4_describe_images` y a fusionné 40 200 passages d'images (MERGE sur `chunk_id` + colonne `anchor_chunk_index`). Constats : 305 REF avec plus d'IDDOC que v1 ; 184 REF avec des passages identiques sous plusieurs IDDOC (3 607 groupes, 3 726 copies en trop) ; 104 documents avec moins de chunks (1 618 chunks nets), dont 19 tronqués ; 1 REF seulement dans v1. Diagnostic (SQL UAT, lecture seule) :

```sql
-- 5. Chunks de la copie : dans le manifeste ou non, par type
SELECT CASE WHEN m.IDDOC IS NULL THEN 'IDDOC hors manifeste' ELSE 'IDDOC dans le manifeste' END AS categorie,
       c.chunk_content_type, COUNT(DISTINCT c.IDDOC) AS iddocs, COUNT(*) AS chunks
FROM uat_proj.qualibot.chunks c
LEFT JOIN (SELECT DISTINCT IDDOC FROM uat_proj.qualibot.parse_manifest) m ON c.IDDOC = m.IDDOC
GROUP BY 1, 2 ORDER BY 1, chunks DESC;

-- 6. Chunks d'un seul côté, par type
SELECT 'absents de la copie' AS sens, v.chunk_content_type, COUNT(*) AS chunks
FROM uat_landingzone.qualibot.chunks_v1 v LEFT ANTI JOIN uat_proj.qualibot.chunks c ON v.chunk_id = c.chunk_id
GROUP BY v.chunk_content_type
UNION ALL
SELECT 'absents de v1', c.chunk_content_type, COUNT(*)
FROM uat_proj.qualibot.chunks c LEFT ANTI JOIN uat_landingzone.qualibot.chunks_v1 v ON c.chunk_id = v.chunk_id
GROUP BY c.chunk_content_type
ORDER BY 1, 3 DESC;

-- 7. Images de documents hors manifeste
SELECT COUNT(DISTINCT i.IDDOC) AS iddocs_hors_manifeste, COUNT(*) AS images_hors_manifeste
FROM uat_proj.qualibot.image_metadata i
LEFT ANTI JOIN (SELECT DISTINCT IDDOC FROM uat_proj.qualibot.parse_manifest) m ON i.IDDOC = m.IDDOC;

-- 8. Passages en double, par type
SELECT c.chunk_content_type, COUNT(DISTINCT c.ref) AS refs, COUNT(*) AS passages
FROM uat_proj.qualibot.chunks c
JOIN (SELECT ref, body_sha256 FROM uat_proj.qualibot.chunks
      WHERE body_sha256 IS NOT NULL GROUP BY ref, body_sha256 HAVING COUNT(DISTINCT IDDOC) > 1) d
  ON c.ref = d.ref AND c.body_sha256 = d.body_sha256
GROUP BY c.chunk_content_type ORDER BY passages DESC;

-- 9. Documents avec moins de chunks que dans v1 (stratégie de parsing)
WITH v AS (SELECT IDDOC, ANY_VALUE(ref) AS ref, COUNT(*) AS n_v1 FROM uat_landingzone.qualibot.chunks_v1 GROUP BY IDDOC),
     c AS (SELECT IDDOC, COUNT(*) AS n_copie FROM uat_proj.qualibot.chunks GROUP BY IDDOC)
SELECT v.IDDOC, v.ref, v.n_v1, c.n_copie, v.n_v1 - c.n_copie AS perdus, pd.parse_status, pd.parser_strategy
FROM v JOIN c ON v.IDDOC = c.IDDOC
LEFT JOIN uat_proj.qualibot.parsed_documents pd ON pd.IDDOC = v.IDDOC
WHERE c.n_copie < v.n_v1
ORDER BY perdus DESC LIMIT 50;
```

Aucune suppression avant lecture des résultats ; une correction éventuelle passe par un job SP, puis une resynchronisation de `chunks_index`.

Résultat (2026-10-19) : 315 IDDOC hors manifeste portent des chunks (10 885 chunks, surtout du texte) et 41 IDDOC hors manifeste ont des lignes dans `image_metadata`. Cause : en mode `full`, `build_processed_files_and_chunks` et `build_image_metadata` ne filtrent pas sur le périmètre (le filtre n'existe que pour `incremental` et `rechunk`), donc la reconstruction reprend tout le checkpoint, révisions remplacées comprises. Correction : `parsing_run_mode: "rechunk"` pour `qualibot-uat` (purge des IDDOC sortis du périmètre à `2_manifest`, puis reconstruction filtrée), `deploy-uat-jobs`, run de `D_1_Qualibot_Parsing_Pipeline` (J5). Après le run, rejouer les requêtes 5 et 3 : zéro IDDOC hors manifeste. Puis J7 : retour à `incremental`.

```sql
-- 10. Documents avec moins de chunks, avec appartenance au manifeste
WITH v AS (SELECT IDDOC, ANY_VALUE(ref) AS ref, COUNT(*) AS n_v1 FROM uat_landingzone.qualibot.chunks_v1 GROUP BY IDDOC),
     c AS (SELECT IDDOC, COUNT(*) AS n_copie FROM uat_proj.qualibot.chunks GROUP BY IDDOC)
SELECT v.IDDOC, v.ref, v.n_v1, c.n_copie, v.n_v1 - c.n_copie AS perdus,
       m.IDDOC IS NOT NULL AS dans_manifeste, pd.parser_strategy
FROM v JOIN c ON v.IDDOC = c.IDDOC
LEFT JOIN (SELECT DISTINCT IDDOC FROM uat_proj.qualibot.parse_manifest) m ON m.IDDOC = v.IDDOC
LEFT JOIN uat_proj.qualibot.parsed_documents pd ON pd.IDDOC = v.IDDOC
WHERE c.n_copie < v.n_v1
ORDER BY dans_manifeste DESC, perdus DESC LIMIT 50;

-- 11. Un document qui a perdu des chunks : contenu v1 contre copie, et texte du checkpoint
SELECT 'v1' AS src, chunk_content_type, COUNT(*) AS chunks, SUM(LENGTH(chunk_text)) AS caracteres
FROM uat_landingzone.qualibot.chunks_v1 WHERE IDDOC = 25372 GROUP BY chunk_content_type
UNION ALL
SELECT 'copie', chunk_content_type, COUNT(*), SUM(LENGTH(chunk_text))
FROM uat_proj.qualibot.chunks WHERE IDDOC = 25372 GROUP BY chunk_content_type
ORDER BY 1, 2;

SELECT IDDOC, parser_strategy, parse_status, LENGTH(document_text) AS caracteres, image_count
FROM uat_proj.qualibot.parsing_checkpoint WHERE IDDOC IN (25372, 19226, 21324, 25767);
```

- [ ] **J0b. Plan lecture seule UAT** : Bitbucket, pipeline `plan-uat`. Attendu : aucun `delete` ni `recreate` sur le schéma, les volumes ou l'app. Les `create` de nouveaux jobs sont normaux. Les `delete` de jobs (`apps_stop_nightly_uat`, `quality_scoring`, `score_production_qa_uat`, `sync_mlflow_scorer_assessments_uat`, `traces_migration`) sont à valider un par un dans l'UI avant J3.

- [ ] **J0b'. Si `plan-uat` annonce des `create` pour des objets qui existent déjà** (2026-10-19 : `apps.doc-compare`, `postgres_projects.qualibot-history`, `schemas.qualibot_proj_schema`, `volumes.qualibot_doc_compare`, `volumes.qualibot_images`) : l'état de la nouvelle racine ne les connaît pas. Ne pas déployer. Le schéma, les deux volumes et l'app ont été rattachés le 2026-10-19 (pipeline `bind-uat-resources`, depuis retiré). Reste le projet Lakebase : pipeline `bind-uat-lakebase` (SP UAT, identifiant `projects/qualibot`, état seulement). Puis `plan-uat`. Les `permissions` de l'app et du projet restent annoncées en `create` : elles remplacent l'ACL existant par celle de `databricks.yml`. Avant le déploiement, comparer l'ACL réel de l'app (`databricks apps get-permissions qualibot --profile UAT`) à la liste déclarée. Ne pas relancer `uat-state-fix`.

- [ ] **J0b''. Jobs UAT en double (constaté le 2026-10-19)**. Jeu A : racine `/Workspace/Shared`, créé les 03/09 et 16/10, historique des runs. Jeu B : racine `/Workspace/.bundle`, créé le 19/10 à 09:10 UTC par un déploiement sur la nouvelle racine avant que son état existe ; c'est celui que suit l'état. À utiliser : le jeu B. Job de parsing B : `762813135325407` (mode à vérifier et *Run now* sur celui-là, pas sur `1090642136386557`).

| Job | Jeu A (à retirer) | Jeu B (à garder) |
|---|---|---|
| `D_1_Qualibot_Parsing_Pipeline` | `1090642136386557` | `762813135325407` |
| `D_1_Qualibot_Lakebase_Export_To_Volume` | `779339891964836` | `290867940964606` |
| `D_2_Qualibot_Score_Chat_Traces` | `687448690184801` | `161560065917637` |
| `W_1_Qualibot_Start_Weekend_Uat` | `342815175501077` | `854972118459514` |
| `W_1_Qualibot_Stop_Weekend_Uat` | `783022027143685` | `236940839371957` |
| `Z_2_Qualibot_Create_Chat_Experiment` | `824967477595237` | `990105543498681` |
| `Z_3_Qualibot_Register_Chat_Prompts` | `931830613336591` | `36091234966600` |

Tout de suite : mettre en pause les plannings du jeu A encore `UNPAUSED` (export Lakebase, démarrage et arrêt du week-end). Après la réparation des chunks et sur accord écrit : supprimer les sept jobs du jeu A (`databricks jobs delete <id> --profile UAT`), puis seulement M1. Hors bundle, à décider : `qualibot-sync-user-capabilities-uat` (`851363031959777`, planning actif), probablement obsolète depuis que l'accès se vérifie par appartenance de groupe.

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
     Résultat du 2026-10-17 : 2 339 / 2 318 / 2 273 / 0.
  1b. Où sont passées les 20 h (éditeur SQL UAT, lecture seule), pour choisir les optimisations :

     ```sql
     SELECT source_file_extension, parser_strategy, count(*) AS fichiers,
            round(sum(parse_time_seconds) / 3600, 1) AS heures_parse,
            round(percentile(parse_time_seconds, 0.5), 1) AS p50_s, round(percentile(parse_time_seconds, 0.95), 1) AS p95_s,
            round(max(parse_time_seconds), 0) AS max_s, sum(image_count) AS images,
            round(avg(source_file_size_bytes) / 1048576, 1) AS taille_moy_mo
     FROM uat_proj.qualibot.parsing_checkpoint
     WHERE ingestion_run_id = '87f03f9a-b60f-40a7-a336-63561356d22a'
     GROUP BY 1, 2 ORDER BY heures_parse DESC;
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

- [ ] **S10. Alertes SQL** : déployées avec les jobs (J3), en pause. Activation (`alerts_pause_status` à `UNPAUSED`, cible `qualibot-uat`) sur décision écrite.

## Phase D — nettoyage (plusieurs jours après la validation, accord écrit)

- [ ] **D1. Anciens KA UAT** : UI UAT, Agents, ⋮, *Delete* sur `qualibot_ALL_v2`, `qualibot_AS_v2`, `qualibot_IS_v2`.

- [ ] **D2. Objets `_v1` et anciennes tables Lakebase** : liste à faire à ce moment-là, avec un accord écrit pour chaque suppression. Ne rien lancer avant.
