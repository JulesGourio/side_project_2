# Databricks notebook source
# Renames the UAT copies that hold the names of DEV Lakebase tables, so the export of the DEV Lakebase can load its own
# tables under those names. The copies are kept (renamed with a uat_ prefix): nothing is dropped. One-shot, run once.
import logging

logger = logging.getLogger("rename_dev_uat_copies_job")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False

SCHEMA = "dev_proj.qualibot"
TABLES = ["compare_messages", "compare_feedbacks", "app_errors", "catalog_metadata"]

for table in TABLES:
    source = f"{SCHEMA}.{table}"
    target = f"uat_{table}"
    if not spark.catalog.tableExists(source):
        logger.info(f"  {source}: not found, nothing to rename")
        continue
    if spark.catalog.tableExists(f"{SCHEMA}.{target}"):
        raise RuntimeError(f"{SCHEMA}.{target} already exists, {source} not renamed")
    spark.sql(f"ALTER TABLE {source} RENAME TO {target}")
    logger.info(f"  {source} -> {SCHEMA}.{target}")

logger.info("Done")
