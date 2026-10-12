-- Documents whose LATEST parse attempt ended in ERROR or TIMEOUT during the last 24 h
-- (a document that failed then succeeded on a retry does not count).
-- Alert condition: docs_in_error > 0.
WITH latest AS (
  SELECT IDDOC, ref, source_file_extension AS file_type, parser_strategy, parse_status, parse_time_seconds,
         ingestion_timestamp, error_trace
  FROM dev_landingzone.qualibot.processed_files
  WHERE ingestion_timestamp >= current_timestamp() - INTERVAL 1 DAY
  QUALIFY ROW_NUMBER() OVER (PARTITION BY IDDOC ORDER BY ingestion_timestamp DESC) = 1
)
SELECT
  COUNT_IF(parse_status IN ('ERROR', 'TIMEOUT')) AS docs_in_error,
  COUNT_IF(parse_status = 'TIMEOUT') AS docs_in_timeout,
  COUNT(*) AS docs_attempted,
  ARRAY_JOIN(SLICE(COLLECT_LIST(CASE WHEN parse_status IN ('ERROR', 'TIMEOUT') THEN ref END), 1, 10), ', ') AS sample_refs
FROM latest
