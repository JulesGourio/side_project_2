# Databricks notebook source
# One-shot: drops the tables of dev_proj.qualibot that the DEV Lakebase export no longer writes (old app tables and
# their Delta copies). Only the names listed below are touched; impact_* stay as they are.
import logging

logger = logging.getLogger("drop_dev_stale_tables")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False

SCHEMA = "dev_proj.qualibot"
TABLES = [
    "messages",
    "feedbacks",
    "users",
    "errors",
    "llm_requests",
    "doc_catalog",
    "summary_cache",
    "knowledge_base_metadata",
]

for table in TABLES:
    if not spark.catalog.tableExists(f"{SCHEMA}.{table}"):
        logger.info(f"  {SCHEMA}.{table}: not found, nothing to drop")
        continue
    spark.sql(f"DROP TABLE {SCHEMA}.{table}")
    logger.info(f"  {SCHEMA}.{table}: dropped")

logger.info("Done")
