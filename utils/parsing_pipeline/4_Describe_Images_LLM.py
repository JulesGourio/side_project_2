# Databricks notebook source
# MAGIC %md
# MAGIC # 4_DESCRIBE_IMAGES — VISION LLM DESCRIPTION AND IMAGE PASSAGES
# MAGIC
# MAGIC **Description:**
# MAGIC Describes the `PENDING` images of `image_metadata` with a vision LLM, then injects the descriptions as image passages into the chunk tables.
# MAGIC The phases are functions of `describe_steps.py`:
# MAGIC 1. Load the images with status `PENDING` (and legacy `ERROR` rows) from `image_metadata`
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
# MAGIC - `{PARSING_CATALOG_SCHEMA}.image_metadata{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.chunks{PARSING_TABLE_SUFFIX}`, `chunks_archive{PARSING_TABLE_SUFFIX}` (text passages and max chunk index per IDDOC)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.processed_files{PARSING_TABLE_SUFFIX}` (document metadata for the source prefix)
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PARSING_CATALOG_SCHEMA}.image_metadata{PARSING_TABLE_SUFFIX}` (description and status columns)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.chunks{PARSING_TABLE_SUFFIX}`, `chunks_archive{PARSING_TABLE_SUFFIX}` (image passages)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.processed_files{PARSING_TABLE_SUFFIX}` (EMPTY_TEXT / IMAGES_PENDING -> SUCCESS / SKIPPED_EMPTY_IMAGES promotion, once all the images of a document are described)

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The chunk-injection phase reads `image_metadata` and the chunk tables after the description phase has written to them, so those reads sit in `# Data Transformations` instead of `# Inputs`.
# MAGIC - The source prefix of an image passage (`[Source: … | Image: page N, label]`) is built in `describe_steps.build_image_chunks` and, for text passages, in `utils.source_prefixed_text`: the two formats must be changed together.
# MAGIC - The serving token comes from the secret `qualibot/serving_token` and falls back to the notebook's own API token when the secret is missing.
# MAGIC - An image that keeps failing is retried by every run, with no cap on its attempts: its document waits in `IMAGES_PENDING` until the image is described (see `promote_documents_awaiting_images`).

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration
# MAGIC ## Config Standard Package Imports
# MAGIC No `%pip install`: the dependencies are job-cluster libraries in `parsing_pipeline.job.yml`. The repository folder is added to `sys.path` for the driver.

# COMMAND ----------

import os
import sys

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Project Imports
# MAGIC `addPyFile` ships the modules to every executor, which the image placement of Tr. 2 needs for `chunking`.

# COMMAND ----------

import config as cfg
import describe_steps
import llm_describe
import quality
from utils import configure, logger

for _mod in cfg.WORKER_MODULES:
    spark.sparkContext.addPyFile(os.path.join(REPO_DIR, _mod))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets
# MAGIC `rebuild_image_chunks = true` rewrites the passage of EVERY described image from its stored description (new section, caption, long transcriptions split), without any LLM call. It is not needed after a FULL run of `3_parse`, which already empties the chunk tables.
# MAGIC
# MAGIC `llm_fallback_endpoints` is a comma-separated list of vision endpoints tried, in order, after the primary `PARSING_LLM_ENDPOINT`. Each endpoint must take image input.

# COMMAND ----------

dbutils.widgets.removeAll()

dbutils.widgets.dropdown("rebuild_image_chunks", "false", ["false", "true"])
REBUILD_IMAGE_CHUNKS = dbutils.widgets.get("rebuild_image_chunks") == "true"

dbutils.widgets.text("llm_fallback_endpoints", cfg.LLM_FALLBACK_ENDPOINTS, "Fallback endpoints (comma-separated)")
LLM_FALLBACK_ENDPOINTS = dbutils.widgets.get("llm_fallback_endpoints")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Workers and authentication
# MAGIC `configure` gives `token_count_udf` its settings on the executors. The LLM is called with the workspace host and a token: the secret when it exists, the notebook's own token otherwise.

