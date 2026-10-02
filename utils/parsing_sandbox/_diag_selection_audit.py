# Databricks notebook source
# One-off, read-mostly diagnostic: full recursive volume scan + selection.build_unified_audit().
# Writes only to a new _selection_audit_diag table (not part of the pipeline's own tables) — safe
# to run against UAT any time, no effect on processed_files/checkpoint/chunks.
# Not a pipeline task — not registered in resources/parsing_pipeline.job.yml. Delete after use.

# COMMAND ----------

import os
import sys
import time as _time

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, REPO_DIR)

for _mod in ("utils.py", "image_utils.py", "selection.py", "config.py"):
    spark.sparkContext.addPyFile(os.path.join(REPO_DIR, _mod))

import selection
from utils import logger
from config import *

# COMMAND ----------

logger.info(f"Volume root: {VOLUME_ROOT_PATH}")
logger.info(f"Catalog/schema: {CATALOG_SCHEMA} | table suffix: {TABLE_SUFFIX}")

# COMMAND ----------

# Folder-name-level check: does the top-level dbutils.fs.ls name always resolve an IDDOC
# via selection.extract_iddoc_from_name()? This is exactly what the incremental scan path
# in 3_Parse_Pipeline_v2.py relies on to decide which folders even get looked at.
_t0 = _time.time()
_root_dbfs = VOLUME_ROOT_PATH.replace("/Volumes/", "dbfs:/Volumes/")
_root_items = dbutils.fs.ls(_root_dbfs)
_unmatched_folder_names = [
    item.name for item in _root_items
    if selection.extract_iddoc_from_name(item.name) is None
]
logger.info(f"[FOLDER NAMES] {len(_root_items)} top-level items under the volume root")
logger.info(f"[FOLDER NAMES] {len(_unmatched_folder_names)} do NOT match ^[Dd]m?_(\\d+) at all "
            f"(invisible to the incremental targeted scan in 3_Parse_Pipeline_v2.py)")
if _unmatched_folder_names:
    logger.info(f"[FOLDER NAMES] sample of unmatched names: {_unmatched_folder_names[:30]}")

# COMMAND ----------

# Full recursive scan (metadata only — content column stays lazy/unread).
# Expensive relative to the targeted scan, but this is a one-off diagnostic, not the daily job.
logger.info("[SCAN] Starting full recursive volume scan (metadata only)...")
df_meta_raw, df_content = selection.scan_volume_files(spark, VOLUME_ROOT_PATH)
df_meta_raw.cache()
_n_files = df_meta_raw.count()
_n_iddocs_seen = df_meta_raw.select("IDDOC").distinct().count()
logger.info(f"[SCAN] {_n_files} files found, {_n_iddocs_seen} distinct IDDOCs resolved, "
            f"{_time.time() - _t0:.1f}s")

# COMMAND ----------

df_business_meta, df_doc_lookup, df_kb_lookup = selection.load_business_metadata(spark)
df_business_meta.cache()
logger.info(f"[BUSINESS] {df_business_meta.count()} IDDOCs in business scope")

df_matched_full = selection.rank_candidates(df_meta_raw, df_business_meta)

# COMMAND ----------

DIAG_TABLE = f"{CATALOG_SCHEMA}._selection_audit_diag{TABLE_SUFFIX}"
df_audit = selection.build_unified_audit(
    spark, df_meta_raw, df_business_meta, df_matched_full,
    df_doc_lookup, df_kb_lookup, ingestion_run_id="diag-selection-audit",
    target_table=DIAG_TABLE,
)
logger.info(f"[AUDIT] Written to {DIAG_TABLE}")

# COMMAND ----------

# Headline summary, visible directly in the run's stdout log.
from pyspark.sql import functions as F

logger.info("[AUDIT] audit_type / root_cause breakdown:")
for row in (
    df_audit.groupBy("audit_type", "root_cause")
    .agg(F.count("*").alias("n"), F.countDistinct("IDDOC").alias("n_iddoc"))
    .orderBy(F.desc("n"))
    .collect()
):
    logger.info(f"  {row['audit_type']:20s} {row['root_cause']:35s} n={row['n']:>7} n_iddoc={row['n_iddoc']:>7}")

# COMMAND ----------

# Focused view: among currently-selected (rank 1) files with a non-standard or Dm_ name,
# was a better candidate (lower rank) available at all for the same IDDOC?
logger.info("[AUDIT] For rank-1 Dm_/non-standard selections, was there ANY other candidate file "
            "for the same IDDOC (i.e. could a real D_/other file exist that this run's incremental "
            "top-level scan might have missed, or that lost the tie-break)?")
_susceptible = (
    df_matched_full.filter(F.col("priority_rank") == 1)
    .filter(~F.col("source_file_name").rlike(r"^[dD]_"))
    .select("IDDOC", F.col("files_count_for_iddoc"))
    .distinct()
)
_susceptible_multi = _susceptible.filter(F.col("files_count_for_iddoc") > 1).count()
_susceptible_single = _susceptible.filter(F.col("files_count_for_iddoc") == 1).count()
logger.info(f"[AUDIT] non-D_ rank-1 selections with >1 candidate file for that IDDOC "
            f"(a real alternative existed — inspect depriority_reason in {DIAG_TABLE}): {_susceptible_multi}")
logger.info(f"[AUDIT] non-D_ rank-1 selections that are the ONLY file found for that IDDOC "
            f"(nothing else exists on the volume for it): {_susceptible_single}")

logger.info("[DONE] Diagnostic complete.")
