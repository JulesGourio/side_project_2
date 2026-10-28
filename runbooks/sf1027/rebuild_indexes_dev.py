# Databricks notebook source
# MAGIC %md
# MAGIC # DEV — chunks_index on chunk_preprocessed (classic), chunks_text_index on chunk_text (next to it)
# MAGIC
# MAGIC Cells 1-2 are read only. **Cell 3 deletes `chunks_index`**: the chat has no search until the pipeline task `5_sync_index` has created it again (about 1 h). Run it only when ready.
# MAGIC Identity: your own rights if you own the index, otherwise the job runner (SP) must run this notebook (the SP created the index).

# COMMAND ----------

import logging

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from pyspark.sql import functions as F

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rebuild_indexes")

CATALOG = "dev_proj"
CHUNKS = f"{CATALOG}.qualibot.chunks"
INDEX = f"{CATALOG}.qualibot.chunks_index"
TEXT_INDEX = f"{CATALOG}.qualibot.chunks_text_index"
w = WorkspaceClient()

# COMMAND ----------

# 1. Does the table carry the preprocessed text on every row? (otherwise: rechunk first, an index on it would be incomplete)
df = spark.table(CHUNKS)
total = df.count()
nulls = df.filter(F.col("chunk_preprocessed").isNull() | (F.col("chunk_preprocessed") == "")).count() if "chunk_preprocessed" in df.columns else total
canon = df.filter(F.col("chunk_text").rlike(r"(?i)SF[\s\-]?1027") & F.col("chunk_preprocessed").contains("[SF1027]")).count() if "chunk_preprocessed" in df.columns else 0
logger.info(f"chunks: {total} rows, {nulls} without chunk_preprocessed, {canon} SF-1027 chunks carrying [SF1027]")
assert "chunk_preprocessed" in df.columns and nulls == 0, "chunk_preprocessed missing or empty: run the parsing job with run_mode=rechunk first."
assert canon > 0, "No SF-1027 chunk carries [SF1027]: the code normalization did not run on this table (old code in the rechunk?)."

# COMMAND ----------

# 2. What the indexes embed today.
for name in (INDEX, TEXT_INDEX):
    try:
        spec = w.vector_search_indexes.get_index(index_name=name).delta_sync_index_spec
        logger.info(f"{name}: embeds {[c.name for c in spec.embedding_source_columns]}")
    except NotFound:
        logger.info(f"{name}: absent")

# COMMAND ----------

# 3. DESTRUCTIVE: delete chunks_index when it does not embed chunk_preprocessed. The pipeline task creates it again, on chunk_preprocessed.
try:
    spec = w.vector_search_indexes.get_index(index_name=INDEX).delta_sync_index_spec
    if "chunk_preprocessed" in [c.name for c in spec.embedding_source_columns]:
        logger.info(f"{INDEX} already embeds chunk_preprocessed: nothing to delete.")
    else:
        w.vector_search_indexes.delete_index(index_name=INDEX)
        logger.info(f"{INDEX} deleted. Now run the task 5_sync_index (it creates chunks_index and chunks_text_index).")
except NotFound:
    logger.info(f"{INDEX} absent: run the task 5_sync_index.")
