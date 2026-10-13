# lakebase_sync/

| File | Kind | Used by |
|---|---|---|
| `migrations.py` | Idempotent schema migrations, one list | `migrate_lakebase.py`, `migrate_lakebase_job.py` |
| `migrate_lakebase.py` | CLI: `--env DEV\|UAT\|UAT-TEST\|PROD` | by hand |
| `migrate_lakebase_job.py` | Notebook, defaults to `doccompare_test` and refuses `doccompare` in the UAT workspace | `migrate_lakebase_*` jobs |
| `export_lakebase_uat_to_volume_job.py` | Notebook: one JSON Lines file per table into the staging volume | `lakebase_export_uat_to_volume` job |
| `import_lakebase_uat_volume_to_dev_job.py` | Notebook: staging volume to Delta tables of `dev_landingzone.qualibot` (feeds the usage dashboard) | DEV job |
| `import_chatbot_tables_to_uat_job.py` | Notebook: chatbot tables to UAT | UAT job |
| `export_lakebase_to_json.py`, `import_lakebase_from_json.py` | CLI pair for the UAT to PROD data migration (CI steps, `--profile`) | `bitbucket-pipelines.yml` |
| `copy_Lakebase_tables.py` | CLI: pulls the exported JSON from the UAT volume and creates Delta tables in `dev_landingzone.qualibot` | by hand |

Connection settings and helpers shared by the scripts live in `utils/ops_config.py`.

## Copy UAT Lakebase tables to the DEV catalog

The workstation cannot reach Lakebase (port 5432 is blocked from the corporate network), so:

1. Run the UAT job `lakebase_export_uat_to_volume`.
2. `python utils/lakebase_sync/copy_Lakebase_tables.py` (needs `psycopg2-binary` in the project `.venv`; with the
   corporate proxy install it with `uv pip install --system-certs psycopg2-binary`).
   `--direct` connects to Lakebase straight from the workstation when port 5432 is open.

## DEV is never deployed by a script

`utils/deploy/deploy_qualibot.ps1` only knows `uat`, `uat-test` and `prod`. Changing anything under `targets.dev` in
`databricks.yml` (for example the `spark_env_vars` of `lakebase_import_uat_to_dev`) has no effect until someone runs
`databricks bundle deploy --target dev --profile DEV`. Check the result with `databricks jobs get <job_id> --profile DEV`.
