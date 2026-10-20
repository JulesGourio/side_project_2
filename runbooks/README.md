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
- CLI profiles: `DEV` (DEV), `UAT` (UAT), `qualibot-prod` (PROD). `migrate_lakebase.py` accepts the environments `DEV`, `UAT` and `PROD`.
- The application and pipeline objects (tables, volumes, Vector Search indexes) live in `<env>_proj.qualibot`, with the same names in every environment. The Intraqual catalog (`prod_landingzone.intraqual`) is not modified by these runbooks.

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
| [`scripts/dev_copy/export_uat_to_staging.py`](scripts/dev_copy/export_uat_to_staging.py) | Notebook, UAT workspace. Copies the `chunks` table and the parsing state of `uat_proj.qualibot` to the volume `staging/dev_copy`, with the LibreOffice archive. Read-only on the UAT tables. Starts the copy of the corpus into DEV. | See "Notebook commands" below |
| [`scripts/dev_copy/export_uat_submit.json`](scripts/dev_copy/export_uat_submit.json) | Body of the `jobs submit` command that runs the notebook above | `databricks jobs submit --json @runbooks/scripts/dev_copy/export_uat_submit.json --profile UAT` |
| [`scripts/lakebase/copy_Lakebase_tables.py`](scripts/lakebase/copy_Lakebase_tables.py) | CLI. Creates or replaces, in `dev_proj.qualibot`, one Delta table per table of the UAT Lakebase database, from the export on the volume (or read directly from Lakebase with `--direct`, which needs port 5432) | After the job `lakebase_export` has run: `python runbooks/scripts/lakebase/copy_Lakebase_tables.py`; direct: `python runbooks/scripts/lakebase/copy_Lakebase_tables.py --direct` |
| [`scripts/lakebase/migrate_lakebase.py`](scripts/lakebase/migrate_lakebase.py) | CLI. Applies the changes of `migrations.py` to one Lakebase database. `--env` takes `DEV`, `UAT` or `PROD` (default `DEV`) | `python runbooks/scripts/lakebase/migrate_lakebase.py --env DEV` |
| [`scripts/lakebase/migrations.py`](scripts/lakebase/migrations.py) | Ordered, idempotent list of schema changes (table renames, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `DROP TABLE IF EXISTS`) and the function `apply_migrations` | Module imported by the two migration scripts: no command |
| [`scripts/soffice/package_libreoffice.py`](scripts/soffice/package_libreoffice.py) | CLI. Builds the portable LibreOffice archive (tar.gz) used by the "Exact (PDF)" preview, from the LibreOffice bundle downloaded beforehand. Needs network access to the Ubuntu archive | `python runbooks/scripts/soffice/package_libreoffice.py <bundle.tar.gz> <sortie.tar.gz>`, then `databricks fs cp <sortie.tar.gz> dbfs:/Volumes/<catalog>/<schema>/<volume>/libreoffice/libreoffice-linux-x64.tar.gz` |
| [`scripts/soffice/scan_needed_libs.py`](scripts/soffice/scan_needed_libs.py) | CLI. Lists the shared libraries a portable LibreOffice tree needs but does not ship. Used to check the system libraries that `package_libreoffice.py` bundles | `python runbooks/scripts/soffice/scan_needed_libs.py <dossier_extrait>` |
| [`scripts/compare/compare_preview.py`](scripts/compare/compare_preview.py) | CLI. Offline report on a document pair: extraction, diff, images and chunking, with no model call. Logs what would be sent to the model | `python runbooks/scripts/compare/compare_preview.py old.docx new.docx`. Options: `--method {standard,structured,comparative}` (default `structured`), `--dump-diff <fichier>` (writes the full text block), `--threshold <valeur>` (overrides the similarity threshold, default 0.55), `--chunk-threshold` (default 60000), `--chunk-size` (default 45000) |

### Notebook commands

Notebooks do not run from a workstation. Import each one into the workspace at the path given, then run it with its parameters.

**Copy UAT to DEV** (profile `UAT`, from the repository root). The notebook path must be identical to `notebook_path` in `export_uat_submit.json`:

```powershell
databricks workspace mkdirs /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy --profile UAT
databricks workspace import /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy/export_uat_to_staging --file runbooks/scripts/dev_copy/export_uat_to_staging.py --format SOURCE --language PYTHON --overwrite --profile UAT
databricks jobs submit --json @runbooks/scripts/dev_copy/export_uat_submit.json --profile UAT
```

### Rules for the scripts

- Nothing here is imported by `server/`, `utils/` or any file of `resources/`. A script that a job or a deployment must run belongs in `utils/`.
- Notebooks and CLI scripts log with `logger`, never `print`.
- No job reads the default values (widgets, constants) of these scripts. Check them before running a script again.
