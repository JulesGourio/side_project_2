# alerts

Six SQL queries that watch the parsing, the chat and the jobs. Each file is the query of one Databricks SQL alert.
The query returns a column that triggers the alert when its condition holds. No job runs these files.

## Files

| File | Triggers when (window) | Column and condition | Table read |
|---|---|---|---|
| `01_parsed_without_chunks.sql` | A document `SUCCESS` meant for the RAG has no passage in `chunks` 4 hours after its parse (documents of the last 7 days) | `docs_without_chunks` > 0 | `parsed_documents`, `chunks` |
| `02_parse_errors.sql` | The latest parse attempt of a document is `ERROR` or `TIMEOUT` (last 24 hours). A document that succeeded on a retry is not counted | `docs_in_error` > 0 | `parsed_documents` |
| `03_chat_failure_rate.sql` | In the last hour, too many chat answers are errors or have no source. The threshold depends on the traffic; below 5 answers nothing triggers | `alert_triggered` = 1 | `chat_messages` |
| `04_job_failures.sql` | A job whose name contains `_Qualibot_` has a failed run in the last 24 hours | `failed_runs` > 0 | `system.lakeflow` |
| `05_new_error_kinds.sql` | An error fingerprint (route, step, type and place in the code) appears in the last 24 hours and was never seen before. Warnings are left out | `new_error_kinds` > 0 | `app_errors` |
| `06_chat_degraded_turns.sql` | Over 24 hours, at least 20 % of the chat turns (and at least 10 turns) have `status` = `degraded` | `alert_triggered` = 1 | `chat_turns` |

The context columns (`sample_refs`, `failed_jobs`, `sample_errors`, `top_causes`) feed the message template of the
alert. They list the documents, jobs, errors or causes concerned.

## Rules

- The queries name the schema `dev_proj.qualibot`. It is the only environment-specific part of the files.
- `03`, `05` and `06` read the Delta copies of `chat_messages`, `app_errors` and `chat_turns`, which the export and
  import jobs of [`lakebase_sync/`](../lakebase_sync/README.md) write. They are only as fresh as those copies. While
  a table does not exist (no row imported), the query fails with "table not found".
- `04` needs SELECT on `system.lakeflow`.
- `01` and `02` read `parsed_documents`: they see the documents parsed by the daily chain only
  (see [`parsing_pipeline/`](../parsing_pipeline/README.md)).
- Not covered: the freshness of the Intraqual sources (task `1_categories` fails in that case, and `04` reports the
  failed run) and the health of the Vector Search indexes.
- The alert schedule, the SQL warehouse, the recipients and the message template are set in Databricks SQL. The
  repository holds none of them.
