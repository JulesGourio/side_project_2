# Databricks notebook source
# Read-only: lists the Delta tables of dev_proj.qualibot that the Lakebase export would refuse to replace
# (same table name as a Lakebase table, not written by the export). Copy the cell into a DEV notebook and run it.

# COMMAND ----------

import logging

logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
logger = logging.getLogger("dev_export_conflicts")

SCHEMA = "dev_proj.qualibot"
LAKEBASE_TABLES = [
    "analysis_cache", "app_errors", "catalog_metadata", "chat_feedbacks", "chat_messages", "chat_retrieved_chunks",
    "chat_sessions", "chat_turns", "compare_feedbacks", "compare_messages", "doc_catalog", "impact_cache",
    "impact_document_results", "impact_feedbacks", "impact_requests", "llm_requests", "summary_cache", "users",
]
FIRST_LOADED = {"chat_feedbacks", "chat_messages", "chat_sessions", "chat_turns", "chat_retrieved_chunks", "app_errors"}


def owner_of(table: str) -> str:
    """How the export sees a Delta table of the schema.

    :param table: table name without catalog and schema
    :return: ``absent``, ``export`` (written by the export, will be replaced) or ``CONFLICT`` (not written by the export)
    """
    full = f"{SCHEMA}.{table}"
    if not spark.catalog.tableExists(full):
        return "absent"
    if table in FIRST_LOADED:
        return "export"
    properties = {row["key"]: row["value"] for row in spark.sql(f"SHOW TBLPROPERTIES {full}").collect()}
    return "export" if properties.get("lakebase_export") == "true" else "CONFLICT"


for name in LAKEBASE_TABLES:
    logger.info("%-26s %s", name, owner_of(name))
