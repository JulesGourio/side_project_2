# Databricks notebook source
# MAGIC %md
# MAGIC # 03 — Document Parse Pipeline
# MAGIC
# MAGIC **Description:**
# MAGIC Performs the full document parsing and chunking pipeline:
# MAGIC
# MAGIC 1. **File Selection** — Scans the volume, loads business metadata, ranks candidates per IDDOC
# MAGIC 2. **Parsing** — Converts documents (PDF, DOC, DOCX, ODT, XLSX…) to Markdown via Docling
# MAGIC 3. **Image Extraction** — Saves detected images to a UC Volume with deduplication
# MAGIC 4. **Retry** — Re-attempts failed IDDOCs using the next-best file variant
# MAGIC 5. **Chunking** — Splits successful parses into semantic chunks (table-aware hybrid strategy)
# MAGIC 6. **Persistence** — Writes `processed_files`, `chunks`, and `image_metadata` Delta tables
# MAGIC
# MAGIC **Dependencies:** `parse_steps.py` (one function per phase below), `utils.py` (parse/chunk engine), `image_utils.py` (image extraction UDF), `selection.py` (file selection & audit).
# MAGIC
# MAGIC **Highlighted complexities:** the selection, parse and retry phases all read and write the same checkpoint table, so the notebook stays strictly sequential.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `3_parse`, GPU cluster)
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - `{PARSING_CATALOG_SCHEMA}.parse_manifest{PARSING_TABLE_SUFFIX}` (from task `2_manifest`)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.category_reference{PARSING_TABLE_SUFFIX}` (from task `1_categories`)
# MAGIC - `{PARSING_CATALOG_SCHEMA}._pipeline_checkpoint{PARSING_TABLE_SUFFIX}` (own checkpoint, re-read across phases)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.processed_files{PARSING_TABLE_SUFFIX}` (resume/staleness detection)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.image_metadata{PARSING_TABLE_SUFFIX}` (preserve already-described rows across a rebuild)
# MAGIC - `{PARSING_VOLUME_ROOT_PATH}` (volume listing + binary content)
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - *(none)*
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PARSING_CATALOG_SCHEMA}._pipeline_checkpoint{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.processed_files{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.chunks{PARSING_TABLE_SUFFIX}` (all divisions)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.image_metadata{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.parsing_run_health{PARSING_TABLE_SUFFIX}` (one row per run, for the monitoring dashboard)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.document_change_log{PARSING_TABLE_SUFFIX}` (one row per NEW/REVISED document, for the monitoring dashboard)

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The phase functions in `parse_steps.py` have no unit tests: they need a Spark session and the UC tables.
# MAGIC - `parse_steps` keeps run-level state (`INGESTION_RUN_ID`, `JOB_RUN_ID`) as module globals, set once by `set_job_run_id`.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Standard Package Imports
# MAGIC The pinned `opencv-python-headless` replaces the build pulled in by Docling, which crashes on the GPU runtime (see `README.md`). Do not remove.

# COMMAND ----------

%pip install -q --force-reinstall --no-deps opencv-python-headless==4.12.0.88

# COMMAND ----------

# MAGIC %md
# MAGIC The repository folder goes on `sys.path` for the driver, and `addPyFile` ships the modules to every executor, late joiners included.

# COMMAND ----------

import os
import sys

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F

# addPyFile ships the modules to every executor, late-joining ones included; sys.path only covers the driver.
for _mod in ("chunking.py", "utils.py", "image_utils.py", "selection.py", "config.py"):
    spark.sparkContext.addPyFile(os.path.join(REPO_DIR, _mod))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Project Imports
# MAGIC
# MAGIC `config` holds every parameter (tables, volumes, chunking, Docling), `parse_steps` the phases below, `utils.configure` and the two worker helpers ship the settings to the executors.

# COMMAND ----------

import config as cfg
import parse_steps
from utils import configure, broadcast_config, write_worker_config, logger

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets
# MAGIC
# MAGIC The parent job run id is written on every row so `2_manifest` rows and these rows can be correlated.

# COMMAND ----------

dbutils.widgets.text("JOB_RUN_ID", "", "Parent job run_id (correlates with 2_manifest's processed_files rows)")
parse_steps.set_job_run_id(dbutils.widgets.get("JOB_RUN_ID"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Spark and workers
# MAGIC Docling needs the model, GPU and chunking settings on every executor. `broadcast_config` does it on a single-user cluster;
# MAGIC on a shared cluster the workers read the same settings from a JSON file on the volume.

# COMMAND ----------

try:
    _ = spark.sparkContext
    IS_SHARED_CLUSTER = False
except Exception:
    IS_SHARED_CLUSTER = True

spark.conf.set("spark.sql.files.maxPartitionBytes", "134217728")
spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "true")
spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", "1")

configure(
    OFFLINE_MODELS_DIR=cfg.OFFLINE_MODELS_DIR, VOLUME_BASE_PATH=cfg.VOLUME_BASE_PATH,
    ANTIWORD_BIN=f"{REPO_DIR}/{cfg.ANTIWORD_BIN_RELATIVE}",
    ANTIWORD_SHARE_DIR=f"{REPO_DIR}/{cfg.ANTIWORD_SHARE_RELATIVE}",
    WORKER_CONFIG_JSON=os.path.join(cfg.VOLUME_BASE_PATH, "_parsing_config.json"),
    USE_GPU=cfg.USE_GPU, DO_OCR=cfg.DO_OCR, GPU_OCR_FALLBACK=cfg.GPU_OCR_FALLBACK,
    TABLE_STRUCTURE_MODE=cfg.TABLE_STRUCTURE_MODE, GENERATE_PICTURE_IMAGES=cfg.GENERATE_PICTURE_IMAGES,
    IMAGE_SCALE=cfg.IMAGE_SCALE, MIN_AREA_RATIO=cfg.MIN_AREA_RATIO, MAX_REPEAT=cfg.MAX_REPEAT,
    USE_TIKTOKEN=cfg.USE_TIKTOKEN, CHARS_PER_TOKEN=cfg.CHARS_PER_TOKEN,
    MIN_CHUNK_TOKENS=cfg.MIN_CHUNK_TOKENS, TARGET_CHUNK_TOKENS=cfg.TARGET_CHUNK_TOKENS,
    MAX_CHUNK_TOKENS=cfg.MAX_CHUNK_TOKENS, MAX_CHUNK_CHARS=cfg.MAX_CHUNK_CHARS,
    CHUNK_OVERLAP_RATIO=cfg.CHUNK_OVERLAP_RATIO,
    LLM_MODEL_ENDPOINT=cfg.LLM_MODEL_ENDPOINT, LLM_MAX_TOKENS=cfg.LLM_MAX_TOKENS,
    LLM_TEMPERATURE=cfg.LLM_TEMPERATURE, LLM_MAX_RETRIES=cfg.LLM_MAX_RETRIES,
    LLM_MAX_CONCURRENT=cfg.LLM_MAX_CONCURRENT,
)

if IS_SHARED_CLUSTER:
    logger.warning("broadcast_config skipped (shared cluster) - workers read the JSON fallback from the volume")
else:
    broadcast_config(spark)
write_worker_config(spark)

try:
    CLUSTER_PARALLELISM = int(spark.sparkContext.defaultParallelism)
    if CLUSTER_PARALLELISM <= 1:
        raise ValueError(f"defaultParallelism={CLUSTER_PARALLELISM} looks wrong")
except Exception as exc:
    logger.warning(f"Could not read defaultParallelism ({exc}) - falling back to 8")
    CLUSTER_PARALLELISM = 8

logger.info(
    f"Run {parse_steps.INGESTION_RUN_ID} | mode={cfg.RUN_MODE} | shared_cluster={IS_SHARED_CLUSTER} | "
    f"parallelism={CLUSTER_PARALLELISM} | GPU={cfg.USE_GPU} | table_mode={cfg.TABLE_STRUCTURE_MODE} | "
    f"filter={cfg.PARSE_FILTER}"
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Import Qualibot tables
# MAGIC ### Parse manifest
# MAGIC One row per in-scope document, written by `2_manifest`. Run `2_Cleanup_Volume` first if it is not up to date.

# COMMAND ----------

df_business_meta = spark.read.table(cfg.PARSE_MANIFEST_TABLE).cache()

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation
# MAGIC ## Prep1 - Scope of the run
# MAGIC Incremental mode skips the IDDOCs already in a terminal status, except those whose `indice`, `doc_date` or `ref` changed
# MAGIC since their last parse: Intraqual gives every revision a new IDDOC, but a rename can happen in place.

# COMMAND ----------

target_iddocs, revised_iddocs = parse_steps.resolve_parse_scope(df_business_meta)
parse_steps.log_document_changes(df_business_meta, target_iddocs, revised_iddocs, parse_steps.JOB_RUN_ID)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep2 - File selection
# MAGIC Scans only the target IDDOCs' folders on the volume, ranks every candidate file per IDDOC and keeps the best one.
# MAGIC The runners-up stay available for the retry phase.

# COMMAND ----------

df_files, df_matched_full, df_content = parse_steps.select_files_for_target_iddocs(target_iddocs, df_business_meta)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep3 - Documents without a parseable file
# MAGIC A target IDDOC whose folder holds nothing parseable is recorded as `SKIPPED_EMPTY_FOLDER` and leaves the run.

# COMMAND ----------

if target_iddocs:
    target_iddocs = parse_steps.mark_empty_folders(target_iddocs, df_files, df_business_meta)
HAS_TARGET_IDDOCS = len(target_iddocs) > 0

num_files = df_files.count()
logger.info(f"[SELECT] {num_files} files selected for parsing")

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Checkpoint snapshot
# MAGIC A full run shallow-clones the live checkpoint before overwriting it; an incremental run only logs the Delta version to restore.

# COMMAND ----------

parse_steps.snapshot_or_log_checkpoint(num_files)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 2 - Parse with Docling
# MAGIC Files already `SUCCESS` in the checkpoint (same path and hash) are skipped, then each batch is parsed and checkpointed.

# COMMAND ----------

parse_fn = parse_steps.make_parse_fn(CLUSTER_PARALLELISM)

if num_files > 0:
    df_files = parse_steps.exclude_already_parsed(df_files)
    num_files = df_files.count()
    if num_files > 0:
        parse_steps.cleanup_stale_image_folders(df_files)
        parse_steps.parse_pending_files(df_files, parse_fn)
        parse_steps.show_failed_documents(df_business_meta)
else:
    logger.info("Nothing to parse - all documents are already processed.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 3 - Retry failed documents with their next-best file
# MAGIC Controlled by `ENABLE_RETRY`. Without it, the checkpoint is read as is.

# COMMAND ----------

if num_files == 0:
    df_full_pipeline = None
elif cfg.ENABLE_RETRY:
    df_full_pipeline = parse_steps.retry_failed_iddocs(df_matched_full, df_content, df_business_meta)
else:
    df_full_pipeline = parse_steps.read_checkpoint_deduped()
    logger.info(f"Retry disabled. {df_full_pipeline.filter(F.col('parse_status') != 'SUCCESS').count()} documents still in error.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 4 - image_metadata from the checkpoint

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    df_image_metadata = parse_steps.build_image_metadata(target_iddocs)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 5 - processed_files and chunks
# MAGIC Pre-cutoff documents go to the archive chunks, the others to the chatbot chunks.

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    df_processed_files, df_chunks_all, df_chunks_archive = parse_steps.build_processed_files_and_chunks(
        target_iddocs, df_image_metadata, revised_iddocs
    )

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC ## REF mapping consistency
# MAGIC Reports chunks whose `ref` disagrees with `processed_files`; informational, it does not stop the write.

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    ref_issues = parse_steps.validate_ref_mapping(df_chunks_all, df_processed_files)

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Write final Delta tables

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    parse_steps.write_outputs(df_processed_files, df_chunks_all, df_chunks_archive, df_image_metadata, revised_iddocs)
else:
    logger.info("No target IDDOCs - nothing to write.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Archive notices (pre-cutoff documents)
# MAGIC Runs on every run, parsed documents or not: it only depends on the manifest.
# MAGIC After `write_outputs`, which deletes and overwrites chunk rows per IDDOC.

# COMMAND ----------

parse_steps.write_archive_notices(parse_steps.build_archive_notices(df_business_meta))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ghost document guard
# MAGIC Detects SUCCESS documents with zero chunks right after the write and flags them `MISSING_CHUNKS`,
# MAGIC so the next incremental run re-parses them.

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    parse_steps.flag_ghost_documents()
