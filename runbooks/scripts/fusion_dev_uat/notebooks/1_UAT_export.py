# Databricks notebook source
# One-shot. Workspace UAT. Identity: your own account (no job runner needed).
# Rights needed: SELECT on uat_proj.qualibot.parsing_checkpoint and uat_proj.qualibot.image_metadata, WRITE VOLUME on uat_proj.qualibot.staging.
# Read-only on the tables. Writes a parquet snapshot of both for the DEV + UAT merge (see runbooks/scripts/fusion_dev_uat/README.md).

# COMMAND ----------

SCHEMA = "uat_proj.qualibot"
OUTPUT_DIR = "/Volumes/uat_proj/qualibot/staging/merge_export/uat"
TABLES = ["parsing_checkpoint", "image_metadata"]

# COMMAND ----------

spark.sql(f"CREATE VOLUME IF NOT EXISTS {SCHEMA}.staging")
for table in TABLES:
    path = f"{OUTPUT_DIR}/{table}"
    df = spark.table(f"{SCHEMA}.{table}")
    n_source = df.count()
    df.write.format("parquet").mode("overwrite").save(path)
    n_written = spark.read.format("parquet").load(path).count()
    if n_written != n_source:
        raise RuntimeError(f"{table}: {n_written} rows written, {n_source} in the table")
    dbutils.fs.put(f"dbfs:{OUTPUT_DIR}/rows_{table}.txt", str(n_written), overwrite=True)
    print(f"OK {table}: {n_written} rows -> {path}")
