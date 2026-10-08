# Retest après le nettoyage du code (démarré le 2026-10-08)

Chantier : moins de commentaires, plus de code mort, notebooks de parsing découpés, `utils/` rangé.
Rien n'a pu être exécuté sur Databricks depuis la machine de développement : tout ce qui suit
est à rejouer sur **DEV** (`-t dev`) après un `bundle deploy`. Cocher et dater au fur et à mesure.

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
