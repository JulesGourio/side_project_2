# Databricks notebook source
# MAGIC %md
# MAGIC # DEV copy (1/2) — export the UAT corpus to the staging volume
# MAGIC
# MAGIC **Description:**
# MAGIC Runs once, in the **UAT** workspace, as a one-off `databricks jobs submit`
# MAGIC (steps in `operations_dev.md`) — not part of any bundle target, so the
# MAGIC `qualibot-uat` target is left untouched. Writes a snapshot of the chunk
# MAGIC tables and of the parsing pipeline state to
# MAGIC `uat_landingzone.qualibot.staging`, the volume the DEV workspace can
# MAGIC already read (`lakebase_import_uat_to_dev` reads it the same way). The DEV
# MAGIC side (`import_staging_to_dev_job.py`) then recreates the tables in
# MAGIC `dev_landingzone.qualibot` and builds the Vector Search indexes, so the
# MAGIC DEV parsing pipeline never needs a full (GPU) run.
# MAGIC
# MAGIC Read-only on the UAT tables. Overwrites `{OUTPUT_DIR}` on every run.
# MAGIC
# MAGIC **Input Tables**
# MAGIC - `{SOURCE_CATALOG_SCHEMA}.<table>{TABLE_SUFFIX}` for each table of `TABLES`
# MAGIC - `{SOFFICE_ARCHIVE}` (LibreOffice archive used by the "Exact (PDF)" preview)
# MAGIC
# MAGIC **Outputs**
# MAGIC - `{OUTPUT_DIR}/tables/<table>` — one Delta (or Parquet) folder per table, suffix dropped
# MAGIC - `{OUTPUT_DIR}/libreoffice/<archive>.tar.gz`
# MAGIC - `{OUTPUT_DIR}/manifest.json` — row count per table, read back by the DEV import

# COMMAND ----------

import json
import os
from datetime import datetime, timezone

dbutils.widgets.text("SOURCE_CATALOG_SCHEMA", "uat_landingzone.qualibot")
# UAT table names carry no suffix once OPERATIONS.md D5 renamed them; "_v1" before.
dbutils.widgets.text("TABLE_SUFFIX", "")
# Chunk table (source of the Vector Search index) + parsing pipeline
# state (so the first DEV run is incremental). Archive tables (pre-2018 test
# phase) deliberately left out.
dbutils.widgets.text(
    "TABLES",
    "chunks,"
    "_pipeline_checkpoint,processed_files,image_metadata,parse_manifest,category_reference",
)
dbutils.widgets.text("OUTPUT_DIR", "/Volumes/uat_landingzone/qualibot/staging/dev_copy")
dbutils.widgets.dropdown("FORMAT", "delta", ["delta", "parquet"])
dbutils.widgets.text(
    "SOFFICE_ARCHIVE",
    "/Volumes/uat_landingzone/qualibot/doc_compare/libreoffice/libreoffice-25.8.7-linux-x64-v3.tar.gz",
)

SOURCE_CATALOG_SCHEMA = dbutils.widgets.get("SOURCE_CATALOG_SCHEMA").strip()
TABLE_SUFFIX = dbutils.widgets.get("TABLE_SUFFIX").strip()
TABLES = [t.strip() for t in dbutils.widgets.get("TABLES").split(",") if t.strip()]
OUTPUT_DIR = dbutils.widgets.get("OUTPUT_DIR").rstrip("/")
FORMAT = dbutils.widgets.get("FORMAT")
SOFFICE_ARCHIVE = dbutils.widgets.get("SOFFICE_ARCHIVE").strip()

if not OUTPUT_DIR.startswith("/Volumes/"):
    raise ValueError(f"OUTPUT_DIR must be a UC volume path, got {OUTPUT_DIR!r}")

import logging

logger = logging.getLogger("export_uat_to_staging")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False


logger.info(f"{SOURCE_CATALOG_SCHEMA}.*{TABLE_SUFFIX} -> {OUTPUT_DIR} ({FORMAT})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tables

# COMMAND ----------

manifest = {
    "exported_at": datetime.now(timezone.utc).isoformat(),
    "source_catalog_schema": SOURCE_CATALOG_SCHEMA,
    "table_suffix": TABLE_SUFFIX,
    "format": FORMAT,
    "tables": {},
}

missing = []
for table in TABLES:
    source = f"{SOURCE_CATALOG_SCHEMA}.{table}{TABLE_SUFFIX}"
    if not spark.catalog.tableExists(source):
        logger.info(f"  {source}: not found — skipped")
        missing.append(source)
        continue
    df = spark.table(source)
    path = f"{OUTPUT_DIR}/tables/{table}"
    df.write.format(FORMAT).mode("overwrite").option("overwriteSchema", "true").save(path)
    rows = spark.read.format(FORMAT).load(path).count()
    manifest["tables"][table] = {"source": source, "path": path, "rows": rows}
    logger.info(f"  {source}: {rows} rows -> {path}")

# The chunk table is what the index is built from — never export without it.
for required in ("chunks",):
    if required in TABLES and required not in manifest["tables"]:
        raise RuntimeError(f"{SOURCE_CATALOG_SCHEMA}.{required}{TABLE_SUFFIX} is missing — nothing to build the DEV index from")

# COMMAND ----------

# MAGIC %md
# MAGIC ## LibreOffice archive

# COMMAND ----------

if SOFFICE_ARCHIVE:
    target = f"{OUTPUT_DIR}/libreoffice/{os.path.basename(SOFFICE_ARCHIVE)}"
    dbutils.fs.cp(f"dbfs:{SOFFICE_ARCHIVE}", f"dbfs:{target}")
    manifest["soffice_archive"] = target
    logger.info(f"LibreOffice archive -> {target}")

dbutils.fs.put(f"dbfs:{OUTPUT_DIR}/manifest.json", json.dumps(manifest, indent=2), overwrite=True)
logger.info(json.dumps(manifest, indent=2))
if missing:
    logger.warning(f"WARNING: {len(missing)} table(s) not found, not exported: {missing}")
