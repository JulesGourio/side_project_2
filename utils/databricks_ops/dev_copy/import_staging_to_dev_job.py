# Databricks notebook source
# MAGIC %md
# MAGIC # DEV copy (2/2) — load the UAT snapshot into `dev_landingzone.qualibot`
# MAGIC
# MAGIC **Description:**
# MAGIC First task of the DEV job `qualibot-copy-uat-to-dev` (`databricks.yml`,
# MAGIC target `dev`). Reads the snapshot written by `export_uat_to_staging.py`
# MAGIC (UAT workspace) from the staging volume and recreates each table as
# MAGIC `{TARGET_CATALOG_SCHEMA}.<table>{TABLE_SUFFIX}`, with the same table
# MAGIC properties as the UAT `_v1` tables: 60-day retention everywhere (see
# MAGIC `databricks.yml`, `qualibot-uat` variables, for the incident behind it)
# MAGIC and Change Data Feed on the 3 chunk tables (required by Delta Sync
# MAGIC Vector Search indexes). The next tasks of the job create the endpoint and
# MAGIC the indexes — no run of the parsing pipeline needed.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC An existing target table is left alone unless `OVERWRITE=true`:
# MAGIC replacing a chunk table under a live index breaks its incremental sync
# MAGIC (`DIFFERENT_DELTA_TABLE_READ_BY_STREAMING_SOURCE`). After an overwrite of
# MAGIC a chunk table, delete its index and let the next task recreate it.
# MAGIC
# MAGIC **Input**
# MAGIC - `{SOURCE_DIR}/manifest.json`, `{SOURCE_DIR}/tables/<table>`, `{SOURCE_DIR}/libreoffice/*.tar.gz`
# MAGIC
# MAGIC **Output Tables**
# MAGIC - `{TARGET_CATALOG_SCHEMA}.<table>{TABLE_SUFFIX}` for each table of the manifest
# MAGIC - `{SOFFICE_TARGET_DIR}/<archive>.tar.gz`

# COMMAND ----------

import json
import os

dbutils.widgets.text("SOURCE_DIR", "/Volumes/uat_landingzone/qualibot/staging/dev_copy")
dbutils.widgets.text("TARGET_CATALOG_SCHEMA", "dev_landingzone.qualibot")
dbutils.widgets.text("TABLE_SUFFIX", "_v1")
dbutils.widgets.dropdown("OVERWRITE", "false", ["false", "true"])
dbutils.widgets.text("SOFFICE_TARGET_DIR", "/Volumes/dev_landingzone/qualibot/doc_compare/libreoffice")

SOURCE_DIR = dbutils.widgets.get("SOURCE_DIR").rstrip("/")
TARGET_CATALOG_SCHEMA = dbutils.widgets.get("TARGET_CATALOG_SCHEMA").strip()
TABLE_SUFFIX = dbutils.widgets.get("TABLE_SUFFIX").strip()
OVERWRITE = dbutils.widgets.get("OVERWRITE") == "true"
SOFFICE_TARGET_DIR = dbutils.widgets.get("SOFFICE_TARGET_DIR").rstrip("/")

CHUNK_TABLES = {"chunks", "src_chunks_as", "src_chunks_is"}
RETENTION = "interval 60 days"

manifest = json.loads(dbutils.fs.head(f"dbfs:{SOURCE_DIR}/manifest.json", 1024 * 1024))
FORMAT = manifest["format"]
print(f"Snapshot of {manifest['source_catalog_schema']}.*{manifest['table_suffix']} "
      f"exported {manifest['exported_at']} ({FORMAT}) -> {TARGET_CATALOG_SCHEMA}.*{TABLE_SUFFIX}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tables

# COMMAND ----------

skipped, loaded = [], []
for table, info in manifest["tables"].items():
    target = f"{TARGET_CATALOG_SCHEMA}.{table}{TABLE_SUFFIX}"
    if spark.catalog.tableExists(target) and not OVERWRITE:
        print(f"  {target}: already exists — left as is (OVERWRITE=false)")
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
    print(f"  {target}: {rows} rows")
    loaded.append(target)

# COMMAND ----------

# MAGIC %md
# MAGIC ## LibreOffice archive

# COMMAND ----------

archive = manifest.get("soffice_archive")
if archive:
    target = f"{SOFFICE_TARGET_DIR}/{os.path.basename(archive)}"
    dbutils.fs.cp(f"dbfs:{archive}", f"dbfs:{target}")
    print(f"LibreOffice archive -> {target}")

print(f"\n{len(loaded)} table(s) loaded, {len(skipped)} left as is.")
if skipped and OVERWRITE is False:
    print("Re-run with OVERWRITE=true to replace them (then delete + recreate any index on a replaced chunk table).")
