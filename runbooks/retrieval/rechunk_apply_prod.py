# Databricks notebook source
# Rechunk of PROD, switch: loads the Parquet files copied to the PROD staging volume into
# prod_proj.qualibot.chunks_union_prod, checks them against prod_proj.qualibot.chunks, replaces the content of chunks
# with them and syncs chunks_index (created again if it is missing). The previous chunks stay reachable by Delta time
# travel: the log prints the RESTORE command. Runs as the PROD job service principal, serverless, one cell.
import logging
import sys
import time

import pyspark.sql.functions as F
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest, EmbeddingSourceColumn, PipelineType, VectorIndexType,
)

logger = logging.getLogger("rechunk_apply_prod")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
logger.propagate = False

IMPORT_PATH = "/Volumes/prod_proj/qualibot/staging/chunks_union_prod"
NEW_CHUNKS = "prod_proj.qualibot.chunks_union_prod"
CHUNKS = "prod_proj.qualibot.chunks"
INDEX = "prod_proj.qualibot.chunks_index"
ENDPOINT = "qualibot"
EMBEDDING_MODEL = "databricks-qwen3-embedding-0-6b"
EMBEDDED_COLUMN = "chunk_preprocessed"
# Share of the current documents the new table must cover: below it the switch is refused.
MIN_DOC_COVERAGE = 0.99
SYNC_TIMEOUT_S = 3 * 3600

# 1. Staging table from the copied files.
imported = spark.read.parquet(IMPORT_PATH)
imported.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(NEW_CHUNKS)
new = spark.table(NEW_CHUNKS)
current = spark.table(CHUNKS)

# 2. Checks: same columns, unique non-null keys, text to embed, documents covered.
missing_columns = [c for c in current.columns if c not in new.columns]
new_rows = new.count()
problems = {
    "missing columns": missing_columns,
    "duplicate chunk_id": new_rows - new.select("chunk_id").distinct().count(),
    "null chunk_id or IDDOC": new.where(F.col("chunk_id").isNull() | F.col("IDDOC").isNull()).count(),
    f"empty {EMBEDDED_COLUMN}": new.where(F.coalesce(F.col(EMBEDDED_COLUMN), F.lit("")) == "").count(),
}
current_docs = current.select("IDDOC").distinct()
covered = current_docs.join(new.select("IDDOC").distinct(), "IDDOC").count()
coverage = covered / max(current_docs.count(), 1)
logger.info("%s: %d rows; current %s: %d rows; documents covered: %.2f %%",
            NEW_CHUNKS, new_rows, CHUNKS, current.count(), 100 * coverage)
if any(problems.values()) or new_rows == 0 or coverage < MIN_DOC_COVERAGE:
    raise RuntimeError(f"Switch refused, {CHUNKS} unchanged: {problems}, coverage {coverage:.4f}")

# 3. Switch, with the version to restore.
version = spark.sql(f"DESCRIBE HISTORY {CHUNKS} LIMIT 1").collect()[0]["version"]
logger.info("Rollback if needed: RESTORE TABLE %s TO VERSION AS OF %s", CHUNKS, version)
column_list = ", ".join(f"`{c}`" for c in current.columns)
spark.sql(f"INSERT OVERWRITE {CHUNKS} ({column_list}) SELECT {column_list} FROM {NEW_CHUNKS}")
target_rows = spark.table(CHUNKS).count()
logger.info("%s: %d rows written", CHUNKS, target_rows)

# 4. Index: sync, or creation when it is missing.
w = WorkspaceClient()
try:
    w.vector_search_indexes.get_index(index_name=INDEX)
    w.vector_search_indexes.sync_index(index_name=INDEX)
    logger.info("%s: sync started", INDEX)
except NotFound:
    logger.info("%s missing: creation on endpoint %s", INDEX, ENDPOINT)
    w.vector_search_indexes.create_index(
        name=INDEX,
        endpoint_name=ENDPOINT,
        primary_key="chunk_id",
        index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=CHUNKS,
            pipeline_type=PipelineType.TRIGGERED,
            embedding_source_columns=[EmbeddingSourceColumn(name=EMBEDDED_COLUMN,
                                                            embedding_model_endpoint_name=EMBEDDING_MODEL)],
            embedding_writeback_table=f"{INDEX}_writeback_table",
        ),
    )

started = time.time()
while True:
    status = w.vector_search_indexes.get_index(index_name=INDEX).status
    indexed = getattr(status, "indexed_row_count", None) or 0
    elapsed = time.time() - started
    logger.info("%s: ready=%s, %d / %d rows, %.0f min", INDEX, status.ready, indexed, target_rows, elapsed / 60)
    if status.ready and indexed == target_rows:
        logger.info("%s synced: the switch is done", INDEX)
        break
    if elapsed > SYNC_TIMEOUT_S:
        raise RuntimeError(f"{INDEX} not synced after {SYNC_TIMEOUT_S // 3600} h: check it in Catalog Explorer")
    time.sleep(60)
