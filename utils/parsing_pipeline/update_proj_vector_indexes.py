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
# MAGIC - `{pipeline_read_env}_proj.qualibot.chunks` and `chunks_archive` (written by the parse tasks) — only to refresh `chunks_full`
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
# MAGIC - The indexes embed and search `chunk_preprocessed`. An index created before that column keeps embedding `chunk_text` (a warning says so): it must be deleted after a `rechunk` run, and this task creates it again.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets
# MAGIC The environments, the quality bypass and the debug mode are widgets: the job passes them as base parameters,
# MAGIC and the notebook copies them to the environment that `config` reads when it is imported.


# COMMAND ----------

import os

dbutils.widgets.removeAll()

dbutils.widgets.text("pipeline_write_env", "", "Environment the tables are written to: dev, uat or prod")
dbutils.widgets.text("pipeline_read_env", "", "Environment of the tables another task of this job wrote (empty: pipeline_write_env)")
dbutils.widgets.text("reference_read_env", "prod", "Environment of the reference tables (Intraqual data asset)")
dbutils.widgets.dropdown("bypass_quality_checks", "false", ["false", "true"], "true: RED quality checks are logged as AMBER")
dbutils.widgets.text("debug", "false")
dbutils.widgets.dropdown("log_level", "info", ["debug", "info", "warning", "error", "critical"])
dbutils.widgets.text("run_mode", "incremental", "Parsing run mode: incremental, full or rechunk")
dbutils.widgets.text("parse_filter", "", "Comma-separated IDDOC list for a targeted run (empty: no filter)")
dbutils.widgets.text("max_parse_files", "500", "Most files one run may parse; 0 = no ceiling")
dbutils.widgets.text("max_source_staleness_hours", "36", "Freshness window of the Intraqual tables; 0 disables the guard")
dbutils.widgets.text("redescribe_all_images", "false", "true: describe every image again")
dbutils.widgets.text("describe_images", "true", "false: no LLM call in 4_describe_images (the descriptions already made are still injected)")

PIPELINE_WRITE_ENV = dbutils.widgets.get("pipeline_write_env")
if PIPELINE_WRITE_ENV not in ("dev", "uat", "prod"):
    raise ValueError(f"pipeline_write_env must be dev, uat or prod, got {PIPELINE_WRITE_ENV!r}")
PIPELINE_READ_ENV = dbutils.widgets.get("pipeline_read_env") or PIPELINE_WRITE_ENV
DEBUG = dbutils.widgets.get("debug").lower() == "true"
LOG_LEVEL = dbutils.widgets.get("log_level")

# config reads the environment when it is imported, so the widget values are copied there first.
os.environ["PIPELINE_WRITE_ENV"] = PIPELINE_WRITE_ENV
os.environ["PIPELINE_READ_ENV"] = dbutils.widgets.get("pipeline_read_env")
os.environ["REFERENCE_READ_ENV"] = dbutils.widgets.get("reference_read_env")
os.environ["BYPASS_QUALITY_CHECKS"] = dbutils.widgets.get("bypass_quality_checks")
os.environ["PARSING_RUN_MODE"] = dbutils.widgets.get("run_mode")
os.environ["PARSING_PARSE_FILTER"] = dbutils.widgets.get("parse_filter")
os.environ["PARSING_MAX_PARSE_FILES"] = dbutils.widgets.get("max_parse_files")
os.environ["PARSING_MAX_SOURCE_STALENESS_HOURS"] = dbutils.widgets.get("max_source_staleness_hours")
os.environ["PARSING_REDESCRIBE_ALL_IMAGES"] = dbutils.widgets.get("redescribe_all_images")

# COMMAND ----------

# MAGIC %md
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
# MAGIC ### Job parameters
# MAGIC This task runs on the classic CPU cluster of the chain and does not import `config.py`, so everything comes from the job parameters. `indexes` falls back on `PARSING_VECTOR_SEARCH_INDEXES` when it is empty.
# MAGIC
# MAGIC The endpoint, embedding model, catalog and suffix are only read when an index has to be created.

