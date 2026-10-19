# Databricks notebook source
# MAGIC %md
# MAGIC # PROJ PARSING CHECKPOINT
# MAGIC
# MAGIC **Description:**
# MAGIC Decides what this run parses and parses it on CPU. One function per phase, in `parse_steps.py`:
# MAGIC 1. **Scope** — the IDDOCs of the manifest to parse (incremental: the new and revised ones; full: all; rechunk: none)
# MAGIC 2. **File selection** — scans the volume folders of the target IDDOCs and keeps the best candidate file per IDDOC
# MAGIC 3. **Resume** — drops the files whose IDDOC already has a reusable checkpoint row for the same bytes, then stops the
# MAGIC    task if more files are left than `parsing_max_parse_files`
# MAGIC 4. **Routing** — Docling uses its GPU models on PDFs only: above `parsing_gpu_min_pdfs` PDFs, they are queued for
# MAGIC    the GPU task (`3b_parse_pdf`); otherwise everything is parsed here and the GPU cluster never starts
# MAGIC 5. **Parsing** — Docling (docx, pptx, html, small PDF sets) or a format parser (doc, xls, ppt…), images saved to the volume
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC The checkpoint is appended batch by batch during the parse, so an interrupted run resumes where it stopped.
# MAGIC The scope and the GPU queue are rewritten on every run: `3b_parse_pdf` and `3c_chunks` read them.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `3_parse`, CPU cluster)
# MAGIC
# MAGIC **Inputs Data**
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parse_manifest` (task `2_manifest`)
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_checkpoint` (resume), `parsed_documents` (incremental scope)
# MAGIC - `{PARSING_VOLUME_ROOT_PATH}` (volume listing and binary content)
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_checkpoint` (one row per parsed file, appended)
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_run_scope` (the IDDOCs of this run, for `3c_chunks`)
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_gpu_queue` (the PDFs left for `3b_parse_pdf`)
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.document_change_log` (one row per NEW/REVISED document)
# MAGIC - task value `pdfs_for_gpu` (read by the condition that starts the GPU task)

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - `parse_steps` keeps the run-level state (`INGESTION_RUN_ID`, `JOB_RUN_ID`) as module globals.
# MAGIC - The checkpoint is written during the parse, before the grouped quality checks: an interrupted run must keep its batches.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Standard Package Imports
# MAGIC Docling and its parsers are installed in the notebook's own environment, not as cluster libraries: on the standard
# MAGIC runtime (the S policy allows no ML runtime) the torch stack installed on the cluster stops the Python REPL from
# MAGIC starting. `xlrd`, `openpyxl` and Great Expectations come with the S policy. pydantic and pandas are pinned so they
# MAGIC land in this environment too: the runtime's pydantic is too old for this Docling (it fails at import on the
# MAGIC executors), and pandas stays below 3 for the runtime's Spark. Docling versions tested together on Python 3.12.

# COMMAND ----------

%pip install -q "numpy<2" "pandas>=2.1.4,<3" "pydantic>=2.10.3,<3" docling==2.140.0 docling-core==2.102.0 langchain-text-splitters tiktoken striprtf odfpy lxml olefile pyxlsb openai tenacity

# COMMAND ----------

# MAGIC %md
# MAGIC The pinned `opencv-python-headless` replaces the build Docling pulls in, which fails without a display library.
# MAGIC 4.11, not 4.12: 4.12 is built for numpy 2, and the standard runtime keeps numpy 1 (`--no-deps` would not catch it).

# COMMAND ----------

%pip install -q --force-reinstall --no-deps opencv-python-headless==4.11.0.86

# COMMAND ----------

# MAGIC %md
# MAGIC Python restarts so the packages just installed replace the runtime's copies before any import.

# COMMAND ----------

dbutils.library.restartPython()

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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Function Imports
# MAGIC `tables.require_gx()` fails here, before any work, on a cluster without Great Expectations.

# COMMAND ----------

import config as cfg
import output_steps
import parse_steps
import tables
tables.require_gx()
from tables import table_exists, write_table
from leap_utils.common import logger as log_utils

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets
# MAGIC The parent job run id is written on every row so the rows of the tasks of one run can be correlated.

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
# MAGIC The executors get the parsing modules and the Docling, image and chunking settings; the installed package versions are logged.

# COMMAND ----------

CLUSTER_PARALLELISM = parse_steps.setup_workers(REPO_DIR)

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Parse manifest
# MAGIC One row per in-scope document, written by `2_manifest`.

# COMMAND ----------

df_business_meta = spark.read.table(cfg.PARSE_MANIFEST_TABLE).cache()

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation
# MAGIC ## Prep1 - Scope of the run
# MAGIC Incremental mode skips the IDDOCs already in a terminal status, except those whose `indice`, `doc_date` or `ref`
# MAGIC changed since their last parse. A rebuild (`rechunk`) changes no document: nothing is logged as NEW or REVISED.

