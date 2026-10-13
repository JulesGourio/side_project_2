# Databricks notebook source
# MAGIC %md
# MAGIC # 03 — Sync Vector Search Index
# MAGIC
# MAGIC **Description:**
# MAGIC Last link of the chain: triggers a sync on the Delta Sync index
# MAGIC whose source table was just rewritten by tasks `1_parse` / `2_describe_images`.
# MAGIC
# MAGIC Structurally identical to `parsing_pipeline/5_Sync_Vector_Indexes`.
# MAGIC Self-provisioning: creates the index if it doesn't exist yet.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

import os
import time

dbutils.widgets.text("indexes", "", "Vector Search indexes (comma-separated)")
dbutils.widgets.text("wait_minutes", "45", "Max wait for sync completion (minutes, 0 = don't wait)")
dbutils.widgets.text("vector_search_endpoint", "qualibot", "Vector Search endpoint (for index creation)")
dbutils.widgets.text("embedding_model", "databricks-qwen3-embedding-0-6b", "Embedding model endpoint (for index creation)")
dbutils.widgets.text("catalog_schema", "", "catalog.schema of the chunk tables (for index creation)")
dbutils.widgets.text("table_suffix", "", "Suffix shared by the chunk tables and their indexes (for index creation)")

_raw = dbutils.widgets.get("indexes").strip() or os.environ.get("GENERIC_VECTOR_SEARCH_INDEXES", "")
INDEXES = [n.strip() for n in _raw.split(",") if n.strip()]
WAIT_MINUTES = int(dbutils.widgets.get("wait_minutes") or "0")
VECTOR_SEARCH_ENDPOINT = dbutils.widgets.get("vector_search_endpoint")
EMBEDDING_MODEL = dbutils.widgets.get("embedding_model")
CATALOG_SCHEMA = dbutils.widgets.get("catalog_schema")
TABLE_SUFFIX = dbutils.widgets.get("table_suffix")

if not INDEXES:
    print("No index to sync (`indexes` param empty) — nothing to do.")
    dbutils.notebook.exit("no_index")

print(f"{len(INDEXES)} index(es) to sync:")
for n in INDEXES:
    print(f"  - {n}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC
# MAGIC ## Create any missing index, then read current status

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    PipelineType,
    VectorIndexType,
)

# Single index/source pair for the generic pipeline.
KNOWN_SOURCE_TABLE = {
    f"{CATALOG_SCHEMA}.chunks_index{TABLE_SUFFIX}": f"{CATALOG_SCHEMA}.chunks{TABLE_SUFFIX}",
} if CATALOG_SCHEMA else {}

w = WorkspaceClient()


def _create_index(name, source_table):
    print(f"{name}: not found — creating (endpoint={VECTOR_SEARCH_ENDPOINT}, source={source_table})...")
    w.vector_search_indexes.create_index(
        name=name,
        endpoint_name=VECTOR_SEARCH_ENDPOINT,
        primary_key="chunk_id",
        index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=source_table,
            pipeline_type=PipelineType.TRIGGERED,
            embedding_source_columns=[
                EmbeddingSourceColumn(
                    name="chunk_text",
                    embedding_model_endpoint_name=EMBEDDING_MODEL,
                ),
            ],
        ),
    )
    print(f"{name}: creation triggered.")


before = {}
for name in INDEXES:
    try:
        idx = w.vector_search_indexes.get_index(index_name=name)
        st = idx.status
        state = (getattr(st, "detailed_state", None) or "").upper() if st else ""
        rows = getattr(st, "indexed_row_count", "?") if st else "?"
        before[name] = rows
        print(f"{name}: ready={st.ready} state={state} rows={rows}")
    except NotFound:
        source = KNOWN_SOURCE_TABLE.get(name)
        if source:
            _create_index(name, source)
            before[name] = 0
        else:
            print(f"{name}: not found AND not in KNOWN_SOURCE_TABLE — cannot auto-create, skipping.")
            INDEXES.remove(name)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Trigger the syncs

# COMMAND ----------

failed = []
for name in INDEXES:
    try:
        w.vector_search_indexes.sync_index(index_name=name)
        print(f"sync triggered: {name}")
    except Exception as exc:
        print(f"sync rejected: {name} — {exc}")
        failed.append((name, str(exc)))

if failed:
    raise RuntimeError(f"Failed to trigger {len(failed)} sync(s): {failed}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Wait for the syncs to finish

# COMMAND ----------

if WAIT_MINUTES <= 0:
    print("Wait disabled — syncs triggered, state not verified.")
else:
    deadline = time.time() + WAIT_MINUTES * 60
    pending = set(INDEXES)
    while pending and time.time() < deadline:
        time.sleep(30)
        for name in sorted(pending):
            idx = w.vector_search_indexes.get_index(index_name=name)
            st = idx.status
            state = (getattr(st, "detailed_state", None) or "").upper() if st else ""
            if "FAILED" in state or "OFFLINE" in state:
                msg = getattr(st, "message", None) or "no further detail from the SDK"
                raise RuntimeError(f"{name}: sync failed (state={state}) — {msg}")
            if st and st.ready and "PROVISIONING" not in state and "SYNC" not in state:
                rows = getattr(st, "indexed_row_count", "?")
                print(f"{name}: done — indexed_rows {before.get(name, '?')} -> {rows}")
                pending.discard(name)
                continue
            print(f"  ... {name}: state={state}")
    if pending:
        print(f"WARNING: Timeout after {WAIT_MINUTES} min. Still pending: {pending}")
    else:
        print("All syncs completed.")