# COMMAND ----------

configure(
    VOLUME_BASE_PATH=cfg.VOLUME_BASE_PATH, USE_TIKTOKEN=cfg.USE_TIKTOKEN, CHARS_PER_TOKEN=cfg.CHARS_PER_TOKEN,
)

try:
    WS_TOKEN = dbutils.secrets.get(scope="qualibot", key="serving_token")
except Exception:
    WS_TOKEN = _ctx.apiToken().get()
WS_HOST = spark.conf.get("spark.databricks.workspaceUrl")

LLM_ENDPOINTS = llm_describe.endpoint_chain(cfg.LLM_MODEL_ENDPOINT, LLM_FALLBACK_ENDPOINTS)
logger.info(f"RUN_MODE={cfg.RUN_MODE} | Endpoints={' -> '.join(LLM_ENDPOINTS)} | concurrency={cfg.LLM_MAX_CONCURRENT} | "
            f"batch={cfg.LLM_BATCH_SIZE or 'ALL'}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Images needing a description
# MAGIC `incremental` (default, safe to re-run): `PENDING` images, plus legacy `ERROR` rows. An image that failed on every endpoint is left `PENDING` with no description, so the next run describes it again. `DONE`, `SKIPPED` and `SKIPPED_DECORATIVE` stay terminal.
# MAGIC
# MAGIC `full` resets every status to `PENDING` first and describes everything again.

# COMMAND ----------

if cfg.RUN_MODE == "full":
    describe_steps.reset_images_to_pending()

df_pending = describe_steps.pending_images()
pending_count = df_pending.count()

status_counts = {r["status"]: r["cnt"] for r in df_pending.groupBy("status").agg(F.count("*").alias("cnt")).collect()}
n_pending = status_counts.get("PENDING", 0)
n_retry = status_counts.get("ERROR", 0)
logger.info(f"Images to process: {pending_count} total ({n_pending} new PENDING + {n_retry} ERROR retries)")
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
logger.info(f"Batch: {len(rows_to_process)} images ({n_pending} new + {n_retry} retries)" + (f" | batched from {pending_count}" if batched else ""))

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Describe the images (checkpointed)
# MAGIC The vision LLM is called in chunks of `LLM_CHECKPOINT_CHUNK_SIZE`, at a rate bounded by the endpoint quota. Each chunk is merged into `image_metadata` as soon as it is done, so a failure in the middle of a multi-hour run keeps the progress.
# MAGIC Each image tries the endpoint chain in `LLM_ROUNDS` passes, with a wait between passes. No description is stored for an image that failed everywhere: it stays `PENDING` for the next run.

# COMMAND ----------

describe_steps.describe_images(rows_to_process, WS_HOST, WS_TOKEN, LLM_ENDPOINTS)

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
    quality.check_dataframe(df_image_chunks, "image chunks", [
        quality.not_null("chunk_id"),
        quality.unique("chunk_id"),
        quality.not_null("IDDOC"),
        quality.not_null("REF"),
        quality.not_null("chunk_text"),
        quality.not_null("division", mostly=0.99, severity=quality.AMBER),
    ], logger)

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
# MAGIC A document with no text but images is `IMAGES_PENDING` (parked by 3_parse) while an image is still to describe. Once none is left, this step makes it `SUCCESS` (text assembled from the descriptions) or `SKIPPED_EMPTY_IMAGES` when no image was usable. A document with an image still `PENDING` stays parked.

# COMMAND ----------

describe_steps.promote_documents_awaiting_images()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

display(
    spark.table(describe_steps.IMAGE_TABLE).groupBy("status").agg(
        F.count("*").alias("count"),
        F.sum("input_tokens").alias("total_input_tokens"),
        F.sum("output_tokens").alias("total_output_tokens"),
    )
)
