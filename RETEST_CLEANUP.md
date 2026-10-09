# Retest après le nettoyage du code (démarré le 2026-10-08)

Chantier : moins de commentaires, plus de code mort, notebooks de parsing découpés, `utils/` rangé.
Rien n'a pu être exécuté sur Databricks depuis la machine de développement : tout ce qui suit
est à rejouer sur **DEV** (`-t dev`) après un `bundle deploy`. Cocher et dater au fur et à mesure.

## 0. Procédure complète de test sur DEV (à suivre dans l'ordre)

PowerShell, profil CLI `DEV`. Cochez au fur et à mesure; si un pas échoue, envoyez-moi l'erreur et arrêtez-vous là.

**0.1 Préparer** (dossier **vide**, des fichiers ont été déplacés et supprimés)
```powershell
# extraire le zip de la branche feature/chat-vsi-merged-on-impact-search dans un dossier vide, puis :
cd <ce dossier>
Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
databricks current-user me --profile DEV
```

**0.2 Cas où vous avez aussi supprimé des ressources DEV** : l'app, le projet Lakebase, les volumes ou les tables.
Rien de ce qui suit ne les recrée seul : refaire d'abord `operations_dev.md` blocs **R** (rattacher l'app existante), **I** (infra : `bundle deploy`, binds), **E** (export du corpus UAT) puis **C** (job `copy_uat_to_dev`, qui recopie les tables et recrée l'index).
Si vous n'avez supprimé que le code et les jobs, passez à 0.3.

**0.3 Valider puis déployer les jobs et ressources**
```powershell
databricks bundle validate -t dev --profile DEV
databricks bundle deploy   -t dev --profile DEV
```
Attendu : aucune erreur de chemin de notebook (tous les notebooks ont changé de dossier) et les anciens jobs sont inchangés.
Si `validate` cite un fichier introuvable, notez son chemin : c'est un oubli de ma part.

**0.4 Tester chaque job déplacé** (un par dossier de `utils/`) :
```powershell
databricks bundle run grant_app_access_dev     -t dev --profile DEV   # utils/grants        -> une ligne OK: par droit
databricks bundle run migrate_lakebase_dev     -t dev --profile DEV   # utils/lakebase_sync -> migrations idempotentes
databricks bundle run lakebase_export_dev_to_volume -t dev --profile DEV   # utils/lakebase_sync (export)
databricks bundle run lakebase_import_uat_to_dev -t dev --profile DEV # utils/lakebase_sync (import)
databricks bundle run score_production_qa      -t dev --profile DEV   # utils/evaluation
databricks bundle run apps_stop_nightly_dev    -t dev --profile DEV   # utils/app_mgmt (arrête l'app !)
databricks apps start qualibot --profile DEV                          # puis la relancer
```
Dans les logs de chaque exécution, cherchez `NameError`, `ModuleNotFoundError` et `No module named 'ops_config'`.
`copy_uat_to_dev` (utils/dev_copy) est lourd : ne le lancer que si 0.2 est nécessaire.

**0.5 Pipeline de parsing, tâche par tâche** (`3_parse` tourne sur GPU : démarrage de cluster de quelques minutes)
```powershell
databricks bundle run parsing_pipeline -t dev --profile DEV --only 1_categories
databricks bundle run parsing_pipeline -t dev --profile DEV --only 2_manifest
databricks bundle run parsing_pipeline -t dev --profile DEV --only 3_parse
databricks bundle run parsing_pipeline -t dev --profile DEV --only 4_describe_images
databricks bundle run parsing_pipeline -t dev --profile DEV --only 5_sync_index
databricks bundle run parsing_pipeline -t dev --profile DEV --only 6_update_kb_metadata
```
Attendu, sur des tables à jour (incrémental) :
- `3_parse` : `[SCOPE] ... to scan=0` (ou quelques IDDOC), `[SELECT] 0 files selected`, puis `[GHOST GUARD] All SUCCESS docs have chunks - OK.` ; aucune erreur.
- `5_sync_index` : une ligne `sync triggered: chunks_index` puis `done`.
- `6_update_kb_metadata` : `doc_catalog N -> N documents`.

Puis **un vrai parsing** sans toucher aux vraies tables (suffixe `_test`, 2 ou 3 IDDOC pris dans `dev_landingzone.qualibot.parse_manifest`) :
```powershell
databricks bundle deploy -t dev --profile DEV --var="parsing_table_suffix=_test" --var="parsing_parse_filter=<IDDOC1>,<IDDOC2>" --var="parsing_run_mode=full"
databricks bundle run parsing_pipeline -t dev --profile DEV --only 1_categories
databricks bundle run parsing_pipeline -t dev --profile DEV --only 2_manifest
databricks bundle run parsing_pipeline -t dev --profile DEV --only 3_parse
databricks bundle run parsing_pipeline -t dev --profile DEV --only 4_describe_images
```
Vérifier dans `dev_landingzone.qualibot` : `chunks_test`, `processed_files_test`, `image_metadata_test` ont des lignes pour ces IDDOC (`parse_status = 'SUCCESS'`), et le journal de `3_parse` montre le retry et l'écriture.
**Ensuite remettre la configuration normale** (sinon le prochain run écrit dans `_test`) :
```powershell
databricks bundle deploy -t dev --profile DEV
```
Et supprimer les tables `*_test` : `SHOW TABLES IN dev_landingzone.qualibot LIKE '*_test'`, puis `DROP TABLE` sur chacune.

**0.6 Déployer l'app** (reconstruit le client : c'est ce qui met `bun.lock` et `client/out/` à jour)
```powershell
.\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
git status   # client/bun.lock et client/out/ ont changé : les commiter
```

**0.7 Tester l'app DEV** (ouvrir l'URL de l'app)
- Chat : une question en ALL, une en AS, une en IS; une question hors sujet (refus); une question en espagnol; la réponse cite des documents cliquables.
- Compare : charger deux révisions d'un même document; **Change Summary** (texte) et **Change Table** (tableau) se génèrent; voter 👍 puis commenter (c'est le nouveau composant `CardFeedback`) sur les deux cartes; bouton Export Excel / Download PDF; résumé d'un seul document; **Judge Impacted Docs** (recherche d'impact) avec son vote par document; historique d'une comparaison; chat partagé (lien « Share » ouvert dans une fenêtre privée).
- Logs de l'app (UI Apps > Logs) : aucune stack trace au démarrage; `doc_catalog: loaded N documents from Lakebase`.

**0.8 Revenir vers moi avec** : la sortie de `bundle validate`, l'erreur éventuelle de chaque pas, et un « tout est vert » si c'est le cas. Je coche alors les sections 1 à 6 ci-dessous et je mets les blocs concernés d'`operations_dev.md` à jour (daté).

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
