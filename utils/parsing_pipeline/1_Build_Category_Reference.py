# Databricks notebook source
# MAGIC %md
# MAGIC # 01 — Freshness guard + Build Category Reference
# MAGIC
# MAGIC **Description:**
# MAGIC First link of the daily chain. Two roles:
# MAGIC
# MAGIC 1. **Freshness guard** — checks that the Intraqual source tables were
# MAGIC    refreshed by the `intraqual_ingestion` job within
# MAGIC    `MAX_SOURCE_STALENESS_HOURS`. A stale source must **fail** the run:
# MAGIC    otherwise the pipeline recomputes a scope on stale data and silently
# MAGIC    ships a partial index.
# MAGIC 2. **Category consolidation** — `division` / `niveau_plus_1..N` per IDDOC
# MAGIC    into `category_reference`, from `gd_doc_cat_latest` x `gd_cat_latest`
# MAGIC    (+ `DIVISION_ARCHIVE_TABLE` if enabled, empty by default —
# MAGIC    prod_bronze resolves 99.6% alone).
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `1_categories`)
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - None
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - `{PARSING_INGESTION_FRESHNESS_TABLE}`
# MAGIC - `{PARSING_INTRAQUAL_BRONZE}.gd_doc_cat_latest` (via `selection.build_division_reference`)
# MAGIC - `{PARSING_INTRAQUAL_BRONZE}.gd_cat_latest` (via `selection.build_division_reference`)
# MAGIC - `{PARSING_DIVISION_ARCHIVE_TABLE}` (via `selection.build_division_reference`, disabled by default — empty)
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PARSING_CATALOG_SCHEMA}.category_reference{PARSING_TABLE_SUFFIX}`

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The tables behind `category_reference` (`gd_doc_cat_latest`, `gd_cat_latest`, the optional division archive) are read inside `selection.build_division_reference`, not in `# Inputs`: this notebook cannot show them or check them on its own.
# MAGIC - The guard does not cover `DIVISION_ARCHIVE_TABLE` (disabled by default): enabling it means adding it to `_REQUIRED_SOURCES`.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Imports
# MAGIC `selection` and `config` live next to this notebook; the repository folder is added to `sys.path` because a job task does not do it for a notebook.

# COMMAND ----------

import os
import sys

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F
import selection
from utils import logger
from config import (
    DIVISION_ARCHIVE_TABLE, DIVISION_REFERENCE_TABLE,
    GD_DOC_LATEST, GD_DOC_CAT_LATEST, GD_CAT_LATEST, GD_TYPDOC_LATEST,
    GD_UTILISATEUR_LATEST, INGESTION_FRESHNESS_TABLE, MAX_SOURCE_STALENESS_HOURS,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Constants
# MAGIC The guard checks the five Intraqual tables the whole chain reads (documents, categories, document types, users). They are named once here so the guard and the logs agree.

# COMMAND ----------

logger.info(f"Output      : {DIVISION_REFERENCE_TABLE}")
logger.info(f"Archive     : {DIVISION_ARCHIVE_TABLE or '(disabled)'}")

_REQUIRED_SOURCES = [
    GD_DOC_LATEST, GD_DOC_CAT_LATEST, GD_CAT_LATEST,
    GD_TYPDOC_LATEST, GD_UTILISATEUR_LATEST,
]

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Ingestion freshness table
# MAGIC One row per Intraqual table with the time of its last refresh, written by the `intraqual_ingestion` job. It is read only to compute the age of the required tables.
# MAGIC
# MAGIC The category tables themselves are read inside `selection.build_division_reference()`, see Technical debt.
# MAGIC
# MAGIC `MAX_SOURCE_STALENESS_HOURS` set to 0 disables the guard (catch-up run after an ingestion outage).

# COMMAND ----------

if MAX_SOURCE_STALENESS_HOURS <= 0:
    df_fresh = None
else:
    df_fresh = (
        spark.table(INGESTION_FRESHNESS_TABLE)
        .filter(F.lower(F.col("table_name")).isin([t.lower() for t in _REQUIRED_SOURCES]))
        .select(
            "table_name",
            "last_update_time",
            F.round(
                (F.unix_timestamp(F.current_timestamp()) - F.unix_timestamp("last_update_time")) / 3600.0, 1
            ).alias("age_hours"),
        )
        .orderBy(F.desc("age_hours"))
    )

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation
# MAGIC #N/A

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Build category_reference
# MAGIC One row per IDDOC with its `division` and `niveau_plus_1..N`, from the document-category and category tables (the archive table only fills the gaps when enabled). `division_source` says where each row was resolved, so the count per source is logged.

# COMMAND ----------

df_reference = selection.build_division_reference(spark).cache()

n_total = df_reference.count()
logger.info(f"{n_total} IDDOCs with a resolved division")
(
    df_reference.groupBy("division_source").count()
    .withColumnRenamed("count", "n_iddocs")
    .orderBy(F.desc("n_iddocs"))
    .show(truncate=False)
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC ## Freshness guard (RED)
# MAGIC A stale or missing source must fail the run before anything is written: otherwise the next tasks compute the scope on old data and the index silently misses documents. The message names the stale and the missing tables, since the usual cause is a failed `intraqual_ingestion` job.

# COMMAND ----------

if df_fresh is None:
    logger.info("Freshness guard disabled (MAX_SOURCE_STALENESS_HOURS <= 0).")
else:
    _rows = df_fresh.collect()
    for r in _rows:
        logger.info(f"  {r.age_hours:>6} h  {r.table_name}")

    _seen = {r.table_name.lower() for r in _rows}
    _absent = [t for t in _REQUIRED_SOURCES if t.lower() not in _seen]
    _stale = [(r.table_name, r.age_hours) for r in _rows if r.age_hours > MAX_SOURCE_STALENESS_HOURS]
    if _absent or _stale:
        raise RuntimeError(
            f"Intraqual sources not fresh (threshold {MAX_SOURCE_STALENESS_HOURS}h) — "
            f"the `intraqual_ingestion` job likely failed.\n"
            f"  Stale: {_stale or 'none'}\n"
            f"  Missing from {INGESTION_FRESHNESS_TABLE}: {_absent or 'none'}"
        )
    logger.info(f"All sources fresh (< {MAX_SOURCE_STALENESS_HOURS}h).")

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Write category_reference
# MAGIC Full overwrite with `overwriteSchema`: the table is rebuilt from scratch every day, so a column added to the source must reach it without a manual migration.

# COMMAND ----------

(
    df_reference.write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(DIVISION_REFERENCE_TABLE)
)
logger.info(f"Saved: {DIVISION_REFERENCE_TABLE} ({n_total} rows)")
