# Retest après le nettoyage du code (démarré le 2026-10-08)

Chantier : moins de commentaires, plus de code mort, notebooks de parsing découpés, `utils/` rangé.
Rien n'a pu être exécuté sur Databricks depuis la machine de développement : tout ce qui suit
est à rejouer sur **DEV** (`-t dev`) après un `bundle deploy`. Cocher et dater au fur et à mesure.

## 0. Procédure de test sur DEV (version corrigée)

PowerShell, profil CLI `DEV`. Si un pas échoue, collez-moi l'erreur et arrêtez-vous là.

**Deux interdits**, parce que votre DEV a un chunking et des index plus récents que ceux de l'UAT :
- ne **jamais** lancer `copy_uat_to_dev` ni les blocs **C**, **E** et **S** d'`operations_dev.md` : ils recopient le chunking de l'UAT par-dessus le vôtre;
- `bundle deploy` ne touche **ni aux tables ni aux index Vector Search** (ils ne sont pas dans le bundle) : il ne peut pas les écraser. Il ne gère que l'app, le projet Lakebase, les schemas, les volumes, les rôles et les jobs.

### 0.1 Préparer

```powershell
# dossier vide + zip de la branche feature/chat-vsi-merged-on-impact-search, puis :
Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
databricks current-user me --profile DEV
```

### 0.2 Où en est-on ? (lecture seule, rien n'est modifié)

Copiez tout le bloc PowerShell, puis le bloc SQL, et renvoyez-moi les deux sorties.

```powershell
"=== app"
(databricks apps get qualibot --profile DEV -o json | ConvertFrom-Json) | Select-Object name, url, service_principal_client_id, @{n='compute';e={$_.compute_status.state}}, @{n='app';e={$_.app_status.state}} | Format-List
"=== projets Lakebase"
databricks postgres list-projects --profile DEV
"=== volumes dev_landingzone.qualibot"
databricks volumes list dev_landingzone qualibot --profile DEV
"=== modeles Docling"
databricks fs ls dbfs:/Volumes/dev_landingzone/qualibot/docling_models/docling_models --profile DEV
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
SHOW TABLES IN dev_landingzone.qualibot;

-- la table qui nourrit chunks_index : le chunking est-il celui du code (150/300/450 jetons, 1 600 caractères) ?
SELECT count(*) AS passages, count(DISTINCT IDDOC) AS documents,
       round(avg(chunk_token_count)) AS jetons_moyens, max(chunk_token_count) AS jetons_max, max(length(chunk_text)) AS caracteres_max
FROM dev_landingzone.qualibot.chunks;
SHOW TBLPROPERTIES dev_landingzone.qualibot.chunks;   -- delta.enableChangeDataFeed doit valoir true

-- l'état du pipeline : s'il manque, un run complet de 3_parse reparserait tout le corpus sur GPU
SELECT '_pipeline_checkpoint' AS t, count(*) AS n FROM dev_landingzone.qualibot._pipeline_checkpoint
UNION ALL SELECT 'processed_files', count(*) FROM dev_landingzone.qualibot.processed_files
UNION ALL SELECT 'image_metadata', count(*) FROM dev_landingzone.qualibot.image_metadata
UNION ALL SELECT 'parse_manifest', count(*) FROM dev_landingzone.qualibot.parse_manifest
UNION ALL SELECT 'category_reference', count(*) FROM dev_landingzone.qualibot.category_reference;
-- TABLE_OR_VIEW_NOT_FOUND : cette table n'existe plus, retirez sa ligne et notez-la.
```

**Lecture du résultat et conduite à tenir :**

