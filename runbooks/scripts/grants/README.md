# grants

One-shot grant scripts, run by hand and never deployed: nothing in the app or in a bundle job imports or runs them.
The notebooks grant only, they never revoke, and every grant is idempotent.

## Files

| File | Kind | What it does |
|---|---|---|
| `grant_app_access.py` | Databricks notebook | Grants the DEV app's service principal USE CATALOG and USE SCHEMA on the volume and project schemas, READ and WRITE VOLUME on the listed volumes, SELECT on the listed indexes. Grants the DEV pipeline's service principal USE, CREATE TABLE, CREATE FUNCTION, EXECUTE, SELECT and MODIFY on the project schema, and SELECT and MODIFY on the listed tables. Its failures are collected and raised together |
| `grant_app_access_dev_submit.json` | Data file (body of `jobs submit`) | Runs the notebook above as the DEV job SP (`run_as`), with the DEV values |
| `grant_intraqual_gold.py` | Databricks notebook | Grants the UAT and DEV job service principals USE CATALOG on `prod_gold`, USE SCHEMA on `prod_gold.quality_system` and SELECT on `document_revisions_intraqual` and `category_ancestors_intraqual` (the Intraqual data asset tables the parsing pipeline reads) |
| `grant_intraqual_gold_submit.json` | Data file (body of `jobs submit`) | Runs the notebook above as the UAT job SP (`run_as`) |
| `grant_volume_access_job.py` | Databricks notebook | Grants USE CATALOG, USE SCHEMA and one volume privilege (READ VOLUME or WRITE VOLUME) on one volume to one principal. Not used by any job today |

## Rules

- The identity that runs a grant must hold GRANT rights on the catalog. The notebooks do not elevate it.
- Grants are never given on serving endpoints (`CAN_QUERY`): every workspace user already queries them.
- The app's principal is not granted on the MLflow prompt registry: prompt versions reach the app as experiment tags.
- When `pipeline_service_principal` is empty, no statement goes to the pipeline's principal.
