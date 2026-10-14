# Retest — ce qui reste à rejouer

Liste courante de ce qui doit être relancé ou retesté après les derniers changements de code. Rien n'a encore été exécuté sur Databricks depuis la machine de développement : chaque point est à faire sur **DEV** (`-t dev`), sauf indication contraire.

Statut : `[ ]` à faire ; `[x] (jj/mm/aaaa)` fait et confirmé à la date indiquée.

**Règles** :
- `bundle deploy` ne gère que l'app, le projet Lakebase, les schémas, les volumes, les rôles et les jobs. Il ne touche **ni aux tables ni aux index Vector Search**.
- Avant de relancer `copy_uat_to_dev` (`runbooks/dev.md`, bloc C) : vérifier que les tables UAT ont le chunking du code (section 0.2). Sinon la copie remplace le chunking de DEV.

## 0. Procédure de test sur DEV

PowerShell, profil CLI `DEV`. Si un pas échoue, transmettre l'erreur au développeur et s'arrêter là.

### 0.1 Préparer

```powershell
# dossier vide + zip de la branche feature/chat-vsi-merged-on-impact-search, puis :
Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
databricks current-user me --profile DEV
```

- [ ] 0.1 fait

### 0.2 Où en est-on ? (lecture seule, rien n'est modifié)

Copier tout le bloc PowerShell, puis le bloc SQL, et transmettre les deux sorties.

```powershell
"=== app"
(databricks apps get qualibot --profile DEV -o json | ConvertFrom-Json) | Select-Object name, url, service_principal_client_id, @{n='compute';e={$_.compute_status.state}}, @{n='app';e={$_.app_status.state}} | Format-List
"=== projets Lakebase"
databricks postgres list-projects --profile DEV
"=== volumes dev_proj.qualibot"
databricks volumes list dev_proj qualibot --profile DEV
"=== modeles Docling"
databricks fs ls dbfs:/Volumes/dev_proj/qualibot/docling_models/docling_models --profile DEV
"=== schema projet"
databricks schemas get dev_proj.qualibot --profile DEV
"=== endpoint Vector Search"
databricks vector-search-endpoints list-endpoints --profile DEV
"=== index et table source"
foreach ($i in (databricks vector-search-indexes list-indexes qualibot --profile DEV -o json | ConvertFrom-Json)) {
  (databricks vector-search-indexes get-index $i.name --profile DEV -o json | ConvertFrom-Json) |
    Select-Object name, @{n='source';e={$_.delta_sync_index_spec.source_table}}, @{n='modele';e={$_.delta_sync_index_spec.embedding_source_columns[0].embedding_model_endpoint_name}}, @{n='pret';e={$_.status.ready}}, @{n='lignes';e={$_.status.indexed_row_count}} | Format-List
}
"=== jobs"
databricks jobs list --profile DEV -o json | ConvertFrom-Json | Where-Object { $_.settings.name -match 'Qualibot|qualibot' } | ForEach-Object { "$($_.job_id)  $($_.settings.name)" }
"=== plan du bundle (aucun changement appliqué)"
python utils/deploy/render_target_config_env.py dev target_config.env
databricks bundle validate -t dev --profile DEV
databricks bundle plan -t dev --profile DEV 2>&1 | Select-String -Pattern "^(update|create|delete|recreate)|^Plan:"
```

```sql
-- éditeur SQL DEV
SHOW TABLES IN dev_proj.qualibot;

-- la table qui nourrit chunks_index : le chunking est-il celui du code (150/300/450 jetons, 1 600 caractères) ?
SELECT count(*) AS passages, count(DISTINCT IDDOC) AS documents,
       round(avg(chunk_token_count)) AS jetons_moyens, max(chunk_token_count) AS jetons_max, max(length(chunk_text)) AS caracteres_max
FROM dev_proj.qualibot.chunks;
SHOW TBLPROPERTIES dev_proj.qualibot.chunks;   -- delta.enableChangeDataFeed doit valoir true

-- l'état du pipeline : s'il manque, un run complet de 3_parse reparserait tout le corpus sur GPU
SELECT 'parsing_checkpoint' AS t, count(*) AS n FROM dev_proj.qualibot.parsing_checkpoint
UNION ALL SELECT 'parsed_documents', count(*) FROM dev_proj.qualibot.parsed_documents
UNION ALL SELECT 'image_metadata', count(*) FROM dev_proj.qualibot.image_metadata
UNION ALL SELECT 'parse_manifest', count(*) FROM dev_proj.qualibot.parse_manifest
UNION ALL SELECT 'category_reference', count(*) FROM dev_proj.qualibot.category_reference;
-- TABLE_OR_VIEW_NOT_FOUND : cette table n'existe pas, retirer sa ligne et la noter.
```

