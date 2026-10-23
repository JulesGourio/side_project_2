# lakebase_sync

Scripts that take the data out of the Lakebase database of the application (to a volume, or as JSON Lines files) and
load it elsewhere: Delta tables of `<env>_proj.qualibot`, and the Lakebase database of PROD (JSON import, Bitbucket step). The connection settings shared
with the other folders (CLI profiles, Lakebase projects, databases per environment, table order) are in
[`ops_config.py`](../ops_config.py).

One-shot migrations and one-off table copies are run by hand and never deployed.

## Files

| File | Role | Run by |
|---|---|---|
| `export_lakebase_to_volume_job.py` | Notebook: one table becomes one JSON Lines file in the staging volume. Parameters: project, branch, endpoint, database, output volume, tables to skip | Task `export_lakebase_to_volume` of `lakebase_export` (every target) |
| `import_lakebase_tables_to_delta_job.py` | Notebook: loads every table of the staging folder (chat, compare, impact search, users, caches, request logs, errors) into a Delta table of the same name in `<env>_proj.qualibot`. Does nothing when its `ENABLED` parameter is not `true` | Task `import_lakebase_tables_to_delta` of `lakebase_export`, after the export |
| `sync_user_capabilities_job.py` | Notebook: reads the latest add/remove event per (visitor, tracked group) in `system.access.audit` and writes `groups`, `can_chat`, `can_compare` and `groups_synced_at` to `users`; the app's access check reads them. For each (visitor, group) the audit wins; pairs older than the audit's retention keep the stored value. `DRY_RUN=true` only logs the result. The job identity needs SELECT on `system.access.audit` | Job `lakebase_user_capabilities`, every two hours, separate from the export |
| `user_capabilities.py` | Module of that notebook: tracked groups, audit query, merge and write | Imported by `sync_user_capabilities_job.py` |

The jobs are defined in [`resources/lakebase_sync.yml`](../../resources/lakebase_sync.yml). The staging volume is declared
in the bundle of every target. After a complete export, the JSON files that no exported table wrote are removed, so a table
that is skipped or dropped is not loaded again from an old file.

## Notebook parameters

- `export_lakebase_to_volume_job.py` takes the widgets `LAKEBASE_PROJECT_ID`, `LAKEBASE_BRANCH`,
  `LAKEBASE_ENDPOINT`, `LAKEBASE_DATABASE`, `OUTPUT_VOLUME_CATALOG`, `OUTPUT_VOLUME_SCHEMA`, `OUTPUT_VOLUME_NAME`
  and `TABLES_TO_SKIP` (default empty: every table is exported). `OUTPUT_VOLUME_CATALOG` is required (`<env>_proj`). It writes to `/Volumes/<catalog>/<schema>/<volume>/lakebase_export`.
  The export tasks run on serverless compute: classic compute cannot reach the Lakebase endpoint.
- `import_lakebase_tables_to_delta_job.py` takes the widgets `CATALOG` (required, `<env>_proj`), `SCHEMA`, `VOLUME_NAME` and
  `ENABLED`. It reads `/Volumes/<CATALOG>/<SCHEMA>/<VOLUME_NAME>/lakebase_export`, loads every exported table and skips a table
  with 0 rows. Each table it writes gets the property `lakebase_export = true` (the six chat tables, loaded before it existed,
  are exempt): a Delta table of the same name that does not carry it is left untouched and the task fails at the end with
  its name. The job passes `ENABLED` from `lakebase_import_to_delta` of its target. Each target imports only the Lakebase of its own environment.

## Lakebase environments

The environments known by `ops_config.py`:

| Environment | CLI profile | Database | Note |
|---|---|---|---|
| `UAT` | `UAT` | `doccompare` | Database of the UAT application |
| `DEV` | `DEV` | `doccompare` | Database of the DEV application |
| `PROD` | `PROD` | `doccompare` | Database of the PROD application |

All three use the Lakebase project `qualibot`, branch `production`, endpoint `primary`.

## Rules

- Each environment's export copies its own Lakebase to its own `<env>_proj.qualibot`. The only copy between environments is the UAT-to-PROD pair of Bitbucket steps "Sync prod data", started by hand.
  Without it, the next insert of the application would reuse an imported id.
- The JSON files hold user data (questions, answers, identifiers). They stay in the staging folders and volumes.
