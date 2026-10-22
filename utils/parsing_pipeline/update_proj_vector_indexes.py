# Databricks notebook source
# MAGIC %md
# MAGIC # PROJ VECTOR_INDEXES
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
# MAGIC - `{pipeline_write_env}_proj.qualibot.chunks` and `chunks_archive` — only to refresh `chunks_full`
# MAGIC - The Vector Search indexes named by `indexes`: comma-separated, empty = nothing to do
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{pipeline_write_env}_proj.qualibot.chunks_full` — only when `build_chunks_full` is on or `chunks_full_index` is in `indexes`
# MAGIC - The synced indexes; `vector_search_endpoint` and `embedding_model` are only read to create an index
# MAGIC - `<index>_writeback_table` (e.g. `chunks_index_writeback_table`, the only name the API accepts): the embeddings the index computed, written back by Databricks for an index created by this notebook

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The `archive_notice` content type is filtered out of `chunks_full` here and in `config.py` (`ARCHIVE_NOTICE_CONTENT_TYPE`): change both together.
# MAGIC - Only `chunks_index` and `chunks_full_index` are created automatically; any other index listed in `indexes` is synced but never created, since its source table is unknown.

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
# MAGIC ## Config Function Imports
# MAGIC `tables` lives next to this notebook; Databricks puts the notebook folder on `sys.path`, and neither module needs the `PARSING_*` environment variables, which this task does not receive.

# COMMAND ----------

import tables

tables.require_gx()  # fails here, not after the work, on a cluster without Great Expectations
from index_state import index_is_updating
from tables import add_missing_columns, merge_into, table_exists, write_table

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config logger

# COMMAND ----------

from leap_utils.common import logger as log_utils


# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets
# MAGIC This task runs on the classic CPU cluster of the chain and does not import `config.py`, so everything comes from the job parameters. `indexes` falls back on `PARSING_VECTOR_SEARCH_INDEXES` when it is empty.
# MAGIC
# MAGIC The endpoint, embedding model, catalog and suffix are only read when an index has to be created.

# COMMAND ----------

dbutils.widgets.removeAll()

dbutils.widgets.dropdown("log_level", "info", ["debug", "info", "warning", "error", "critical"])
LOG_LEVEL = dbutils.widgets.get("log_level")
dbutils.widgets.text("debug", "false")
DEBUG = dbutils.widgets.get("debug").lower() == "true"
logger = log_utils.setup_applevel_logger(log_level=log_utils.LOGGER_MAPPING[LOG_LEVEL])

dbutils.widgets.text(
    "pipeline_write_env", "", "Environment the tables are written to: dev, uat or prod"
)
PIPELINE_WRITE_ENV = dbutils.widgets.get("pipeline_write_env")

if PIPELINE_WRITE_ENV not in ("dev", "uat", "prod"):
    raise ValueError(f"pipeline_write_env must be dev, uat or prod, got {PIPELINE_WRITE_ENV!r}")
dbutils.widgets.text("indexes", "", "Vector Search indexes (comma-separated)")
dbutils.widgets.text("wait_minutes", "45", "Max wait for sync completion (minutes, 0 = don't wait)")
# Only used to auto-create a missing index (this task has no PARSING_* env vars).
dbutils.widgets.text(
    "vector_search_endpoint", "qualibot", "Vector Search endpoint (for index creation)"
)
dbutils.widgets.text(
    "embedding_model",
    "databricks-qwen3-embedding-0-6b",
    "Embedding model endpoint (for index creation)",
)
dbutils.widgets.text(
    "build_chunks_full",
    "false",
    "Refresh chunks_full (chunks + chunks_archive) even without its index",
)

INDEXES = [n.strip() for n in dbutils.widgets.get("indexes").split(",") if n.strip()]
WAIT_MINUTES = int(dbutils.widgets.get("wait_minutes") or "0")
VECTOR_SEARCH_ENDPOINT = dbutils.widgets.get("vector_search_endpoint")
EMBEDDING_MODEL = dbutils.widgets.get("embedding_model")
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

FULL_INDEX = f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks_full_index"
FULL_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks_full"
REFRESH_CHUNKS_FULL = BUILD_CHUNKS_FULL or FULL_INDEX in INDEXES

chunk_sources = [
    t
    for t in (
        f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks",
        f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks_archive",
    )
    if REFRESH_CHUNKS_FULL and table_exists(t)
]
if REFRESH_CHUNKS_FULL and not chunk_sources:
    raise RuntimeError(
        f"chunks_full requested but neither chunks nor chunks_archive exists in {PIPELINE_WRITE_ENV}_proj.qualibot"
    )

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
# MAGIC #N/A

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC ## chunks_full
# MAGIC The MERGE below deletes every `chunks_full` row missing from the source: an empty source would empty the table and its index (RED). `chunk_id` is the primary key of the index (RED).

# COMMAND ----------

if REFRESH_CHUNKS_FULL:
    tables.check_dataframe(
        df_src,
        "chunks_full",
        [
            tables.non_empty(),
            tables.not_null("chunk_id"),
            tables.unique("chunk_id"),
            tables.not_null("chunk_text"),
        ],
        logger,
    )

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
        changed = (
            " OR ".join(
                f"NOT (target.`{c}` <=> source.`{c}`)"
                for c in df_src.columns
                if c != "chunk_id" and c in target_cols
            )
            or "true"
        )
        merge_into(
            df_src,
            FULL_TABLE,
            ["chunk_id"],
            matched_clause=f"({changed})",
            do_when_notmatchedbysource=True,
        )
        logger.info(f"Merged {chunk_sources} into {FULL_TABLE}")
    if DEBUG:
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
KNOWN_SOURCE_TABLE = {
    f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks_index": f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks",
    FULL_INDEX: FULL_TABLE,
}

w = WorkspaceClient()


def _create_index(name, source_table) -> None:
    """Create a Delta Sync index on a source table and wait until it leaves PROVISIONING.

    :param name: Full name of the index, ``catalog.schema.name``.
    :param source_table: Full name of the table the index is built from.
    """
    logger.info(
        f"{name}: not found -- creating (endpoint={VECTOR_SEARCH_ENDPOINT}, source={source_table})..."
    )
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
                    name="chunk_text", embedding_model_endpoint_name=EMBEDDING_MODEL
                )
            ],
            # The embeddings the index computes are written back to a Delta table: another environment can copy them
            # instead of embedding the whole corpus again. The API accepts only this name.
            embedding_writeback_table=f"{name}_writeback_table",
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
    logger.warning(
        f"{name}: still PROVISIONING after 5 min -- proceeding anyway, the sync trigger below may need a retry tomorrow."
    )


before = {}
created = set()
for name in INDEXES:
    try:
        idx = w.vector_search_indexes.get_index(index_name=name)
    except NotFound:
        source_table = KNOWN_SOURCE_TABLE.get(name)
        if not source_table:
            raise RuntimeError(
                f"{name}: index doesn't exist and isn't one of the pipeline-managed indexes -- "
                f"can't auto-create it (unknown source table)."
            )
        _create_index(name, source_table)
        created.add(name)
        idx = w.vector_search_indexes.get_index(index_name=name)

    src = idx.delta_sync_index_spec.source_table if idx.delta_sync_index_spec else "?"
    rows = idx.status.indexed_row_count if idx.status else None
    before[name] = rows
    logger.info(
        f"{name}\n    source={src}  indexed_rows={rows}  ready={idx.status.ready if idx.status else '?'}"
    )

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
            logger.info(
                f"sync not triggered: {name} — a sync is already running, waiting for it instead"
            )
            continue
        logger.warning(f"sync rejected: {name} — {exc}")
        failed.append((name, str(exc)))

if failed:
    raise RuntimeError(f"Failed to trigger {len(failed)} sync(s): {failed}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Wait for the syncs to finish
# MAGIC Waits for each index to leave a syncing state so the job reflects the real outcome; a failed or offline index raises immediately instead of waiting out the timeout, and an index that stays on a failed update (it keeps serving its last good rows) raises after a few polls. `wait_minutes = 0` triggers and returns.

# COMMAND ----------

FAILED_POLLS_BEFORE_ERROR = 3

if WAIT_MINUTES <= 0:
    logger.info("Wait disabled — syncs triggered, state not verified.")
else:
    deadline = time.time() + WAIT_MINUTES * 60
    pending = set(INDEXES)
    failed_polls = {name: 0 for name in INDEXES}
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
            if st and st.ready and not index_is_updating(state):
                # A failed sync leaves the index ready on its last good state: only its message says so. The previous
                # run's message lasts until the new update starts, hence several polls before raising.
                message = getattr(st, "message", None) or ""
                if "update failed" in message.lower():
                    failed_polls[name] += 1
                    if failed_polls[name] >= FAILED_POLLS_BEFORE_ERROR:
                        raise RuntimeError(
                            f"{name}: sync failed, the index still serves its previous rows — {message}"
                        )
                    continue
                logger.info(f"{name}: done — indexed_rows {before[name]} -> {st.indexed_row_count}")
                pending.discard(name)
    if pending:
        # Not an error — sync keeps running server-side; the job's useful work is done.
        logger.warning(f"Still running after {WAIT_MINUTES} min: {sorted(pending)}")
    logger.info("Done.")
