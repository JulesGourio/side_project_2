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
# MAGIC # Technical Debt
# MAGIC
# MAGIC None.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports

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
# MAGIC ## Constants

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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ingestion freshness table
# MAGIC
# MAGIC `gd_doc_cat_latest`/`gd_cat_latest`/the division archive are read inside `selection.build_division_reference()`, not here.

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
# MAGIC # Data Transformations

# COMMAND ----------

# MAGIC %md
# MAGIC ## Freshness guard
# MAGIC
# MAGIC Set `MAX_SOURCE_STALENESS_HOURS` to 0 to disable (catch-up run).

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
# MAGIC ## Build category_reference

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
# MAGIC # Outputs

# COMMAND ----------

(
    df_reference.write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(DIVISION_REFERENCE_TABLE)
)
logger.info(f"Saved: {DIVISION_REFERENCE_TABLE} ({n_total} rows)")