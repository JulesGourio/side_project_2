# Databricks notebook source
# MAGIC %md
# MAGIC # PROJ CATEGORY_REFERENCE
# MAGIC
# MAGIC **Description:**
# MAGIC First link of the daily chain. Two roles:
# MAGIC 1. **Freshness guard** — checks that the Intraqual data asset tables were last written within
# MAGIC    `MAX_SOURCE_STALENESS_HOURS`. A stale source must **fail** the run: otherwise the pipeline recomputes a scope on stale
# MAGIC    data and silently ships a partial index.
# MAGIC 2. **Category consolidation** — `division` / `niveau_plus_1..N` per IDDOC into `category_reference`, from
# MAGIC    `document_revisions_intraqual` x `category_ancestors_intraqual` (+ `DIVISION_ARCHIVE_TABLE` if enabled, empty by default).
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC The freshness guard fails the task after `category_reference` is built but before it is written, so a stale run leaves the previous table in place.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `1_categories`)
# MAGIC
# MAGIC **Inputs Data**
# MAGIC - `{REFERENCE_READ_ENV}_gold.quality_system.document_revisions_intraqual`, `category_ancestors_intraqual` (Intraqual data asset; read by `categories.build_division_reference`, last write time read by the freshness guard)
# MAGIC - `{PARSING_DIVISION_ARCHIVE_TABLE}` (same function, disabled by default: empty)
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.category_reference`

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The tables behind `category_reference` are read in `categories.build_division_reference`, not in `# Inputs`: this notebook cannot show or check them on its own.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Standard Package Imports
# MAGIC The repository folder is added to `sys.path` because a job task does not do it for a notebook.

# COMMAND ----------

import os
import sys

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(
    0, os.path.dirname(os.path.dirname(REPO_DIR))
)  # leap_utils/; inserted first so that REPO_DIR (its own `utils` module) stays ahead
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as f

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Function Imports
# MAGIC `categories`, `tables` and `config` live next to this notebook.

# COMMAND ----------

import categories
import tables

tables.require_gx()  # fails here, not after the work, on a cluster without Great Expectations
from tables import ensure_primary_key, write_table
from leap_utils.common import logger as log_utils

from config import (
    DIVISION_ARCHIVE_TABLE,
    DIVISION_REFERENCE_TABLE,
    INTRAQUAL_REQUIRED_TABLES,
    MAX_SOURCE_STALENESS_HOURS,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config logger
# MAGIC The logger is created once the widgets are read, so the level they give applies to every line after.

# COMMAND ----------

logger = log_utils.setup_applevel_logger(log_level=log_utils.LOGGER_MAPPING[LOG_LEVEL])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Constants
# MAGIC The guard checks the Intraqual data asset tables the whole chain reads (revisions and category ancestors). They are named once in `config.py` so the guard and the logs agree.

# COMMAND ----------

logger.info(f"Output      : {DIVISION_REFERENCE_TABLE}")
logger.info(f"Archive     : {DIVISION_ARCHIVE_TABLE or '(disabled)'}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Age of the source tables
# MAGIC Hours since each required table was last written, read from the Delta table details. The Intraqual data asset rewrites them on every run, so their age is the age of the last successful run of that job.
# MAGIC
# MAGIC A `MAX_SOURCE_STALENESS_HOURS` of 0 disables the guard.

# COMMAND ----------

source_ages = (
    {}
    if MAX_SOURCE_STALENESS_HOURS <= 0
    else categories.source_ages_hours(spark, INTRAQUAL_REQUIRED_TABLES)
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation
# MAGIC #N/A

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Build category_reference
# MAGIC One row per IDDOC with its `division` and `niveau_plus_1..N`, from the document-category and category tables (the archive table only fills the gaps when enabled). `division_source` says where each row was resolved, and, in debug mode, the count per source is shown.

# COMMAND ----------

df_reference = categories.build_division_reference(spark).cache()

if DEBUG:
    (
        df_reference.groupBy("division_source")
        .count()
        .withColumnRenamed("count", "n_iddocs")
        .orderBy(f.desc("n_iddocs"))
        .show(truncate=False)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC ## Freshness guard (RED)
# MAGIC A stale or unreadable source must fail the run before anything is written: otherwise the next tasks compute the scope on old data and the index silently misses documents. The message names the stale and the unreadable tables, since the usual cause is a failed Intraqual data asset job.

# COMMAND ----------

if MAX_SOURCE_STALENESS_HOURS <= 0:
    logger.info("Freshness guard disabled (MAX_SOURCE_STALENESS_HOURS <= 0).")
else:
    for table, age in source_ages.items():
        logger.info(f"  {age if age is not None else '?':>6} h  {table}")

    _unreadable = [t for t, age in source_ages.items() if age is None]
    _stale = [
        (t, age)
        for t, age in source_ages.items()
        if age is not None and age > MAX_SOURCE_STALENESS_HOURS
    ]
    if _unreadable or _stale:
        raise RuntimeError(
            f"Intraqual sources not fresh (threshold {MAX_SOURCE_STALENESS_HOURS}h) — "
            f"the Intraqual data asset job likely failed.\n"
            f"  Stale: {_stale or 'none'}\n"
            f"  Missing or unreadable: {_unreadable or 'none'}"
        )
    logger.info(f"All sources fresh (< {MAX_SOURCE_STALENESS_HOURS}h).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## category_reference
# MAGIC The key is `IDDOC`: a duplicate would multiply the rows of every join made with this table by the next tasks (RED). An empty table would give every document no division (RED). `division` is expected on 99 % of the documents (AMBER).

# COMMAND ----------

tables.check_dataframe(
    df_reference,
    "category_reference",
    [
        tables.non_empty(),
        tables.not_null("IDDOC"),
        tables.unique("IDDOC"),
        tables.not_null("division", mostly=0.99, severity=tables.AMBER),
    ],
    logger,
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Write category_reference
# MAGIC Full overwrite with `overwriteSchema`: the table is rebuilt from scratch every day, so a column added to the source must reach it without a manual migration.

# COMMAND ----------

write_table(df_reference, DIVISION_REFERENCE_TABLE, "overwrite", overwrite_schema=True)
ensure_primary_key(DIVISION_REFERENCE_TABLE, ["IDDOC"])
logger.info(f"Saved: {DIVISION_REFERENCE_TABLE}")
