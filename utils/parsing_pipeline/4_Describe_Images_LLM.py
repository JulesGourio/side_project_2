# Databricks notebook source
# MAGIC %md
# MAGIC # 04 — Describe Images with Vision LLM
# MAGIC
# MAGIC **Description:**
# MAGIC Describes `PENDING` images from `image_metadata` using a vision LLM, then
# MAGIC injects the descriptions as enriched chunks into the `chunks` table.
# MAGIC
# MAGIC The phases are functions of `describe_steps.py`.
# MAGIC
# MAGIC **Workflow:**
# MAGIC 1. Load images with status = `PENDING` from `image_metadata`
# MAGIC 2. Call the vision LLM asynchronously (batched, with concurrency control)
# MAGIC 3. UPDATE descriptions back into `image_metadata` (targeted row-level updates)
# MAGIC 4. Build image-based chunks and APPEND/overwrite them into `chunks`
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC This notebook has two sequential read-then-write phases (description, then
# MAGIC chunk injection) where the second phase's read of `image_metadata`
# MAGIC intentionally happens AFTER the first phase's writes, to pick up the
# MAGIC freshly-described rows. So not every table read is hoisted into the
# MAGIC top-level Inputs section below — the second phase's reads stay attached to
# MAGIC their own Data Transformations subsection, right where they need to run.
# MAGIC
# MAGIC **Pre-requisite:** task `3_parse` must run first to populate `image_metadata`.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `4_describe_images`)
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - `{PARSING_CATALOG_SCHEMA}.image_metadata{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.chunks{PARSING_TABLE_SUFFIX}` (max chunk index per IDDOC)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.processed_files{PARSING_TABLE_SUFFIX}` (doc_date)
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - *(none)*
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PARSING_CATALOG_SCHEMA}.image_metadata{PARSING_TABLE_SUFFIX}` (description/status columns)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.chunks{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.processed_files{PARSING_TABLE_SUFFIX}` (EMPTY_TEXT -> SUCCESS / SKIPPED_EMPTY_IMAGES promotion, once all of a document's images are described)

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The chunk-injection phase reads `image_metadata` and the chunk tables after the description phase has written to them, so those reads sit in `# Data Transformations` instead of `# Inputs`.
# MAGIC - The source prefix of an image passage (`[Source: … | Image: page N, label]`) is built in `describe_steps.build_image_chunks` and, for text passages, in `utils.source_prefixed_text`: the two formats must be changed together.
# MAGIC - The serving token comes from the secret `qualibot/serving_token` and falls back to the notebook's own API token when the secret is missing.
# MAGIC - A document whose images all end in ERROR is promoted with whatever descriptions exist (see `promote_empty_text_documents`); there is no alert on it.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration
# MAGIC ## Config Imports
# MAGIC No `%pip install`: the dependencies are job-cluster libraries in `parsing_pipeline.job.yml`.
# MAGIC
# MAGIC `addPyFile` ships the modules to every executor, which the image placement of Tr. 2 needs for `chunking`.

# COMMAND ----------

import os
import sys

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F

for _mod in ("chunking.py", "utils.py", "image_utils.py", "selection.py", "config.py"):
    spark.sparkContext.addPyFile(os.path.join(REPO_DIR, _mod))

import config as cfg
import describe_steps
from utils import configure, logger

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets
# MAGIC `rebuild_image_chunks = true` rewrites the passage of EVERY described image from its stored description (new section, caption, long transcriptions split), without any LLM call. It is not needed after a FULL run of `3_parse`, which already empties the chunk tables.

# COMMAND ----------

dbutils.widgets.dropdown("rebuild_image_chunks", "false", ["false", "true"])
REBUILD_IMAGE_CHUNKS = dbutils.widgets.get("rebuild_image_chunks") == "true"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Workers and authentication
# MAGIC `configure` gives `token_count_udf` its settings on the executors. The LLM is called with the workspace host and a token: the secret when it exists, the notebook's own token otherwise.

# COMMAND ----------

configure(
    VOLUME_BASE_PATH=cfg.VOLUME_BASE_PATH, LLM_MODEL_ENDPOINT=cfg.LLM_MODEL_ENDPOINT,
    LLM_MAX_TOKENS=cfg.LLM_MAX_TOKENS, LLM_TEMPERATURE=cfg.LLM_TEMPERATURE,
    LLM_MAX_RETRIES=cfg.LLM_MAX_RETRIES, LLM_MAX_CONCURRENT=cfg.LLM_MAX_CONCURRENT,
    USE_TIKTOKEN=cfg.USE_TIKTOKEN, CHARS_PER_TOKEN=cfg.CHARS_PER_TOKEN,
)

try:
    WS_TOKEN = dbutils.secrets.get(scope="qualibot", key="serving_token")
except Exception:
    WS_TOKEN = _ctx.apiToken().get()
WS_HOST = spark.conf.get("spark.databricks.workspaceUrl")

logger.info(f"RUN_MODE={cfg.RUN_MODE} | Model={cfg.LLM_MODEL_ENDPOINT} | concurrency={cfg.LLM_MAX_CONCURRENT} | "
            f"batch={cfg.LLM_BATCH_SIZE or 'ALL'}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Images needing a description
# MAGIC `incremental` (default, safe to re-run): `PENDING` images and the `ERROR` ones, which are retried because a failure is usually a blank LLM answer. `DONE`, `SKIPPED` and `SKIPPED_DECORATIVE` stay terminal.
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

# COMMAND ----------

describe_steps.describe_images(rows_to_process, WS_HOST, WS_TOKEN)

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
    df_image_chunks = describe_steps.build_image_chunks(df_described)

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC #N/A

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
# MAGIC ## Promote the EMPTY_TEXT documents
# MAGIC A document with no text but images is `EMPTY_TEXT` until its images are described; this step then makes it `SUCCESS` (text assembled from the descriptions) or `SKIPPED_EMPTY_IMAGES` when every image was judged non-informative.

# COMMAND ----------

describe_steps.promote_empty_text_documents()

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
