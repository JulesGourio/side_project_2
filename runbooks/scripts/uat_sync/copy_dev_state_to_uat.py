# Databricks notebook source
# One-shot, run once as the UAT service principal (job-runner-sa-uat) by runbooks/uat.md, block R.
# Copies the parsing state of dev_proj.qualibot into uat_proj.qualibot, so the next UAT run parses only the documents DEV has not parsed.
# The chunk tables are not copied: the run rebuilds them from this state, and replacing chunks under a live index breaks its sync.

# COMMAND ----------

# Config Standard Package Imports
import logging

from pyspark.sql import functions as f

# COMMAND ----------

# Config logger
logger = logging.getLogger("copy_dev_state_to_uat")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# COMMAND ----------

# Inputs
SOURCE_SCHEMA = "dev_proj.qualibot"
TARGET_SCHEMA = "uat_proj.qualibot"
# The tables the parsing pipeline resumes from. chunks, chunks_archive and the Vector Search index are rebuilt by the run.
STATE_TABLES = ["parsing_checkpoint", "parsed_documents", "image_metadata", "parse_manifest", "category_reference"]
SOURCE_VOLUME_PREFIX = "/Volumes/dev_proj/"
TARGET_VOLUME_PREFIX = "/Volumes/uat_proj/"
RETENTION_PROPERTIES = "'delta.deletedFileRetentionDuration' = 'interval 60 days', 'delta.logRetentionDuration' = 'interval 60 days'"

# COMMAND ----------

# Data Preparation - the source must be readable before anything is written
# A missing grant fails here, before the first table is touched.
source_counts = {}
for table in STATE_TABLES:
    source_counts[table] = spark.table(f"{SOURCE_SCHEMA}.{table}").count()
    logger.info(f"[SOURCE] {SOURCE_SCHEMA}.{table}: {source_counts[table]} rows")

# COMMAND ----------

# Data Preparation - keep the current UAT state, once
# The first backup is kept: a second run does not replace it, so it stays the state from before the copy.
existing = {row.tableName for row in spark.sql(f"SHOW TABLES IN {TARGET_SCHEMA}").collect()}
for table in STATE_TABLES:
    if table in existing:
        spark.sql(f"CREATE TABLE IF NOT EXISTS {TARGET_SCHEMA}.{table}_before_dev_sync DEEP CLONE {TARGET_SCHEMA}.{table}")
        logger.info(f"[BACKUP] {TARGET_SCHEMA}.{table} -> {table}_before_dev_sync")

# COMMAND ----------

# Tr. 1 - exact copy of each state table
# The copy keeps every column and row of DEV, so the parse resumes from the same (IDDOC, document_sha256) keys.
for table in STATE_TABLES:
    spark.sql(f"CREATE OR REPLACE TABLE {TARGET_SCHEMA}.{table} DEEP CLONE {SOURCE_SCHEMA}.{table}")
    spark.sql(f"ALTER TABLE {TARGET_SCHEMA}.{table} SET TBLPROPERTIES ({RETENTION_PROPERTIES})")
    logger.info(f"[COPY] {SOURCE_SCHEMA}.{table} -> {TARGET_SCHEMA}.{table}")

# COMMAND ----------

# Tr. 2 - point the copied paths at the UAT volumes
# The checkpoint and image rows hold absolute paths of DEV's volume. Round-tripping through JSON rewrites them in every column, nested ones included.
for table in STATE_TABLES:
    df_target = spark.table(f"{TARGET_SCHEMA}.{table}")
    rows_with_dev_path = df_target.filter(f.to_json(f.struct("*")).contains(SOURCE_VOLUME_PREFIX)).count()
    if rows_with_dev_path == 0:
        logger.info(f"[PATHS] {table}: no DEV volume path")
        continue
    # Written to a temporary table first: a table cannot be overwritten while the same plan reads it.
    schema = df_target.schema
    tmp_table = f"{TARGET_SCHEMA}.{table}_paths_tmp"
    df_fixed = df_target.withColumn(
        "_json",
        f.regexp_replace(f.to_json(f.struct("*")), SOURCE_VOLUME_PREFIX, TARGET_VOLUME_PREFIX),
    )
    df_fixed = df_fixed.select(f.from_json("_json", schema).alias("row")).select("row.*")
    df_fixed.write.mode("overwrite").saveAsTable(tmp_table)
    spark.sql(f"CREATE OR REPLACE TABLE {TARGET_SCHEMA}.{table} DEEP CLONE {tmp_table}")
    spark.sql(f"DROP TABLE {tmp_table}")
    spark.sql(f"ALTER TABLE {TARGET_SCHEMA}.{table} SET TBLPROPERTIES ({RETENTION_PROPERTIES})")
    logger.info(f"[PATHS] {table}: {rows_with_dev_path} row(s) rewritten to {TARGET_VOLUME_PREFIX}")

# COMMAND ----------

# Quality Checks - the copy has the same row counts as DEV, and no DEV volume path is left
errors = []
for table in STATE_TABLES:
    target_df = spark.table(f"{TARGET_SCHEMA}.{table}")
    target_count = target_df.count()
    if target_count != source_counts[table]:
        errors.append(f"{table}: {target_count} rows in UAT, {source_counts[table]} in DEV")
    left = target_df.filter(f.to_json(f.struct("*")).contains(SOURCE_VOLUME_PREFIX)).count()
    if left:
        errors.append(f"{table}: {left} row(s) still point to {SOURCE_VOLUME_PREFIX}")
    logger.info(f"[CHECK] {table}: {target_count} rows, DEV paths left: {left}")

if errors:
    raise RuntimeError("DEV state copy failed the checks: " + "; ".join(errors))

# COMMAND ----------

# Outputs
logger.info(f"[OUTPUT] {len(STATE_TABLES)} state tables copied from {SOURCE_SCHEMA} to {TARGET_SCHEMA}; the backups are the *_before_dev_sync tables")
