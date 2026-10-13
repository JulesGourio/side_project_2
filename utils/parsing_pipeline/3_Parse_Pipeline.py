# Databricks notebook source
# MAGIC %md
# MAGIC # 3_PARSE — DOCUMENT PARSE AND CHUNK
# MAGIC
# MAGIC **Description:**
# MAGIC Parses the documents of the manifest and builds the tables the chat reads. One function per phase, in `parse_steps.py`,
# MAGIC `build_steps.py` and `output_steps.py`:
# MAGIC 1. **File selection** — scans the volume folders of the target IDDOCs and keeps the best candidate file per IDDOC
# MAGIC 2. **Parsing** — converts PDF, DOC(X), PPT(X), ODT, XLS(X)… to Markdown with Docling or a format fallback, saves the detected images to the volume
# MAGIC 3. **Retry** — re-attempts the failed IDDOCs with their next-best file variant
# MAGIC 4. **Chunking** — splits the successful parses into passages (section-aware, table-aware)
# MAGIC 5. **Persistence** — writes `processed_files`, `chunks` / `chunks_archive` and `image_metadata`, then the run health, the change log and the archive notices
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC The selection, parse and retry phases all read and write the same checkpoint table, so the notebook stays strictly sequential.
# MAGIC The Docling parse runs on the executors (GPU): the modules listed in `config.WORKER_MODULES` are shipped to them.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `3_parse`, GPU cluster)
# MAGIC
# MAGIC **Inputs Data**
# MAGIC - `{PARSING_CATALOG_SCHEMA}.parse_manifest{PARSING_TABLE_SUFFIX}` (task `2_manifest`), `category_reference{PARSING_TABLE_SUFFIX}` (task `1_categories`)
# MAGIC - `{PARSING_CATALOG_SCHEMA}._pipeline_checkpoint{PARSING_TABLE_SUFFIX}` (own checkpoint, re-read across phases)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.processed_files{PARSING_TABLE_SUFFIX}` (resume and revision detection)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.image_metadata{PARSING_TABLE_SUFFIX}` (keeps the rows already described across a rebuild)
# MAGIC - `{PARSING_VOLUME_ROOT_PATH}` (volume listing and binary content)
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PARSING_CATALOG_SCHEMA}._pipeline_checkpoint{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.processed_files{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.chunks{PARSING_TABLE_SUFFIX}`, `chunks_archive{PARSING_TABLE_SUFFIX}`, `chunks_archive_notices{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.image_metadata{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.parsing_run_health{PARSING_TABLE_SUFFIX}` (one row per run, for the monitoring dashboard)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.document_change_log{PARSING_TABLE_SUFFIX}` (one row per NEW/REVISED document, for the monitoring dashboard)

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The phase functions have no unit test: they need a Spark session and the Unity Catalog tables (the quality rules in `quality.py` and the chunking in `chunking.py` do).
# MAGIC - `parse_steps` keeps run-level state (`INGESTION_RUN_ID`, `JOB_RUN_ID`) as module globals, set once by `set_job_run_id` and passed on to `build_steps` / `output_steps` as arguments.
# MAGIC - The quality checks read the lazy DataFrames before the write, so `processed_files` and `image_metadata` are computed once more than the write needs.

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
# MAGIC The repository folder goes on `sys.path` for the driver.

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
# MAGIC
# MAGIC `config` holds every parameter (tables, volumes, chunking, Docling); `parse_steps`, `build_steps` and `output_steps` the phases below;
# MAGIC `utils.configure` and the two worker helpers ship the settings to the executors. `addPyFile` ships the modules to every
# MAGIC executor, late-joining ones included (`sys.path` only covers the driver).

# COMMAND ----------

import config as cfg
import build_steps
import output_steps
import parse_checks
import parse_steps
import quality
from utils import configure, broadcast_config, write_worker_config, logger

for _mod in cfg.WORKER_MODULES:
    spark.sparkContext.addPyFile(os.path.join(REPO_DIR, _mod))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets
# MAGIC
# MAGIC The parent job run id is written on every row so `2_manifest` rows and these rows can be correlated.

# COMMAND ----------

dbutils.widgets.removeAll()

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
if cfg.RUN_MODE != "rechunk":  # a rebuild changes no document: nothing to log as NEW or REVISED
    output_steps.log_document_changes(df_business_meta, target_iddocs, revised_iddocs, parse_steps.JOB_RUN_ID)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep2 - File selection
# MAGIC Scans only the target IDDOCs' folders on the volume, ranks every candidate file per IDDOC and keeps the best one.
# MAGIC The runners-up stay available for the retry phase. `rechunk` reads no file: it only rebuilds from the checkpoint.

# COMMAND ----------

RECHUNK_ONLY = cfg.RUN_MODE == "rechunk"
if RECHUNK_ONLY:
    df_files = df_matched_full = df_content = None
else:
    df_files, df_matched_full, df_content = parse_steps.select_files_for_target_iddocs(target_iddocs, df_business_meta)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep3 - Documents without a parseable file
# MAGIC A target IDDOC whose folder holds nothing parseable is recorded as `SKIPPED_EMPTY_FOLDER` and leaves the run.

# COMMAND ----------

if target_iddocs and not RECHUNK_ONLY:
    target_iddocs = parse_steps.mark_empty_folders(target_iddocs, df_files, df_business_meta)
HAS_TARGET_IDDOCS = len(target_iddocs) > 0

num_files = 0 if RECHUNK_ONLY else df_files.count()
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
    logger.info("Nothing was parsed - no failed document to retry.")
elif cfg.ENABLE_RETRY:
    parse_steps.retry_failed_iddocs(df_matched_full, df_content, df_business_meta)
else:
    n_in_error = parse_steps.read_checkpoint_deduped().filter(F.col("parse_status") != "SUCCESS").count()
    logger.info(f"Retry disabled. {n_in_error} documents still in error.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 4 - image_metadata from the checkpoint

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    df_image_metadata = build_steps.build_image_metadata(target_iddocs, parse_steps.INGESTION_RUN_ID)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 5 - processed_files and chunks
# MAGIC Pre-cutoff documents go to the archive chunks, the others to the chatbot chunks.

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    df_processed_files, df_chunks_all, df_chunks_archive = build_steps.build_processed_files_and_chunks(
        target_iddocs, df_image_metadata, parse_steps.JOB_RUN_ID, revised_iddocs
    )

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC Run on the DataFrames before anything is written: a RED failure stops the task and leaves the tables as they were. Each table is read once.
# MAGIC
# MAGIC ## REF mapping consistency (AMBER)
# MAGIC Reports the chunks whose `REF` disagrees with `parse_manifest`, belongs to no manifest row, or disagrees with their own `[Source: …]` prefix; it does not stop the write.

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    parse_checks.check_ref_mapping(spark, df_chunks_all, logger)

# COMMAND ----------

# MAGIC %md
# MAGIC ## processed_files
# MAGIC One row per `IDDOC` (RED): the incremental runs and the deletes of the next steps key on it. `ref` and `division` are expected on 99 % of the documents (AMBER).

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    quality.check_dataframe(df_processed_files, "processed_files", [
        quality.not_null("IDDOC"),
        quality.unique("IDDOC"),
        quality.not_null("parse_status"),
        quality.not_null("ref", mostly=0.99, severity=quality.AMBER),
        quality.not_null("division", mostly=0.99, severity=quality.AMBER),
    ], logger)

# COMMAND ----------

# MAGIC %md
# MAGIC ## chunks (and chunks_archive)
# MAGIC `chunk_id` is the primary key of the Vector Search index: a null or a duplicate breaks the sync (RED), checked across `chunks` and `chunks_archive` together. A chunk without `IDDOC`, `REF` or text cannot be served (RED). The search filters on `division` and shows `titre`: expected on 99 % of the passages (AMBER).

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    quality.check_dataframe(df_chunks_all.unionByName(df_chunks_archive), "chunks", [
        quality.not_null("chunk_id"),
        quality.unique("chunk_id"),
        quality.not_null("IDDOC"),
        quality.not_null("REF"),
        quality.not_null("chunk_text"),
        quality.not_null("division", mostly=0.99, severity=quality.AMBER),
        quality.not_null("titre", mostly=0.99, severity=quality.AMBER),
    ], logger)

# COMMAND ----------

# MAGIC %md
# MAGIC ## image_metadata
# MAGIC The key is `(IDDOC, image_id)`: the MERGE of the description step fails on a duplicate (RED). An image without a file on the volume (`EXTRACTION_FAILED`) is expected on less than 5 % of the images (AMBER).

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    quality.check_dataframe(df_image_metadata, "image_metadata", [
        quality.not_null("IDDOC"),
        quality.not_null("image_id"),
        quality.unique("IDDOC", "image_id"),
        quality.not_null("volume_path", mostly=0.95, severity=quality.AMBER),
    ], logger)

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Write final Delta tables

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    output_steps.write_outputs(
        df_processed_files, df_chunks_all, df_chunks_archive, df_image_metadata, parse_steps.JOB_RUN_ID, revised_iddocs
    )
else:
    logger.info("No target IDDOCs - nothing to write.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Archive notices (pre-cutoff documents)
# MAGIC Runs on every run, parsed documents or not: it only depends on the manifest.
# MAGIC After `write_outputs`, which deletes and overwrites chunk rows per IDDOC.

# COMMAND ----------

output_steps.write_archive_notices(build_steps.build_archive_notices(df_business_meta))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ghost document guard
# MAGIC Detects SUCCESS documents with zero chunks right after the write and flags them `MISSING_CHUNKS`,
# MAGIC so the next incremental run re-parses them.

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    output_steps.flag_ghost_documents()
