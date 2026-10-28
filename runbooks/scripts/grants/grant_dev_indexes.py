# Databricks notebook source
# One-shot: the DEV app service principal reads the DEV indexes again after they were created anew. Run as the DEV job
# service principal, which owns the indexes.

import logging

logger = logging.getLogger("grant_dev_indexes")
logging.basicConfig(level=logging.INFO)

APP_SP = "8e411164-a7e8-46ff-8013-8c56af2c3656"
STATEMENTS = [
    f"GRANT USE CATALOG ON CATALOG dev_proj TO `{APP_SP}`",
    f"GRANT USE SCHEMA ON SCHEMA dev_proj.qualibot TO `{APP_SP}`",
    f"GRANT SELECT ON TABLE dev_proj.qualibot.chunks_index TO `{APP_SP}`",
    f"GRANT SELECT ON TABLE dev_proj.qualibot.chunks_text_index TO `{APP_SP}`",
]

failures = []
for statement in STATEMENTS:
    try:
        spark.sql(statement)
        logger.info("done: %s", statement)
    except Exception as error:
        failures.append(statement)
        logger.error("failed: %s: %s", statement, error)

if failures:
    raise RuntimeError(f"grants not applied: {failures}")