**Lecture du résultat et conduite à tenir :**

| Constat | Ce qu'il faut faire |
|---|---|
| `bundle plan` ne montre que des `update` / `create`, aucun `delete` ni `recreate` | continuer en 0.3 |
| `bundle plan` montre un `delete` ou un `recreate` | **ne pas déployer**, transmettre le plan au développeur |
| App, projet Lakebase ou volumes absents | `bundle deploy` (0.3) les recrée ; le projet Lakebase repart vide (l'app recrée ses tables au démarrage, l'historique DEV est perdu) |
| App ou volume existe mais le déploiement dit « already exists » | rattacher la ressource avant de redéployer : `databricks bundle deployment bind doc-compare qualibot -t dev --profile DEV` (app), `databricks bundle deployment bind qualibot_images dev_proj.qualibot.images -t dev --profile DEV` (volume ; même forme pour `qualibot_doc_compare`, `qualibot_test`, `qualibot_staging`) |
| Le dossier des modèles Docling est vide ou absent | aucun job de parsing ne peut tourner : signaler (ce volume n'est pas dans le bundle en DEV, voir `runbooks/dev.md` 00.2a) |
| Endpoint `qualibot` absent | à recréer avant tout index : signaler |
| `chunks_index` absent, `pret = False`, ou sa `source` n'est pas `dev_proj.qualibot.chunks` | transmettre la sortie : la commande de (re)création sur **votre** table est dans `runbooks/dev.md` 0.3 (pas de copie depuis l'UAT) |
| `jetons_max` ≈ 450 et `caracteres_max` ≈ 1 600 (+ préfixe `[Source: …]`) | le chunking en base est celui du code actuel |
| `jetons_max` vers 1 000 ou plus | `chunks` a l'ancien chunking : signaler la table qui contient le bon chunking |
| `parsing_checkpoint`, `parsed_documents` ou `image_metadata` absentes ou vides | **ne pas lancer `3_parse` sans filtre** : utiliser seulement le test filtré `_test` de 0.5 |

- [ ] 0.2 fait et lu

### 0.3 Déployer les jobs et ressources

```powershell
databricks bundle deploy -t dev --profile DEV
databricks bundle summary -t dev --profile DEV
```

- [ ] 0.3 fait

### 0.4 Tester chaque job, commandes exactes

`bundle run` attend la fin et affiche `SUCCESS` ou l'erreur. Lancer dans cet ordre :

```powershell
# 1. droits du SP de l'app et du SP des jobs (dont chunks_index); une ligne OK: par droit
databricks bundle run grant_app_access_dev -t dev --profile DEV

# 2. migrations du schéma Lakebase (idempotentes), depuis le poste
python runbooks/scripts/lakebase/migrate_lakebase.py --env DEV

# 3. export de la base Lakebase DEV vers le volume staging
databricks bundle run lakebase_export_dev_to_volume -t dev --profile DEV

# 4. import des tables de chat de l'UAT dans dev_proj.qualibot (chat_*, feedbacks...); n'écrit pas dans chunks
databricks bundle run lakebase_import_uat_to_dev -t dev --profile DEV

# 5. notation : estimation seulement, aucun juge appelé
databricks bundle run score_chat_traces_dev -t dev --profile DEV

# 6. arrêt puis démarrage de l'app (le job d'arrêt de nuit et celui de reprise du week-end)
databricks bundle run apps_stop_nightly_dev -t dev --profile DEV
(databricks apps get qualibot --profile DEV -o json | ConvertFrom-Json).compute_status.state     # attendu : STOPPED
databricks bundle run qualibot_start_weekend_dev -t dev --profile DEV
(databricks apps get qualibot --profile DEV -o json | ConvertFrom-Json).compute_status.state     # attendu : ACTIVE
```

