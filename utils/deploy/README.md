# deploy

Deploys the application code to a Databricks App, and writes the per-target settings the app reads at startup.

Parent: [`utils/`](../README.md). The root [README](../../README.md) describes the targets. The steps of each environment
are in [`runbooks/README.md`](../../runbooks/README.md).

## Files

| File | What it does | Runs as |
|---|---|---|
| `deploy_qualibot.ps1` | Builds the frontend, renders `target_config.env`, uploads the code (or deploys the bundle with `-Infra`), starts the app when it is stopped, and deploys it | PowerShell, from the repository root |
| `render_target_config_env.py` | Writes the settings of one target as `export NAME='value'` lines | CLI, also run by `bitbucket-pipelines.yml` for each target |
| `target_env.json` | The app's settings per target, the only source of these values | Read by both files above |

Command forms, from the repository root:

```
.\utils\deploy\deploy_qualibot.ps1 -AppEnv <dev|uat|prod|uat-test> [-SkipBuild] [-SyncOnly] [-Infra]
python utils/deploy/render_target_config_env.py <dev|uat|uat-test|prod> [output_path]
```

## Targets of the script

| `-AppEnv` | Bundle target | App | CLI profile |
|---|---|---|---|
| `dev` | `dev` | `qualibot` | `DEV` |
| `uat` (default) | `qualibot-uat` | `qualibot` | `UAT` |
| `prod` | `qualibot-prod` | `qualibot` | `qualibot-prod` |
| `uat-test` | `qualibot-uat-test` | `qualibot-uat-test` | `UAT` |

## What the script does

1. Builds the frontend with `bun` (skipped by `-SkipBuild` and `-SyncOnly`).
2. Renders `target_config.env` from `target_env.json`. Then either runs `databricks bundle deploy` (`-Infra`), or uploads
   the repository with `databricks sync`, minus the paths listed in the script (among them `runbooks/`,
   `archive/`, the client sources and the notebooks `*.ipynb`).
3. Reads the compute state of the app. `ACTIVE` or `STARTING` needs no action. Any other state makes the script start the
   app, except `qualibot-uat-test`, for which the script stops with an error.
4. Runs `databricks apps deploy` with the source path `/Workspace/Shared/.bundle/qualibot/<target>/files`. For `prod` the
   root is `/Workspace/.bundle`.

`-SyncOnly` ends after the upload. It skips the state check and the app deployment. `-Infra` needs MANAGE on the catalog.

## render_target_config_env.py

Writes one `export NAME='value'` line per key of the target, with LF line endings, to `target_config.env` by default. An
unknown target stops with the list of targets.

`target_env.json` holds one object per target. The keys differ per target: the volume paths, the index names, the chat
rewrite endpoint, the MLflow experiment, `CHAT_TRANSLATE_ONLY`, and `CAPS_BYPASS` (on `dev` only).

## Rules

- A key that a target does not set keeps the value of `app.yaml`.
- The index keys `CHAT_INDEX` and `COMPARE_IMPACT_INDEX` are set for `dev`, `uat`, `uat-test` and `prod`, and point at the
  project schema of that environment. Index names carry no version suffix. No `CHAT_ENDPOINT*` key is set.
- `CAPS_BYPASS` turns off the group check for every visitor, so it is `true` on `dev` only.
- `render_target_config_env.py` writes LF. A CRLF line ending would add a `\r` to every value.
- Deploying `uat` or `prod` needs an explicit approval, as stated in [`CLAUDE.md`](../../CLAUDE.md).
- The guards of `target_env.json` are in `tests/test_deploy_config.py` (see [`tests/README.md`](../../tests/README.md)).
