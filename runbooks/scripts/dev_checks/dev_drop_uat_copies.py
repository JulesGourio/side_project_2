# Databricks notebook source
# Drops the three obsolete UAT copies of impact-search tables in dev_proj.qualibot, so that the Lakebase export of DEV can
# write its own tables of the same names. Copy the cell into a DEV notebook and run it. A dropped managed table can be
# restored for a few days with UNDROP TABLE.

# COMMAND ----------

import logging

logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
logger = logging.getLogger("dev_drop_uat_copies")

SCHEMA = "dev_proj.qualibot"
UAT_COPIES = ["impact_cache", "impact_document_results", "impact_requests"]


def drop_if_not_exported(table: str) -> None:
    """Drops a table of the schema unless the Lakebase export wrote it.

    :param table: table name without catalog and schema
    :return: None
    """
    full = f"{SCHEMA}.{table}"
    if not spark.catalog.tableExists(full):
        logger.info("%s: absent", full)
        return
    properties = {row["key"]: row["value"] for row in spark.sql(f"SHOW TBLPROPERTIES {full}").collect()}
    if properties.get("lakebase_export") == "true":
        logger.info("%s: written by the export, kept", full)
        return
    rows = spark.table(full).count()
    spark.sql(f"DROP TABLE {full}")
    logger.info("%s: dropped (%d rows)", full, rows)


for name in UAT_COPIES:
    drop_if_not_exported(name)
