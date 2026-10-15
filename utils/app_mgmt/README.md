# app_mgmt

Stops or starts Databricks Apps on a schedule: nightly and at the weekend for the non-production targets. One notebook
serves every such job.

Parent: [`utils/`](../README.md). The jobs are defined in [`resources/app_schedules.yml`](../../resources/app_schedules.yml).

## Files

| File | What it does | Runs as |
|---|---|---|
| `stop_start_app_job.py` | For each app of `app_names`, reads its compute state, then stops or starts it unless it is already in the requested state | Task `stop_apps` or `stop_app` or `start_app` of the jobs below (notebook) |

## Jobs that run it

| Job | Target | Action | App name |
|---|---|---|---|
| `D_1_Qualibot_Apps_Stop_Nightly_Dev` | `dev` | stop | `${var.app_name}` |
| `W_1_Qualibot_Stop_Weekend_Dev` | `dev` | stop | `${var.app_name}` |
| `W_1_Qualibot_Start_Weekend_Dev` | `dev` | start | `${var.app_name}` |
| `W_1_Qualibot_Stop_Weekend_Uat` | `qualibot-uat` | stop | `qualibot` |
| `W_1_Qualibot_Start_Weekend_Uat` | `qualibot-uat` | start | `qualibot` |

The jobs run in the workspace of their target, under the identity set in their `run_as`. The `dev` jobs and the UAT jobs
have different identities, and the identity needs CAN_MANAGE on each app it stops or starts.

## Parameters

- `action`: `stop` or `start` (dropdown, default `stop`).
- `app_names`: comma-separated app names. An empty value raises an error.

## Rules

- The notebook calls the SDK with the ambient identity (`WorkspaceClient()`, no profile).
- An app already in the requested state is skipped: `STOPPED` or `STOPPING` for a stop, `ACTIVE` or `STARTING` for a start.
  The SDK would reject that call.
- Every app is processed even when an earlier one fails. At the end, the notebook raises an error that names the apps
  that failed.
- The deploy script of [`utils/deploy/`](../deploy/README.md) starts a stopped app itself.
