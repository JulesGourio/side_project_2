-- Error kinds (errors.fingerprint: same route + step + type + place in the code) seen in the last 24 h
-- and never before: a new bug or a new failing dependency, not the usual noise. Warnings left out.
-- Alert condition: new_error_kinds > 0. The custom template can list {{sample_errors}}.
-- Reads the Delta copy of errors (`lakebase_export_*` + import jobs): only as fresh as that copy.
WITH kinds AS (
  SELECT fingerprint,
         MIN(TRY_CAST(created_at AS TIMESTAMP)) AS first_seen,
         COUNT(*) AS occurrences,
         MAX_BY(CONCAT(COALESCE(endpoint, ''), ' | ', COALESCE(stage, ''), ' | ', COALESCE(error_type, ''), ' | ',
                       COALESCE(origin, ''), ' | ', LEFT(COALESCE(error_msg, ''), 120)),
                TRY_CAST(created_at AS TIMESTAMP)) AS sample
  FROM dev_landingzone.qualibot.errors
  WHERE fingerprint IS NOT NULL AND COALESCE(severity, 'error') = 'error'
  GROUP BY fingerprint
)
SELECT
  COUNT(*) AS new_error_kinds,
  SUM(occurrences) AS occurrences,
  CONCAT_WS(' || ', SLICE(COLLECT_LIST(sample), 1, 5)) AS sample_errors
FROM kinds
WHERE first_seen >= current_timestamp() - INTERVAL 1 DAY
