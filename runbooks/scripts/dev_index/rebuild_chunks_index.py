# Databricks notebook source
# One-shot: rebuild dev_proj.qualibot.chunks_index. Its sync reads the Change Data Feed of chunks and fails for good once
# the schema of chunks has changed incompatibly (DELTA_CHANGE_DATA_FEED_INCOMPATIBLE_SCHEMA_CHANGE); a new index starts
# from the current table. Submitted as the DEV job service principal, which created the index and owns its tables.

import logging
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    PipelineType,
    VectorIndexType,
)

logger = logging.getLogger("rebuild_chunks_index")
logging.basicConfig(level=logging.INFO)

INDEX = "dev_proj.qualibot.chunks_index"
SOURCE_TABLE = "dev_proj.qualibot.chunks"
WRITEBACK_TABLE = f"{INDEX}_writeback_table"
ENDPOINT = "qualibot"
EMBEDDING_MODEL = "databricks-qwen3-embedding-0-6b"
DELETE_WAIT_MINUTES = 10
SYNC_WAIT_MINUTES = 120

w = WorkspaceClient()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Delete the index
# MAGIC The chat has no search on this index until the new one is online. The writeback table is dropped with it: a new index
# MAGIC cannot take over a table that exists, and it is written again by the first sync.

# COMMAND ----------

try:
    w.vector_search_indexes.delete_index(index_name=INDEX)
    logger.info("delete requested: %s", INDEX)
except NotFound:
    logger.info("index already absent: %s", INDEX)

deadline = time.time() + DELETE_WAIT_MINUTES * 60
while time.time() < deadline:
    try:
        w.vector_search_indexes.get_index(index_name=INDEX)
    except NotFound:
        break
    time.sleep(15)
else:
    raise RuntimeError(f"{INDEX} still exists after {DELETE_WAIT_MINUTES} min")

spark.sql(f"DROP TABLE IF EXISTS {WRITEBACK_TABLE}")
logger.info("index deleted, writeback table dropped (or absent): %s", WRITEBACK_TABLE)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create the index
# MAGIC Same specification as `update_proj_vector_indexes._create_index`. Creating it starts its first full sync.

# COMMAND ----------

w.vector_search_indexes.create_index(
    name=INDEX,
    endpoint_name=ENDPOINT,
    primary_key="chunk_id",
    index_type=VectorIndexType.DELTA_SYNC,
    delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
        source_table=SOURCE_TABLE,
        pipeline_type=PipelineType.TRIGGERED,
        embedding_source_columns=[
            EmbeddingSourceColumn(name="chunk_text", embedding_model_endpoint_name=EMBEDDING_MODEL)
        ],
        embedding_writeback_table=WRITEBACK_TABLE,
    ),
)
logger.info("index created: %s (source %s)", INDEX, SOURCE_TABLE)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Wait for the first sync and check the result
# MAGIC The index must hold as many rows as the table.

# COMMAND ----------

deadline = time.time() + SYNC_WAIT_MINUTES * 60
status = None
while time.time() < deadline:
    time.sleep(30)
    status = w.vector_search_indexes.get_index(index_name=INDEX).status
    state = (getattr(status, "detailed_state", None) or "").upper()
    message = getattr(status, "message", None) or ""
    logger.info("state=%s ready=%s indexed_rows=%s %s", state, status.ready, status.indexed_row_count, message)
    if "FAILED" in state or "OFFLINE" in state or "update failed" in message.lower():
        raise RuntimeError(f"{INDEX}: first sync failed — {message}")
    if status.ready and "PROVISIONING" not in state and "SYNC" not in state:
        break
else:
    raise RuntimeError(f"{INDEX}: first sync not finished after {SYNC_WAIT_MINUTES} min")

table_rows = spark.table(SOURCE_TABLE).count()
logger.info("table rows=%s indexed rows=%s", table_rows, status.indexed_row_count)
if status.indexed_row_count != table_rows:
    raise RuntimeError(f"{INDEX}: {status.indexed_row_count} rows indexed for {table_rows} in the table")

logger.info("done: %s rebuilt", INDEX)
