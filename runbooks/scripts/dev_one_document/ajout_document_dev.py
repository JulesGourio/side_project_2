# Databricks notebook source
# One-shot DEV: parse one document (categories, manifest, parse, chunks, image descriptions) on a single cluster.
# Copy this notebook into the workspace folder that holds create_proj_chunks (deployed pipeline), attach it to a DEV
# single-user cluster (runtime 16.4) and run it. Runs with the rights of the user who runs it. Idempotent.

# COMMAND ----------

import logging
import os
import time

logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
logger = logging.getLogger("ajout_document_dev")

IDDOC = "25531"
PIPELINE_ENV = "dev"
REFERENCE_ENV = "prod"
TASK_TIMEOUT_SECONDS = 3 * 3600
STEPS = [
    "create_proj_category_reference",
    "create_proj_parse_manifest",
    "create_proj_parsing_checkpoint",
    "create_proj_chunks",
    "create_proj_image_metadata",
]
ARGS = {
    "pipeline_write_env": PIPELINE_ENV,
    "pipeline_read_env": PIPELINE_ENV,
    "reference_read_env": REFERENCE_ENV,
    "run_mode": "incremental",
    "parse_filter": IDDOC,
    "max_parse_files": "500",
    "max_source_staleness_hours": "36",
    "redescribe_all_images": "false",
    "describe_images": "true",
    "job_run_id": f"manual-ajout-{IDDOC}",
}

# COMMAND ----------

# Fails fast on a missing right or a missing cluster setting, before any long step.
spark.sql(f"SELECT 1 FROM {PIPELINE_ENV}_proj.qualibot.parse_manifest LIMIT 1").collect()
row = spark.sql(
    f"SELECT document_reference, root_category_ID, revision_status_code, lifecycle_state_code, is_hidden "
    f"FROM {REFERENCE_ENV}_gold.quality_system.document_revisions_intraqual "
    f"WHERE document_revision_ID = {IDDOC}"
).first()
if row is None:
    raise ValueError(f"IDDOC {IDDOC} not found in document_revisions_intraqual")
logger.info(f"IDDOC {IDDOC}: {row.asDict()}")
if os.environ.get("PARSING_IMAGES_VOLUME") != "images":
    raise RuntimeError("Cluster env var PARSING_IMAGES_VOLUME=images is missing: set it in the cluster Spark config and restart")

# COMMAND ----------

for step in STEPS:
    start = time.time()
    logger.info(f"[{step}] start")
    result = dbutils.notebook.run(step, TASK_TIMEOUT_SECONDS, ARGS)
    logger.info(f"[{step}] done in {time.time() - start:.0f}s: {result}")

# COMMAND ----------

for table in ["parse_manifest", "parsed_documents", "chunks", "image_metadata"]:
    try:
        count = spark.sql(f"SELECT COUNT(*) AS n FROM {PIPELINE_ENV}_proj.qualibot.{table} WHERE IDDOC = {IDDOC}").first().n
        logger.info(f"{table}: {count} rows for IDDOC {IDDOC}")
    except Exception as error:
        logger.warning(f"{table}: count failed ({type(error).__name__})")
