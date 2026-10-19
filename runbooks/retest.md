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
| `parsing_checkpoint`, `parsed_documents` ou `image_metadata` absentes ou vides | **ne pas lancer `3_parse` sans filtre** : le test filtré n'existe plus, ne pas lancer `3_parse` sur des tables vides |

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
databricks bundle run lakebase_export -t dev --profile DEV

# 4. notation : estimation seulement, aucun juge appelé
databricks bundle run score_chat_traces_dev -t dev --profile DEV

# 5. arrêt puis démarrage de l'app (le job d'arrêt de nuit et celui de reprise du week-end)
databricks bundle run apps_stop_nightly_dev -t dev --profile DEV
(databricks apps get qualibot --profile DEV -o json | ConvertFrom-Json).compute_status.state     # attendu : STOPPED
databricks bundle run qualibot_start_weekend_dev -t dev --profile DEV
(databricks apps get qualibot --profile DEV -o json | ConvertFrom-Json).compute_status.state     # attendu : ACTIVE
```

Si un job échoue, l'erreur est dans l'UI Jobs DEV, onglet **Runs**, cellule en rouge : chercher `NameError`, `ModuleNotFoundError`, `No module named 'ops_config'`.

- [ ] 0.4 étape 1 (`grant_app_access_dev`) : une ligne `OK:` par droit
- [ ] 0.4 étape 2 (`migrate_lakebase.py --env DEV`)
- [ ] 0.4 étape 3 (`lakebase_export`)
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

Le suffixe de tables `_test` n'existe plus : il n'y a pas de test de parsing filtré. Les tests de parsing se font sur les tables de DEV, dans la copie complète.

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
- [ ] `databricks bundle validate -t dev --profile DEV`, puis `databricks bundle validate -t qualibot-prod --profile qualibot-prod` : aucun chemin de notebook introuvable, aucune variable inconnue
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
- [ ] `6_update_kb_metadata` est serverless (pas de GX) : vérifier qu'il n'importe que la bibliothèque standard pour le logger.
- [ ] `--only 5_sync_index` puis `--only 6_update_kb_metadata` : `sync triggered`, `doc_catalog N -> N documents`.

## 3. Client

- [ ] `cd client`, puis `bun install` (met `client/bun.lock` à jour), puis `bun run build`.
- [ ] Versionner le `client/out/` régénéré.
- [ ] Ouvrir l'app DEV : chat, comparaison, historique, chat partagé.

## 4. Journal Lakebase des tours et des requêtes

- [ ] App DEV redéployée, puis `runbooks/dev.md` 0.13 (requêtes `chat_turns`, `chat_retrieved_chunks`, `app_errors`) : une ligne `chat_turns` par question, passages dans `chat_retrieved_chunks`.
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

- [ ] Le job de parsing s'appelle `D_1_Qualibot_Parsing_Pipeline` (sans suffixe de cible) : après le déploiement, `databricks jobs list --profile DEV -o json | ConvertFrom-Json | Where-Object { $_.settings.name -like '*Qualibot*' } | ForEach-Object { $_.settings.name }` ne montre aucun nom qui finit par `_dev`, `_uat` ou `_prod`.
- [ ] Le job `qualibot-sync-user-capabilities-uat` (hors bundle) se supprime : `runbooks/uat.md` étape 0.8.
- [ ] Les alertes SQL viennent du bundle (`resources/alerts.yml`), en pause : voir `runbooks/uat.md` D5.8d.
- [ ] Plannings des jobs d'arrêt et de reprise de l'app : fuseau `Europe/Brussels` (`resources/app_schedules.yml`), à conserver.

## 2026-10-17 — parsing découpé en 3_parse (CPU) / 3b_parse_pdf (GPU) / 3c_chunks (CPU)

Notebooks : `create_proj_parsing_checkpoint.py` (nouveau, `3_parse`), `create_proj_parsing_checkpoint_gpu.py` (nouveau,
`3b_parse_pdf`), `create_proj_chunks.py` (devient `3c_chunks`). Nouvelles tables : `parsing_run_scope`, `parsing_gpu_queue`.
À relancer : un run complet du job de parsing ; vérifier dans le log de `3_parse` les lignes `Packages`, `[RESUME]`,
`[BUDGET]`, `[ROUTE]`, puis que `3b_parse_pdf` est « Excluded » et `3c_chunks` réussi.

## 2026-10-19 — parsing : modules fusionnés (19 → 15), `utils.py` devient `worker.py`

Notebooks : aucun changement de logique, seuls leurs imports changent (`quality` → `tables`, `utils` → `worker`, `doc_status` → `config`). `config.WORKER_MODULES` ne contient plus `image_store.py` et `utils.py` : il est lu par `3_parse` et `4_describe_images` (`addPyFile`).

- [ ] Déployer les jobs et notebooks sur DEV (pipeline Bitbucket `deploy-dev-jobs`, app non touchée).
- [ ] `databricks bundle run parsing_pipeline -t dev --profile DEV --only 1_categories` : les imports de `tables` et de `config` passent, les contrôles GX de `1_categories` tournent.
- [ ] `databricks bundle run parsing_pipeline -t dev --profile DEV --only 3_parse` (run incrémental) : le job se termine sans `ModuleNotFoundError` ni `No module named 'worker'` sur les exécuteurs.
- [ ] `databricks bundle run parsing_pipeline -t dev --profile DEV --only 4_describe_images` : `worker.py` et `image_fallbacks.py` sont envoyés, la description des images tourne.


## 2026-10-19 — Lakebase DEV dans dev_proj.qualibot
- Nouveau job manuel `Z_1_Qualibot_Rename_Dev_Uat_Copies` (cible `dev`, `utils/lakebase_sync/rename_dev_uat_copies_job.py`) : renomme les 4 copies UAT en `uat_<nom>`. Une fois, puis le retirer de `resources/lakebase_sync.yml`.
- DEV : `lakebase_import_to_delta` = `true`, `lakebase_tables_to_skip` (liste dans `databricks.yml`). L'export charge les autres tables dans `dev_proj.qualibot`.
- À relancer : `runbooks/dev.md` bloc L (L.1 à L.4). Puis `pytest tests/test_lakebase_sync_config.py tests/test_deploy_config.py tests/test_bundle_paths.py`.
