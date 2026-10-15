# lakebase

One-shot scripts, run by hand and never deployed: nothing in the app or in a bundle job imports or runs them. They copy the Lakebase tables of UAT into the DEV catalog and apply the schema changes of `migrations.py` to one Lakebase database.
## Files

| File | Kind | What it does |
|---|---|---|
| `copy_Lakebase_tables.py` | CLI | Creates or replaces one Delta table of `dev_proj.qualibot` per UAT Lakebase table. Skips `llm_requests` |
| `migrate_lakebase.py` | CLI | Applies `MIGRATIONS` to the Lakebase database of the environment given by `--env` |
| `migrate_lakebase_job.py` | Databricks notebook (import it into the workspace) | The same migration, run from a workspace notebook; parameters are widgets |
| `migrations.py` | Python module | Ordered list `MIGRATIONS` and `apply_migrations(conn)`. Imported by the two migration scripts, no command |

## How to run (from the repository root)

```
python runbooks/scripts/lakebase/copy_Lakebase_tables.py
python runbooks/scripts/lakebase/copy_Lakebase_tables.py --direct
python runbooks/scripts/lakebase/migrate_lakebase.py --env DEV
python runbooks/scripts/lakebase/migrate_lakebase.py --env UAT
```

`--env` takes `DEV`, `UAT`, `UAT-TEST` or `PROD` (default `DEV`); each connects with its CLI profile and database:

| `--env` | CLI profile | Database |
|---|---|---|
| `DEV` | `DEV` | `doccompare` |
| `UAT` | `UAT` | `doccompare` |
| `UAT-TEST` | `UAT` | `doccompare_test` |
| `PROD` | `qualibot-prod` | `doccompare` |

To run the migration from the workspace, import the notebook, then run it with its widgets (defaults below):

```
databricks workspace import <path> --file runbooks/scripts/lakebase/migrate_lakebase_job.py --format SOURCE --language PYTHON --overwrite --profile <profile>
```

Widgets (defaults): `LAKEBASE_PROJECT_ID` = `qualibot`, `LAKEBASE_BRANCH` = `production`, `LAKEBASE_ENDPOINT` = `primary`, `LAKEBASE_DATABASE` = `doccompare_test`. The value `doccompare` is refused on the UAT workspace host.

## Inputs and outputs

- Copy, default: reads the export of the UAT job `lakebase_export_uat_to_volume` (JSON Lines in `/Volumes/uat_proj/qualibot/staging/lakebase_export`), downloaded to `lakebase_export/`. Copy, `--direct`: reads Lakebase and writes `lakebase_export/<schema>/<table>.json`.
- Copy: each table is uploaded to `/Volumes/uat_proj/qualibot/staging/<table>.json` and replaces `dev_proj.qualibot.<table>` (a Delta table). Tables with zero rows are skipped.
- Migrations: the entries of `MIGRATIONS` are applied to the database. Each runs in its own transaction; a failing entry is logged and does not stop the others.

## Before you run

- The Databricks CLI is on the PATH, with the profiles `UAT` and `DEV` for the copy (or the profile of `--env` for the migration). The packages `psycopg2-binary` and `databricks-sdk` are installed.
- The default copy needs the export files on the UAT volume; `--direct` needs network access to port 5432 of Lakebase.
