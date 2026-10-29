# Databricks notebook source
# One-shot. Workspace PROD. Identity: the PROD job runner (service principal b82d3b5a-b633-4738-8dde-68bb2142cb02),
# as runbooks/prod.md requires for everything that writes in PROD (Workflows > Create job > task notebook = this
# notebook > Run as = that service principal > Run now). Your own account is not enough if the schema belongs to it.
# Loads the UAT snapshot copied into /Volumes/prod_proj/qualibot/staging/prod_copy as prod_proj.qualibot.<table>.
# A table that already exists is left as it is. The app and the index do not exist in PROD yet: nothing live is touched.
# Row counts are checked against the export manifest; UAT volume paths stored in the rows are rewritten to PROD.

# COMMAND ----------

import json

import pyspark.sql.functions as f

TARGET_SCHEMA = "prod_proj.qualibot"
SOURCE_DIR = "/Volumes/prod_proj/qualibot/staging/prod_copy"
SOURCE_VOLUME_PREFIX = "/Volumes/uat_proj/"
TARGET_VOLUME_PREFIX = "/Volumes/prod_proj/"
CHUNK_TABLES = {"chunks"}  # Change Data Feed is required by the Delta Sync Vector Search index
RETENTION = "interval 60 days"

manifest = json.loads(dbutils.fs.head(f"dbfs:{SOURCE_DIR}/manifest.json", 1024 * 1024))
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {TARGET_SCHEMA}")

# COMMAND ----------

errors, loaded = [], []
for table, info in manifest["tables"].items():
    target = f"{TARGET_SCHEMA}.{table}"
    if spark.catalog.tableExists(target):
        print(f"[SKIP] {target} existe deja")
        continue
    df = spark.read.format("parquet").load(f"{SOURCE_DIR}/tables/{table}")
    n_source = df.count()
    if n_source != info["rows"]:
        errors.append(f"{table}: {n_source} lignes copiees, {info['rows']} exportees")
        continue
    schema = df.schema
    n_with_path = df.filter(f.to_json(f.struct("*")).contains(SOURCE_VOLUME_PREFIX)).count()
    if n_with_path:
        df = df.select(
            f.from_json(f.regexp_replace(f.to_json(f.struct("*")), SOURCE_VOLUME_PREFIX, TARGET_VOLUME_PREFIX), schema).alias("row")
        ).select("row.*")
    df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(target)
    properties = f"'delta.deletedFileRetentionDuration' = '{RETENTION}', 'delta.logRetentionDuration' = '{RETENTION}'"
    if table in CHUNK_TABLES:
        properties += ", 'delta.enableChangeDataFeed' = 'true'"
    spark.sql(f"ALTER TABLE {target} SET TBLPROPERTIES ({properties})")
    n_target = spark.table(target).count()
    n_left = spark.table(target).filter(f.to_json(f.struct("*")).contains(SOURCE_VOLUME_PREFIX)).count()
    print(f"[LOAD] {target}: {n_target} lignes, chemins UAT restants : {n_left}")
    if n_target != info["rows"] or n_left:
        errors.append(f"{table}: {n_target} lignes (attendu {info['rows']}), {n_left} chemins UAT restants")
    loaded.append(table)

if errors:
    raise RuntimeError("Controles en echec : " + "; ".join(errors))
print(f"OK: {len(loaded)} table(s) chargee(s) : {loaded}")
