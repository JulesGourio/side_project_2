# Databricks notebook source
# MAGIC %md
# MAGIC # Test index on `chunk_preprocessed`
# MAGIC Creates (first run) and syncs (every run) `dev_proj.qualibot.chunks_test_index`: same `chunks` table as `chunks_index`, but it embeds `chunk_preprocessed` (codes in their canonical spelling). Run it after each parsing run to refresh the test index. It never touches `chunks_index`.

# COMMAND ----------

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("create_test_index")

INDEX_NAME = "dev_proj.qualibot.chunks_test_index"
SOURCE_TABLE = "dev_proj.qualibot.chunks"
EMBEDDED_COLUMN = "chunk_preprocessed"
PRIMARY_KEY = "chunk_id"
ENDPOINT = "qualibot"
EMBEDDING_MODEL = "databricks-qwen3-embedding-0-6b"
WAIT_MINUTES = 45

w = WorkspaceClient()

# COMMAND ----------


def index_exists() -> bool:
    """Tell whether the test index already exists.

    :return: True when the index is found on the endpoint.
    """
    try:
        w.vector_search_indexes.get_index(index_name=INDEX_NAME)
        return True
    except NotFound:
        return False


def create_index() -> None:
    """Create the Delta Sync test index and wait until it leaves PROVISIONING (5 minutes at most)."""
    logger.info(f"{INDEX_NAME}: creating on {ENDPOINT}, source={SOURCE_TABLE}, embeds {EMBEDDED_COLUMN}")
    w.vector_search_indexes.create_index(
        name=INDEX_NAME,
        endpoint_name=ENDPOINT,
        primary_key=PRIMARY_KEY,
        index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=SOURCE_TABLE,
            pipeline_type=PipelineType.TRIGGERED,
            embedding_source_columns=[
                EmbeddingSourceColumn(name=EMBEDDED_COLUMN, embedding_model_endpoint_name=EMBEDDING_MODEL)
            ],
            embedding_writeback_table=f"{INDEX_NAME}_writeback_table",
        ),
    )
    deadline = time.time() + 5 * 60
    while time.time() < deadline:
        state = (getattr(w.vector_search_indexes.get_index(index_name=INDEX_NAME).status, "detailed_state", None) or "").upper()
        if "PROVISIONING" not in state:
            return
        time.sleep(15)
    logger.warning(f"{INDEX_NAME}: still PROVISIONING after 5 minutes")


def sync_index() -> None:
    """Trigger a sync and wait for it, up to WAIT_MINUTES."""
    try:
        w.vector_search_indexes.sync_index(index_name=INDEX_NAME)
    except Exception as exc:
        logger.warning(f"{INDEX_NAME}: sync not triggered ({exc}); a sync may already be running")
    deadline = time.time() + WAIT_MINUTES * 60
    while time.time() < deadline:
        status = w.vector_search_indexes.get_index(index_name=INDEX_NAME).status
        state = (getattr(status, "detailed_state", None) or "").upper()
        logger.info(f"{INDEX_NAME}: {state}, indexed_rows={getattr(status, 'indexed_row_count', None)}")
        if "FAILED" in state or "OFFLINE" in state:
            raise RuntimeError(f"{INDEX_NAME}: {state}")
        if state.startswith("ONLINE") and "UPDATING" not in state and "PIPELINE" not in state:
            return
        time.sleep(30)
    logger.warning(f"{INDEX_NAME}: sync still running after {WAIT_MINUTES} minutes")


# COMMAND ----------

if not index_exists():
    create_index()
sync_index()
logger.info(f"Done. To compare in the DEV app, set CHAT_INDEX={INDEX_NAME} (the normal index stays dev_proj.qualibot.chunks_index).")