| Constat | Ce qu'il faut faire |
|---|---|
| `bundle plan` ne montre que des `update` / `create`, aucun `delete` ni `recreate` | continuer en 0.3 |
| `bundle plan` montre un `delete` ou un `recreate` | **ne pas déployer**, me coller le plan |
| App, projet Lakebase ou volumes absents | `bundle deploy` (0.3) les recrée; le projet Lakebase repart vide (l'app recrée ses tables au démarrage, l'historique DEV est perdu) |
| App ou volume existe mais le déploiement dit « already exists » | rattacher la ressource avant de redéployer : `databricks bundle deployment bind doc-compare qualibot -t dev --profile DEV` (app), `databricks bundle deployment bind qualibot_images dev_landingzone.qualibot.images -t dev --profile DEV` (volume; même forme pour `qualibot_doc_compare`, `qualibot_test`, `qualibot_staging`) |
| Le dossier des modèles Docling est vide ou absent | aucun job de parsing ne peut tourner : dites-le moi, ce volume n'est pas dans le bundle |
| Endpoint `qualibot` absent | à recréer avant tout index : dites-le moi |
| `chunks_index` absent, `pret = False`, ou sa `source` n'est pas `dev_landingzone.qualibot.chunks` | me le dire avec la sortie : je vous donne la commande de (re)création sur **votre** table (pas de copie depuis l'UAT) |
| `jetons_max` ≈ 450 et `caracteres_max` ≈ 1 600 (+ préfixe `[Source: …]`) | le chunking en base est celui du code actuel |
| `jetons_max` vers 1 000 ou plus | `chunks` a l'ancien chunking : dites-moi quelle table contient le bon, je vous donne la commande |
| `_pipeline_checkpoint`, `processed_files` ou `image_metadata` absentes ou vides | **ne lancez pas `3_parse` sans filtre** : utilisez seulement le test filtré `_test` de 0.5 |

### 0.3 Déployer les jobs et ressources

```powershell
databricks bundle deploy -t dev --profile DEV
databricks bundle summary -t dev --profile DEV
```

### 0.4 Tester chaque job, commandes exactes

`bundle run` attend la fin et affiche `SUCCESS` ou l'erreur. Lancez dans cet ordre :

```powershell
# 1. droits du SP de l'app et du SP des jobs (dont chunks_index); une ligne OK: par droit
databricks bundle run grant_app_access_dev -t dev --profile DEV

# 2. migrations du schéma Lakebase (idempotentes)
databricks bundle run migrate_lakebase_dev -t dev --profile DEV

# 3. export de la base Lakebase DEV vers le volume staging
databricks bundle run lakebase_export_dev_to_volume -t dev --profile DEV

# 4. import des tables de chat de l'UAT dans dev_landingzone.qualibot (chat_*, feedbacks...); n'écrit pas dans chunks
databricks bundle run lakebase_import_uat_to_dev -t dev --profile DEV

# 5. notation qualité : 5 tours seulement (appels LLM payants)
databricks bundle run score_production_qa -t dev --profile DEV --notebook-params test_limit=5

# 6. arrêt puis démarrage de l'app (le job d'arrêt de nuit et celui de reprise du week-end)
databricks bundle run apps_stop_nightly_dev -t dev --profile DEV
(databricks apps get qualibot --profile DEV -o json | ConvertFrom-Json).compute_status.state     # attendu : STOPPED
databricks bundle run qualibot_start_weekend_dev -t dev --profile DEV
(databricks apps get qualibot --profile DEV -o json | ConvertFrom-Json).compute_status.state     # attendu : ACTIVE
```

Si un job échoue, l'erreur est dans l'UI Jobs DEV, onglet **Runs**, cellule en rouge : cherchez `NameError`, `ModuleNotFoundError`, `No module named 'ops_config'`. Si `--notebook-params` est refusé, lancez `score_production_qa` depuis l'UI avec le widget `test_limit` à 5.

### 0.5 Pipeline de parsing

Tâche par tâche, sur les vraies tables. Seulement si l'état du pipeline existe (voir 0.2) :

```powershell
databricks bundle run parsing_pipeline -t dev --profile DEV --only 1_categories
databricks bundle run parsing_pipeline -t dev --profile DEV --only 2_manifest
databricks bundle run parsing_pipeline -t dev --profile DEV --only 3_parse
databricks bundle run parsing_pipeline -t dev --profile DEV --only 4_describe_images
databricks bundle run parsing_pipeline -t dev --profile DEV --only 5_sync_index
databricks bundle run parsing_pipeline -t dev --profile DEV --only 6_update_kb_metadata
```

Attendu (tables à jour) : `3_parse` journalise `[SCOPE] ... to scan=0` et `[GHOST GUARD] All SUCCESS docs have chunks - OK.`; `5_sync_index` affiche `sync triggered`; `6_update_kb_metadata` affiche `doc_catalog N -> N documents`.

Test de parsing réel, **sans toucher à vos tables** (suffixe `_test`). Choisissez 2 ou 3 IDDOC :

