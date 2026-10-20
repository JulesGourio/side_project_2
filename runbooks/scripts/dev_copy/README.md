# dev_copy

One-shot scripts, run by hand and never deployed: nothing in the app or in a bundle job imports or runs them. They copy the
chunk tables and the parsing state of `uat_proj.qualibot` into a staging volume, from which the DEV environment is loaded, then
build its Vector Search endpoint and index. The DEV parsing pipeline does not need a full run.

## Files

| File | Kind | What it does |
|---|---|---|
| `export_uat_to_staging.py` | Databricks notebook (UAT workspace) | Copies the tables listed in `TABLES` to `OUTPUT_DIR`, copies the LibreOffice archive and writes `manifest.json` |
| `export_uat_submit.json` | Data file (body of `jobs submit`) | One `notebook_task` that runs the export notebook |
| `import_staging_to_dev_job.py` | Databricks notebook (DEV workspace) | Reads `manifest.json` from the staging volume and recreates each table as `dev_proj.qualibot.<table>`. Copies the LibreOffice archive |
| `ensure_vector_search_endpoint.py` | Databricks notebook (DEV workspace) | Creates the STANDARD Vector Search endpoint when it is missing and waits until it is ONLINE |
| `copy_uat_to_dev_submit.json` | Data file (body of `jobs submit`) | Runs the three steps in order: import, endpoint, then the index sync of `utils/parsing_pipeline/update_proj_vector_indexes.py` from the DEV bundle files |

## Rules

- A table that already exists is left as it is, unless `overwrite` is `true` (default `false`). Replacing `chunks` under a
  live index breaks the incremental sync of that index.
- The import raises an error when the row count of a loaded table differs from the manifest.
- The endpoint step leaves an existing endpoint untouched. It raises an error when the endpoint is OFFLINE, RED_STATE or
  DELETED, or when it is not ONLINE after `wait_minutes`.
- `copy_uat_to_dev_submit.json` runs as the person who submits it, not as a service principal: the DEV SP has no READ VOLUME on
  the UAT staging volume. The DEV bundle must be deployed first, because the index step reads its notebook from the bundle files.
- The grants that let the DEV app and pipeline read these objects are in [`../grants/`](../grants/README.md).
