# Databricks notebook source
# Rebuild of prod_proj.qualibot.chunks_index: deletes the index, rewrites chunks in many files from the Parquet files of
# the switch (same rows: many files let the index pipeline embed in parallel) and creates the index again with the
# spec of the parsing pipeline. The index is absent until the rebuild ends. Runs as the PROD job service principal,
# serverless, one cell.
import logging
import sys
import time

import pyspark.sql.functions as F
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest, EmbeddingSourceColumn, PipelineType, VectorIndexType,
)

logger = logging.getLogger("rebuild_index_prod")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
logger.propagate = False

IMPORT_PATH = "/Volumes/prod_proj/qualibot/staging/chunks_union_prod"
CHUNKS = "prod_proj.qualibot.chunks"
INDEX = "prod_proj.qualibot.chunks_index"
WRITEBACK = f"{INDEX}_writeback_table"
ENDPOINT = "qualibot"
EMBEDDING_MODEL = "databricks-qwen3-embedding-0-6b"
EMBEDDED_COLUMN = "chunk_preprocessed"
TARGET_FILES = 128
WRITE_PROPS = ["delta.autoOptimize.optimizeWrite", "delta.autoOptimize.autoCompact"]
DELETE_TIMEOUT_S = 20 * 60
BUILD_TIMEOUT_S = 6 * 3600

w = WorkspaceClient()

# 1. Delete the index and wait until it is gone.
try:
    w.vector_search_indexes.delete_index(index_name=INDEX)
    logger.info("%s: deletion requested", INDEX)
except NotFound:
    logger.info("%s: already absent", INDEX)
started = time.time()
while True:
    try:
        w.vector_search_indexes.get_index(index_name=INDEX)
    except NotFound:
        logger.info("%s: deleted", INDEX)
        break
    if time.time() - started > DELETE_TIMEOUT_S:
        raise RuntimeError(f"{INDEX} still present after {DELETE_TIMEOUT_S // 60} min: nothing else was changed")
    time.sleep(20)
spark.sql(f"DROP TABLE IF EXISTS {WRITEBACK}")
logger.info("%s: dropped if it existed", WRITEBACK)

# 2. Same rows, many files: optimized writes and auto compaction would merge them, so they are off for this write.
current = spark.table(CHUNKS)
expected_rows = current.count()
props = {r["key"]: r["value"] for r in spark.sql(f"SHOW TBLPROPERTIES {CHUNKS}").collect()}
spark.sql(f"ALTER TABLE {CHUNKS} SET TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'false', "
          f"'delta.autoOptimize.autoCompact' = 'false')")
new = spark.read.parquet(IMPORT_PATH)
if new.count() != expected_rows:
    raise RuntimeError(f"{IMPORT_PATH} has {new.count()} rows, {CHUNKS} has {expected_rows}: chunks left as it is")
(new.select(*[F.col(f.name).cast(f.dataType) for f in current.schema.fields]).repartition(TARGET_FILES)
 .write.insertInto(CHUNKS, overwrite=True))
for p in WRITE_PROPS:
    if p in props:
        spark.sql(f"ALTER TABLE {CHUNKS} SET TBLPROPERTIES ('{p}' = '{props[p]}')")
    else:
        spark.sql(f"ALTER TABLE {CHUNKS} UNSET TBLPROPERTIES IF EXISTS ('{p}')")
detail = spark.sql(f"DESCRIBE DETAIL {CHUNKS}").collect()[0]
rows = spark.table(CHUNKS).count()
logger.info("%s: %d rows in %d files", CHUNKS, rows, detail["numFiles"])

# 3. Index created again, then followed until every row is embedded.
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
        embedding_writeback_table=WRITEBACK,
    ),
)
logger.info("%s: creation requested", INDEX)
started = time.time()
while True:
    time.sleep(60)
    status = w.vector_search_indexes.get_index(index_name=INDEX).status
    indexed = getattr(status, "indexed_row_count", None) or 0
    elapsed = time.time() - started
    rate = indexed / elapsed if indexed else 0.0
    eta = (rows - indexed) / rate / 60 if rate else float("nan")
    logger.info("%s: ready=%s, %d / %d rows, %.0f min, %.1f rows/s, about %.0f min left, %s",
                INDEX, status.ready, indexed, rows, elapsed / 60, rate, eta, (status.message or "")[:150])
    if status.ready and indexed == rows:
        logger.info("%s rebuilt: %d rows", INDEX, indexed)
        break
    if elapsed > BUILD_TIMEOUT_S:
        raise RuntimeError(f"{INDEX} not built after {BUILD_TIMEOUT_S // 3600} h: check it in Catalog Explorer")
