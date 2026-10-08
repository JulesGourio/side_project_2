# Databricks notebook source
# MAGIC %md
# MAGIC # 05 — Sync Vector Search indexes
# MAGIC
# MAGIC **Description:**
# MAGIC Last link of the daily chain: triggers a sync on the Delta Sync indexes
# MAGIC whose source tables were just rewritten by tasks `3_parse`/`4_describe_images`.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC Indexes are `pipeline_type = TRIGGERED`: without this step, new chunks sit
# MAGIC in the Delta table and are never queryable — the rest of the chain would
# MAGIC have run for nothing. Replaces the local script
# MAGIC `utils/databricks_ops/vector_search_sync/resync_uat_index.py`, which
# MAGIC exported DEV to UAT by hand and still targeted the v1 tables/indexes —
# MAGIC this job runs in the workspace that owns the indexes, so no export needed.
# MAGIC
# MAGIC Self-provisioning: an index in `indexes` that doesn't exist yet is created
# MAGIC (not just synced) — this task runs `run_as` the environment's owning SP, so
# MAGIC it already holds whatever grant creating the index needs (no separate
# MAGIC manual step or elevated personal access required for a new environment).
# MAGIC
# MAGIC When `build_chunks_full` is on (or `chunks_full_index` is in `indexes`), the
# MAGIC table `chunks_full` (`chunks` + `chunks_archive`, every document whatever its
# MAGIC date — impact search only) is first refreshed by MERGE. The index itself is
# MAGIC only created/synced when it is listed in `indexes`. Otherwise this notebook is plain
# MAGIC Vector Search SDK calls — see Outputs for the sync trigger + wait logic.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `5_sync_index`)
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - `{catalog_schema}.chunks{table_suffix}` + `chunks_archive{table_suffix}` — only to refresh `chunks_full`
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - *(none)*
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{catalog_schema}.chunks_full{table_suffix}` — only when `build_chunks_full` is on or `chunks_full_index` is in `indexes`
# MAGIC - `{indexes}` (or `{PARSING_VECTOR_SEARCH_INDEXES}`) — comma-separated Vector Search index names, empty = nothing to do (the DEV case, no index deployed there)
# MAGIC - `{vector_search_endpoint}`/`{embedding_model}`/`{catalog_schema}`/`{table_suffix}` — only read if one of `indexes` doesn't exist yet and needs creating

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical Debt
# MAGIC
# MAGIC None.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

import os
import time

dbutils.widgets.text("indexes", "", "Vector Search indexes (comma-separated)")
dbutils.widgets.text("wait_minutes", "45", "Max wait for sync completion (minutes, 0 = don't wait)")
# Only used to auto-create a missing index (below) -- this task is serverless
# and doesn't get the PARSING_* env vars the rest of the pipeline reads
# config.py through, so these come in as their own widgets instead.
dbutils.widgets.text("vector_search_endpoint", "qualibot", "Vector Search endpoint (for index creation)")
dbutils.widgets.text("embedding_model", "databricks-qwen3-embedding-0-6b", "Embedding model endpoint (for index creation)")
dbutils.widgets.text("catalog_schema", "", "catalog.schema of the chunk tables (for index creation)")
dbutils.widgets.text("table_suffix", "", "Suffix shared by the chunk tables and their indexes (for index creation)")
dbutils.widgets.text("build_chunks_full", "false", "Refresh chunks_full (chunks + chunks_archive) even without its index")

_raw = dbutils.widgets.get("indexes").strip() or os.environ.get("PARSING_VECTOR_SEARCH_INDEXES", "")
INDEXES = [n.strip() for n in _raw.split(",") if n.strip()]
WAIT_MINUTES = int(dbutils.widgets.get("wait_minutes") or "0")
VECTOR_SEARCH_ENDPOINT = dbutils.widgets.get("vector_search_endpoint")
EMBEDDING_MODEL = dbutils.widgets.get("embedding_model")
CATALOG_SCHEMA = dbutils.widgets.get("catalog_schema")
TABLE_SUFFIX = dbutils.widgets.get("table_suffix")
BUILD_CHUNKS_FULL = dbutils.widgets.get("build_chunks_full").strip().lower() in ("1", "true", "yes")

print(f"{len(INDEXES)} index(es) to sync:")
for n in INDEXES:
    print(f"  - {n}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs

# COMMAND ----------

# MAGIC %md
# MAGIC ## Refresh `chunks_full` (impact-search source table)
# MAGIC
# MAGIC `chunks_full` = `chunks` (RAG, post-cutoff) + `chunks_archive` (pre-cutoff),
# MAGIC kept in sync by MERGE rather than overwrite so Change Data Feed only carries
# MAGIC the day's real changes and the index re-embeds just those rows.
# MAGIC Built when `build_chunks_full` is on, index or not: the table can be checked
# MAGIC in SQL before `chunks_full_index` is ever created.

# COMMAND ----------

FULL_INDEX = f"{CATALOG_SCHEMA}.chunks_full_index{TABLE_SUFFIX}"
FULL_TABLE = f"{CATALOG_SCHEMA}.chunks_full{TABLE_SUFFIX}"

if CATALOG_SCHEMA and (BUILD_CHUNKS_FULL or FULL_INDEX in INDEXES):
    from delta.tables import DeltaTable

    _sources = [t for t in (f"{CATALOG_SCHEMA}.chunks{TABLE_SUFFIX}", f"{CATALOG_SCHEMA}.chunks_archive{TABLE_SUFFIX}")
                if spark.catalog.tableExists(t)]
    df_src = spark.table(_sources[0])
    for _t in _sources[1:]:
        df_src = df_src.unionByName(spark.table(_t), allowMissingColumns=True)
    # Archive notices (config.ARCHIVE_NOTICE_CONTENT_TYPE) carry no content: nothing to judge an impact on.
    df_src = df_src.filter("NOT (chunk_content_type <=> 'archive_notice')")
    # A document that crossed the cutoff between runs could briefly sit in both tables.
    df_src = df_src.dropDuplicates(["chunk_id"])

    if not spark.catalog.tableExists(FULL_TABLE):
        df_src.write.format("delta").saveAsTable(FULL_TABLE)
        # 60-day history, like the _v1 chunk tables: a TRIGGERED sync can never resume once the CDF
        # it needs has aged out (VECTOR_SEARCH_SOURCE_HISTORY_OUT_OF_RETENTION, see databricks.yml).
        spark.sql(f"""
            ALTER TABLE {FULL_TABLE} SET TBLPROPERTIES (
                delta.enableChangeDataFeed = true,
                delta.deletedFileRetentionDuration = 'interval 60 days',
                delta.logRetentionDuration = 'interval 60 days'
            )
        """)
        print(f"Created {FULL_TABLE} from {_sources}")
    else:
        # New chunk columns (titre, type_document, langue… audit 2026-10) reach chunks_full too.
        spark.conf.set("spark.databricks.delta.schema.autoMerge.enabled", "true")
        _target_cols = set(spark.table(FULL_TABLE).columns)
        _changed = " OR ".join(f"NOT (t.`{c}` <=> s.`{c}`)" for c in df_src.columns
                               if c != "chunk_id" and c in _target_cols) or "true"
        (
            DeltaTable.forName(spark, FULL_TABLE).alias("t")
            .merge(df_src.alias("s"), "t.chunk_id = s.chunk_id")
            .whenMatchedUpdateAll(condition=_changed)
            .whenNotMatchedInsertAll()
            .whenNotMatchedBySourceDelete()
            .execute()
        )
        print(f"Merged {_sources} into {FULL_TABLE}")
    print(f"{FULL_TABLE}: {spark.table(FULL_TABLE).count()} chunks")

if not INDEXES:
    print("No index to sync (`indexes` param empty) — nothing more to do.")
    dbutils.notebook.exit("no_index")

# COMMAND ----------

# MAGIC %md
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

# Only these 4 index/source pairs are ever provisioned by this pipeline --
# an index name in `indexes` that isn't one of them is synced (existing
# behavior) but never auto-created, since we wouldn't know its source table.
# Mirrors config.py's TARGET_CHUNK_TABLE*/TARGET_CHUNK_TABLE_AS/_IS naming
# ({catalog_schema}.{name}{table_suffix}), rebuilt here from widgets instead
# of importing config.py -- see the widget comments above.
KNOWN_SOURCE_TABLE = {
    f"{CATALOG_SCHEMA}.chunks_index{TABLE_SUFFIX}": f"{CATALOG_SCHEMA}.chunks{TABLE_SUFFIX}",
    f"{CATALOG_SCHEMA}.chunks_as_index{TABLE_SUFFIX}": f"{CATALOG_SCHEMA}.src_chunks_as{TABLE_SUFFIX}",
    f"{CATALOG_SCHEMA}.chunks_is_index{TABLE_SUFFIX}": f"{CATALOG_SCHEMA}.src_chunks_is{TABLE_SUFFIX}",
    FULL_INDEX: FULL_TABLE,
} if CATALOG_SCHEMA else {}

w = WorkspaceClient()


def _create_index(name, source_table):
    print(f"{name}: not found -- creating (endpoint={VECTOR_SEARCH_ENDPOINT}, source={source_table})...")
    w.vector_search_indexes.create_index(
        name=name,
        endpoint_name=VECTOR_SEARCH_ENDPOINT,
        primary_key="chunk_id",
        index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=source_table,
            pipeline_type=PipelineType.TRIGGERED,
            embedding_source_columns=[
                EmbeddingSourceColumn(name="chunk_text", embedding_model_endpoint_name=EMBEDDING_MODEL)
            ],
        ),
    )
    # A brand-new index must clear PROVISIONING before it can accept a sync
    # trigger below -- wait up to 5 min rather than let that call race it.
    deadline = time.time() + 5 * 60
    while time.time() < deadline:
        idx = w.vector_search_indexes.get_index(index_name=name)
        state = (getattr(idx.status, "detailed_state", None) or "").upper()
        if "PROVISIONING" not in state:
            return
        time.sleep(15)
    print(f"{name}: still PROVISIONING after 5 min -- proceeding anyway, the sync trigger below may need a retry tomorrow.")


before = {}
for name in INDEXES:
    try:
        idx = w.vector_search_indexes.get_index(index_name=name)
    except NotFound:
        source_table = KNOWN_SOURCE_TABLE.get(name)
        if not source_table:
            raise RuntimeError(f"{name}: index doesn't exist and isn't one of the 4 pipeline-managed indexes -- "
                                f"can't auto-create it (unknown source table).")
        _create_index(name, source_table)
        idx = w.vector_search_indexes.get_index(index_name=name)

    src = idx.delta_sync_index_spec.source_table if idx.delta_sync_index_spec else "?"
    rows = idx.status.indexed_row_count if idx.status else None
    before[name] = rows
    print(f"{name}\n    source={src}  indexed_rows={rows}  ready={idx.status.ready if idx.status else '?'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Trigger the syncs
# MAGIC
# MAGIC A sync failure must fail the task: otherwise the job ends in SUCCESS while
# MAGIC the index is still on yesterday's chunks.

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
# MAGIC
# MAGIC Waits for each index to leave a "syncing" state so the job reflects the
# MAGIC real outcome. `WAIT_MINUTES=0` to just trigger and return.

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
            # Must raise here, or a failed/offline sync waits out the timeout as "still running".
            if "FAILED" in state or "OFFLINE" in state:
                msg = getattr(st, "message", None) or "no further detail from the SDK"
                raise RuntimeError(f"{name}: sync failed (state={state}) — {msg}")
            if st and st.ready and "PROVISIONING" not in state and "SYNC" not in state:
                print(f"{name}: done — indexed_rows {before[name]} -> {st.indexed_row_count}")
                pending.discard(name)
    if pending:
        # Not an error — sync keeps running server-side; the job's useful work is done.
        print(f"Still running after {WAIT_MINUTES} min: {sorted(pending)}")
    print("\nDone.")
