# Databricks notebook source
# MAGIC %md
# MAGIC # PROJ PARSING CHECKPOINT GPU
# MAGIC
# MAGIC **Description:**
# MAGIC Parses on GPU the PDFs that `3_parse` queued (more than `parsing_gpu_min_pdfs` of them). Docling runs its layout and
# MAGIC table models on the GPU; the other formats never come here.
# MAGIC 1. **Queue** — the PDFs of this run, their bytes read again from the volume (same hash only)
# MAGIC 2. **Parsing** — Docling, images saved to the volume, scanned PDFs queued for LLM-OCR, checkpoint appended per batch
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC The task only starts when the condition `3b_needs_gpu` is true: a daily run with a handful of PDFs parses them on CPU.
# MAGIC The Docling parse runs on the executors (one task per GPU): the modules listed in `config.WORKER_MODULES` are shipped to them.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `3b_parse_pdf`, GPU cluster, only when needed)
# MAGIC
# MAGIC **Inputs Data**
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_run_scope` (run id), `parsing_gpu_queue` (task `3_parse`)
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parse_manifest` (failure report)
# MAGIC - `{PARSING_VOLUME_ROOT_PATH}` (binary content)
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_checkpoint` (one row per parsed PDF, appended)

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The checkpoint is written during the parse, before the grouped quality checks: an interrupted run must keep its batches.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Standard Package Imports
# MAGIC The pinned `opencv-python-headless` replaces the build Docling pulls in, which fails without a display library.

# COMMAND ----------

%pip install -q --force-reinstall --no-deps opencv-python-headless==4.12.0.88

# COMMAND ----------

# MAGIC %md
# MAGIC The repository folder goes on `sys.path` for the driver, ahead of the repository root (`leap_utils`).

# COMMAND ----------

import os
import sys

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, os.path.dirname(os.path.dirname(REPO_DIR)))
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Function Imports
# MAGIC `tables.require_gx()` fails here, before the GPU parse, on a cluster without Great Expectations.

# COMMAND ----------

import config as cfg
import parse_steps
import tables
tables.require_gx()
from leap_utils.common import logger as log_utils

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets

# COMMAND ----------

dbutils.widgets.removeAll()

dbutils.widgets.dropdown("log_level", "info", ["debug", "info", "warning", "error", "critical"])
LOG_LEVEL = dbutils.widgets.get("log_level")
dbutils.widgets.text("job_run_id", "", "Parent job run_id")
parse_steps.set_job_run_id(dbutils.widgets.get("job_run_id"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config logger

# COMMAND ----------

logger = log_utils.setup_applevel_logger(log_level=log_utils.LOGGER_MAPPING[LOG_LEVEL])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Spark and workers
# MAGIC The executors get the parsing modules and the Docling settings (GPU on); one Spark task per GPU.

# COMMAND ----------

CLUSTER_PARALLELISM = parse_steps.setup_workers(REPO_DIR)

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Run scope and parse manifest
# MAGIC The scope gives the ingestion run id of `3_parse`: the PDFs parsed here carry the same one.

# COMMAND ----------

parse_steps.read_run_scope()
df_business_meta = spark.read.table(cfg.PARSE_MANIFEST_TABLE).cache()

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation
# MAGIC ## Prep1 - GPU queue
# MAGIC Only the PDFs queued by this run; a file changed since `3_parse` is left for the next run.

# COMMAND ----------

df_pdf = parse_steps.read_gpu_queue()

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Parse with Docling on GPU
# MAGIC The image folders of these IDDOCs are emptied first; each batch is appended to the checkpoint as soon as it is parsed.

# COMMAND ----------

if df_pdf is not None:
    parse_steps.cleanup_stale_image_folders(df_pdf)
    parse_steps.parse_pending_files(df_pdf, parse_steps.make_parse_fn(CLUSTER_PARALLELISM))
    parse_steps.show_failed_documents(df_business_meta)
else:
    logger.info("GPU queue empty for this run.")

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC ## Checkpoint rows of this run
# MAGIC `3c_chunks` keys the rebuild on `IDDOC` and the resume on `document_sha256` (RED, before `3c_chunks` runs).

# COMMAND ----------

df_written = spark.table(cfg.CHECKPOINT_TABLE).filter(
    (F.col("ingestion_run_id") == parse_steps.INGESTION_RUN_ID) & (F.col("source_file_extension") == "pdf"))
tables.check_dataframe(df_written, "parsing_checkpoint (GPU PDFs)", [
    tables.not_null("IDDOC"), tables.not_null("document_sha256"), tables.not_null("parse_status"),
], logger)

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC The checkpoint rows were appended batch by batch in Tr. 1; `3c_chunks` builds the passages from them.

# COMMAND ----------

logger.info(f"[OUTPUT] PDF checkpoint row(s) written by run {parse_steps.INGESTION_RUN_ID}")
