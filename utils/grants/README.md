# grants

Unity Catalog grants that the jobs of the bundle run: the privileges of the app's and the pipeline's service principals on
the project schema, the volumes and the indexes, and one volume privilege for one principal. The notebooks grant only;
they never revoke.

Parent: [`utils/`](../README.md). The jobs are defined in [`resources/grants.yml`](../../resources/grants.yml). The grant
rules of the project are in [`CLAUDE.md`](../../CLAUDE.md) (Databricks section).

## Files

| File | What it does | Runs as |
|---|---|---|
| `grant_app_access.py` | Grants the app's service principal USE CATALOG and USE SCHEMA on the volume schema and the project schema, READ and WRITE VOLUME on the listed volumes, SELECT on the listed indexes. For the pipeline's service principal: USE and CREATE TABLE, CREATE FUNCTION, EXECUTE, SELECT, MODIFY on the project schema, and SELECT, MODIFY on the listed tables | Task `grant_app_access` of `Z_1_Qualibot_Grant_App_Access_<target>` (`dev`, `qualibot-uat-test`) |
| `grant_volume_access_job.py` | Grants USE CATALOG, USE SCHEMA and one volume privilege (READ VOLUME or WRITE VOLUME) on one volume to one principal | Task `grant` of `Z_1_Qualibot_Grant_Volume_Access_<target>` (`qualibot-uat-test`) |

Both notebooks take their values from the job parameters, which `resources/grants.yml` sets per target.

## Rules

- `grant_app_access.py` builds its statements from comma-separated lists (`volumes`, `indexes`, `pipeline_tables`).
  Every statement runs. The failures are collected, and the notebook raises an error that lists them.
- When `pipeline_service_principal` is empty, no statement goes to the pipeline's principal. The `qualibot-uat-test`
  job sets it empty.
- The grants are idempotent: an existing grant is not an error, and no grant is removed.
- `grant_volume_access_job.py` checks `volume_permission` (READ VOLUME or WRITE VOLUME) and raises an error when a
  statement fails. The error says that the run identity lacks GRANT rights on the schema.
- The identity that runs a grant job must hold GRANT rights on the catalog. The notebooks do not elevate it.
- Grants are never given on serving endpoints (`CAN_QUERY`): every workspace user already queries them.
- The app's principal is not granted on the MLflow prompt registry: prompt versions reach the app as experiment tags.
