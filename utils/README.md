# utils

Scripts and jobs that run outside the web application: the document parsing pipeline, the manual deployment of the
application, the chat alerts, the chat and search evaluation, the Unity Catalog grants, the Lakebase copies, and the
start and stop of the applications. One folder per concern.

The application is in [`server/`](../server/) and [`client/`](../client/); see the [root README](../README.md).

## Files

| Folder or file | Content | Runs as |
|---|---|---|
| [`parsing_pipeline/`](parsing_pipeline/README.md) | Daily chain of parsing, passage splitting and indexing (tasks `1_categories` to `6_update_kb_metadata`) | Tasks of the job `parsing_pipeline` (`resources/parsing_pipeline.job.yml`) |
| [`deploy/`](deploy/) | `deploy_qualibot.ps1`: deploys the application code. `bitbucket_deploy.sh`: one Bitbucket step (profile, bundle deploy). `render_target_config_env.py`: writes `target_config.env` from `target_env.json`. `target_env.json`: application settings per target | `deploy_qualibot.ps1` from PowerShell; `bitbucket_deploy.sh` from the Bitbucket pipeline (`bitbucket-pipelines.yml`), which runs `render_target_config_env.py` |
| [`alerts/`](alerts/README.md) | Six SQL queries of alerts on the parsing, the chat and the jobs | Pasted as the query of a Databricks SQL alert; no job runs them |
| [`app_mgmt/`](app_mgmt/) | `stop_start_app_job.py`: stop and start of the Databricks Apps on a schedule | Tasks of the jobs in `resources/app_schedules.yml` |
| [`lakebase_sync/`](lakebase_sync/README.md) | Export of the Lakebase database to a volume, import into DEV, JSON export and import for PROD | Tasks of the jobs in `resources/lakebase_sync.yml`; CLI steps of `bitbucket-pipelines.yml` |
| [`dev_copy/`](dev_copy/) | Import of the UAT snapshot into DEV, creation of the Vector Search endpoint | Tasks of the job `copy_uat_to_dev` (`resources/dev_copy.yml`) |
| [`grants/`](grants/) | Unity Catalog grants of the service principals of the application and of the pipeline, and volume access | Tasks of the jobs in `resources/grants.yml` |
| [`evaluation/`](evaluation/) | Chat and search evaluation: load tests, answer comparison, retrieval evaluation, MLflow experiment and prompts, trace scoring, vision endpoint check | Jobs in `resources/chat_mlflow.yml` (`create_chat_experiment.py`, `register_chat_prompts.py`, `score_chat_traces.py`); the others by hand |
| [`ops_config.py`](ops_config.py) | Shared settings: CLI profiles, Lakebase projects per environment, connection helpers, table order of the PROD copy | Imported by `lakebase_sync/` and by the one-shot scripts |

## How each script runs

Notebooks (files that start with `# Databricks notebook source`) are tasks of the jobs listed above. They take their
parameters from the job (`base_parameters` or the widgets). They do not run from the command line.

Command-line scripts run from the repository root:

```
python utils/deploy/render_target_config_env.py <dev|uat|prod> [output_path]
python utils/lakebase_sync/export_lakebase_to_json.py --profile <cli-profile> [--database doccompare] [--out-dir staging/lakebase]
python utils/lakebase_sync/import_lakebase_from_json.py --profile <cli-profile> [--database doccompare] [--in-dir staging/lakebase]
python utils/evaluation/check_vision_endpoints.py --profile <cli-profile> --images <folder> [--endpoints <a,b>] [--question <text>] [--timeout 120]
python utils/evaluation/create_chat_experiment.py --profile <cli-profile> --app qualibot [--experiment /Shared/qualibot/chat_turns] [--uc-schema <catalog.schema> --warehouse-id <id>] [--managers <group,group>]
python utils/evaluation/register_chat_prompts.py --profile <cli-profile> --schema <catalog.schema> [--experiment /Shared/qualibot/chat_turns] [--app-root <folder>]
```

- `render_target_config_env.py` writes `target_config.env` (default path) with one `export` line per setting.
- `export_lakebase_to_json.py` and `import_lakebase_from_json.py` default to the database `doccompare` and to the folder `staging/lakebase`.
- `check_vision_endpoints.py` exits with 0 when every request was answered, 1 when one failed, 2 on a bad folder or an empty list.
- `create_chat_experiment.py` and `register_chat_prompts.py` run as Python tasks of the job without `--profile`.
  `register_chat_prompts.py` needs `--app-root` inside a job, where it runs through `exec` and has no `__file__`.

PowerShell, from the repository root:

```
.\utils\deploy\deploy_qualibot.ps1 -AppEnv <dev|uat|prod> [-SkipBuild] [-SyncOnly] [-Infra]
```

`-AppEnv` defaults to `uat`. `-SkipBuild` skips the frontend build, `-SyncOnly` only uploads the code, and
`-Infra` runs the full bundle deployment (app permissions, volume bindings), which needs MANAGE on the catalog.

## Settings

- `target_env.json` holds the application settings per target (`dev`, `uat`, `prod`). A key that a target
  does not set keeps its value from `app.yaml`. `render_target_config_env.py` turns one target into `target_config.env`.
- `ops_config.py` holds the Databricks CLI profiles, the Lakebase project (`qualibot`, branch `production`,
  endpoint `primary`) and the database of each environment.

## Rules

- The Lakebase copies to PROD replace the rows of each table of the target database: `import_lakebase_from_json.py`
  runs one `DELETE` and one `INSERT` per table in a single transaction, so a failure rolls back the whole import.
- The import writes the `id` values as they are and sets each table's sequence to its maximum `id`.
- The tables of `MIGRATE_TO_PROD_TABLES_TO_SKIP` (`llm_requests`) are not copied to PROD. The other tables are copied in
  the order of `MIGRATE_TO_PROD_TABLE_ORDER`, parents first.
- The exported files hold user data (questions, answers, identifiers). They stay in the staging folders and volumes.
- The grant jobs cover the volumes, the indexes, the tables and the project schemas. They never grant on serving
  endpoints: every workspace user already queries them.
- `stop_start_app_job.py` reads the state of each application before it acts, and skips an application that is
  already in the requested state.
- The parsing pipeline (`parsing_pipeline/`) is described in its own README.