Si un job échoue, l'erreur est dans l'UI Jobs DEV, onglet **Runs**, cellule en rouge : chercher `NameError`, `ModuleNotFoundError`, `No module named 'ops_config'`.

- [ ] 0.4 étape 1 (`grant_app_access_dev`) : une ligne `OK:` par droit
- [ ] 0.4 étape 2 (`migrate_lakebase.py --env DEV`)
- [ ] 0.4 étape 3 (`lakebase_export_dev_to_volume`)
- [ ] 0.4 étape 4 (`lakebase_import_uat_to_dev`)
- [ ] 0.4 étape 5 (`score_chat_traces_dev`, estimation) : lignes par juge lues
- [ ] 0.4 étape 6 (arrêt et démarrage de l'app : `STOPPED` puis `ACTIVE`)

### 0.5 Pipeline de parsing

Tâche par tâche, sur les vraies tables. Seulement si l'état du pipeline existe (voir 0.2). Le bloc R de `runbooks/dev.md` (colonnes renommées) doit être passé **avant** ce run.

```powershell
databricks bundle run parsing_pipeline -t dev --profile DEV --only 1_categories
databricks bundle run parsing_pipeline -t dev --profile DEV --only 2_manifest
databricks bundle run parsing_pipeline -t dev --profile DEV --only 3_parse
databricks bundle run parsing_pipeline -t dev --profile DEV --only 4_describe_images
databricks bundle run parsing_pipeline -t dev --profile DEV --only 5_sync_index
databricks bundle run parsing_pipeline -t dev --profile DEV --only 6_update_kb_metadata
```

Attendu (tables à jour) : `3_parse` journalise `[SCOPE] ... to scan=0` et `[GHOST GUARD] All SUCCESS docs have chunks - OK.` ; `5_sync_index` affiche `sync triggered` ; `6_update_kb_metadata` affiche `doc_catalog N -> N documents`.

Test de parsing réel, **sans toucher aux tables de DEV** (suffixe `_test`). Choisir 2 ou 3 IDDOC, de préférence des documents qui ont des images (c'est ce qui exerce le placement des images de `4_describe_images`) :

```sql
SELECT m.IDDOC, m.ref, count(i.image_id) AS images
FROM dev_proj.qualibot.parse_manifest m
LEFT JOIN dev_proj.qualibot.image_metadata i ON i.IDDOC = m.IDDOC
WHERE m.parse_content
GROUP BY m.IDDOC, m.ref
HAVING count(i.image_id) BETWEEN 1 AND 15
ORDER BY images DESC LIMIT 3;
```

```powershell
$ids = "<IDDOC1>,<IDDOC2>"
databricks bundle deploy -t dev --profile DEV --var="parsing_table_suffix=_test" --var="parsing_parse_filter=$ids" --var="parsing_run_mode=full"
databricks bundle run parsing_pipeline -t dev --profile DEV --only 1_categories
databricks bundle run parsing_pipeline -t dev --profile DEV --only 2_manifest
databricks bundle run parsing_pipeline -t dev --profile DEV --only 3_parse
databricks bundle run parsing_pipeline -t dev --profile DEV --only 4_describe_images
```

```sql
SELECT parse_status, count(*) FROM dev_proj.qualibot.parsed_documents_test GROUP BY 1;
SELECT IDDOC, count(*) AS passages, max(chunk_token_count) AS jetons_max FROM dev_proj.qualibot.chunks_test GROUP BY 1;
```

Attendu : `SUCCESS` pour les IDDOC choisis, des passages avec `jetons_max` ≈ 450. Puis **remettre la configuration normale et nettoyer** :

```powershell
databricks bundle deploy -t dev --profile DEV
```

```sql
DROP TABLE IF EXISTS dev_proj.qualibot.chunks_test;
DROP TABLE IF EXISTS dev_proj.qualibot.parsed_documents_test;
DROP TABLE IF EXISTS dev_proj.qualibot.image_metadata_test;
DROP TABLE IF EXISTS dev_proj.qualibot.parsing_checkpoint_test;
DROP TABLE IF EXISTS dev_proj.qualibot.parse_manifest_test;
DROP TABLE IF EXISTS dev_proj.qualibot.category_reference_test;
```

Si `SHOW TABLES IN dev_proj.qualibot LIKE '*_test'` en montre d'autres, les supprimer aussi.

- [ ] 0.5 `1_categories` à `6_update_kb_metadata` lancés un par un, sans erreur
- [ ] 0.5 test filtré `_test` : `SUCCESS` pour les IDDOC, `jetons_max` ≈ 450, tables `_test` supprimées

### 0.6 Déployer l'app

Reconstruit le client, ce qui régénère `client/bun.lock` et `client/out/` :

```powershell
.\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
git status      # client/bun.lock et client/out/ ont changé : les versionner avec le code
databricks apps logs qualibot --profile DEV
```

Dans les logs : `Lakebase schema ready`, `Lakebase ready`, `doc_catalog: loaded N documents from Lakebase`, aucune stack trace.

- [ ] 0.6 fait, logs relus

### 0.7 Tester l'app (URL de `databricks apps get`)

- **Chat** : une question en ALL, une en AS, une en IS ; une hors sujet (refus) ; une en espagnol ; les documents cités sont cliquables.
- **Compare** : deux révisions d'un même document ; Change Summary et Change Table ; 👍 puis commentaire sur chacune des deux cartes ; export Excel et PDF ; résumé d'un seul document ; **Judge Impacted Docs** et son vote par document ; History (recharger une comparaison) ; lien « Share » d'un chat ouvert en navigation privée.

- [ ] 0.7 fait

### 0.8 Transmettre le résultat

La sortie de 0.2 (PowerShell et SQL), puis l'erreur de chaque pas qui échoue, ou « tout est vert ». Cocher alors les sections 1 à 7 et mettre `runbooks/dev.md` à jour (daté).

## 1. Contrôles de code et de configuration

- [ ] `python -m pytest -q` (depuis la racine du dépôt)
- [ ] `databricks bundle validate -t dev --profile DEV`, puis `databricks bundle validate -t qualibot-uat-test --profile UAT`, puis `databricks bundle validate -t qualibot-prod --profile qualibot-prod` : aucun chemin de notebook introuvable, aucune variable inconnue
- [ ] `databricks bundle plan -t dev --profile DEV` avant le déploiement : aucune ressource en `delete` ni `recreate` (les clés de ressources ne changent pas)
- [ ] `python runbooks/scripts/compare/compare_preview.py <a.docx> <b.docx>` une fois : les imports de `runbooks/scripts/` sont bons
- [ ] Après `bundle deploy -t dev` : les noms des jobs suivent `<D|W|Z>_<niveau>_<Domaine>_<Rôle>` :

  ```powershell
  databricks jobs list --profile DEV -o json | ConvertFrom-Json | Where-Object { $_.settings.name -match 'Qualibot' } | ForEach-Object { $_.settings.name }
  ```

## 2. Pipeline de parsing (notebooks renommés, garde-fous, images)

Les notebooks sont `create_proj_<asset>.py` et `update_proj_vector_indexes.py` dans `utils/parsing_pipeline/` ; les noms de tâches du job (`1_categories`…`6_update_kb_metadata`) ne changent pas.

- [ ] Lancer le job complet une fois sur DEV (`1_categories` à `6_update_kb_metadata`) et lire les journaux de chaque tâche : aucune `NameError` sur `logger`.
- [ ] `--only 1_categories` : `All sources fresh`, `category_reference` écrite.
- [ ] `--only 2_manifest` : les compteurs `KEEP/DELETE/ORPHAN/...`, `parse_manifest written: N IDDOCs in scope`, `[DRY RUN] ... would be deleted`. Comparer N avec la valeur d'hier (`SELECT count(*) FROM dev_proj.qualibot.parse_manifest`). Garde-fou RED : la tâche échoue si `parse_manifest` est vide ou a un IDDOC en double.
- [ ] `--only 5_sync_index` : garde-fou RED : la tâche échoue si `chunks_full` est vide (au premier run, vérifier).
- [ ] `--only 4_describe_images` sur des tables à jour : `Nothing to describe`, `No new image chunks to inject`, ou la promotion des documents en attente.
- [ ] Statuts `IMAGES_PENDING`, plafond de tentatives, disjoncteur : `runbooks/dev.md` Q3 à Q5 (requêtes de contrôle).
- [ ] Modèles de vision : `runbooks/dev.md` Q1 (`utils/evaluation/check_vision_endpoints.py`).
- [ ] `5_sync_index` et `6_update_kb_metadata` sont serverless : vérifier qu'ils n'importent que la bibliothèque standard pour le logger.
- [ ] `--only 5_sync_index` puis `--only 6_update_kb_metadata` : `sync triggered`, `doc_catalog N -> N documents`.

## 3. Client

- [ ] `cd client`, puis `bun install` (met `client/bun.lock` à jour), puis `bun run build`.
- [ ] Versionner le `client/out/` régénéré.
- [ ] Ouvrir l'app DEV : chat, comparaison, historique, chat partagé.

## 4. Journal Lakebase des tours et des requêtes

- [ ] App DEV redéployée, puis `runbooks/dev.md` 0.13 (requêtes `chat_turns`, `chat_retrieved_chunks`, `errors`) : une ligne `chat_turns` par question, passages dans `chat_retrieved_chunks`.
- [ ] Une comparaison + « Judge Impacted Docs », puis dans l'éditeur SQL Lakebase (base `doccompare`) :

  ```sql
  SELECT created_at, status, search_ms, judge_ms FROM impact_requests ORDER BY id DESC LIMIT 1;
  SELECT created_at, status, total_ms FROM llm_requests ORDER BY id DESC LIMIT 1;
  ```

  Attendu : `impact_requests.status = 'ok'` avec `search_ms` et `judge_ms` remplis ; `llm_requests` avec `status` et `total_ms`.

## 5. Notation des traces et MLflow du chat

- [ ] `runbooks/dev.md` 0.8 à 0.14 (expérience, prompts, traces, notation). Après chaque changement de `server/prompts/chat/` : `register_chat_prompts_dev` (`runbooks/dev.md` 0.9).
- [ ] Prompts `system/{base,all,as,is}.md` : poser une question en AS, IS et ALL, puis vérifier que `instructions_sha` a changé et que les réponses restent conformes :

  ```sql
  SELECT trace_id, division, instructions_sha, answer_endpoint, status FROM chat_turns ORDER BY id DESC LIMIT 6;
  ```

## 6. Lakebase : migrations

- [ ] Après l'ajout d'une colonne ou d'une table dans `runbooks/scripts/lakebase/migrations.py` : `python runbooks/scripts/lakebase/migrate_lakebase.py --env DEV`, puis relancer l'app.

## 7. Colonnes booléennes renommées

- [ ] Bloc R de `runbooks/dev.md` : SQL de renommage de `parsed_documents` (`is_in_rag`, `is_filtered_by_date`, `is_chunks_truncated`) **avant** le déploiement du code ou le premier run de parsing.

## 8. Avant le premier déploiement UAT / PROD (sur accord)

Au prochain `bundle deploy -t qualibot-uat`, Databricks supprime les jobs qui ne sont plus déclarés (jobs de l'ancien assistant et de la notation associée, job de migration de test, job de setup de schéma, job de synchronisation des droits). Ne pas déployer l'UAT pour cela seul.

- [ ] Le job de parsing est renommé de `D_1_qualibot-parsing-pipeline-<cible>` en `D_1_Qualibot_Parsing_Pipeline_<cible>` (comme les jobs d'arrêt, de reprise et d'export). Vérifier qu'aucun tableau de bord, alerte SQL ou règle de notification ne repère un job par son ancien nom.
- [ ] Le job `qualibot-sync-user-capabilities-uat` (hors bundle) se supprime : `runbooks/uat.md` étape 0.8.
- [ ] Les alertes SQL (`utils/alerts/`) se créent à la main : `runbooks/uat.md` D5.8d.
- [ ] Plannings des jobs d'arrêt et de reprise de l'app : fuseau `Europe/Paris` (`resources/app_schedules.yml`), à conserver.
