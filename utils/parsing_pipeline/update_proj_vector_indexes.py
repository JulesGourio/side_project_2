# Databricks notebook source
# MAGIC %md
# MAGIC # PROJ VECTOR_INDEXES — SYNC VECTOR SEARCH INDEXES
# MAGIC
# MAGIC **Description:**
# MAGIC Last link of the chain that writes the chunks: triggers a sync of the Delta Sync indexes whose source tables were just rewritten by tasks
# MAGIC `3_parse` and `4_describe_images`. An index of `indexes` that does not exist yet is created, not just synced.
# MAGIC When `build_chunks_full` is on (or `chunks_full_index` is in `indexes`), the table `chunks_full` (`chunks` + `chunks_archive`: every document
# MAGIC whatever its date, for impact search only) is refreshed by MERGE first.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC The indexes are `pipeline_type = TRIGGERED`: without this task new chunks sit in the Delta table and are never queryable, the rest of the chain
# MAGIC would have run for nothing. This task runs `run_as` the environment's owning service principal, which already holds the grant needed to
# MAGIC create an index. Apart from the `chunks_full` MERGE it is plain Vector Search SDK calls.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `5_sync_index`)
# MAGIC
# MAGIC **Inputs Data**
# MAGIC - `{catalog_schema}.chunks{table_suffix}` and `chunks_archive{table_suffix}` — only to refresh `chunks_full`
# MAGIC - The Vector Search indexes named by `indexes` (or `PARSING_VECTOR_SEARCH_INDEXES`): comma-separated, empty = nothing to do
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{catalog_schema}.chunks_full{table_suffix}` — only when `build_chunks_full` is on or `chunks_full_index` is in `indexes`
# MAGIC - The synced indexes; `vector_search_endpoint`, `embedding_model`, `catalog_schema` and `table_suffix` are only read to create an index

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - `KNOWN_SOURCE_TABLE` repeats the table naming of `config.py` (`{catalog_schema}.chunks{table_suffix}`) because this serverless task does not import `config.py`; the same goes for the `archive_notice` content type filtered out of `chunks_full`.
# MAGIC - Only `chunks_index` and `chunks_full_index` are created automatically; any other index listed in `indexes` is synced but never created, since its source table is unknown.
# MAGIC - The wait loop polls every 30 s with a fixed timeout (`wait_minutes`): a sync that outlasts it is reported as still running, not as an error.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration
# MAGIC ## Config Standard Package Imports

# COMMAND ----------

import os
import sys
import time

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
# The repository folder and its root (leap_utils/ lives there) go on sys.path: two levels above this folder.
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, os.path.dirname(os.path.dirname(REPO_DIR)))
sys.path.insert(0, REPO_DIR)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Project Imports
# MAGIC `quality` and `tables` live next to this notebook; Databricks puts the notebook folder on `sys.path`, and neither module needs the `PARSING_*` environment variables this serverless task does not have.

# COMMAND ----------

import quality
from tables import add_missing_columns, merge_into, table_exists, write_table

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config logger

# COMMAND ----------

from leap_utils.common.logger import LOGGER_MAPPING, get_logger, setup_applevel_logger

setup_applevel_logger(log_level=LOGGER_MAPPING["info"]).propagate = False
logger = get_logger("sync_index")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets
# MAGIC This task is serverless: it has no `PARSING_*` environment variables and cannot import `config.py`, so everything comes from the job parameters. `indexes` falls back on `PARSING_VECTOR_SEARCH_INDEXES` when it is empty.
# MAGIC
# MAGIC The endpoint, embedding model, catalog and suffix are only read when an index has to be created.

# COMMAND ----------

dbutils.widgets.removeAll()

dbutils.widgets.text("indexes", "", "Vector Search indexes (comma-separated)")
dbutils.widgets.text("wait_minutes", "45", "Max wait for sync completion (minutes, 0 = don't wait)")
# Only used to auto-create a missing index (this task has no PARSING_* env vars).
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

logger.info(f"{len(INDEXES)} index(es) to sync:")
for n in INDEXES:
    logger.info(f"  - {n}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Tables behind chunks_full
# MAGIC `chunks_full` = `chunks` (post-cutoff, the chatbot) + `chunks_archive` (pre-cutoff): every document whatever its date, for impact search only. It is rebuilt only when `build_chunks_full` is on or `chunks_full_index` is in `indexes`.

# COMMAND ----------

FULL_INDEX = f"{CATALOG_SCHEMA}.chunks_full_index{TABLE_SUFFIX}"
FULL_TABLE = f"{CATALOG_SCHEMA}.chunks_full{TABLE_SUFFIX}"
REFRESH_CHUNKS_FULL = bool(CATALOG_SCHEMA) and (BUILD_CHUNKS_FULL or FULL_INDEX in INDEXES)

chunk_sources = [
    t for t in (f"{CATALOG_SCHEMA}.chunks{TABLE_SUFFIX}", f"{CATALOG_SCHEMA}.chunks_archive{TABLE_SUFFIX}")
    if REFRESH_CHUNKS_FULL and table_exists(t)
]
if REFRESH_CHUNKS_FULL and not chunk_sources:
    raise RuntimeError(f"chunks_full requested but neither chunks nor chunks_archive exists in {CATALOG_SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation
# MAGIC ## Prep1 - Union of the chunk tables
# MAGIC Archive notices carry no content and are left out, and a chunk id seen in both tables (a document that crossed the cutoff between two runs) is kept once.

# COMMAND ----------

if REFRESH_CHUNKS_FULL:
    df_src = spark.table(chunk_sources[0])
    for table in chunk_sources[1:]:
        df_src = df_src.unionByName(spark.table(table), allowMissingColumns=True)
    # Archive notices (config.ARCHIVE_NOTICE_CONTENT_TYPE) carry no content: nothing to judge an impact on.
    df_src = df_src.filter("NOT (chunk_content_type <=> 'archive_notice')")
    # A document that crossed the cutoff between two runs can briefly sit in both tables.
    df_src = df_src.dropDuplicates(["chunk_id"])

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC ## chunks_full
# MAGIC The MERGE below deletes every `chunks_full` row missing from the source: an empty source would empty the table and its index (RED). `chunk_id` is the primary key of the index (RED).

# COMMAND ----------

if REFRESH_CHUNKS_FULL:
    quality.check_dataframe(df_src, "chunks_full", [
        quality.non_empty(),
        quality.not_null("chunk_id"),
        quality.unique("chunk_id"),
        quality.not_null("chunk_text"),
    ], logger)

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Write chunks_full
# MAGIC Synced by MERGE rather than overwritten, so the Change Data Feed carries only the day's real changes and the index re-embeds only those rows.

# COMMAND ----------

if REFRESH_CHUNKS_FULL:
    if not table_exists(FULL_TABLE):
        write_table(df_src, FULL_TABLE, "errorifexists")
        # 60-day history, like the chunk tables: a TRIGGERED sync cannot resume once the Change Data Feed it
        # needs has aged out (VECTOR_SEARCH_SOURCE_HISTORY_OUT_OF_RETENTION).
        spark.sql(f"""
            ALTER TABLE {FULL_TABLE} SET TBLPROPERTIES (
                delta.enableChangeDataFeed = true,
                delta.deletedFileRetentionDuration = 'interval 60 days',
                delta.logRetentionDuration = 'interval 60 days'
            )
        """)
        logger.info(f"Created {FULL_TABLE} from {chunk_sources}")
    else:
        # Columns added to the chunk tables reach chunks_full too (added before the merge).
        add_missing_columns(df_src, FULL_TABLE)
        target_cols = set(spark.table(FULL_TABLE).columns)
        changed = " OR ".join(f"NOT (target.`{c}` <=> source.`{c}`)" for c in df_src.columns
                              if c != "chunk_id" and c in target_cols) or "true"
        merge_into(df_src, FULL_TABLE, ["chunk_id"], matched_clause=f"({changed})",
                   do_when_notmatchedbysource=True)
        logger.info(f"Merged {chunk_sources} into {FULL_TABLE}")
    logger.info(f"{FULL_TABLE}: {spark.table(FULL_TABLE).count()} chunks")

if not INDEXES:
    logger.info("No index to sync (`indexes` param empty) - nothing more to do.")
    dbutils.notebook.exit("no_index")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create any missing index, then read its status
# MAGIC An index in `indexes` that does not exist is created (this task runs as the environment's owning service principal, which already holds the grants for it), then every index is read to log its source table and indexed rows before the sync.

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    PipelineType,
    VectorIndexType,
)

# Only these index/source pairs are auto-created; any other index in `indexes` is synced but never created (its source
# table is unknown).
# Same naming as config.py's TARGET_CHUNK_TABLE ({catalog_schema}.{name}{table_suffix}), rebuilt from widgets because
# this task does not import config.py.
KNOWN_SOURCE_TABLE = {
    f"{CATALOG_SCHEMA}.chunks_index{TABLE_SUFFIX}": f"{CATALOG_SCHEMA}.chunks{TABLE_SUFFIX}",
    FULL_INDEX: FULL_TABLE,
} if CATALOG_SCHEMA else {}

w = WorkspaceClient()


def _create_index(name, source_table):
    logger.info(f"{name}: not found -- creating (endpoint={VECTOR_SEARCH_ENDPOINT}, source={source_table})...")
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
    logger.warning(f"{name}: still PROVISIONING after 5 min -- proceeding anyway, the sync trigger below may need a retry tomorrow.")


before = {}
created = set()
for name in INDEXES:
    try:
        idx = w.vector_search_indexes.get_index(index_name=name)
    except NotFound:
        source_table = KNOWN_SOURCE_TABLE.get(name)
        if not source_table:
            raise RuntimeError(f"{name}: index doesn't exist and isn't one of the pipeline-managed indexes -- "
                                f"can't auto-create it (unknown source table).")
        _create_index(name, source_table)
        created.add(name)
        idx = w.vector_search_indexes.get_index(index_name=name)

    src = idx.delta_sync_index_spec.source_table if idx.delta_sync_index_spec else "?"
    rows = idx.status.indexed_row_count if idx.status else None
    before[name] = rows
    logger.info(f"{name}\n    source={src}  indexed_rows={rows}  ready={idx.status.ready if idx.status else '?'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Trigger the syncs
# MAGIC Indexes are `TRIGGERED`: without this step new chunks stay in the Delta table and are never queryable. A rejected sync fails the task, otherwise the job would end in SUCCESS with the index still on yesterday's chunks.

# COMMAND ----------

failed = []
for name in INDEXES:
    if name in created:
        # Creating an index starts its first full sync, and a trigger is refused until that one ends.
        logger.info(f"sync not triggered: {name} — just created, its initial sync is running")
        continue
    try:
        w.vector_search_indexes.sync_index(index_name=name)
        logger.info(f"sync triggered: {name}")
    except Exception as exc:
        if "not ready to sync yet" in str(exc):
            logger.info(f"sync not triggered: {name} — a sync is already running, waiting for it instead")
            continue
        logger.warning(f"sync rejected: {name} — {exc}")
        failed.append((name, str(exc)))

if failed:
    raise RuntimeError(f"Failed to trigger {len(failed)} sync(s): {failed}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Wait for the syncs to finish
# MAGIC Waits for each index to leave a syncing state so the job reflects the real outcome; a failed or offline index raises immediately instead of waiting out the timeout. `wait_minutes = 0` triggers and returns.

# COMMAND ----------

if WAIT_MINUTES <= 0:
    logger.info("Wait disabled — syncs triggered, state not verified.")
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
                logger.info(f"{name}: done — indexed_rows {before[name]} -> {st.indexed_row_count}")
                pending.discard(name)
    if pending:
        # Not an error — sync keeps running server-side; the job's useful work is done.
        logger.warning(f"Still running after {WAIT_MINUTES} min: {sorted(pending)}")
    logger.info("Done.")
