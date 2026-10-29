# Databricks notebook source
# One-shot. Workspace UAT. Identity: your own account (no job runner needed).
# Rights needed: SELECT on the six uat_proj.qualibot tables below, WRITE VOLUME on uat_proj.qualibot.staging.
# Read-only on the UAT tables. Writes a parquet snapshot of each to the UAT staging volume, to be copied to PROD by hand
# (the PROD workspace cannot read uat_proj). Run it when the UAT parsing job is not running, so the six tables are from
# the same moment.

# COMMAND ----------

import json
from datetime import datetime, timezone

SOURCE_SCHEMA = "uat_proj.qualibot"
TABLES = ["chunks", "parsing_checkpoint", "parsed_documents", "image_metadata", "parse_manifest", "category_reference"]
OUTPUT_DIR = "/Volumes/uat_proj/qualibot/staging/prod_copy"

# COMMAND ----------

manifest = {"exported_at": datetime.now(timezone.utc).isoformat(), "source_schema": SOURCE_SCHEMA, "tables": {}}
for table in TABLES:
    path = f"{OUTPUT_DIR}/tables/{table}"
    df = spark.table(f"{SOURCE_SCHEMA}.{table}")
    n_source = df.count()
    df.write.format("parquet").mode("overwrite").save(path)
    n_written = spark.read.format("parquet").load(path).count()
    if n_written != n_source:
        raise RuntimeError(f"{table}: {n_written} rows written, {n_source} in the table")
    manifest["tables"][table] = {"rows": n_written}
    print(f"{table}: {n_written} rows")

dbutils.fs.put(f"dbfs:{OUTPUT_DIR}/manifest.json", json.dumps(manifest, indent=2), overwrite=True)
print(json.dumps(manifest, indent=2))
