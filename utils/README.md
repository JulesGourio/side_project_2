# utils/

Everything that runs outside the app, one folder per concern.

| Folder / file | What | Wired in |
|---|---|---|
| `parsing_pipeline/` | Daily parsing chain (`1_` to `6_`), see its README | `resources/parsing_pipeline.job.yml` |
| `deploy/` | `deploy_qualibot.ps1` (manual deploy), `render_target_config_env.py` + `target_env.json` (per-target app settings, shared by the script and the Bitbucket pipeline), Intraqual download / scrape / unzip / upload helpers, `deploy_sync_user_capabilities_uat_personal.py` (job deployed outside the bundle) | `bitbucket-pipelines.yml`, `app.yaml` |
| `alerts/` | SQL of the Databricks SQL alerts (created by hand, see its README) | by hand |
| `app_mgmt/` | `stop_start_app_job.py`: start / stop schedule of the Databricks Apps | `resources/app_schedules.yml` |
| `lakebase_sync/` | Lakebase export, import and schema migrations, see its README | `resources/lakebase_sync.yml`, `bitbucket-pipelines.yml` |
| `user_capabilities/` | `can_chat` / `can_compare` sync from group membership (job notebook + CLI twin) | `resources/user_capabilities.yml` |
| `dev_copy/` | Copy of the UAT corpus into DEV (export on UAT, import on DEV, Vector Search endpoint) | `resources/dev_copy.yml` |
| `grants/` | Unity Catalog grants for the app service principals | `resources/grants.yml` |
| `uat_setup/` | `setup_proj_schema.py`: deep-clones the parsing state tables into the project schema (`uat_proj.qualibot`) and grants read access | `resources/uat_setup.yml` |
| `evaluation/` | Chat evaluation tools, load tests, MLflow experiment / prompt registration, trace scoring; results in `docs/chat_tests.md` | `resources/chat_mlflow.yml` (the three setup / scoring notebooks) |
| `soffice_packaging/` | Portable LibreOffice build for the exact-layout PDF preview | `app.yaml` (archive path) |
| `compare_preview.py` | Offline DocCompare report of a document pair (no LLM call) | by hand, see `OPERATIONS.md` |
| `ops_config.py` | Shared by the Lakebase and user-capability scripts: CLI profiles, Lakebase project settings, `lakebase_connect*`, `json_serializer`, `sync_users_from_audit` | imported |

Notebooks that need `ops_config.py` reach it with `sys.path.insert(0, os.path.dirname(os.getcwd()))`
(a notebook's folder is its cwd and is on `sys.path`; one level up is `utils/`).

Conventions: `logger` instead of `print`, `dbutils.widgets.removeAll()` before the first widget of a notebook,
`try_cast` / `try_to_date` instead of `cast` / `to_date` on data columns.

## Gotchas worth keeping

- **Duplicated app off-hours jobs.** In the UAT workspace every app start / stop job may exist twice: one bundle-managed
  and one orphan left by an earlier deploy under another bundle root (no `deployment` metadata in its settings). Both
  can run on their own schedule. When auditing "what starts my app", list the workspace jobs and check
  `settings.deployment`; `bundle validate` alone does not show them. An org-wide job (`D_0_Stop_Databricks_Apps`)
  also stops every app nightly.
- **Stop / start idempotency** (`app_mgmt/stop_start_app_job.py`): the SDK calls raise `BadRequest` when the app is
  already in the target state (the CLI silently no-ops), so the notebook reads the app state first and skips the call.
- **Deploying to `qualibot-uat`** goes through the Bitbucket `deploy-uat` pipeline (identity `job-runner-sa-uat`). A local
  `bundle deploy` creates resources owned by the personal identity, which then blocks the pipeline's next run.
  A `databricks workspace import` of a bundle notebook must target the job's `notebook_path` exactly (no `.py`
  suffix), otherwise it silently creates a stray duplicate.
- **`qualibot-sync-user-capabilities-uat`** is deployed by a CoreDev human with
  `deploy/deploy_sync_user_capabilities_uat_personal.py` (outside the bundle), because the pipeline identity lacks
  `USE SCHEMA` on `system.access`. A manual-trigger dry-run copy (`...-uat-dryrun-jules`, created with `jobs create`)
  also lives outside the bundle state, so `bundle deploy` / `destroy` never touch it.
- **`render_target_config_env.py`** writes `target_config.env` with `\n` line endings on purpose: CRLF from a Windows
  deploy once put a trailing `\r` in every value and broke every URL built from them.
