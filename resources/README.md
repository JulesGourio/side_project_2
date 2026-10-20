# resources

Bundle resource files of the Databricks bundle `qualibot`: the jobs of each target, one file per concern. The root
`databricks.yml` includes `resources/*.yml`, and every job is declared under `targets:` of the file that defines it.

Parent: [root README](../README.md). The tasks run the notebooks and scripts of [`utils/`](../utils/README.md).

## Files

| File | Jobs defined | Targets | Target variables read |
|---|---|---|---|
| `alerts.yml` | Six Databricks SQL alerts on the parsing, the chat and the jobs (`parsed_without_chunks`, `parse_errors`, `chat_failure_rate`, `job_failures`, `new_error_kinds`, `chat_degraded_turns`), deployed PAUSED | every target | `pipeline_write_env`, `alerts_warehouse_id`, `alerts_pause_status`, `notification_email` |
| `app_schedules.yml` | Jobs that stop and start the apps (`utils/app_mgmt/stop_start_app_job.py`): `D_1_Qualibot_Apps_Stop_Nightly_*`, `W_1_Qualibot_Stop_Weekend_*`, `W_1_Qualibot_Start_Weekend_*` | `dev`, `uat` | `app_name` |
| `chat_mlflow.yml` | `Z_2_Qualibot_Create_Chat_Experiment` (`create_chat_experiment.py`), `Z_3_Qualibot_Register_Chat_Prompts` (`register_chat_prompts.py`), `D_2_Qualibot_Score_Chat_Traces` (`score_chat_traces.py`, paused) | `dev`, `uat`, `prod` | `pipeline_write_env`, `bundle.name`, `bundle.target` |
| `dev_copy.yml` | `Z_1_Qualibot_Copy_Uat_To_Dev`, three tasks: table import, Vector Search endpoint, index sync | `dev` | `pipeline_write_env`, `parsing_vector_search_indexes`; job parameter `overwrite` |
| `grants.yml` | `Z_1_Qualibot_Grant_App_Access` (`grant_app_access.py`) and `Z_1_Qualibot_Grant_Volume_Access` (`grant_volume_access_job.py`) | `dev` | `pipeline_write_env`, `bundle.target` |
| `lakebase_sync.yml` | Lakebase export to the staging volume, then load of every exported table as Delta tables, `D_1_Qualibot_Lakebase_Export_To_Volume`: one job for every target | `dev`, `uat`, `prod` | `pipeline_write_env`, `lakebase_export_schedule_pause`, `lakebase_import_to_delta` |
| `parsing_pipeline.job.yml` | `D_1_Qualibot_Parsing_Pipeline`, six tasks `1_categories` to `6_update_kb_metadata` (see [`utils/parsing_pipeline/README.md`](../utils/parsing_pipeline/README.md)) | `dev`, `uat`, `prod` | `pipeline_write_env`, the `intraqual_*` and `parsing_*` variables, `bundle.target` |

The apps are declared in `databricks.yml`, not here. Every variable read here is declared in the `variables:` section
of `databricks.yml`. Job names carry no target suffix: each target has its own workspace or its own bundle state.

## Jobs and schedules

Times are in `Europe/Brussels` (the schedules of the apps and of the pipeline); `PAUSED` means the job runs only when started by hand.

| Job name | Resource key | Target | Trigger |
|---|---|---|---|
| `D_1_Qualibot_Parsing_Pipeline` | `parsing_pipeline` | `dev`, `uat`, `prod` | `dev`: `parsing_schedule_cron` (04:00), `PAUSED`; UAT and PROD: manual |
| `D_1_Qualibot_Lakebase_Export_To_Volume` | `lakebase_export` | `dev`, `uat`, `prod` | 02:00 and 14:00 (`lakebase_export_schedule_pause`): `PAUSED` on `dev` and `prod`, `UNPAUSED` on `uat`. The Delta load (`lakebase_import_to_delta`) is off on `dev` |
| `Z_1_Qualibot_Copy_Uat_To_Dev` | `copy_uat_to_dev` | `dev` | manual |
| `Z_1_Qualibot_Grant_App_Access` | `grant_app_access_dev` | `dev` | manual |
| `Z_2_Qualibot_Create_Chat_Experiment` | `create_chat_experiment_<target>` | `dev`, `uat`, `prod` | manual |
| `Z_3_Qualibot_Register_Chat_Prompts` | `register_chat_prompts_<target>` | `dev`, `uat`, `prod` | manual |
| `D_2_Qualibot_Score_Chat_Traces` | `score_chat_traces_<target>` | `dev`, `uat`, `prod` | 02:30, `PAUSED` |
| `D_1_Qualibot_Apps_Stop_Nightly_Dev` | `apps_stop_nightly_dev` | `dev` | 21:00 every day, `UNPAUSED` |
| `W_1_Qualibot_Stop_Weekend_Dev` / `W_1_Qualibot_Start_Weekend_Dev` | `qualibot_stop_weekend_dev` / `qualibot_start_weekend_dev` | `dev` | Friday 21:00 / Monday 07:00, `PAUSED` |
| `W_1_Qualibot_Stop_Weekend_Uat` / `W_1_Qualibot_Start_Weekend_Uat` | `qualibot_stop_weekend_uat` / `qualibot_start_weekend_uat` | `uat` | Friday 21:00 / Monday 07:00, `UNPAUSED` |

The job names carry no target suffix: a target gets its own workspace or its own bundle state, so the same name never clashes.

## Rules

- A job name follows `<D|W|M|F|Z>_<level>_<Domain>_<Purpose>`, optionally suffixed by `${bundle.target}`. The
  `test_every_job_name_follows_the_launch_level_domain_convention` guard checks it.
- Every `notebook_path` and `python_file` of these files exists. The guard `test_every_notebook_path_in_the_bundle_exists`
  checks it.
- Job parameters (`{{job.parameters.*}}`) are declared per job: `dry_run` (default `true`) on the scoring jobs,
  `overwrite` (default `false`) on the DEV copy, `app_service_principal` on the app grant job, and `service_principal`,
  `catalog`, `schema_name`, `volume_name`, `volume_permission` on the volume grant job.
- The alerts read `<pipeline_write_env>_proj.qualibot` on the warehouse of their target. `alerts_pause_status` (default `PAUSED`) switches them on per target. `job_failures` needs SELECT on `system.lakeflow`, granted by a metastore admin. `chat_failure_rate`, `new_error_kinds` and `chat_degraded_turns` read Delta copies of the chatbot tables, which only the UAT export fills: on DEV they report a missing table.
- The scoring jobs are `PAUSED`. The parsing job's schedule takes its pause state from `parsing_schedule_pause`.
- `run_as` is set per job. Most jobs run as a service principal. `Z_1_Qualibot_Copy_Uat_To_Dev` names a user.
- Tests of the bundle files and of the job names: [`tests/README.md`](../tests/README.md).
