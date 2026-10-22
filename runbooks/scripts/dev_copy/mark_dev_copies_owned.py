# Databricks notebook source
# Sets the ownership property on the DEV Delta copies of the Lakebase tables that the import loads.
# A copy without the property is refused by the import; with it, the next import replaces the copy by the DEV Lakebase data.

import logging

logger = logging.getLogger("mark_dev_copies_owned")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False

dbutils.widgets.removeAll()
dbutils.widgets.text("target_catalog", "")
dbutils.widgets.text("target_schema", "qualibot")
TARGET_CATALOG = dbutils.widgets.get("target_catalog").strip()
TARGET_SCHEMA = dbutils.widgets.get("target_schema").strip()
if not TARGET_CATALOG:
    raise ValueError("The target_catalog parameter is required")

# Lakebase tables of the application (server/services/infra/lakebase/schema.py).
APP_TABLES = [
    "analysis_cache", "app_errors", "catalog_metadata", "chat_feedbacks", "chat_messages", "chat_retrieved_chunks",
    "chat_sessions", "chat_turns", "compare_feedbacks", "compare_messages", "doc_catalog", "impact_cache",
    "impact_document_results", "impact_feedbacks", "impact_requests", "llm_requests", "summary_cache", "users",
]
# Not exported on DEV (lakebase_tables_to_skip of the dev target in databricks.yml).
SKIPPED_ON_DEV = {
    "doc_catalog", "impact_cache", "impact_document_results", "impact_feedbacks", "impact_requests", "llm_requests",
    "summary_cache", "users",
}
# Dropped and rewritten by the import whatever their property (utils/lakebase_sync/import_lakebase_tables_to_delta_job.py).
FIRST_LOADED_TABLES = {"chat_feedbacks", "chat_messages", "chat_sessions", "chat_turns", "chat_retrieved_chunks", "app_errors"}
OWNER_PROPERTY = "lakebase_export"

to_mark = [table for table in APP_TABLES if table not in SKIPPED_ON_DEV and table not in FIRST_LOADED_TABLES]
logger.info(f"Tables to mark in {TARGET_CATALOG}.{TARGET_SCHEMA}: {to_mark}")

marked = []
for table in to_mark:
    full_name = f"{TARGET_CATALOG}.{TARGET_SCHEMA}.{table}"
    if not spark.catalog.tableExists(full_name):
        logger.info(f"  {full_name}: does not exist, the import will create it")
        continue
    properties = {row["key"]: row["value"] for row in spark.sql(f"SHOW TBLPROPERTIES {full_name}").collect()}
    if properties.get(OWNER_PROPERTY) == "true":
        logger.info(f"  {full_name}: already marked")
        continue
    spark.sql(f"ALTER TABLE {full_name} SET TBLPROPERTIES ('{OWNER_PROPERTY}' = 'true')")
    marked.append(full_name)
    logger.info(f"  {full_name}: marked")

logger.info(f"Done. {len(marked)} table(s) marked")