# COMMAND ----------

target_iddocs, revised_iddocs = parse_steps.resolve_parse_scope(df_business_meta)
if cfg.RUN_MODE != "rechunk":
    output_steps.log_document_changes(df_business_meta, target_iddocs, revised_iddocs, parse_steps.JOB_RUN_ID)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep2 - File selection
# MAGIC Scans only the target IDDOCs' folders, ranks every candidate file per IDDOC and keeps the best one.
# MAGIC `rechunk` reads no file: `3c_chunks` rebuilds from the checkpoint.

# COMMAND ----------

RECHUNK_ONLY = cfg.RUN_MODE == "rechunk"
if RECHUNK_ONLY:
    df_files = None
else:
    df_files, _, _ = parse_steps.select_files_for_target_iddocs(target_iddocs, df_business_meta)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep3 - Documents without a parseable file
# MAGIC A target IDDOC whose folder holds nothing parseable is recorded as `SKIPPED_EMPTY_FOLDER` and leaves the run.

# COMMAND ----------

if target_iddocs and not RECHUNK_ONLY:
    target_iddocs = parse_steps.mark_empty_folders(target_iddocs, df_files, df_business_meta)
num_files = 0 if RECHUNK_ONLY else df_files.count()
logger.info(f"[SELECT] {num_files} files selected")

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Checkpoint snapshot
# MAGIC A full run shallow-clones the live checkpoint first; an incremental run only logs the Delta version to restore.

# COMMAND ----------

parse_steps.snapshot_or_log_checkpoint(num_files)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 2 - Resume, budget and routing
# MAGIC A file whose IDDOC already has a reusable checkpoint row for the same bytes is not parsed again (the path does not
# MAGIC matter). Above `parsing_max_parse_files` pending files the task stops here, before any parse. Then the PDFs go to
# MAGIC the GPU task when there are more than `parsing_gpu_min_pdfs` of them.

# COMMAND ----------

df_cpu = df_gpu = None
pdfs_for_gpu = 0
if num_files > 0:
    df_pending = parse_steps.exclude_already_parsed(df_files)
    parse_steps.enforce_parse_budget(df_pending.count())
    df_cpu, df_gpu, pdfs_for_gpu = parse_steps.split_for_gpu(df_pending)
else:
    logger.info("Nothing to parse - all documents are already processed.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 3 - Parse on CPU
# MAGIC The image folders of the IDDOCs about to be parsed are emptied first, so old and new extracted images never mix.
# MAGIC Each batch is appended to the checkpoint as soon as it is parsed.

# COMMAND ----------

if df_cpu is not None and df_cpu.count() > 0:
    parse_steps.cleanup_stale_image_folders(df_cpu)
    parse_steps.parse_pending_files(df_cpu, parse_steps.make_parse_fn(CLUSTER_PARALLELISM))
    parse_steps.show_failed_documents(df_business_meta)

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC ## Run scope and GPU queue
# MAGIC `3c_chunks` rebuilds the documents of the scope by IDDOC, and the GPU task reads its files back by path: one row
# MAGIC per key, no null key (RED). The checkpoint rows were written batch by batch during the parse.

# COMMAND ----------

df_scope = parse_steps.build_run_scope(target_iddocs, revised_iddocs)
tables.check_dataframe(df_scope, "parsing_run_scope", [
    tables.not_null("IDDOC"), tables.unique("IDDOC"), tables.not_null("ingestion_run_id"),
], logger)

df_queue = df_gpu.drop("content") if df_gpu is not None else None
if df_queue is not None:
    tables.check_dataframe(df_queue, "parsing_gpu_queue", [
        tables.not_null("source_path"), tables.unique("source_path"),
        tables.not_null("IDDOC"), tables.not_null("document_sha256"),
    ], logger)

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Run scope, GPU queue and the task value of the GPU condition
# MAGIC The queue is emptied when nothing goes to the GPU, so a later GPU task never parses the PDFs of an older run.

# COMMAND ----------

write_table(df_scope, cfg.RUN_SCOPE_TABLE, "overwrite", overwrite_schema=True)
if df_queue is not None:
    write_table(df_queue, cfg.GPU_QUEUE_TABLE, "overwrite", overwrite_schema=True)
elif table_exists(cfg.GPU_QUEUE_TABLE):
    spark.sql(f"DELETE FROM {cfg.GPU_QUEUE_TABLE}")

dbutils.jobs.taskValues.set(key="pdfs_for_gpu", value=pdfs_for_gpu)
logger.info(f"[OUTPUT] scope={len(target_iddocs)} IDDOC(s) | PDFs for the GPU task={pdfs_for_gpu}")
