# Runbooks

This folder holds everything that is run by hand: the operator runbooks for each environment, the retest list and the one-shot scripts. Nothing here is deployed. The folder is excluded from the bundle (`databricks.yml`, `.databricksignore`, `utils/deploy/deploy_qualibot.ps1`). Anything that creates a resource from the repository (bundle, `resources/*.yml`, job notebooks, scripts of `utils/deploy/`) stays in the code.

The operator runbooks (`dev.md`, `uat.md`, `prod.md`, `retest.md`) are written in French, for the operator.

## Runbooks

| File | Content |
|---|---|
| `dev.md` | DEV environment: copy of the UAT corpus into `dev_proj.qualibot`, deployment, indexes, evaluation, trace scoring, parsing pipeline |
| `uat.md` | UAT environment: migration to `uat_proj.qualibot`, validation, going live, chatbot switch-over, activations after validation, removal of the former test target |
| `prod.md` | PROD environment: `prod_proj.qualibot`, volumes, indexes, grants, deployment on explicit approval |
| `uat_bascule.md` | Checklist cochable de la bascule UAT (préparation, jobs, bascule de l'app, nettoyage) |
| `retest.md` | What still has to be re-run or re-tested after the latest code changes, with the command and the status of each item |
| `scripts/` | One-shot scripts: schema migrations, table copies, LibreOffice packaging, offline comparison report (see below) |

## Rules

- Every manual step gives all its commands in full, in order (PowerShell, `databricks bundle run`, SQL). A step never refers to another step without repeating its commands.
- A step marked "on approval" is never run without an explicit go-ahead.
- Status: `[ ]` to do; `[x]` done and confirmed by the operator, with the confirmation date written after it.
- CLI profiles: `DEV` (DEV), `UAT` (UAT), `PROD` (PROD). `migrate_lakebase.py` accepts the environments `DEV`, `UAT` and `PROD`.
- The application and pipeline objects (tables, volumes, Vector Search indexes) live in `<env>_proj.qualibot`, with the same names in every environment. The Intraqual catalog (`prod_landingzone.intraqual`) is not modified by these runbooks.

## State and what remains (2026-10-22)

| Environment | Done | Remains, in order |
|---|---|---|
| DEV | Official scope (3 roots), date 2016, divisions AS/IS/GROUP, batch fix, parsing run and checks passed | Rebuild `chunks_index` (`dev.md` I), test the chat with the AS and IS filters, reset the temporary DEV settings (`dev.md` H, K) |
| UAT | Test index `chunks_index_test` deleted | `uat.md` H: push, `deploy-uat-jobs`, `full` run, checks, `deploy-uat`; then `uat.md` I: clean-up list from the table dates |
| PROD | Reset and copy scripts written (`scripts/prod_cleanup/`, `scripts/prod_copy/`) | `prod.md` 0.3 P0 to P6, then sections 4 to 6 (grants by the SP, `target_env.json`, deployment on explicit approval) |

PROD starts after DEV and UAT are validated (checklist at the end of `prod.md`).

## Where to find an operation

- Copy of the UAT corpus to DEV: `dev.md` 00.3, 00.4 and block C.
- Lakebase export to the volume, loading of the chat tables: `uat.md` step 0.5 (UAT) and `dev.md` 00.6 (DEV).
- Validation of UAT on `uat`: `uat.md` block A.
- Parsing, indexes and metadata: `uat.md` blocks B and D; `dev.md` block Q.
- Trace scoring and the MLflow experiment: `dev.md` 0.8, 0.9 and 0.14.
- Scripts: the table below.

## Scripts (`scripts/`)

One-shot scripts: schema migrations, table copies, packaging and checks, run by hand once or rarely. No bundle job runs them, and this folder is never deployed. Scripts used by the application, a job or a deployment are in [`utils/`](../utils/README.md).

Run every command from the repository root. Command-line Python scripts import `utils/ops_config.py` when they need it.

| File | What it does | How to run it |
|---|---|---|
| [`scripts/code_quality/check_docstrings.py`](scripts/code_quality/check_docstrings.py) | CLI. Lists the functions with an undocumented `:param` or no return annotation | `python runbooks/scripts/code_quality/check_docstrings.py server utils` (must report `0 problem(s)`) |
| [`scripts/dev_copy/export_uat_to_staging.py`](scripts/dev_copy/export_uat_to_staging.py) | Notebook, UAT workspace. Copies the `chunks` table and the parsing state of `uat_proj.qualibot` to the volume `staging/dev_copy`, with the LibreOffice archive. Read-only on the UAT tables. Starts the copy of the corpus into DEV. | See "Notebook commands" below |
| [`scripts/dev_copy/export_uat_submit.json`](scripts/dev_copy/export_uat_submit.json) | Body of the `jobs submit` command that runs the notebook above | `databricks jobs submit --json @runbooks/scripts/dev_copy/export_uat_submit.json --profile UAT` |
| [`scripts/dev_copy/import_staging_to_dev_job.py`](scripts/dev_copy/import_staging_to_dev_job.py) | Notebook, DEV workspace. Recreates the tables of the snapshot as `dev_proj.qualibot.<table>` | See "Notebook commands" below |
| [`scripts/dev_copy/ensure_vector_search_endpoint.py`](scripts/dev_copy/ensure_vector_search_endpoint.py) | Notebook, DEV workspace. Creates the Vector Search endpoint `qualibot` if missing, waits until ONLINE | See "Notebook commands" below |
| [`scripts/dev_copy/copy_uat_to_dev_submit.json`](scripts/dev_copy/copy_uat_to_dev_submit.json) | Body of the `jobs submit` that runs the three DEV steps in order (import, endpoint, index sync) | `databricks jobs submit --json @runbooks/scripts/dev_copy/copy_uat_to_dev_submit.json --profile DEV` |
| [`scripts/grants/grant_app_access.py`](scripts/grants/grant_app_access.py) | Notebook, DEV workspace. Grants the DEV app and pipeline service principals what they use (see `scripts/grants/README.md`) | See "Notebook commands" below |
| [`scripts/grants/grant_app_access_dev_submit.json`](scripts/grants/grant_app_access_dev_submit.json) | Body of the `jobs submit` that runs the grant notebook as the DEV job SP | `databricks jobs submit --json @runbooks/scripts/grants/grant_app_access_dev_submit.json --profile DEV` |
| [`scripts/grants/grant_volume_access_job.py`](scripts/grants/grant_volume_access_job.py) | Notebook. Grants one volume privilege to one principal. Not run by any job today | Import and submit as the other notebooks, with its widgets |
| [`scripts/lakebase/copy_Lakebase_tables.py`](scripts/lakebase/copy_Lakebase_tables.py) | CLI. Creates or replaces, in `dev_proj.qualibot`, one Delta table per table of the UAT Lakebase database, from the export on the volume (or read directly from Lakebase with `--direct`, which needs port 5432) | After the job `lakebase_export` has run: `python runbooks/scripts/lakebase/copy_Lakebase_tables.py`; direct: `python runbooks/scripts/lakebase/copy_Lakebase_tables.py --direct` |
| [`scripts/lakebase/migrate_lakebase.py`](scripts/lakebase/migrate_lakebase.py) | CLI. Applies the changes of `migrations.py` to one Lakebase database. `--env` takes `DEV`, `UAT` or `PROD` (default `DEV`) | `python runbooks/scripts/lakebase/migrate_lakebase.py --env DEV` |
| [`scripts/lakebase/migrations.py`](scripts/lakebase/migrations.py) | Ordered, idempotent list of schema changes (table renames, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `DROP TABLE IF EXISTS`) and the function `apply_migrations` | Module imported by the two migration scripts: no command |
| [`scripts/soffice/package_libreoffice.py`](scripts/soffice/package_libreoffice.py) | CLI. Builds the portable LibreOffice archive (tar.gz) used by the "Exact (PDF)" preview, from the LibreOffice bundle downloaded beforehand. Needs network access to the Ubuntu archive | `python runbooks/scripts/soffice/package_libreoffice.py <bundle.tar.gz> <sortie.tar.gz>`, then `databricks fs cp <sortie.tar.gz> dbfs:/Volumes/<catalog>/<schema>/<volume>/libreoffice/libreoffice-linux-x64.tar.gz` |
| [`scripts/soffice/scan_needed_libs.py`](scripts/soffice/scan_needed_libs.py) | CLI. Lists the shared libraries a portable LibreOffice tree needs but does not ship. Used to check the system libraries that `package_libreoffice.py` bundles | `python runbooks/scripts/soffice/scan_needed_libs.py <dossier_extrait>` |
| [`scripts/compare/compare_preview.py`](scripts/compare/compare_preview.py) | CLI. Offline report on a document pair: extraction, diff, images and chunking, with no model call. Logs what would be sent to the model | `python runbooks/scripts/compare/compare_preview.py old.docx new.docx`. Options: `--method {standard,structured,comparative}` (default `structured`), `--dump-diff <fichier>` (writes the full text block), `--threshold <valeur>` (overrides the similarity threshold, default 0.55), `--chunk-threshold` (default 60000), `--chunk-size` (default 45000) |
| [`scripts/prod_copy/probe_access.py`](scripts/prod_copy/probe_access.py) | Notebook, PROD workspace, read-only. Tells which environments PROD can read (tables and volumes) and lists the PROD schemas | `prod.md` 0.3, step P1 |
| [`scripts/prod_copy/copy_tables_to_prod.py`](scripts/prod_copy/copy_tables_to_prod.py) | Notebook, PROD workspace. Copies the corpus and parsing state of DEV into `prod_proj.qualibot` (deep clone, volume paths rewritten, row counts checked). Dry run by default | `prod.md` 0.3, step P4 |
| [`scripts/prod_cleanup/drop_prod_legacy.py`](scripts/prod_cleanup/drop_prod_legacy.py) | Notebook, PROD workspace. Deletes the legacy Knowledge Assistant jobs and endpoints, by exact name. Dry run by default | `prod.md` 0.3, step P2 |
| [`scripts/uat_cleanup/drop_uat_test_index.py`](scripts/uat_cleanup/drop_uat_test_index.py) | Notebook, UAT workspace. Deletes the test Vector Search index `chunks_index_test` and its writeback table | Done on 2026-10-22 |

### Notebook commands

Notebooks do not run from a workstation. Import each one into the workspace at the path given, then run it with its parameters.

**Copy UAT to DEV** (profile `UAT`, from the repository root). The notebook path must be identical to `notebook_path` in `export_uat_submit.json`:

```powershell
databricks workspace mkdirs /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy --profile UAT
databricks workspace import /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy/export_uat_to_staging --file runbooks/scripts/dev_copy/export_uat_to_staging.py --format SOURCE --language PYTHON --overwrite --profile UAT
databricks jobs submit --json @runbooks/scripts/dev_copy/export_uat_submit.json --profile UAT
```

**Grants on DEV** (profile `DEV`, from the repository root). Run it after the volumes and the index exist:

```powershell
databricks workspace mkdirs /Users/jules.gourio.external@latecoere.aero/qualibot_grants --profile DEV
databricks workspace import /Users/jules.gourio.external@latecoere.aero/qualibot_grants/grant_app_access --file runbooks/scripts/grants/grant_app_access.py --format SOURCE --language PYTHON --overwrite --profile DEV
databricks jobs submit --json @runbooks/scripts/grants/grant_app_access_dev_submit.json --profile DEV
```

**Copy UAT to DEV, the DEV side** (profile `DEV`). Run it after `export_uat_to_staging` has written the snapshot. The DEV bundle must be deployed first:

```powershell
databricks workspace mkdirs /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy --profile DEV
databricks workspace import /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy/import_staging_to_dev_job --file runbooks/scripts/dev_copy/import_staging_to_dev_job.py --format SOURCE --language PYTHON --overwrite --profile DEV
databricks workspace import /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy/ensure_vector_search_endpoint --file runbooks/scripts/dev_copy/ensure_vector_search_endpoint.py --format SOURCE --language PYTHON --overwrite --profile DEV
databricks jobs submit --json @runbooks/scripts/dev_copy/copy_uat_to_dev_submit.json --profile DEV
```

### Rules for the scripts

- Nothing here is imported by `server/`, `utils/` or any file of `resources/`. A script that a job or a deployment must run belongs in `utils/`.
- Notebooks and CLI scripts log with `logger`, never `print`.
- No job reads the default values (widgets, constants) of these scripts. Check them before running a script again.

- `leap_dbx_repository/`: copie de référence des notebooks et YAML du data asset Intraqual (silver/gold) qui appartiennent au dépôt `leap-dbx-repository`; jamais cités hors de ce dossier.
