# Databricks notebook source
# MAGIC %md
# MAGIC # Baseline index without code normalization (DEV)
# MAGIC
# MAGIC Builds `dev_proj.qualibot.chunks_baseline_index`, the index of Qualibot **before** code normalization, on the same
# MAGIC chunks as the app's `chunks_index`, so the retrieval evaluation compares two versions on the same corpus.
# MAGIC
# MAGIC | | `chunks_index` (new version) | `chunks_baseline_index` (baseline) |
# MAGIC |---|---|---|
# MAGIC | Embedded column | `chunk_preprocessed` (text + `[SF1027]` canonical forms) | `chunk_text` (text as parsed) |
# MAGIC | Keyword side | every text column, `chunk_preprocessed` and `cited_codes` included | `chunk_text`, `semantic_headers`, `REF`, `url` only |
# MAGIC
# MAGIC The keyword side of a Vector Search hybrid query covers every synced text column. `chunks_text_index` embeds
# MAGIC `chunk_text` but still syncs `chunk_preprocessed`, so its keywords see the canonical forms: it is not a baseline.
# MAGIC This index syncs only the columns listed in `SYNCED_COLUMNS`.
# MAGIC
# MAGIC Writes nothing in `chunks` and does not touch `chunks_index` (the app's index). Re-run it at any time: an existing
# MAGIC index is checked, then synced with the current `chunks`.
# MAGIC Identity: your own rights if you have `CREATE TABLE` on `dev_proj.qualibot` and `CAN USE` on the Vector Search
# MAGIC endpoint (cell 2 stops at once with a clear message otherwise; then the job runner must run it). The index then
# MAGIC belongs to whoever created it.

# COMMAND ----------

import logging
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    PipelineType,
    VectorIndexType,
)

logger = logging.getLogger("baseline_index")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False

SOURCE_TABLE = "dev_proj.qualibot.chunks"
REFERENCE_INDEX = "dev_proj.qualibot.chunks_index"
BASELINE_INDEX = "dev_proj.qualibot.chunks_baseline_index"
# The columns the chat reads and reranks on. chunk_preprocessed and cited_codes hold the canonical code forms: left out.
SYNCED_COLUMNS = ["chunk_id", "IDDOC", "REF", "division", "url", "semantic_headers", "chunk_text"]
EMBEDDED_COLUMN = "chunk_text"
FORBIDDEN_COLUMNS = {"chunk_preprocessed", "cited_codes"}
WAIT_MINUTES = 120

w = WorkspaceClient()

# COMMAND ----------

# 1. Same endpoint and embedding model as the app's index, so only the code normalization differs.
reference = w.vector_search_indexes.get_index(index_name=REFERENCE_INDEX)
ENDPOINT = reference.endpoint_name
EMBEDDING_MODEL = reference.delta_sync_index_spec.embedding_source_columns[0].embedding_model_endpoint_name
missing = set(SYNCED_COLUMNS) - set(spark.table(SOURCE_TABLE).columns)
assert not missing, f"{SOURCE_TABLE} has no column {sorted(missing)}"
SOURCE_ROWS = spark.table(SOURCE_TABLE).count()
logger.info(f"{REFERENCE_INDEX}: endpoint={ENDPOINT}, embedding model={EMBEDDING_MODEL}, "
            f"indexed rows={reference.status.indexed_row_count}; {SOURCE_TABLE}: {SOURCE_ROWS} rows")

# COMMAND ----------

# 2. Create the baseline index, or check the existing one and sync it.
try:
    existing = w.vector_search_indexes.get_index(index_name=BASELINE_INDEX)
except NotFound:
    existing = None

if existing is None:
    try:
        w.vector_search_indexes.create_index(
            name=BASELINE_INDEX,
            endpoint_name=ENDPOINT,
            primary_key="chunk_id",
            index_type=VectorIndexType.DELTA_SYNC,
            delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
                source_table=SOURCE_TABLE,
                pipeline_type=PipelineType.TRIGGERED,
                columns_to_sync=SYNCED_COLUMNS,
                embedding_source_columns=[
                    EmbeddingSourceColumn(name=EMBEDDED_COLUMN, embedding_model_endpoint_name=EMBEDDING_MODEL)
                ],
            ),
        )
    except PermissionDenied as exc:
        raise PermissionError(
            f"Your account cannot create {BASELINE_INDEX} ({exc}). It needs CREATE TABLE on dev_proj.qualibot and "
            f"CAN USE on the endpoint {ENDPOINT}: run this notebook as the job runner."
        ) from None
    logger.info(f"{BASELINE_INDEX}: created on {SOURCE_TABLE}, embeds {EMBEDDED_COLUMN}; its first sync is running")
else:
    spec = existing.delta_sync_index_spec
    synced = set(spec.columns_to_sync or [])
    embedded = [c.name for c in spec.embedding_source_columns or []]
    assert spec.source_table == SOURCE_TABLE, f"{BASELINE_INDEX} is built on {spec.source_table}, not {SOURCE_TABLE}"
    assert embedded == [EMBEDDED_COLUMN], f"{BASELINE_INDEX} embeds {embedded}, not {EMBEDDED_COLUMN}: delete it and run again"
    assert synced and not (synced & FORBIDDEN_COLUMNS), (
        f"{BASELINE_INDEX} syncs {sorted(synced) or 'every column'}: its keywords would see the canonical codes. "
        "Delete it and run again.")
    try:
        w.vector_search_indexes.sync_index(index_name=BASELINE_INDEX)
        logger.info(f"{BASELINE_INDEX}: exists with the right spec, sync triggered")
    except Exception as exc:
        if "not ready to sync" not in str(exc):
            raise
        logger.info(f"{BASELINE_INDEX}: a sync is already running, waiting for it")

# COMMAND ----------

# 3. Wait until the index holds every row of the table (the evaluation refuses to compare indexes of different sizes).
deadline = time.time() + WAIT_MINUTES * 60
while True:
    status = w.vector_search_indexes.get_index(index_name=BASELINE_INDEX).status
    rows = status.indexed_row_count or 0
    if status.ready and rows == SOURCE_ROWS:
        logger.info(f"{BASELINE_INDEX}: ready, {rows} rows = {SOURCE_TABLE}")
        break
    if time.time() > deadline:
        raise TimeoutError(f"{BASELINE_INDEX}: {rows}/{SOURCE_ROWS} rows after {WAIT_MINUTES} min "
                           f"({status.detailed_state}); run this cell again later")
    logger.info(f"{BASELINE_INDEX}: {rows}/{SOURCE_ROWS} rows, {status.detailed_state}")
    time.sleep(60)

# COMMAND ----------

# 4. Proof that the baseline cannot see the canonical forms: asking for chunk_preprocessed must fail, the text must not.
ok = w.vector_search_indexes.query_index(index_name=BASELINE_INDEX, columns=["chunk_id", "REF", "chunk_text"],
                                         query_text="SF-1027", query_type="HYBRID", num_results=3)
logger.info(f"baseline, 'SF-1027': {[row[1] for row in ok.result.data_array]}")
try:
    w.vector_search_indexes.query_index(index_name=BASELINE_INDEX, columns=["chunk_id", "chunk_preprocessed"],
                                        query_text="SF-1027", query_type="HYBRID", num_results=1)
    raise AssertionError(f"{BASELINE_INDEX} returns chunk_preprocessed: it is not a baseline")
except AssertionError:
    raise
except Exception as exc:
    logger.info(f"baseline has no chunk_preprocessed, as expected ({type(exc).__name__})")
