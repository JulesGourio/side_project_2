# Databricks notebook source
# MAGIC %md
# MAGIC # PROJ IMAGE_METADATA
# MAGIC
# MAGIC **Description:**
# MAGIC Describes the `PENDING` images of `image_metadata` with a vision LLM, then injects the descriptions as image passages into the chunk tables.
# MAGIC The phases are functions of `describe_steps.py`:
# MAGIC 1. Load the images with status `PENDING` (and `ERROR` rows) from `image_metadata`
# MAGIC 2. Call the vision LLM asynchronously (batched, with concurrency control and a rate limiter), through the endpoint chain
# MAGIC 3. MERGE the descriptions back into `image_metadata`, chunk by chunk
# MAGIC 4. Build the image passages and MERGE them into `chunks` / `chunks_archive`
# MAGIC 5. Promote the documents waiting for their images (`EMPTY_TEXT`, `IMAGES_PENDING`) whose images are now all described
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC Two sequential read-then-write phases (description, then passage injection): the second phase reads `image_metadata` AFTER the
# MAGIC first one wrote to it, to pick up the freshly described rows. Its reads therefore stay in `# Data Transformations`, not in `# Inputs`.
# MAGIC Task `3_parse` must have run first to fill `image_metadata`.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `4_describe_images`)
# MAGIC
# MAGIC **Inputs Data**
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.image_metadata`
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.chunks`, `chunks_archive` (text passages and max chunk index per IDDOC)
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsed_documents` (document metadata for the source prefix)
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.image_metadata` (description and status columns)
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.chunks`, `chunks_archive` (image passages)
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsed_documents` (EMPTY_TEXT / IMAGES_PENDING -> SUCCESS / SKIPPED_EMPTY_IMAGES promotion, once all the images of a document are described)

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The chunk-injection phase reads `image_metadata` and the chunk tables after the description phase has written to them, so those reads sit in `# Data Transformations`.
# MAGIC - The source prefix of an image passage (`[Source: … | Image: page N, label]`) is built in `describe_steps.build_image_chunks` and, for text passages, in `worker.source_prefixed_text`: change both together.

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
# MAGIC No `%pip install`: the dependencies are job-cluster libraries in `parsing_pipeline.job.yml`. The repository folder is added to `sys.path` for the driver.

# COMMAND ----------

import os
import sys

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(
    0, os.path.dirname(os.path.dirname(REPO_DIR))
)  # leap_utils/; inserted first so that REPO_DIR (its own `utils` module) stays ahead
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as f

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Function Imports
# MAGIC `addPyFile` ships the modules to every executor, which the image placement of Tr. 2 needs for `chunking`.

# COMMAND ----------

import config as cfg
import describe_steps
import llm_describe
import tables

tables.require_gx()  # fails here, not after the work, on a cluster without Great Expectations
from worker import configure
from leap_utils.common import logger as log_utils


for _mod in cfg.WORKER_MODULES:
    spark.sparkContext.addPyFile(os.path.join(REPO_DIR, _mod))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Job parameters
# MAGIC `rebuild_image_chunks = true` rewrites the passage of EVERY described image from its stored description (new section, caption, long transcriptions split), without any LLM call. It is on whenever `redescribe_all_images` is `true`, so new descriptions reach the index.
# MAGIC
# MAGIC The vision endpoints are constants of `config.py`: the primary `LLM_MODEL_ENDPOINT`, then `LLM_FALLBACK_ENDPOINTS`, tried in order. Each endpoint must take image input.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config logger
# MAGIC The logger is created once the widgets are read, so the level they give applies to every line after.

# COMMAND ----------

logger = log_utils.setup_applevel_logger(log_level=log_utils.LOGGER_MAPPING[LOG_LEVEL])

dbutils.widgets.dropdown("rebuild_image_chunks", "false", ["false", "true"])
REBUILD_IMAGE_CHUNKS = (
    dbutils.widgets.get("rebuild_image_chunks") == "true" or cfg.REDESCRIBE_ALL_IMAGES
)


# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Workers and authentication
# MAGIC `configure` gives `token_count_udf` its settings on the executors. The LLM is called with the workspace host and the token of the secret `qualibot/serving_token`.

# COMMAND ----------

configure(
    VOLUME_BASE_PATH=cfg.VOLUME_BASE_PATH,
    USE_TIKTOKEN=cfg.USE_TIKTOKEN,
    CHARS_PER_TOKEN=cfg.CHARS_PER_TOKEN,
)

try:
    WS_TOKEN = dbutils.secrets.get(scope="qualibot", key="serving_token")
except Exception:
    WS_TOKEN = _ctx.apiToken().get()
WS_HOST = spark.conf.get("spark.databricks.workspaceUrl")

