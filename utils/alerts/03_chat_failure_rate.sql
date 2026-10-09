-- Chatbot, last hour: HTTP errors (status = 'error') and answers without any source (sources_json IS NULL).
-- Thresholds depend on the traffic: at low volume one failure is a large percentage.
-- Alert condition: alert_triggered = 1.
-- Reads the Delta copy of chat_messages (`lakebase_export_*` + import jobs): the alert is only as fresh as that copy.
-- Turns the browser left before the end (status = 'aborted') are neither failures nor answers: left out.
-- Per-step detail of each turn (degraded steps, durations): chat_turns, see docs/lakebase_schema.md.
WITH stats AS (
  SELECT
    COUNT(*) AS total_messages,
    COUNT_IF(status = 'error') AS http_errors,
    COUNT_IF(status = 'ok' AND sources_json IS NULL) AS zero_doc_responses,
    ROUND(COUNT_IF(status = 'error') * 100.0 / NULLIF(COUNT(*), 0), 1) AS http_error_rate_pct,
    ROUND(COUNT_IF(status = 'ok' AND sources_json IS NULL) * 100.0 / NULLIF(COUNT(*), 0), 1) AS zero_doc_rate_pct
  FROM dev_landingzone.qualibot.chat_messages
  WHERE role = 'assistant' AND status != 'aborted'
    AND TRY_CAST(created_at AS TIMESTAMP) >= current_timestamp() - INTERVAL 1 HOUR
)
SELECT
  total_messages, http_errors, zero_doc_responses, http_error_rate_pct, zero_doc_rate_pct,
  CASE
    WHEN total_messages < 5 THEN 0
    WHEN total_messages <= 10 AND (http_error_rate_pct >= 15 OR zero_doc_rate_pct >= 50) THEN 1
    WHEN total_messages <= 25 AND (http_error_rate_pct >= 10 OR zero_doc_rate_pct >= 40) THEN 1
    WHEN total_messages > 25 AND (http_error_rate_pct >= 5 OR zero_doc_rate_pct >= 25) THEN 1
    ELSE 0
  END AS alert_triggered
FROM stats
