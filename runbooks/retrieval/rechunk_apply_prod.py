# Databricks notebook source
# Rechunk PROD, step 2: replaces the content of prod_proj.qualibot.chunks with prod_proj.qualibot.chunks_test_tailles
# (written by rechunk_test_prod.py), then starts the sync of chunks_index. The previous version stays reachable by
# Delta time travel: the log prints the RESTORE command. One cell, serverless.
import logging
import sys

from databricks.sdk import WorkspaceClient

logger = logging.getLogger("rechunk_apply_prod")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
logger.propagate = False

CHUNKS = "prod_proj.qualibot.chunks"
NEW_CHUNKS = "prod_proj.qualibot.chunks_test_tailles"
INDEX = "prod_proj.qualibot.chunks_index"

columns = spark.table(CHUNKS).columns
missing = [c for c in columns if c not in spark.table(NEW_CHUNKS).columns]
if missing:
    raise RuntimeError(f"{NEW_CHUNKS} lacks the columns {missing}")
new_rows = spark.table(NEW_CHUNKS).count()
duplicates = new_rows - spark.table(NEW_CHUNKS).select("chunk_id").distinct().count()
if new_rows == 0 or duplicates:
    raise RuntimeError(f"{NEW_CHUNKS}: {new_rows} rows, {duplicates} duplicate chunk_id: nothing is written")

version = spark.sql(f"DESCRIBE HISTORY {CHUNKS} LIMIT 1").collect()[0]["version"]
logger.info("Rollback if needed: RESTORE TABLE %s TO VERSION AS OF %s", CHUNKS, version)
column_list = ", ".join(f"`{c}`" for c in columns)
spark.sql(f"INSERT OVERWRITE {CHUNKS} ({column_list}) SELECT {column_list} FROM {NEW_CHUNKS}")
logger.info("%s: %d rows written (version before: %s)", CHUNKS, spark.table(CHUNKS).count(), version)

try:
    WorkspaceClient().vector_search_indexes.sync_index(index_name=INDEX)
    logger.info("%s: sync started, it embeds every row again (about 30 to 60 min)", INDEX)
except Exception as exc:
    logger.warning("%s: sync not started with this account (%s); task 5_sync_index of the next parsing run does it",
                   INDEX, str(exc)[:200])
