# Databricks notebook source
# One-shot, run as the UAT job SP before the first Lakebase export into uat_proj.qualibot: the import drops and
# recreates the chat tables, so each one is copied first (DEEP CLONE, nothing dropped). Existing copies are refused.
import logging

logger = logging.getLogger("backup_uat_chat_tables")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False

SCHEMA = "uat_proj.qualibot"
SUFFIX = "_avant_20261019"
TABLES = ["chat_messages", "chat_sessions", "chat_feedbacks", "chat_turns", "chat_retrieved_chunks", "app_errors"]

for table in TABLES:
    source = f"{SCHEMA}.{table}"
    target = f"{table}{SUFFIX}"
    if not spark.catalog.tableExists(source):
        logger.info(f"  {source}: not found, nothing to back up")
        continue
    if spark.catalog.tableExists(f"{SCHEMA}.{target}"):
        raise RuntimeError(f"{SCHEMA}.{target} already exists, not overwritten")
    spark.sql(f"CREATE TABLE {SCHEMA}.{target} DEEP CLONE {source}")
    rows = spark.table(f"{SCHEMA}.{target}").count()
    logger.info(f"  {source} -> {SCHEMA}.{target} ({rows} row(s))")

logger.info("Done")
