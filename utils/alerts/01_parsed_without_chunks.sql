-- Documents parsed with SUCCESS and meant for the RAG, but with no chunk in `chunks` after a 4 h grace period
-- (the index sync runs after the parse; a document stuck here is invisible to the chatbot).
-- Alert condition: docs_without_chunks > 0.
WITH missing AS (
  SELECT pf.IDDOC, pf.ref, pf.titre, pf.type_document, pf.source_file_extension AS file_type, pf.parser_strategy,
         pf.chunking_strategy, pf.ingestion_timestamp
  FROM dev_landingzone.qualibot.processed_files pf
  WHERE pf.parse_status = 'SUCCESS'
    AND pf.include_in_rag = true
    AND pf.filtered_by_date = false
    AND pf.ingestion_timestamp >= current_date() - INTERVAL 7 DAY
    AND pf.ingestion_timestamp < current_timestamp() - INTERVAL 4 HOUR
    AND NOT EXISTS (SELECT 1 FROM dev_landingzone.qualibot.chunks c WHERE c.IDDOC = pf.IDDOC)
)
SELECT
  COUNT(*) AS docs_without_chunks,
  ARRAY_JOIN(SLICE(COLLECT_LIST(CONCAT(ref, ' (', file_type, ')')), 1, 10), ', ') AS sample_refs
FROM missing
