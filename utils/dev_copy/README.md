# dev_copy

Loads the UAT snapshot into the project schema of the DEV target and prepares its Vector Search endpoint and index.
Three notebooks run as the tasks of the job `Z_1_Qualibot_Copy_Uat_To_Dev`, defined in
[`resources/dev_copy.yml`](../../resources/dev_copy.yml) for the `dev` target only.

Parent: [`utils/`](../README.md). The snapshot is written by the one-shot notebook `export_uat_to_staging.py` in the UAT workspace, run by hand.

## Files

| File | What it does | Runs as |
|---|---|---|
| `import_staging_to_dev_job.py` | Reads `manifest.json` from the staging volume and recreates each table as `<pipeline_write_env>_proj.qualibot.<table>`. Copies the LibreOffice archive of the snapshot | Task `1_import_tables` (notebook) |
| `ensure_vector_search_endpoint.py` | Creates the STANDARD Vector Search endpoint when it is missing and waits until it is ONLINE | Task `2_vector_search_endpoint` (notebook) |
| `../parsing_pipeline/update_proj_vector_indexes.py` | Creates or syncs the indexes of `parsing_vector_search_indexes` (the shared parsing-pipeline notebook) | Task `3_sync_indexes`, with `build_chunks_full` set to `false` |

The job runs the tasks in that order, each after the previous one. It does not run the parsing pipeline.

## Rules

- A table that already exists is left as it is, unless the job parameter `overwrite` is `true` (default `false`).
  Replacing `chunks` under a live index breaks the incremental sync of that index.
- After the copy, each table has a 60-day retention for deleted files and for the log. `chunks` also has Change Data
  Feed enabled, which a Delta Sync index needs.
- The import raises an error when the row count of a loaded table differs from the manifest.
- The endpoint task leaves an existing endpoint untouched. It raises an error when the endpoint is OFFLINE, RED_STATE or
  DELETED, or when it is not ONLINE after `wait_minutes` (30 in the job).
- The job exists in the `dev` target only. Its schema, endpoint and index names are the DEV ones, set by the job
  parameters in `resources/dev_copy.yml`.
- The grants that let the DEV app and pipeline read these objects are granted by the notebooks of
  [`utils/grants/`](../grants/README.md).