# COMMAND ----------

logger = log_utils.setup_applevel_logger(log_level=log_utils.LOGGER_MAPPING[LOG_LEVEL])

dbutils.widgets.text(
    "pipeline_write_env", "", "Environment the tables are written to: dev, uat or prod"
)

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
# chunk_text with the canonical form of each reference code (code_normalization): the column the indexes embed and search.
INDEXED_TEXT_COLUMN = "chunk_preprocessed"
FULL_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks_full"
REFRESH_CHUNKS_FULL = BUILD_CHUNKS_FULL or FULL_INDEX in INDEXES

chunk_sources = [
    t
    for t in (
        f"{PIPELINE_READ_ENV}_proj.qualibot.chunks",
        f"{PIPELINE_READ_ENV}_proj.qualibot.chunks_archive",
    )
    if REFRESH_CHUNKS_FULL and table_exists(t)
]
if REFRESH_CHUNKS_FULL and not chunk_sources:
    raise RuntimeError(
        f"chunks_full requested but neither chunks nor chunks_archive exists in {PIPELINE_READ_ENV}_proj.qualibot"
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
# MAGIC The MERGE below deletes every `chunks_full` row missing from the source: an empty source would empty the table and its index (RED). `chunk_id` is the primary key of the index (RED). `chunk_preprocessed` is the column the index embeds: it is empty on the rows written before it existed, until a `rechunk` run (AMBER).

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
            tables.not_null(INDEXED_TEXT_COLUMN, severity=tables.AMBER),
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
# MAGIC An index in `indexes` that does not exist is created (this task runs as the environment's owning service principal, which already holds the grants for it), then every index is read to log its source table and indexed rows before the sync, noting when it embeds another column than the one it is built for.

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    PipelineType,
    VectorIndexType,
)

# Only these indexes are auto-created; any other index in `indexes` is synced but never created (its source
# table is unknown).
# Each entry gives the source table and the column the index embeds: `chunks_text_index` is built on the same table as
# `chunks_index`, so both can be compared side by side.
KNOWN_INDEX = {
    f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks_index": (f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks", INDEXED_TEXT_COLUMN),
    f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks_text_index": (
        f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks",
        "chunk_text",
    ),
    FULL_INDEX: (FULL_TABLE, INDEXED_TEXT_COLUMN),
}

w = WorkspaceClient()


def _create_index(name, source_table, text_column) -> None:
    """Create a Delta Sync index on a source table and wait until it leaves PROVISIONING.

    :param name: Full name of the index, ``catalog.schema.name``.
    :param source_table: Full name of the table the index is built from.
    :param text_column: Column the index embeds.
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
                    name=text_column, embedding_model_endpoint_name=EMBEDDING_MODEL
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
        source_table, text_column = KNOWN_INDEX.get(name, (None, None))
        if not source_table:
            raise RuntimeError(
                f"{name}: index doesn't exist and isn't one of the pipeline-managed indexes -- "
                f"can't auto-create it (unknown source table)."
            )
        _create_index(name, source_table, text_column)
        created.add(name)
        idx = w.vector_search_indexes.get_index(index_name=name)

    src = idx.delta_sync_index_spec.source_table if idx.delta_sync_index_spec else "?"
    embedded = [c.name for c in (idx.delta_sync_index_spec.embedding_source_columns or [])] if idx.delta_sync_index_spec else []
    expected_column = KNOWN_INDEX.get(name, (None, INDEXED_TEXT_COLUMN))[1]
    if embedded and expected_column not in embedded:
        logger.info(
            f"{name} embeds {embedded}, not {expected_column}: the reference codes of the passages are searched in "
            f"one spelling only. To switch it, delete the index after a rechunk run; this task creates it again."
        )
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
