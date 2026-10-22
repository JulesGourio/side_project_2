# Databricks notebook source
# MAGIC %md
# MAGIC # PROJ CHUNKS
# MAGIC
# MAGIC **Description:**
# MAGIC Builds the tables the chat reads from the parse checkpoint, once both parse tasks are done (`3_parse` on CPU and,
# MAGIC when needed, `3b_parse_pdf` on GPU). One function per phase, in `parse_steps.py`, `build_steps.py` and `output_steps.py`:
# MAGIC 1. **Retry** — re-attempts the IDDOCs that failed with their next-best file variant (only their folders are scanned again)
# MAGIC 2. **Chunking** — splits the successful parses into passages (section-aware, table-aware)
# MAGIC 3. **Persistence** — writes `parsed_documents`, `chunks` / `chunks_archive` and `image_metadata`, then the run health and the archive notices
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC The IDDOCs to rebuild come from `parsing_run_scope`, written by `3_parse` for this job run (refused if another run wrote it).
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `3c_chunks`, CPU cluster)
# MAGIC
# MAGIC **Inputs Data**
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_run_scope` (task `3_parse`)
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parse_manifest` (task `2_manifest`), `category_reference` (task `1_categories`)
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_checkpoint` (tasks `3_parse` and `3b_parse_pdf`)
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.image_metadata` (keeps the rows already described across a rebuild)
# MAGIC - `{VOLUME_ROOT_PATH}` (the folders of the failed IDDOCs, for the retry)
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsed_documents`
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.chunks`, `chunks_archive`, `archive_notices`
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.image_metadata`
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_run_health` (one row per run, for the monitoring dashboard)

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - `parse_steps` keeps the run-level state (`INGESTION_RUN_ID`, `JOB_RUN_ID`) as module globals.
# MAGIC - The quality checks read the lazy DataFrames before the write, so `parsed_documents` and `image_metadata` are computed once more than the write needs.

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
import build_steps
import output_steps
import parse_steps
import tables

tables.require_gx()
from leap_utils.common import logger as log_utils

# COMMAND ----------

# MAGIC %md
# MAGIC ### Job parameters
# MAGIC The parent job run id must match the one `3_parse` wrote in the run scope.

# COMMAND ----------

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
# MAGIC The retry parses on the executors: they get the parsing modules and settings.

# COMMAND ----------

parse_steps.setup_workers(REPO_DIR)

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Parse manifest
# MAGIC One row per in-scope document, written by `2_manifest`.

# COMMAND ----------

df_business_meta = spark.read.table(cfg.PARSE_MANIFEST_TABLE).cache()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run scope
# MAGIC The IDDOCs `3_parse` put in scope for this run, and its ingestion run id (shared by the rows of both parse tasks).

# COMMAND ----------

target_iddocs, revised_iddocs = parse_steps.read_run_scope()
HAS_TARGET_IDDOCS = len(target_iddocs) > 0

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation
# MAGIC ## Prep1 - Retry failed documents with their next-best file
# MAGIC Only when this run parsed something (`ENABLE_RETRY`): the folders of the failed IDDOCs are scanned again.

# COMMAND ----------

if parse_steps.files_parsed_this_run() == 0:
    logger.info("Nothing was parsed this run - no failed document to retry.")
elif cfg.ENABLE_RETRY:
    parse_steps.retry_failed_iddocs(df_business_meta)
else:
    logger.info("Retry disabled.")

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - image_metadata from the checkpoint

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    df_image_metadata = build_steps.build_image_metadata(
        target_iddocs, parse_steps.INGESTION_RUN_ID
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 2 - parsed_documents and chunks
# MAGIC Pre-cutoff documents go to the archive chunks, the others to the chatbot chunks.

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    df_processed_files, df_chunks_all, df_chunks_archive = (
        build_steps.build_processed_files_and_chunks(
            target_iddocs, df_image_metadata, parse_steps.JOB_RUN_ID, revised_iddocs
        )
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
    tables.check_ref_mapping(spark, df_chunks_all, logger)

# COMMAND ----------

# MAGIC %md
# MAGIC ## parsed_documents
# MAGIC One row per `IDDOC` (RED): the incremental runs and the deletes of the next steps key on it. `ref` and `division` are expected on 99 % of the documents (AMBER).

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    tables.check_dataframe(
        df_processed_files,
        "parsed_documents",
        [
            tables.not_null("IDDOC"),
            tables.unique("IDDOC"),
            tables.not_null("parse_status"),
            tables.not_null("ref", mostly=0.99, severity=tables.AMBER),
            tables.not_null("division", mostly=0.99, severity=tables.AMBER),
        ],
        logger,
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## chunks (and chunks_archive)
# MAGIC `chunk_id` is the primary key of the Vector Search index: a null or a duplicate breaks the sync (RED), checked across `chunks` and `chunks_archive` together. A chunk without `IDDOC`, `REF` or text cannot be served (RED). The search filters on `division` and shows `titre`: expected on 99 % of the passages (AMBER).

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    tables.check_dataframe(
        df_chunks_all.unionByName(df_chunks_archive),
        "chunks",
        [
            tables.not_null("chunk_id"),
            tables.unique("chunk_id"),
            tables.not_null("IDDOC"),
            tables.not_null("REF"),
            tables.not_null("chunk_text"),
            tables.not_null("division", mostly=0.99, severity=tables.AMBER),
            tables.not_null("titre", mostly=0.99, severity=tables.AMBER),
        ],
        logger,
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## image_metadata
# MAGIC The key is `(IDDOC, image_id)`: the MERGE of the description step fails on a duplicate (RED). An image without a file on the volume (`EXTRACTION_FAILED`) is expected on less than 5 % of the images (AMBER).

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    tables.check_dataframe(
        df_image_metadata,
        "image_metadata",
        [
            tables.not_null("IDDOC"),
            tables.not_null("image_id"),
            tables.unique("IDDOC", "image_id"),
            tables.not_null("volume_path", mostly=0.95, severity=tables.AMBER),
        ],
        logger,
    )

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Write final Delta tables

# COMMAND ----------

if HAS_TARGET_IDDOCS:
    output_steps.write_outputs(
        df_processed_files,
        df_chunks_all,
        df_chunks_archive,
        df_image_metadata,
        parse_steps.JOB_RUN_ID,
        revised_iddocs,
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
