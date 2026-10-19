# Databricks notebook source
# MAGIC %md
# MAGIC # DEV copy (1/2) — export the UAT corpus to the staging volume
# MAGIC
# MAGIC **Description:**
# MAGIC Runs once, in the **UAT** workspace, as a one-off `databricks jobs submit`
# MAGIC — not part of any bundle target, so the
# MAGIC `qualibot-uat` target is left untouched. Writes a snapshot of the chunk
# MAGIC tables and of the parsing pipeline state to
# MAGIC `uat_proj.qualibot.staging`, the volume the DEV workspace can
# MAGIC already read. The DEV
# MAGIC side (`import_staging_to_dev_job.py`) then recreates the tables in
# MAGIC `dev_proj.qualibot` and builds the Vector Search indexes, so the
# MAGIC DEV parsing pipeline never needs a full (GPU) run.
# MAGIC
# MAGIC Read-only on the UAT tables. Overwrites `{OUTPUT_DIR}` on every run.
# MAGIC
# MAGIC **Run by hand** (from the repo root, CLI profile `UAT`; no bundle job runs it):
# MAGIC ```
# MAGIC databricks workspace import /Users/jules.gourio.external@latecoere.aero/qualibot_dev_copy/export_uat_to_staging --file runbooks/scripts/dev_copy/export_uat_to_staging.py --format SOURCE --language PYTHON --overwrite --profile UAT
# MAGIC databricks jobs submit --json @runbooks/scripts/dev_copy/export_uat_submit.json --profile UAT
# MAGIC ```
# MAGIC
# MAGIC **Input Tables**
# MAGIC - `{SOURCE_SCHEMA}.<table>` for each table of `TABLES`
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

dbutils.widgets.removeAll()
# Chunk table (source of the Vector Search index) and parsing pipeline state
# (so the first DEV run is incremental). Archive tables are left out on purpose.
dbutils.widgets.text(
    "TABLES",
    "chunks,"
    "parsing_checkpoint,parsed_documents,image_metadata,parse_manifest,category_reference",
)
dbutils.widgets.text("OUTPUT_DIR", "/Volumes/uat_proj/qualibot/staging/dev_copy")
dbutils.widgets.dropdown("FORMAT", "delta", ["delta", "parquet"])
dbutils.widgets.text(
    "SOFFICE_ARCHIVE",
    "/Volumes/uat_proj/qualibot/doc_compare/libreoffice/libreoffice-25.8.7-linux-x64-v3.tar.gz",
)

SOURCE_SCHEMA = "uat_proj.qualibot"
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


logger.info(f"{SOURCE_SCHEMA}.* -> {OUTPUT_DIR} ({FORMAT})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tables

# COMMAND ----------

manifest = {
    "exported_at": datetime.now(timezone.utc).isoformat(),
    "source_schema": SOURCE_SCHEMA,
    "format": FORMAT,
    "tables": {},
}

missing = []
for table in TABLES:
    source = f"{SOURCE_SCHEMA}.{table}"
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
        raise RuntimeError(f"{SOURCE_SCHEMA}.{required} is missing — nothing to build the DEV index from")

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
