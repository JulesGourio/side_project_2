-- Qualibot jobs (name contains `_Qualibot_`) whose last 24 h run did not succeed.
-- Needs SELECT on system.lakeflow (metastore admin grants it). Alert condition: failed_runs > 0.
WITH qualibot_jobs AS (
  SELECT job_id, name
  FROM system.lakeflow.jobs
  WHERE name LIKE '%\\_Qualibot\\_%'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY change_time DESC) = 1
),
runs AS (
  SELECT t.job_id, j.name, t.run_id, t.result_state, t.period_end_time
  FROM system.lakeflow.job_run_timeline t
  JOIN qualibot_jobs j USING (job_id)
  WHERE t.period_end_time >= current_timestamp() - INTERVAL 1 DAY
    AND t.result_state IS NOT NULL
)
SELECT
  COUNT_IF(result_state NOT IN ('SUCCEEDED', 'CANCELED')) AS failed_runs,
  ARRAY_JOIN(SLICE(COLLECT_SET(CASE WHEN result_state NOT IN ('SUCCEEDED', 'CANCELED') THEN name END), 1, 10), ', ') AS failed_jobs
FROM runs
