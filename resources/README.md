# resources

Bundle resource files of the Databricks bundle `qualibot`: the jobs of each target, one file per concern. The root
`databricks.yml` includes `resources/*.yml`, and every job is declared under `targets:` of the file that defines it.

Parent: [root README](../README.md). The tasks run the notebooks and scripts of [`utils/`](../utils/README.md).

## Files

| File | Jobs defined | Targets | Target variables read |
|---|---|---|---|
| `app_schedules.yml` | Six jobs that stop and start the apps (`utils/app_mgmt/stop_start_app_job.py`): `D_1_Qualibot_Apps_Stop_Nightly_*`, `W_1_Qualibot_Stop_Weekend_*`, `W_1_Qualibot_Start_Weekend_*` | `dev`, `qualibot-uat`, `qualibot-uat-test` | `app_name` |
| `chat_mlflow.yml` | `Z_2_Qualibot_Create_Chat_Experiment_<target>` (`create_chat_experiment.py`), `Z_3_Qualibot_Register_Chat_Prompts_<target>` (`register_chat_prompts.py`), `D_2_Qualibot_Score_Chat_Traces_<target>` (`score_chat_traces.py`, paused) | all four | `app_proj_catalog`, `app_proj_schema`, `bundle.name`, `bundle.target` |
| `dev_copy.yml` | `Z_1_Qualibot_Copy_Uat_To_Dev`, three tasks: table import, Vector Search endpoint, index sync | `dev` | `app_proj_catalog`, `app_proj_schema`, `parsing_table_suffix`, `parsing_vector_search_indexes`; job parameter `overwrite` |
| `grants.yml` | `Z_1_Qualibot_Grant_App_Access_<target>` (`grant_app_access.py`) and `Z_1_Qualibot_Grant_Volume_Access_<target>` (`grant_volume_access_job.py`) | `dev`, `qualibot-uat-test` (the volume job only on the second) | `app_proj_catalog`, `app_proj_schema`, `parsing_table_suffix`, `bundle.target` |
| `lakebase_sync.yml` | Lakebase export to a volume, `D_1_Qualibot_Lakebase_Export_To_Volume_<target>`; import into DEV, `D_1_Qualibot_Lakebase_Import_Uat_To_Dev`; a chatbot-table import task after the export on UAT | `dev`, `qualibot-uat` | `app_proj_catalog`, `app_proj_schema`, `bundle.target` |
| `parsing_pipeline.job.yml` | `D_1_Qualibot_Parsing_Pipeline_<target>`, six tasks `1_categories` to `6_update_kb_metadata` (see [`utils/parsing_pipeline/README.md`](../utils/parsing_pipeline/README.md)) | all four | `app_proj_catalog`, `app_proj_schema`, the `intraqual_*` and `parsing_*` variables, `bundle.target` |

The apps are declared in `databricks.yml`, not here. Every variable read here is declared in the `variables:` section
of `databricks.yml`.

## Rules

- A job name follows `<D|W|M|F|Z>_<level>_<Domain>_<Purpose>`, optionally suffixed by `${bundle.target}`. The
  `test_every_job_name_follows_the_launch_level_domain_convention` guard checks it.
- Every `notebook_path` and `python_file` of these files exists. The guard `test_every_notebook_path_in_the_bundle_exists`
  checks it.
- Job parameters (`{{job.parameters.*}}`) are declared per job: `dry_run` (default `true`) on the scoring jobs,
  `overwrite` (default `false`) on the DEV copy, `app_service_principal` on the app grant job, and `service_principal`,
  `catalog`, `schema_name`, `volume_name`, `volume_permission` on the volume grant job.
- The scoring jobs are `PAUSED`. The parsing job's schedule takes its pause state from `parsing_schedule_pause`.
- `run_as` is set per job. Most jobs run as a service principal. `Z_1_Qualibot_Copy_Uat_To_Dev` names a user, and
  `D_1_Qualibot_Lakebase_Import_Uat_To_Dev` has no `run_as`.
- Tests of the bundle files and of the job names: [`tests/README.md`](../tests/README.md).
