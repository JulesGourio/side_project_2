# Databricks notebook source
# MAGIC %md
# MAGIC # PROJ CATEGORY_REFERENCE
# MAGIC
# MAGIC **Description:**
# MAGIC First link of the daily chain. Two roles:
# MAGIC 1. **Freshness guard** — checks that the Intraqual source tables were refreshed by the `intraqual_ingestion` job within
# MAGIC    `MAX_SOURCE_STALENESS_HOURS`. A stale source must **fail** the run: otherwise the pipeline recomputes a scope on stale
# MAGIC    data and silently ships a partial index.
# MAGIC 2. **Category consolidation** — `division` / `niveau_plus_1..N` per IDDOC into `category_reference`, from
# MAGIC    `gd_doc_cat_latest` x `gd_cat_latest` (+ `DIVISION_ARCHIVE_TABLE` if enabled, empty by default).
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC The freshness guard fails the task after `category_reference` is built but before it is written, so a stale run leaves the previous table in place.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `1_categories`)
# MAGIC
# MAGIC **Inputs Data**
# MAGIC - `{PARSING_INGESTION_FRESHNESS_TABLE}`
# MAGIC - `{PARSING_INTRAQUAL_BRONZE}.gd_doc_cat_latest`, `gd_cat_latest` (read by `categories.build_division_reference`)
# MAGIC - `{PARSING_DIVISION_ARCHIVE_TABLE}` (same function, disabled by default: empty)
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PARSING_WRITE_ENV}_proj.qualibot.category_reference`

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The tables behind `category_reference` are read in `categories.build_division_reference`, not in `# Inputs`: this notebook cannot show or check them on its own.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration
# MAGIC ## Config Standard Package Imports
# MAGIC The repository folder is added to `sys.path` because a job task does not do it for a notebook.

# COMMAND ----------

import os
import sys

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, os.path.dirname(os.path.dirname(REPO_DIR)))  # leap_utils/; inserted first so that REPO_DIR (its own `utils` module) stays ahead
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Project Imports
# MAGIC `categories`, `quality`, `tables` and `config` live next to this notebook.

# COMMAND ----------

import categories
import quality
from tables import ensure_primary_key, write_table
from leap_utils.common import logger as log_utils

from config import (
    DIVISION_ARCHIVE_TABLE, DIVISION_REFERENCE_TABLE,
    GD_DOC_LATEST, GD_DOC_CAT_LATEST, GD_CAT_LATEST, GD_TYPDOC_LATEST,
    GD_UTILISATEUR_LATEST, INGESTION_FRESHNESS_TABLE, MAX_SOURCE_STALENESS_HOURS,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets
# MAGIC This task takes no parameter: everything comes from the `PARSING_*` environment variables of the cluster.

# COMMAND ----------

dbutils.widgets.removeAll()

dbutils.widgets.dropdown("log_level", "info", ["debug", "info", "warning", "error", "critical"])
LOG_LEVEL = dbutils.widgets.get("log_level")
logger = log_utils.setup_applevel_logger(log_level=log_utils.LOGGER_MAPPING[LOG_LEVEL])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Constants
# MAGIC The guard checks the five Intraqual tables the whole chain reads (documents, categories, document types, users). They are named once here so the guard and the logs agree.

# COMMAND ----------

logger.info(f"Output      : {DIVISION_REFERENCE_TABLE}")
logger.info(f"Archive     : {DIVISION_ARCHIVE_TABLE or '(disabled)'}")

REQUIRED_SOURCES = [
    GD_DOC_LATEST, GD_DOC_CAT_LATEST, GD_CAT_LATEST,
    GD_TYPDOC_LATEST, GD_UTILISATEUR_LATEST,
]

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Ingestion freshness table
# MAGIC One row per Intraqual table with the time of its last refresh, written by the `intraqual_ingestion` job. It is read only to compute the age of the required tables.
# MAGIC
# MAGIC The category tables themselves are read inside `categories.build_division_reference()`, see Technical debt.
# MAGIC
# MAGIC A `MAX_SOURCE_STALENESS_HOURS` of 0 disables the guard.

# COMMAND ----------

if MAX_SOURCE_STALENESS_HOURS <= 0:
    df_fresh = None
else:
    df_fresh = (
        spark.table(INGESTION_FRESHNESS_TABLE)
        .filter(F.lower(F.col("table_name")).isin([t.lower() for t in REQUIRED_SOURCES]))
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

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Build category_reference
# MAGIC One row per IDDOC with its `division` and `niveau_plus_1..N`, from the document-category and category tables (the archive table only fills the gaps when enabled). `division_source` says where each row was resolved, so the count per source is logged.

# COMMAND ----------

df_reference = categories.build_division_reference(spark).cache()

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
    _absent = [t for t in REQUIRED_SOURCES if t.lower() not in _seen]
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
# MAGIC ## category_reference
# MAGIC The key is `IDDOC`: a duplicate would multiply the rows of every join made with this table by the next tasks (RED). An empty table would give every document no division (RED). `division` is expected on 99 % of the documents (AMBER).

# COMMAND ----------

quality.check_dataframe(df_reference, "category_reference", [
    quality.non_empty(),
    quality.not_null("IDDOC"),
    quality.unique("IDDOC"),
    quality.not_null("division", mostly=0.99, severity=quality.AMBER),
], logger)

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Write category_reference
# MAGIC Full overwrite with `overwriteSchema`: the table is rebuilt from scratch every day, so a column added to the source must reach it without a manual migration.

# COMMAND ----------

write_table(df_reference, DIVISION_REFERENCE_TABLE, "overwrite", overwrite_schema=True)
ensure_primary_key(DIVISION_REFERENCE_TABLE, ["IDDOC"])
logger.info(f"Saved: {DIVISION_REFERENCE_TABLE}")
