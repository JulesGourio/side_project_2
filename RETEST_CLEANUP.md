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