Prenez de préférence des documents qui ont des images (c'est ce qui exerce le placement des images de `4_describe_images`) :

```sql
SELECT m.IDDOC, m.ref, count(i.image_id) AS images
FROM dev_landingzone.qualibot.parse_manifest m
LEFT JOIN dev_landingzone.qualibot.image_metadata i ON i.IDDOC = m.IDDOC
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
SELECT parse_status, count(*) FROM dev_landingzone.qualibot.processed_files_test GROUP BY 1;
SELECT IDDOC, count(*) AS passages, max(chunk_token_count) AS jetons_max FROM dev_landingzone.qualibot.chunks_test GROUP BY 1;
```

Attendu : `SUCCESS` pour vos IDDOC, des passages avec `jetons_max` ≈ 450. Puis **remettre la configuration normale et nettoyer** :

```powershell
databricks bundle deploy -t dev --profile DEV
```

```sql
DROP TABLE IF EXISTS dev_landingzone.qualibot.chunks_test;
DROP TABLE IF EXISTS dev_landingzone.qualibot.processed_files_test;
DROP TABLE IF EXISTS dev_landingzone.qualibot.image_metadata_test;
DROP TABLE IF EXISTS dev_landingzone.qualibot._pipeline_checkpoint_test;
DROP TABLE IF EXISTS dev_landingzone.qualibot.parse_manifest_test;
DROP TABLE IF EXISTS dev_landingzone.qualibot.category_reference_test;
```

(Si `SHOW TABLES IN dev_landingzone.qualibot LIKE '*_test'` en montre d'autres, supprimez-les aussi.)

### 0.6 Déployer l'app

Reconstruit le client, ce qui régénère `client/bun.lock` et `client/out/` :

```powershell
.\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
git status      # client/bun.lock et client/out/ ont changé : les commiter
databricks apps logs qualibot --profile DEV
```

Dans les logs : `Starting in production mode`, `Lakebase ready`, `doc_catalog: loaded N documents from Lakebase`, aucune stack trace.

### 0.7 Tester l'app (URL de `databricks apps get`)

- **Chat** : une question en ALL, une en AS, une en IS; une hors sujet (refus); une en espagnol; les documents cités sont cliquables.
- **Compare** : deux révisions d'un même document; Change Summary et Change Table; 👍 puis commentaire sur chacune des deux cartes; export Excel et PDF; résumé d'un seul document; **Judge Impacted Docs** et son vote par document; History (recharger une comparaison); lien « Share » d'un chat ouvert en navigation privée.

### 0.8 Me renvoyer

La sortie de 0.2 (PowerShell et SQL), puis l'erreur de chaque pas qui échoue, ou « tout est vert ». Je coche alors les sections 1 à 6 et je mets `operations_dev.md` à jour (daté).

## 1. Rangement de `utils/` (2026-10-08)

`utils/databricks_ops/` n'existe plus, tout est directement sous `utils/` :

| Avant | Après |
|---|---|
| `utils/databricks_ops/{app_mgmt,dev_copy,lakebase_sync,user_capabilities,evaluation}/` | `utils/{app_mgmt,dev_copy,lakebase_sync,user_capabilities,evaluation}/` |
| `utils/databricks_ops/config.py` | `utils/ops_config.py` (importé par `from ops_config import …`) |
| `utils/databricks_ops/README.md` | `utils/README.md` |
| `dev_copy/grant_app_access.py`, `grant_volume_access_job.py` | `utils/grants/` |
| `utils/quality_monitoring/`, `utils/traces_migration/` (jobs KA, hérités) | `utils/ka_legacy/` |
| `utils/databricks_ops/vector_search_sync/`, `utils/deploy/_test_*_head.py` | supprimés (cassés / jetables) |

Les chemins sont réécrits dans `databricks.yml`, `resources/*.yml`, `bitbucket-pipelines.yml`, les `.ps1` et les docs.

- [ ] `databricks bundle validate -t dev --profile DEV` (aucun chemin de notebook introuvable)
- [ ] `databricks bundle deploy -t dev --profile DEV`
- [ ] Lancer un job de chaque dossier déplacé :
  - [ ] `databricks bundle run grant_app_access_dev -t dev --profile DEV` (grants)
  - [ ] le job de migration Lakebase DEV (`lakebase_sync/migrate_lakebase_job.py`)
  - [ ] le job `sync_user_capabilities` DEV (importe `ops_config` via `sys.path`)
  - [ ] un job `apps_stop_*` / `apps_start_*` (app_mgmt)
  - [ ] `score_production_qa` DEV (evaluation)

## 2. Notebook `3_Parse_Pipeline.py` découpé (2026-10-08)

Le notebook passe de 1 639 à ~320 lignes et suit le plan LEAP (`Inputs`, `Prep 1-3`, `Tr. 1-5`, `Quality Checks`, `Outputs`).
Les fonctions de chaque phase sont dans le nouveau `utils/parsing_pipeline/parse_steps.py` (code déplacé, logique inchangée).
Changements de comportement voulus, tous mineurs :

- la cellule `importlib.reload(image_utils)` et `%autoreload` ont disparu (inutiles dans un job) ;
- `CHECKPOINT_BATCH_SIZE` est lu directement dans `config.py` (100) ;
- l'étape « dossier vide » et le « ghost guard » sont devenus les fonctions `mark_empty_folders` et `flag_ghost_documents` ;
- `config.py` n'exige plus `PARSING_INTRAQUAL_SOURCE` (constante inutilisée) et ne définit plus `TARGET_AUDIT_TABLE`.

À rejouer sur DEV (aucune exécution possible depuis la machine de dev) :

- [ ] `databricks bundle run parsing_pipeline -t dev --profile DEV` en incrémental (rien à parser) : `3_parse` doit finir sans erreur, journaux `[SCOPE]`, `[SELECT]`, `[GHOST GUARD]`.
- [ ] Même job avec `PARSING_PARSE_FILTER=<2-3 IDDOC>` et `PARSING_RUN_MODE=full` sur des tables `_test` (`PARSING_TABLE_SUFFIX=_test`) : parsing réel, retry, `processed_files`, `chunks`, `image_metadata` écrits.
- [ ] Vérifier qu'un IDDOC sans fichier parsable reçoit `SKIPPED_EMPTY_FOLDER`.
- [ ] Vérifier que `parse_steps.py` est bien déployé à côté du notebook (le bundle synchronise tout `utils/`).

## 3. Autres notebooks du pipeline (2026-10-08)

`print` remplacé par `logger` (niveau `warning` / `error` pour les échecs) dans `1_`, `2_`, `4_`, `5_`, `6_` et `generic_pipeline/`;
`DBTITLE` supprimés; imports inutilisés retirés; `4_Describe_Images_LLM.py` importe `DeltaTable` en tête de notebook.

- [ ] Lancer le job complet une fois sur DEV (`1_categories` à `6_update_kb_metadata`) et lire les journaux de chaque tâche : aucune `NameError` sur `logger`.
- [ ] `5_sync_index` et `6_update_kb_metadata` sont serverless : vérifier qu'ils n'importent rien d'autre que la bibliothèque standard pour le logger (c'est voulu).

## 4. Client (2026-10-08)

Supprimés : pages `HomePage` / `AboutPage` (jamais routées) et tout ce qu'elles seules utilisaient (`components/home`, `components/about`, `background`, `FeedbackPanel`, `lib/utils.ts`, `lib/types.ts`), les images de la page d'accueil, et les dépendances `three`, `@types/three`, `clsx`, `tailwind-merge`, `class-variance-authority`.

- [ ] `cd client && bun install` (met `bun.lock` à jour, il n'a pas pu être régénéré ici) puis `bun run build`.
- [ ] Commiter le nouveau `client/out/` (le dossier versionné est plus ancien que les sources, et ses fichiers de la page d'accueil y sont encore).
- [ ] Ouvrir l'app DEV : chat, comparaison, historique, chat partagé.

## 5. YAML et commentaires

`databricks.yml`, `resources/parsing_pipeline.job.yml`, `app.yaml`, `server/` et `config.py` : seuls des commentaires ont changé (structure YAML et AST Python vérifiés identiques avant / après).

- [ ] `databricks bundle validate -t dev --profile DEV` (puis `-t qualibot-uat-test`).

## 6. Deuxième passe (2026-10-08)

- **Jobs Knowledge Assistant supprimés** : `resources/traces_migration.yml`, `resources/quality_scoring.yml` (job `D_3`), le job `sync_mlflow_scorer_assessments_uat` de `databricks.yml` et leurs notebooks (`utils/ka_legacy/`, `sync_mlflow_scorer_assessments.py`). Au prochain `bundle deploy -t qualibot-uat`, Databricks les **détruira** côté UAT. Le job de notation courant (`score_production_qa_uat`, notebook `utils/evaluation/score_production_qa.py`) reste.
  - [ ] Avant ce déploiement UAT (qui demande votre accord), vérifier que le dashboard « ChatBot - Quality » ne lit plus les tables `ka_*` / `chat_quality_*` alimentées par ces jobs.
- **`PARSING_INTRAQUAL_SOURCE` / variable `intraqual_source_catalog_schema`** retirées du bundle et du job de parsing.
- **`print` remplacé par `logger`** dans tous les scripts et notebooks de `utils/` (sorties CLI incluses : même texte, préfixé par le niveau pour les notebooks). Les barres de progression `print(..., end=" ")` de `copy_Lakebase_tables.py` deviennent une ligne par table.
  - [ ] Lancer une fois chaque job Lakebase / grants / dev_copy et vérifier qu'il n'y a pas de `NameError: logger`.
- **Sections LEAP** ajoutées (`Technical debt`, `Inputs`, `Data Preparation`, `Data Transformations`, `Quality Checks`, `#N/A` si vide) dans les notebooks 1, 2, 4, 5, 6 et generic 2, 3.
- **`generic_pipeline/1_Parse_Chunk_Generic.py`** réécrit : widgets (mêmes valeurs par défaut), fonctions dans `chunk_steps.py`, contrôle d'unicité de `chunk_id` avant l'écriture, attente de l'index dédupliquée.
  - [ ] Si vous vous en servez : le lancer sur `/Volumes/uat_landingzone/qualibot/test/test_documents` et vérifier la table `chunks_test_generic` et l'index.
- **Client** : `CompareView.tsx` (2 700 lignes) découpé en `compareShared.ts`, `PdfDropZone`, `JsonDiffTable`, `StructuredResultCard`, `ResultCard`, `DocSummaryCard`, `CardFeedback` (vote partagé par les deux cartes de résultat). Typecheck et build Vite passent.
  - [ ] Après `bun run build`, passer une comparaison complète (Change Summary, Change Table, vote et commentaire, résumé d'un document, recherche d'impact).

## 7. Notebooks du pipeline refactorés (2026-10-09)

Les notebooks `2_`, `4_`, `5_`, `6_` du parsing et `1_`, `2_`, `3_` du pipeline générique ont maintenant la même forme que `3_` : un bloc markdown du « pourquoi » avant chaque cellule de code, toutes les sections LEAP, une dette technique réelle.
La logique de `2_` et `4_` a été déplacée dans `manifest_steps.py` et `describe_steps.py` (code déplacé, comportement conservé).

Changements de comportement à connaître :
- `2_Cleanup_Volume` : les `DELETE` des tables dérivées ne sont plus silencieux si une table échoue (un avertissement est journalisé, les autres tables continuent); `EXCLUDED_IDCATS` vient de `config.py`; plus de rechargement de `config`/`selection` en cours de route.
- `4_Describe_Images_LLM` : la table temporaire `_image_updates_temp` prend le suffixe `PARSING_TABLE_SUFFIX` (sans effet quand il est vide); `%autoreload` retiré; `DeltaTable` importé en tête.
- `5_Sync_Vector_Indexes` : `chunks_full` est lu, préparé et écrit dans des sections séparées; une erreur claire est levée si `chunks_full` est demandé sans table `chunks`.
- `6_Update_Knowledge_Base_Metadata` : la vérification « catalogue non vide » est maintenant dans `# Quality Checks`; une erreur claire si `parse_manifest` ou `chunks` n'existe pas.
- `1_Build_Category_Reference` : la garde de fraîcheur passe en `# Quality Checks` et s'exécute après la construction de `category_reference`, mais toujours avant son écriture.
- `generic_pipeline/1_Parse_Chunk_Generic` : un widget `environment` (défaut `dev`) remplace les chemins `uat_landingzone` en dur; `3_Sync_Vector_Index` : un index inconnu n'empêche plus de synchroniser l'index suivant de la liste (bug de suppression pendant l'itération).

À rejouer sur DEV après un `bundle deploy -t dev` :
- [ ] `--only 1_categories` : `All sources fresh`, `category_reference` écrite.
- [ ] `--only 2_manifest` avec `DRY_RUN` par défaut : les compteurs `KEEP/DELETE/ORPHAN/...`, `parse_manifest written: N IDDOCs in scope`, `[DRY RUN] ... would be deleted`. Comparer N avec la valeur d'hier (`SELECT count(*) FROM dev_landingzone.qualibot.parse_manifest`).
- [ ] `--only 4_describe_images` sur des tables à jour : `Nothing to describe`, `No new image chunks to inject`, `No EMPTY_TEXT document pending promotion` ou la promotion; le test `_test` de la section 0.5 exerce le placement des images.
- [ ] `--only 5_sync_index` puis `--only 6_update_kb_metadata` : comme avant (`sync triggered`, `doc_catalog N -> N documents`).
- [ ] `generic_pipeline/1_Parse_Chunk_Generic` (facultatif) : l'ouvrir dans DEV avec `environment = dev`; les modèles se trouvent par défaut dans `docling_models/docling_models`.

## 8. Noms de jobs (2026-10-09)

Les 21 jobs du bundle suivent `<D|W|Z>_<niveau>_<Domaine>_<Rôle>` (`tests/test_bundle_paths.py` le vérifie) : `D` planifié chaque jour, `W` chaque semaine, `Z` manuel; niveau `1` partout (aucun de ces jobs n'en déclenche un autre); domaine `Qualibot`. Exemples DEV : `D_1_Qualibot_Parsing_Pipeline_dev`, `D_1_Qualibot_Lakebase_Import_Uat_To_Dev`, `W_1_Qualibot_Stop_Weekend_Dev`, `Z_1_Qualibot_Grant_App_Access_dev`, `Z_1_Qualibot_Copy_Uat_To_Dev`.

Les clés de ressource (`grant_app_access_dev`, `parsing_pipeline`…) ne changent pas : les commandes `bundle run` restent les mêmes. Au prochain `bundle deploy`, le job est renommé sur place (même identifiant, mêmes liens).

- [ ] DEV : après `databricks bundle deploy -t dev --profile DEV`, vérifier la liste (`databricks jobs list --profile DEV -o json | ConvertFrom-Json | Where-Object { $_.settings.name -match 'Qualibot' } | ForEach-Object { $_.settings.name }`).
- [ ] **Avant le premier déploiement UAT / PROD** (qui demande votre accord) : le job de parsing y est renommé de `D_1_qualibot-parsing-pipeline-<cible>` en `D_1_Qualibot_Parsing_Pipeline_<cible>`, comme les jobs d'arrêt/reprise et d'export. Vérifier qu'aucun tableau de bord, alerte SQL ou règle de notification ne repère un job par son ancien nom.
- Non renommé : le job déployé hors bundle par `utils/deploy/deploy_sync_user_capabilities_uat_personal.py` (`qualibot-sync-user-capabilities-uat`) : le renommer en recréerait un deuxième. Dites-moi si vous voulez le faire.
- Fuseau des plannings : `Europe/Paris`, conservé (décision du 2026-10-09). Alertes d'échec : à traiter à part.

## 9. Jobs déplacés de `databricks.yml` vers `resources/` (2026-10-09)

Les 17 jobs déclarés par cible dans `databricks.yml` sont maintenant dans `resources/` : `app_schedules`, `lakebase_sync`, `dev_copy`, `grants`, `evaluation`, `user_capabilities` (`*.job.yml`), à côté de `parsing_pipeline.job.yml`. Définitions identiques (comparées en YAML avant / après), seuls les `notebook_path` passent en `../utils/...`. `databricks.yml` ne garde que variables, apps, schémas, volumes, Lakebase.

À faire : `databricks bundle validate -t dev --profile DEV` puis `bundle deploy -t dev` : le plan doit afficher 0 add / 0 delete (clés de ressources inchangées).

## 10. Tags et alertes (2026-10-09)

Tout job du bundle porte maintenant `project: Qualibot` (il manquait sur `score_production_qa` et `lakebase_import_uat_to_dev`) ; au prochain `bundle deploy -t dev` : 2 jobs modifiés.
Alertes : `utils/alerts/` (4 requêtes + README avec la création pas à pas). À créer à la main dans Databricks SQL, rien ne les déploie.
