# Databricks notebook source
# MAGIC %md
# MAGIC # UAT — set up the project schema for the new chatbot
# MAGIC
# MAGIC Job `qualibot-setup-proj-schema-<target>` (`databricks.yml`), run as
# MAGIC `job-runner-sa-uat`: everything created here is owned by that SP.
# MAGIC
# MAGIC 1. Checks the identity and that the target schema exists.
# MAGIC 2. Creates the pipeline volume(s) (`volumes`) if missing.
# MAGIC 3. Deep-clones each parsing state table `{source}.{name}{source_suffix}` into
# MAGIC    `{target}.{name}{target_suffix}` (60-day history, like every pipeline table),
# MAGIC    so the first parsing run re-chunks from the checkpoint instead of reparsing
# MAGIC    the corpus on GPU. The source is only read. An existing target table is
# MAGIC    left alone unless `overwrite` is `true`.
# MAGIC 4. Grants `reader_groups` read access to what it created.
# MAGIC
# MAGIC Idempotent: a second run with `overwrite=false` changes nothing.

# COMMAND ----------

dbutils.widgets.removeAll()
dbutils.widgets.text("target_catalog_schema", "uat_proj.qualibot")
dbutils.widgets.text("target_suffix", "_test")
dbutils.widgets.text("source_catalog_schema", "uat_landingzone.qualibot")
dbutils.widgets.text("source_suffix", "_v1")
dbutils.widgets.text("tables", "_pipeline_checkpoint,processed_files,image_metadata,parse_manifest,category_reference")
dbutils.widgets.text("volumes", "images_test")
dbutils.widgets.text("reader_groups", "Role-Project-LEAP-CoreAdmin,Role-Project-LEAP-CoreDev")
dbutils.widgets.dropdown("overwrite", "false", ["false", "true"])


import logging

logger = logging.getLogger("setup_proj_schema")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False


def _list(name):
    return [v.strip() for v in dbutils.widgets.get(name).split(",") if v.strip()]


target = dbutils.widgets.get("target_catalog_schema").strip()
target_suffix = dbutils.widgets.get("target_suffix").strip()
source = dbutils.widgets.get("source_catalog_schema").strip()
source_suffix = dbutils.widgets.get("source_suffix").strip()
overwrite = dbutils.widgets.get("overwrite") == "true"
target_catalog, target_schema = target.split(".")
assert target != source or target_suffix != source_suffix, "source and target are the same tables"

logger.info(f"Running as : {spark.sql('SELECT current_user()').first()[0]}")
logger.info(f"Source     : {source}.*{source_suffix}  (read only)")
logger.info(f"Target     : {target}.*{target_suffix}  (overwrite={overwrite})")

# COMMAND ----------

row = spark.sql(f"DESCRIBE SCHEMA EXTENDED `{target_catalog}`.`{target_schema}`").collect()
info = {r[0]: r[1] for r in row}
logger.info(f"Schema {target}: owner = {info.get('Owner')}")

# COMMAND ----------

for v in _list("volumes"):
    spark.sql(f"CREATE VOLUME IF NOT EXISTS `{target_catalog}`.`{target_schema}`.`{v}`")
    logger.info(f"  volume {target}.{v}: OK")

# COMMAND ----------

created = []
for name in _list("tables"):
    src = f"{source}.{name}{source_suffix}"
    dst = f"{target}.{name}{target_suffix}"
    if not spark.catalog.tableExists(src):
        logger.info(f"  {src}: missing in the source, skipped")
        continue
    if spark.catalog.tableExists(dst) and not overwrite:
        logger.info(f"  {dst}: already exists, kept ({spark.table(dst).count()} rows)")
        continue
    spark.sql(f"CREATE OR REPLACE TABLE {dst} DEEP CLONE {src}")
    spark.sql(f"""ALTER TABLE {dst} SET TBLPROPERTIES (
        'delta.deletedFileRetentionDuration' = 'interval 60 days',
        'delta.logRetentionDuration' = 'interval 60 days')""")
    n_src, n_dst = spark.table(src).count(), spark.table(dst).count()
    logger.info(f"  {src} -> {dst}: {n_dst} rows (source {n_src})")
    if n_src != n_dst:
        raise RuntimeError(f"{dst}: {n_dst} rows vs {n_src} in {src}")
    created.append(dst)

# COMMAND ----------

failed = []
for g in _list("reader_groups"):
    stmts = [f"GRANT SELECT ON TABLE {t} TO `{g}`" for t in created]
    stmts += [f"GRANT READ VOLUME ON VOLUME `{target_catalog}`.`{target_schema}`.`{v}` TO `{g}`" for v in _list("volumes")]
    for stmt in stmts:
        try:
            spark.sql(stmt)
            logger.info(f"  OK: {stmt}")
        except Exception as exc:
            failed.append(stmt)
            logger.warning(f"  FAILED: {stmt}\n    -> {str(exc).splitlines()[0][:300]}")

# COMMAND ----------

logger.info("Tables in the target schema now:")
display(spark.sql(f"SHOW TABLES IN `{target_catalog}`.`{target_schema}`"))
if failed:
    raise RuntimeError(f"{len(failed)} grant(s) failed (tables and volumes are in place)")
