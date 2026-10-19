# lakebase_sync

Scripts that take the data out of the Lakebase database of the application (to a volume, or as JSON Lines files) and
load it elsewhere: Delta tables of DEV and of UAT, and the Lakebase database of PROD. The connection settings shared
with the other folders (CLI profiles, Lakebase projects, databases per environment, table order) are in
[`ops_config.py`](../ops_config.py).

One-shot migrations and one-off table copies are run by hand and never deployed.

## Files

| File | Role | Run by |
|---|---|---|
| `export_lakebase_uat_to_volume_job.py` | Notebook: one table becomes one JSON Lines file in the staging volume. Parameters: project, branch, endpoint, database, output volume, tables to skip | Task `export_lakebase_uat_to_volume` of `lakebase_export_uat_to_volume` (target `qualibot-uat`); task `export_lakebase_dev_to_volume` of `lakebase_export_dev_to_volume` (target `dev`) |
| `import_chatbot_tables_to_uat_job.py` | Notebook: loads every table of the staging folder (chat, compare, impact search, users, caches, request logs, errors) into a Delta table of the same name in `uat_proj.qualibot`. It keeps the name from when it loaded the chat tables only | Task `import_chatbot_tables_to_uat` of `lakebase_export_uat_to_volume`, after the export |
| `export_lakebase_to_json.py` | CLI: exports every table of a database to JSON Lines | Bitbucket step "Sync prod data (1/2)" |
| `import_lakebase_from_json.py` | CLI: imports these files into a database | Bitbucket step "Sync prod data (2/2)" |

The jobs are defined in [`resources/lakebase_sync.yml`](../../resources/lakebase_sync.yml).

## Command-line scripts

Run from the repository root, with a Databricks CLI profile whose token comes from the workspace that hosts the database:

```
python utils/lakebase_sync/export_lakebase_to_json.py --profile <cli-profile> [--database doccompare] [--out-dir staging/lakebase]
python utils/lakebase_sync/import_lakebase_from_json.py --profile <cli-profile> [--database doccompare] [--in-dir staging/lakebase]
```

`--profile` is required. The export reads the source database and writes one `<table>.json` per table into the
output folder. The import reads the same files from the input folder.

## Notebook parameters

- `export_lakebase_uat_to_volume_job.py` takes the widgets `LAKEBASE_PROJECT_ID`, `LAKEBASE_BRANCH`,
  `LAKEBASE_ENDPOINT`, `LAKEBASE_DATABASE`, `OUTPUT_VOLUME_CATALOG`, `OUTPUT_VOLUME_SCHEMA`, `OUTPUT_VOLUME_NAME`
  and `TABLES_TO_SKIP` (default `llm_requests`). It writes to `/Volumes/<catalog>/<schema>/<volume>/lakebase_export`.
  The export tasks run on serverless compute: classic compute cannot reach the Lakebase endpoint.
- The two Delta imports read their settings from environment variables with defaults: `SOURCE_EXPORT_DIR`
  (`/Volumes/uat_proj/qualibot/staging/lakebase_export`), `TARGET_CATALOG`, `TARGET_SCHEMA`, and for the DEV import
  `TABLES_TO_SKIP`. The UAT import loads every exported table and skips a table with 0 rows. Each table it writes gets the property
  `lakebase_export = true` (the six chat tables, loaded before it existed, are exempt): a Delta table of the same name that
  does not carry it is left untouched and the task fails at the end with its name. The UAT export skips no table.

## Lakebase environments

The environments known by `ops_config.py`:

| Environment | CLI profile | Database | Note |
|---|---|---|---|
| `UAT` | `UAT` | `doccompare` | Database of the UAT application |
| `DEV` | `DEV` | `doccompare` | Database of the DEV application |
| `PROD` | `qualibot-prod` | `doccompare` | Database of the PROD application |

All four use the Lakebase project `qualibot`, branch `production`, endpoint `primary`.

## Rules

- `import_lakebase_from_json.py` replaces the contents of each imported table. For each table it runs a `DELETE` and an
  `INSERT` in one transaction, so a failure rolls back the whole import. It is the UAT-to-PROD copy.
- The import order is `MIGRATE_TO_PROD_TABLE_ORDER` in `ops_config.py`: parent tables first. Tables missing from that list
  come after, in the order of the export. `llm_requests` is never exported to PROD (`MIGRATE_TO_PROD_TABLES_TO_SKIP`).
- The import writes the `id` values as they are and sets each table's `id` sequence to the maximum imported value.
  Without it, the next insert of the application would reuse an imported id.
- The JSON files hold user data (questions, answers, identifiers). They stay in the staging folders and volumes.
