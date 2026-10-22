# Databricks notebook source
# Read-only probe, submitted as the PROD job service principal: which other environments can the PROD workspace read?
# The answer decides how the DEV tables reach prod_proj (direct clone, or an export through a volume).

import logging

logger = logging.getLogger("probe_access")
logging.basicConfig(level=logging.INFO)

logger.info("catalogs visible: %s", sorted(r.catalog for r in spark.sql("SHOW CATALOGS").collect()))

for env in ("dev", "uat", "prod"):
    table = f"{env}_proj.qualibot.chunks"
    try:
        logger.info("READ OK  %s: %d rows", table, spark.table(table).count())
    except Exception as error:
        logger.info("READ NO  %s: %s", table, str(error).splitlines()[0][:200])
    volume = f"/Volumes/{env}_proj/qualibot/docling_models"
    try:
        logger.info("LIST OK  %s: %d entries", volume, len(dbutils.fs.ls(volume)))
    except Exception as error:
        logger.info("LIST NO  %s: %s", volume, str(error).splitlines()[0][:200])

for schema in ("prod_proj.qualibot", "prod_landingzone.qualibot"):
    try:
        rows = spark.sql(f"SHOW TABLES IN {schema}").collect()
        logger.info("TABLES in %s (%d): %s", schema, len(rows), sorted(r.tableName for r in rows))
    except Exception as error:
        logger.info("TABLES in %s: %s", schema, str(error).splitlines()[0][:200])
