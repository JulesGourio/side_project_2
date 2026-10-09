# Alerts (Databricks SQL)

Four queries, one alert each. They read the DEV tables (`dev_landingzone.qualibot`): for UAT, replace the catalog by `uat_landingzone`
(there is no table suffix any more).

| File | Fires when | Condition | Schedule |
|---|---|---|---|
| `01_parsed_without_chunks.sql` | a document is `SUCCESS` but has no chunk 4 h later | `docs_without_chunks` > 0 | every day, after the parsing job |
| `02_parse_errors.sql` | latest parse of a document is `ERROR`/`TIMEOUT` (last 24 h) | `docs_in_error` > 0 | every day, after the parsing job |
| `03_chat_failure_rate.sql` | HTTP errors or answers without sources above a threshold that depends on the traffic | `alert_triggered` = 1 | hourly |
| `04_job_failures.sql` | a job named `*_Qualibot_*` failed in the last 24 h | `failed_runs` > 0 | every day |

## Create one (UI)

1. SQL Editor -> paste the query -> **Save** (name it `Qualibot - <purpose>`), pick a **serverless SQL warehouse**.
2. **Create alert** from the saved query (Alerts -> Create alert).
3. Trigger condition: *Value column* = the column of the table above, operator `>` and threshold `0` (`=` `1` for `alert_triggered`).
4. Schedule: as in the table (Europe/Paris). Warehouse: the same serverless one.
5. Notifications: add your e-mail as destination; in the custom template use the other columns (`{{sample_refs}}`, `{{failed_jobs}}`) so the mail already names the documents/jobs.
6. *Notify again*: once per day at most, otherwise a persistent problem sends a mail at every evaluation.

Alert 04 needs `USE SCHEMA` + `SELECT` on `system.lakeflow` for the owner of the alert; if the query fails with a permission error, ask a metastore admin.

## Limits

- 03 reads the Delta copy of `chat_messages`, which comes from the Lakebase export/import jobs: if that copy is only refreshed weekly the alert is blind in between. Real-time alerting needs the Lakebase table synced to Delta (or the query run on Lakebase).
- The alert owner's e-mail leaves the bundle: alerts are created by hand, not by `bundle deploy`.
- Not covered: Intraqual source freshness (already fails task `1_categories`, so alert 04 catches it) and Vector Search index health.
