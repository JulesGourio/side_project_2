# Databricks notebook source
# MAGIC %md
# MAGIC # DEV copy (2/2) — load the UAT snapshot into `dev_proj.qualibot`
# MAGIC
# MAGIC **Description:**
# MAGIC First task of `runbooks/scripts/dev_copy/copy_uat_to_dev_submit.json` (DEV workspace). Reads the snapshot of the UAT tables written to the staging volume
# MAGIC (UAT workspace) and recreates each table as
# MAGIC `{pipeline_write_env}_proj.qualibot.<table>`, with the same table
# MAGIC properties as the UAT tables: 60-day retention everywhere (see
# MAGIC `utils/parsing_pipeline/README.md`) and Change Data Feed on `chunks` (required by a Delta Sync
# MAGIC Vector Search index). The next tasks of the job create the endpoint and
# MAGIC the index — no run of the parsing pipeline needed.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC An existing target table is left alone unless `overwrite=true`:
# MAGIC replacing a chunk table under a live index breaks its incremental sync
# MAGIC (`DIFFERENT_DELTA_TABLE_READ_BY_STREAMING_SOURCE`).
# MAGIC
# MAGIC **Input**
# MAGIC - `{source_dir}/manifest.json`, `{source_dir}/tables/<table>`, `{source_dir}/libreoffice/*.tar.gz`
# MAGIC
# MAGIC **Output Tables**
# MAGIC - `{pipeline_write_env}_proj.qualibot.<table>` for each table of the manifest
# MAGIC - `{soffice_target_dir}/<archive>.tar.gz`

# COMMAND ----------

import json
import os

dbutils.widgets.removeAll()
dbutils.widgets.text("source_dir", "/Volumes/uat_proj/qualibot/staging/dev_copy")
dbutils.widgets.text("pipeline_write_env", "dev")
dbutils.widgets.dropdown("overwrite", "false", ["false", "true"])
dbutils.widgets.text("soffice_target_dir", "/Volumes/dev_proj/qualibot/doc_compare/libreoffice")

SOURCE_DIR = dbutils.widgets.get("source_dir").rstrip("/")
PIPELINE_WRITE_ENV = dbutils.widgets.get("pipeline_write_env")
OVERWRITE = dbutils.widgets.get("overwrite") == "true"
SOFFICE_TARGET_DIR = dbutils.widgets.get("soffice_target_dir").rstrip("/")

CHUNK_TABLES = {"chunks"}
RETENTION = "interval 60 days"

import logging

logger = logging.getLogger("import_staging_to_dev_job")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False

manifest = json.loads(dbutils.fs.head(f"dbfs:{SOURCE_DIR}/manifest.json", 1024 * 1024))
FORMAT = manifest["format"]
logger.info(f"Snapshot of {manifest['source_schema']}.* "
      f"exported {manifest['exported_at']} ({FORMAT}) -> {PIPELINE_WRITE_ENV}_proj.qualibot.*")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tables

# COMMAND ----------

skipped, loaded = [], []

for table, info in manifest["tables"].items():
    target = f"{PIPELINE_WRITE_ENV}_proj.qualibot.{table}"
    if spark.catalog.tableExists(target) and not OVERWRITE:
        logger.info(f"  {target}: already exists — left as is (overwrite=false)")
        skipped.append(target)
        continue

    df = spark.read.format(FORMAT).load(info["path"])
    df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(target)

    props = {
        "delta.deletedFileRetentionDuration": RETENTION,
        "delta.logRetentionDuration": RETENTION,
    }
    if table in CHUNK_TABLES:
        props["delta.enableChangeDataFeed"] = "true"
    props_sql = ", ".join(f"'{k}' = '{v}'" for k, v in props.items())
    spark.sql(f"ALTER TABLE {target} SET TBLPROPERTIES ({props_sql})")

    rows = spark.table(target).count()
    if rows != info["rows"]:
        raise RuntimeError(f"{target}: {rows} rows loaded, {info['rows']} exported")
    logger.info(f"  {target}: {rows} rows")
    loaded.append(target)

# COMMAND ----------

# MAGIC %md
# MAGIC ## LibreOffice archive

# COMMAND ----------

archive = manifest.get("soffice_archive")
if archive:
    target = f"{SOFFICE_TARGET_DIR}/{os.path.basename(archive)}"
    dbutils.fs.cp(f"dbfs:{archive}", f"dbfs:{target}")
    logger.info(f"LibreOffice archive -> {target}")

logger.info(f"{len(loaded)} table(s) loaded, {len(skipped)} left as is.")
if skipped and OVERWRITE is False:
    logger.info("Re-run with overwrite=true to replace them (then delete + recreate any index on a replaced chunk table).")
