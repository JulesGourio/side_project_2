# Databricks notebook source
# One-shot, workspace PROD. Identity: the PROD job runner (service principal b82d3b5a-b633-4738-8dde-68bb2142cb02):
# Workflows > Create job > notebook task = this notebook > Run as = that service principal > Run now.
# Idempotent. Two fixes on prod_proj.qualibot before the first parsing run, then a consistency report. No other row is touched.

# COMMAND ----------

import logging

import pyspark.sql.functions as f

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("prod_fix")

S = "prod_proj.qualibot"
OUT_OF_SCOPE_IDDOCS = [7555, 21026, 22455, 24595, 24596, 25915, 26619, 26715, 26777, 26779, 27222]
EXPECTED_ROWS_AFTER = 124147 - 305

# COMMAND ----------

# Fix 1: image paths of the scanned-page passages point to dev_proj.
chunks = f"{S}.chunks"
n0 = spark.table(chunks).filter("semantic_headers LIKE '%/Volumes/dev_proj/%'").count()
if n0:
    spark.sql(
        f"UPDATE {chunks} SET semantic_headers = REPLACE(semantic_headers, '/Volumes/dev_proj/', '/Volumes/prod_proj/') "
        f"WHERE semantic_headers LIKE '%/Volumes/dev_proj/%'"
    )
n1 = spark.table(chunks).filter("semantic_headers LIKE '%/Volumes/dev_proj/%'").count()
logger.info(f"[FIX 1] dev_proj paths in chunks.semantic_headers: {n0} -> {n1}")

# COMMAND ----------

# Fix 2: out-of-scope documents still holding chunks (the pipeline never prunes the SKIPPED_REF_OUT_OF_SCOPE status).
ids = ",".join(str(i) for i in OUT_OF_SCOPE_IDDOCS)
for table in ("chunks", "chunks_archive", "image_metadata"):
    try:
        spark.sql(f"DELETE FROM {S}.{table} WHERE IDDOC IN ({ids})")
    except Exception as exc:
        logger.warning(f"[FIX 2] {table} skipped: {str(exc)[:100]}")
left = spark.table(chunks).filter(f"IDDOC IN ({ids})").count()
total = spark.table(chunks).count()
logger.info(f"[FIX 2] chunks left for the {len(OUT_OF_SCOPE_IDDOCS)} documents: {left} | total chunks: {total}")

# COMMAND ----------

# Report: documents of parsed_documents without chunks, and chunks without a usable parsed_documents row.
report = spark.sql(f"""
WITH ok AS (SELECT DISTINCT IDDOC FROM {S}.parsed_documents WHERE parse_status IN ('SUCCESS','IMAGES_PENDING','MISSING_CHUNKS')),
ch AS (SELECT DISTINCT IDDOC FROM {S}.chunks)
SELECT (SELECT count(*) FROM ok) AS docs_ok, (SELECT count(*) FROM ch) AS docs_with_chunks,
       (SELECT count(*) FROM ok LEFT ANTI JOIN ch USING (IDDOC)) AS ok_without_chunks,
       (SELECT count(*) FROM ch LEFT ANTI JOIN ok USING (IDDOC)) AS chunks_without_ok_row
""").collect()[0]
logger.info(f"[REPORT] {report.asDict()}")
logger.info(f"[CDF] {chunks} change data feed: " + str(
    [r.value for r in spark.sql(f"SHOW TBLPROPERTIES {chunks}").collect() if "ChangeDataFeed" in r.key]))

if n1 or left or total != EXPECTED_ROWS_AFTER:
    raise RuntimeError(f"Checks failed: dev paths {n1}, out-of-scope chunks left {left}, total {total} (expected {EXPECTED_ROWS_AFTER})")
logger.info("OK: both fixes applied and verified.")
