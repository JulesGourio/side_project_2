-- Chatbot, last 24 h: share of answered turns that did not run as configured (chat_turns.status = 'degraded':
-- rewrite failed, part of the Vector Search queries failed, fallback model, cut answer…) and the most frequent cause.
-- Alert condition: alert_triggered = 1. Aborted turns (browser left) are left out.
-- Reads the Delta copy of chat_turns (`lakebase_export_*` + import jobs): only as fresh as that copy.
WITH turns AS (
  SELECT status, warnings
  FROM dev_landingzone.qualibot.chat_turns
  WHERE status IN ('ok', 'degraded', 'error')
    AND TRY_CAST(created_at AS TIMESTAMP) >= current_timestamp() - INTERVAL 1 DAY
),
causes AS (
  SELECT w AS warning, COUNT(*) AS n FROM turns LATERAL VIEW EXPLODE(warnings) t AS w
  WHERE status = 'degraded' GROUP BY w
)
SELECT
  COUNT(*) AS turns,
  COUNT_IF(status = 'degraded') AS degraded,
  COUNT_IF(status = 'error') AS failed,
  ROUND(COUNT_IF(status = 'degraded') * 100.0 / NULLIF(COUNT(*), 0), 1) AS degraded_pct,
  (SELECT CONCAT_WS(', ', COLLECT_LIST(CONCAT(warning, ' (', n, ')'))) FROM (SELECT * FROM causes ORDER BY n DESC LIMIT 3))
    AS top_causes,
  CASE WHEN COUNT(*) >= 10 AND COUNT_IF(status = 'degraded') * 100.0 / COUNT(*) >= 20 THEN 1 ELSE 0 END AS alert_triggered
FROM turns