LLM_ENDPOINTS = llm_describe.endpoint_chain(cfg.LLM_MODEL_ENDPOINT, cfg.LLM_FALLBACK_ENDPOINTS)
logger.info(
    f"RUN_MODE={cfg.RUN_MODE} | Endpoints={' -> '.join(LLM_ENDPOINTS)} | concurrency={cfg.LLM_MAX_CONCURRENT} | "
    f"batch={cfg.LLM_BATCH_SIZE or 'ALL'}"
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Images needing a description
# MAGIC `incremental` (default, safe to re-run): `PENDING` images, plus `ERROR` rows (described rather than skipped). An image that failed on every endpoint is left `PENDING` with no description, so the next run describes it again, until it reaches the attempt limit (`FAILED_LIMIT`). `DONE`, `SKIPPED`, `SKIPPED_DECORATIVE`, `EXTRACTION_FAILED` and `FAILED_LIMIT` stay terminal.
# MAGIC
# MAGIC An image whose content and request (context, division, category, label) are the same as one already described reuses its description from `image_description_cache`, without any call; `redescribe_all_images=true` does not use the cache.
# MAGIC
# MAGIC `redescribe_all_images=true` resets every status to `PENDING` first and describes everything again (a prompt or
# MAGIC model change). The parsing run mode does not: a `full` parse keeps every description of an image that did not change.

# COMMAND ----------

if cfg.REDESCRIBE_ALL_IMAGES:
    describe_steps.reset_images_to_pending()

df_pending = describe_steps.pending_images()
pending_count = df_pending.count()

status_counts = {
    r["status"]: r["cnt"]
    for r in df_pending.groupBy("status").agg(f.count("*").alias("cnt")).collect()
}
n_pending = status_counts.get("PENDING", 0)
n_retry = status_counts.get("ERROR", 0)
logger.info(
    f"Images to process: {pending_count} total ({n_pending} new PENDING + {n_retry} ERROR retries)"
)
if pending_count == 0:
    logger.info("Nothing to do - no PENDING or ERROR images.")

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation
# MAGIC ## Prep1 - Batch of the run
# MAGIC `LLM_BATCH_SIZE` caps the images described in one run: a larger batch destabilizes the kernel. The rest waits for the next run.

# COMMAND ----------

batched = bool(cfg.LLM_BATCH_SIZE) and pending_count > cfg.LLM_BATCH_SIZE
df_to_process = df_pending.limit(cfg.LLM_BATCH_SIZE) if batched else df_pending
rows_to_process = [r.asDict() for r in df_to_process.collect()]
logger.info(
    f"Batch: {len(rows_to_process)} images ({n_pending} new + {n_retry} retries)"
    + (f" | batched from {pending_count}" if batched else "")
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Describe the images (checkpointed)
# MAGIC The vision LLM is called in chunks of `LLM_CHECKPOINT_CHUNK_SIZE`, at a rate bounded by the endpoint quota. Each chunk is merged into `image_metadata` as soon as it is done, so a failure in the middle of a multi-hour run keeps the progress.
# MAGIC Each image tries the endpoint chain in `LLM_ROUNDS` passes, with a wait between passes. No description is stored for an image that failed everywhere: it stays `PENDING` for the next run.

# COMMAND ----------

describe_steps.describe_images(
    rows_to_process, WS_HOST, WS_TOKEN, LLM_ENDPOINTS, use_cache=not cfg.REDESCRIBE_ALL_IMAGES
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 2 - Build the image passages
# MAGIC Only `DONE` images whose description is long enough and is not a `SKIP` answer are indexed. The selection is an anti-join on `chunk_id` against the chunk tables, so a run that crashed between the descriptions and this step is repaired by the next one.
# MAGIC
# MAGIC Each passage records where the image sits (the passage holding the words just before it), its section and caption, and its source prefix.

# COMMAND ----------

df_described = describe_steps.described_images_without_chunk(REBUILD_IMAGE_CHUNKS)
described_count = df_described.count()

if described_count == 0:
    describe_steps.log_nothing_to_inject()
else:
    logger.info(f"{described_count} new image chunks to inject into chunk tables.")
    df_image_chunks = describe_steps.build_image_chunks(df_described).cache()

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC ## Image passages
# MAGIC `chunk_id` is the key of the MERGE and of the Vector Search index: a duplicate fails the MERGE and a null breaks the sync (RED). A passage without `IDDOC`, `REF` or text cannot be served (RED); `division` is expected on 99 % of them (AMBER). The passages are cached here, so the write below does not compute them again.

# COMMAND ----------

if described_count > 0:
    tables.check_dataframe(
        df_image_chunks,
        "image chunks",
        [
            tables.not_null("chunk_id"),
            tables.unique("chunk_id"),
            tables.not_null("IDDOC"),
            tables.not_null("REF"),
            tables.not_null("chunk_text"),
            tables.not_null("division", mostly=0.99, severity=tables.AMBER),
        ],
        logger,
    )

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Write the image passages
# MAGIC MERGE on `chunk_id` (not delete-then-insert per IDDOC), so image passages that did not change are not rewritten. Pre-cutoff documents go to `chunks_archive`, the others to `chunks`.

# COMMAND ----------

if described_count > 0:
    describe_steps.write_image_chunks(df_image_chunks)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Promote the documents waiting for their images
# MAGIC A document with no text but images is `IMAGES_PENDING` (parked by 3_parse) while an image is still to describe. Once none is left, this step makes it `SUCCESS` (text assembled from the descriptions) or `SKIPPED_EMPTY_IMAGES` when no image was usable. A document with an image still `PENDING` stays parked; a `FAILED_LIMIT` image does not hold it back.

# COMMAND ----------

describe_steps.promote_documents_awaiting_images()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Restore the documents whose passages are their images
# MAGIC The ghost guard of `3c_chunks` flags a document without any passage as `MISSING_CHUNKS` before its images are described. Once the image passages exist, the document is `SUCCESS` again, so the next run does not parse it for nothing.

# COMMAND ----------

describe_steps.restore_documents_with_passages()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

display(
    spark.table(describe_steps.IMAGE_TABLE)
    .groupBy("status")
    .agg(
        f.count("*").alias("count"),
        f.sum("input_tokens").alias("total_input_tokens"),
        f.sum("output_tokens").alias("total_output_tokens"),
    )
)
